"""
出題邏輯模組（步驟7，v2）：
從頻道的「有效留言」中挑一則，找出風格與正確答案相近的其他觀眾作為干擾選項，
組成一題「這則留言是誰講的？」的選擇題（固定 3 選項：1 正確 + 2 干擾）。

難度分級設計：
- 難度的本質是「干擾選項跟正確答案有多容易混淆」，用干擾選項與正確答案的
  相似度來決定：
    easy   → 干擾選項刻意選相似度低的觀眾（風格差很多，好認）
    medium → 干擾選項選中間層級的相似度
    hard   → 干擾選項選相似度最高的觀眾（風格接近，容易猜錯）
- 每一題算出的 difficulty 分數 = 兩個干擾選項相似度的平均值（0~1），
  這個分數同時決定了它被歸類為 easy/medium/hard 哪一級的門檻依據，
  也會回傳給前端顯示。
- 批次出題（generate_quiz_batch）依照使用者設定的 easy/medium/hard 比例，
  各自呼叫 generate_quiz 並指定 difficulty_level，湊出總題數。

難度門檻（相似度 0~1 區間，可調）：
  easy:   0.00 ~ 0.33
  medium: 0.33 ~ 0.66
  hard:   0.66 ~ 1.00
"""
from __future__ import annotations

import random
from typing import Optional, Literal

from sqlalchemy.orm import Session

from app.models.models import Author, AuthorProfile, ChatMessage, Stream, QuizRecord
from app.services.similarity_service import get_most_similar_authors
from app.services.name_resolve_service import get_display_name_or_fallback

OPTION_COUNT = 3  # 固定 3 選項：1 正確 + 2 干擾

DifficultyLevel = Literal["easy", "medium", "hard", "random"]

DIFFICULTY_RANGES: dict[str, tuple[float, float]] = {
    "easy": (0.0, 0.33),
    "medium": (0.33, 0.66),
    "hard": (0.66, 1.01),  # 上界略大於1，確保相似度剛好等於1的極端情況也能落在 hard
}


def _eligible_message_query(db: Session, channel_db_id: int):
    return (
        db.query(ChatMessage)
        .join(Stream, ChatMessage.stream_id == Stream.id)
        .filter(
            Stream.channel_id == channel_db_id,
            ChatMessage.is_filtered == False,  # noqa: E712
        )
    )


def _pick_distractors_for_level(
    similar: list[tuple[int, float]],
    correct_author_id: int,
    all_other_author_ids: list[int],
    level: DifficultyLevel,
    need: int = OPTION_COUNT - 1,
) -> tuple[list[int], float]:
    """
    依難度等級從相似觀眾清單中挑選干擾選項。
    similar: [(author_db_id, similarity), ...]，已按相似度由高到低排序
    回傳 (distractor_author_ids, difficulty_score)

    difficulty_score 的計算原則：一律取「實際選中的干擾選項」與正確答案的
    相似度平均值，即使資料不足只能選到一兩位有相似度資料的候選人，也照實計算，
    不會用一個固定的預設值蓋掉真實訊號；只有完全沒有任何相似度資料可用
    （例如全頻道只有這唯一一位符合條件的觀眾）時才會是 0.0。
    """
    candidates = [(aid, sim) for aid, sim in similar if aid != correct_author_id]

    if level == "random":
        pool = [aid for aid in all_other_author_ids if aid != correct_author_id]
        random.shuffle(pool)
        chosen_ids = pool[:need]
        sim_lookup = dict(candidates)
        avg_sim = sum(sim_lookup.get(aid, 0.0) for aid in chosen_ids) / max(len(chosen_ids), 1)
        return chosen_ids, round(avg_sim, 4)

    if not candidates:
        # 完全沒有相似度資料可用（例如該觀眾留言太少沒被納入相似度計算），
        # 只能隨機補人，難度誠實標記為 0（無法評估，等同「隨機亂猜」的基準線）
        pool = [aid for aid in all_other_author_ids if aid != correct_author_id]
        random.shuffle(pool)
        return pool[:need], 0.0

    lo, hi = DIFFICULTY_RANGES[level]
    in_range = [(aid, sim) for aid, sim in candidates if lo <= sim < hi]

    if len(in_range) >= need:
        random.shuffle(in_range)
        chosen = in_range[:need]
    else:
        center = (lo + hi) / 2
        in_range_ids = {aid for aid, _ in in_range}
        remaining = [(aid, sim) for aid, sim in candidates if aid not in in_range_ids]
        remaining.sort(key=lambda x: abs(x[1] - center))
        chosen = in_range + remaining[: need - len(in_range)]

    if len(chosen) < need:
        used_ids = {aid for aid, _ in chosen} | {correct_author_id}
        pool = [aid for aid in all_other_author_ids if aid not in used_ids]
        random.shuffle(pool)
        chosen += [(aid, 0.0) for aid in pool[: need - len(chosen)]]

    difficulty_score = sum(sim for _, sim in chosen) / max(len(chosen), 1)
    return [aid for aid, _ in chosen], round(difficulty_score, 4)


