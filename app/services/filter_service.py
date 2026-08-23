"""
留言清洗模組：剔除「複製文/模板式/一片人重複」的留言，保留有個人特色的留言。

核心概念：
- 「模板類」的判斷不是看單一則留言本身，而是看它在「同一場直播」中
  被『多少不同的人』重複講過。如果同一句話（正規化後）在同一場直播
  被 >= N 位不同觀眾說過，代表這是當下流行的彈幕/玩梗跟風，
  不具備「這是某人的個人風格」的辨識度，應該過濾掉。
- 額外規則：
  - 純表情符號/顏文字（無其他文字）→ 過濾（除非長度夠長，可能是自創顏文字梗）
  - 過短且是常見詞（如 "www", "88", "笑死", "+1", "早安"）→ 過濾
  - 內容幾乎相同但只差標點/空白 → 正規化後應視為同一句

正規化規則：
  - 全形轉半形、大小寫統一
  - 移除多餘空白
  - 重複字元壓縮（例如 "wwwwwww" -> "w", "草草草草" -> "草"），
    這樣同一個梗的不同長度版本會被視為同一句模板
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from typing import Iterable

from sqlalchemy.orm import Session

from app.models.models import ChatMessage, Stream
from app.services.emoji_service import is_pure_emoji, YT_EMOTE_PATTERN

# 常見「跟風式」彈幕關鍵詞／完全比對用的黑名單（正規化後比對）
# 這些詞即使沒有被大量重複講，本身辨識度也極低，直接過濾
GENERIC_PHRASES = {
    "www", "w", "888", "88", "666", "6", "草", "笑死", "+1", "1",
    "早安", "晚安", "推", "頂", "簽到", "來了", "first", "ok", "okok",
    "哈哈", "哈哈哈", "lol", "lmao", "nice", "good", "讚", "太強了",
    "好強", "6666", "233", "2333",
}

# 詞彙黑名單：留言只要「包含」這些詞（子字串比對，不用完全相符）就整則過濾。
# 跟 GENERIC_PHRASES（完全比對）不同，這裡是包含比對，適合過濾特定關鍵字/人名。
BLACKLIST_SUBSTRINGS = {
    "安安",
}

# 判定為「同場直播模板彈幕」的門檻：同一正規化文字被 >= 此人數的不同觀眾講過
DUPLICATE_AUTHOR_THRESHOLD = 5

MIN_MEANINGFUL_LENGTH = 2  # 正規化後低於此字數，且命中黑名單才過濾；否則短但獨特的留言仍保留

_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)

_REPEAT_CHAR_PATTERN = re.compile(r"(.)\1{2,}")  # 同一字元連續 3 次以上


def normalize_text(text: str) -> str:
    """
    將留言正規化，用於重複偵測與模板比對：
    - 全形轉半形
    - 轉小寫
    - 去除頭尾空白、壓縮中間空白
    - 連續重複字元壓縮成一個（wwwww -> w，草草草草 -> 草）
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)  # 全形轉半形
    t = t.lower().strip()
    t = re.sub(r"\s+", " ", t)
    t = _REPEAT_CHAR_PATTERN.sub(r"\1", t)
    return t


def is_emoji_only(text: str) -> bool:
    return is_pure_emoji(text)


def classify_message(normalized: str, original: str) -> str | None:
    """
    先做「不需要跨留言比對」的規則判斷。
    回傳 filter_reason（若應過濾），否則回傳 None。
    """
    if not normalized:
        return "empty_after_normalize"

    if normalized in GENERIC_PHRASES:
        return "generic_phrase"

    for word in BLACKLIST_SUBSTRINGS:
        if word in normalized:
            return "blacklisted_word"

    if is_emoji_only(original) and len(normalized) <= 3:
        return "emoji_only_short"

    if len(normalized) < MIN_MEANINGFUL_LENGTH and normalized in GENERIC_PHRASES:
        return "too_short_generic"

    return None


