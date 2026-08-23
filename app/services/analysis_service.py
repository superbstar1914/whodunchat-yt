"""
觀眾特徵分析模組（步驟5）：
針對每位觀眾「通過清洗、未被過濾」的留言，抽取風格特徵，寫入 AuthorProfile.features。

抽取的特徵：
- avg_message_length / message_count：基本統計
- top_words：常用詞（jieba 斷詞後，排除通用停用詞）
- top_emojis：常用 emoji / 顏文字
- punctuation_rate / question_rate / exclaim_rate：標點使用習慣
- hour_distribution：24 小時發言時段分布（用來看是否為固定時段的常駐觀眾）
- catchphrases：口頭禪（在該觀眾留言中出現頻率遠高於全頻道平均的詞）
- char_ngram_text：把該觀眾所有留言串接起來的代表文本，
  之後給 TF-IDF vectorizer 用來計算「風格相似度」（見 similarity_service）

注意：
- 只使用 is_filtered == False 的留言，並且只納入「有足夠留言數」的觀眾
  （留言太少的人統計特徵不可靠，會在 similarity 階段用門檻排除，不在這裡直接跳過，
   避免遺漏資料，只是分析時會標記 message_count 讓後續使用者自行判斷）
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Optional

import jieba
jieba.dt.cache_file = './data/jieba.cache' 
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.models import Author, ChatMessage, AuthorProfile, Stream
from app.services.emoji_service import extract_all_emojis

# 停用詞：分析常用詞時排除，避免全都是虛詞
STOPWORDS = {
    "的", "了", "是", "我", "你", "他", "她", "它", "們", "在", "也", "都",
    "就", "還", "又", "跟", "和", "或", "但", "不", "沒", "很", "太", "啊",
    "喔", "哦", "呢", "吧", "嗎", "耶", "啦", "這", "那", "有", "要", "會",
    "可以", "什麼", "一個", "一下", "一直", "然後", "因為", "所以", "自己",
    "怎麼", "現在", "這個", "那個", "真的", "覺得", "應該", "已經", "還是",
    "for", "the", "and", "you", "is", "to", "a", "of", "in", "it", "that",
}

_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]",
    flags=re.UNICODE,
)
_KAOMOJI_PATTERN = re.compile(r"[（(][^\s()（）]{1,10}[)）]|[><;:=xXoO][_\-~^]{1,3}[><;:=xXoO]")
_PUNCT_PATTERN = re.compile(r"[，。！？、；：,.\!\?;:]")
_QUESTION_PATTERN = re.compile(r"[？?]")
_EXCLAIM_PATTERN = re.compile(r"[！!]")

MIN_MESSAGES_FOR_PROFILE = 3  # 少於此留言數，特徵仍會產生但標記為 low_confidence


def _extract_emojis(text: str) -> list[str]:
    return extract_all_emojis(text)


def _tokenize(text: str) -> list[str]:
    tokens = jieba.lcut(text)
    return [t.strip() for t in tokens if t.strip() and t.strip() not in STOPWORDS and len(t.strip()) > 0]


def _build_features_for_author(
    messages: list[ChatMessage],
    global_word_freq: Counter,
    global_total_words: int,
) -> dict:
    lengths = []
    all_words: Counter = Counter()
    all_emojis: Counter = Counter()
    hour_counts = [0] * 24
    punct_count = 0
    question_count = 0
    exclaim_count = 0
    total_chars = 0
    texts_for_ngram: list[str] = []

    for m in messages:
        text = m.message_text or ""
        norm = unicodedata.normalize("NFKC", text)
        lengths.append(len(norm))
        total_chars += len(norm)

        for w in _tokenize(norm):
            all_words[w] += 1

        for e in _extract_emojis(norm):
            all_emojis[e] += 1

        punct_count += len(_PUNCT_PATTERN.findall(norm))
        question_count += len(_QUESTION_PATTERN.findall(norm))
        exclaim_count += len(_EXCLAIM_PATTERN.findall(norm))

        if m.timestamp:
            hour_counts[m.timestamp.hour] += 1

        texts_for_ngram.append(norm)

    n = max(len(messages), 1)
    avg_len = sum(lengths) / n if lengths else 0.0
    hour_dist = [c / n for c in hour_counts]

    # 口頭禪：該觀眾用詞頻率 vs 全頻道平均頻率的比值，取比值最高且出現次數 >= 3 的詞
    catchphrases = []
    for word, count in all_words.items():
        if count < 3:
            continue
        author_rate = count / n
        global_rate = (global_word_freq.get(word, 0) / global_total_words) if global_total_words else 0
        # 避免除以極小值造成雜訊，設一個平滑項
        score = author_rate / (global_rate + 1e-4)
        catchphrases.append((word, count, round(score, 2)))
    catchphrases.sort(key=lambda x: (-x[2], -x[1]))
    catchphrases = catchphrases[:10]

    features = {
        "message_count": len(messages),
        "avg_message_length": round(avg_len, 2),
        "top_words": all_words.most_common(20),
        "top_emojis": all_emojis.most_common(10),
        "punctuation_rate": round(punct_count / max(total_chars, 1), 4),
        "question_rate": round(question_count / n, 4),
        "exclaim_rate": round(exclaim_count / n, 4),
        "hour_distribution": [round(h, 4) for h in hour_dist],
        "catchphrases": [{"word": w, "count": c, "score": s} for w, c, s in catchphrases],
        "low_confidence": len(messages) < MIN_MESSAGES_FOR_PROFILE,
    }
    return features


def analyze_channel_authors(db: Session, channel_db_id: int, progress_callback=None) -> dict:
    """
    分析某頻道底下所有觀眾的留言特徵，寫入/更新 AuthorProfile。
    只使用 is_filtered == False 的留言。

    回傳 { "authors_analyzed": int, "low_confidence_count": int }
    """
    # 先算全頻道詞頻，作為「口頭禪」判斷的基準分布
    all_kept_messages = (
        db.query(ChatMessage)
        .join(Stream, ChatMessage.stream_id == Stream.id)
        .filter(Stream.channel_id == channel_db_id, ChatMessage.is_filtered == False)  # noqa: E712
        .all()
    )

    global_word_freq: Counter = Counter()
    messages_by_author: dict[int, list[ChatMessage]] = {}
    for m in all_kept_messages:
        norm = unicodedata.normalize("NFKC", m.message_text or "")
        for w in _tokenize(norm):
            global_word_freq[w] += 1
        messages_by_author.setdefault(m.author_id, []).append(m)

    global_total_words = sum(global_word_freq.values())

    author_ids = list(messages_by_author.keys())
    total = len(author_ids)
    low_confidence_count = 0

    for idx, author_db_id in enumerate(author_ids):
        msgs = messages_by_author[author_db_id]
        features = _build_features_for_author(msgs, global_word_freq, global_total_words)
        if features["low_confidence"]:
            low_confidence_count += 1

        profile = db.query(AuthorProfile).filter(AuthorProfile.author_id == author_db_id).one_or_none()
        if profile is None:
            profile = AuthorProfile(author_id=author_db_id, features=features)
            db.add(profile)
        else:
            profile.features = features

        author = db.get(Author, author_db_id)
        if author:
            author.filtered_message_count = len(msgs)

        if progress_callback and idx % 20 == 0:
            progress_callback(idx + 1, total)

    db.commit()

    return {"authors_analyzed": total, "low_confidence_count": low_confidence_count}


def get_author_summary_text(author: Author, profile: Optional[AuthorProfile]) -> str:
    """產生給人看的簡短風格描述（規則式生成，非 LLM）。"""
    if not profile or not profile.features:
        return "尚無足夠資料分析風格。"

    f = profile.features
    parts = []
    parts.append(f"共 {f.get('message_count', 0)} 則有效留言")

    top_words = f.get("top_words") or []
    if top_words:
        words_str = "、".join(w for w, _ in top_words[:5])
        parts.append(f"常用詞：{words_str}")

    catchphrases = f.get("catchphrases") or []
    if catchphrases:
        cp_str = "、".join(c["word"] for c in catchphrases[:3])
        parts.append(f"口頭禪：{cp_str}")

    top_emojis = f.get("top_emojis") or []
    if top_emojis:
        emoji_str = "".join(e for e, _ in top_emojis[:5])
        parts.append(f"愛用表情：{emoji_str}")

    if f.get("question_rate", 0) > 0.15:
        parts.append("很愛發問")
    if f.get("exclaim_rate", 0) > 0.3:
        parts.append("講話很有精神（很多驚嘆號）")

    return "；".join(parts)