def generate_quiz(
    db: Session,
    channel_db_id: int,
    difficulty_level: DifficultyLevel = "random",
    exclude_message_ids: Optional[list[int]] = None,
) -> Optional[dict]:
    """
    產生一題測驗（固定 3 選項）。回傳:
    {
        "message_db_id": int,
        "message_text": str,
        "stream_title": str,
        "options": [{"author_db_id": int, "display_name": str}, ...],
        "correct_author_db_id": int,
        "difficulty": float,
        "difficulty_level": str,
    }
    若頻道資料不足，回傳 None。
    """
    query = _eligible_message_query(db, channel_db_id)
    if exclude_message_ids:
        query = query.filter(ChatMessage.id.notin_(exclude_message_ids))

    candidate_messages = query.all()
    if not candidate_messages:
        return None

    authors_with_profile_ids = {
        aid for (aid,) in db.query(AuthorProfile.author_id)
        .join(Author, Author.id == AuthorProfile.author_id)
        .all()
    }
    preferred = [m for m in candidate_messages if m.author_id in authors_with_profile_ids]
    pool = preferred if preferred else candidate_messages

    chosen_message = random.choice(pool)
    correct_author = db.get(Author, chosen_message.author_id)
    if correct_author is None:
        return None

    all_other_author_ids = [
        aid for (aid,) in db.query(Author.id)
        .join(ChatMessage, ChatMessage.author_id == Author.id)
        .join(Stream, ChatMessage.stream_id == Stream.id)
        .filter(Stream.channel_id == channel_db_id)
        .distinct()
        .all()
        if aid != correct_author.id
    ]

    if not all_other_author_ids:
        return None

    similar = get_most_similar_authors(db, correct_author.id, top_n=50)

    distractor_ids, difficulty_score = _pick_distractors_for_level(
        similar, correct_author.id, all_other_author_ids, difficulty_level,
    )

    option_author_db_ids = [correct_author.id] + distractor_ids
    random.shuffle(option_author_db_ids)

    options = []
    for aid in option_author_db_ids:
        author = db.get(Author, aid)
        options.append({
            "author_db_id": aid,
            "author_id": author.author_id if author else "",
            "display_name": get_display_name_or_fallback(author) if author else "未知觀眾",
            "channel_url": f"https://www.youtube.com/channel/{author.author_id}" if author else "",
        })

    stream = db.get(Stream, chosen_message.stream_id)

    return {
        "message_db_id": chosen_message.id,
        "message_text": chosen_message.message_text,
        "stream_title": stream.title if stream else None,
        "video_id": stream.video_id if stream else None,
        "time_in_seconds": chosen_message.time_in_seconds,
        "amount": chosen_message.amount,
        "message_type": chosen_message.message_type,
        "options": options,
        "correct_author_db_id": correct_author.id,
        "difficulty": difficulty_score,
        "difficulty_level": difficulty_level,
    }


def generate_quiz_batch(
    db: Session,
    channel_db_id: int,
    total_count: int,
    easy_ratio: float = 0.3,
    medium_ratio: float = 0.5,
    hard_ratio: float = 0.2,
) -> list[dict]:
    """
    依比例產生一批題目。比例會自動正規化（不必剛好加總為1）。
    避免同一則留言在同一批次內重複出題。

    若某難度等級的候選不足，仍會用 fallback 補位，不直接失敗；
    但若頻道資料量太小，回傳的題目數可能少於 total_count。
    """
    total_ratio = easy_ratio + medium_ratio + hard_ratio
    if total_ratio <= 0:
        easy_ratio, medium_ratio, hard_ratio = 0.3, 0.5, 0.2
        total_ratio = 1.0

    easy_n = round(total_count * easy_ratio / total_ratio)
    medium_n = round(total_count * medium_ratio / total_ratio)
    hard_n = total_count - easy_n - medium_n

    level_plan: list[DifficultyLevel] = (
        ["easy"] * easy_n + ["medium"] * medium_n + ["hard"] * max(hard_n, 0)
    )
    random.shuffle(level_plan)

    quizzes: list[dict] = []
    used_message_ids: list[int] = []

    for level in level_plan:
        quiz = generate_quiz(db, channel_db_id, difficulty_level=level, exclude_message_ids=used_message_ids)
        if quiz is None:
            continue
        quizzes.append(quiz)
        used_message_ids.append(quiz["message_db_id"])

    return quizzes


def record_quiz_answer(
    db: Session,
    channel_db_id: int,
    quiz: dict,
    answered_author_db_id: Optional[int],
) -> QuizRecord:
    """把出題與作答結果記錄下來（可用於之後統計答對率）。"""
    is_correct = (
        answered_author_db_id is not None
        and answered_author_db_id == quiz["correct_author_db_id"]
    )
    record = QuizRecord(
        channel_id=channel_db_id,
        message_id=quiz["message_db_id"],
        correct_author_id=quiz["correct_author_db_id"],
        option_author_ids=[o["author_db_id"] for o in quiz["options"]],
        difficulty=quiz.get("difficulty"),
        answered_author_id=answered_author_db_id,
        is_correct=is_correct,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record