def filter_stream_messages(db: Session, stream: Stream) -> dict:
    """
    對指定 stream 的所有留言做清洗：
    1. 正規化文字，寫回 normalized_text
    2. 套用單則規則過濾
    3. 統計同一場直播中，相同 normalized_text 被多少「不同 author」講過，
       超過門檻的整批標記為 duplicate_template

    回傳統計 { "total": int, "filtered": int, "kept": int, "reasons": {reason: count} }
    """
    messages = db.query(ChatMessage).filter(ChatMessage.stream_id == stream.id).all()

    reason_counter: Counter = Counter()
    normalized_to_authors: dict[str, set[int]] = defaultdict(set)
    normalized_to_messages: dict[str, list[ChatMessage]] = defaultdict(list)

    # Pass 1: 正規化 + 單則規則過濾 + 收集 normalized -> authors 對照
    for m in messages:
        normalized = normalize_text(m.message_text)
        m.normalized_text = normalized

        reason = classify_message(normalized, m.message_text)
        if reason:
            m.is_filtered = True
            m.filter_reason = reason
            m.message_text = None  # 被過濾的留言原文清空省空間，只留 normalized_text
            reason_counter[reason] += 1
            continue

        # 先標記為未過濾，等 Pass 2 判斷是否為跨人重複模板
        m.is_filtered = False
        m.filter_reason = None
        normalized_to_authors[normalized].add(m.author_id)
        normalized_to_messages[normalized].append(m)

    # Pass 2: 判斷跨觀眾重複的模板彈幕
    for normalized, authors in normalized_to_authors.items():
        if len(authors) >= DUPLICATE_AUTHOR_THRESHOLD:
            for m in normalized_to_messages[normalized]:
                m.is_filtered = True
                m.filter_reason = "duplicate_template"
                m.message_text = None
                reason_counter["duplicate_template"] += 1

    db.commit()

    total = len(messages)
    filtered = sum(reason_counter.values())
    kept = total - filtered

    return {
        "total": total,
        "filtered": filtered,
        "kept": kept,
        "reasons": dict(reason_counter),
    }


def filter_duplicate_across_channel(db: Session, channel_db_id: int) -> dict:
    """
    第二階段清洗（可選，建議在所有場次都跑完 filter_stream_messages 後執行）：
    偵測「跨場次」但仍然大量重複的句子（例如整個頻道觀眾群長期愛用的固定梗、
    固定招呼語），這種即使單場沒有超過門檻，長期來看辨識度依然很低。

    做法：在該頻道所有『尚未被標記為 duplicate_template』的留言中，
    统計 normalized_text 被多少不同 author 用過（不分場次），
    超過較高的門檻（因為基數變大，門檻也拉高）就一併標記為 cross_stream_template。
    """
    from app.models.models import Author  # noqa: F401  局部 import 避免循環

    rows = (
        db.query(ChatMessage)
        .join(Stream, ChatMessage.stream_id == Stream.id)
        .filter(Stream.channel_id == channel_db_id, ChatMessage.is_filtered == False)  # noqa: E712
        .all()
    )

    CROSS_STREAM_THRESHOLD = 15  # 全頻道範圍內，門檻拉高避免誤殺正常巧合

    normalized_to_authors: dict[str, set[int]] = defaultdict(set)
    normalized_to_messages: dict[str, list[ChatMessage]] = defaultdict(list)

    for m in rows:
        if not m.normalized_text:
            continue
        normalized_to_authors[m.normalized_text].add(m.author_id)
        normalized_to_messages[m.normalized_text].append(m)

    filtered_count = 0
    for normalized, authors in normalized_to_authors.items():
        if len(authors) >= CROSS_STREAM_THRESHOLD:
            for m in normalized_to_messages[normalized]:
                m.is_filtered = True
                m.filter_reason = "cross_stream_template"
                m.message_text = None
                filtered_count += 1

    db.commit()
    return {"newly_filtered": filtered_count, "checked": len(rows)}


def filter_all_streams(db: Session, streams: Iterable[Stream]) -> dict:
    """批次對多場直播做清洗，回傳彙總統計。"""
    agg = {"total": 0, "filtered": 0, "kept": 0, "reasons": Counter()}
    for stream in streams:
        result = filter_stream_messages(db, stream)
        agg["total"] += result["total"]
        agg["filtered"] += result["filtered"]
        agg["kept"] += result["kept"]
        agg["reasons"].update(result["reasons"])
    agg["reasons"] = dict(agg["reasons"])
    return agg
