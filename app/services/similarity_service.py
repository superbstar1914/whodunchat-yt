"""
觀眾風格相似度計算模組（支援步驟7的出題邏輯，v5）：
計算頻道內所有觀眾兩兩之間的「風格相似度」，寫入 AuthorSimilarity 表快取。

相似度計算方式（混合兩種訊號，各自正規化到 0~1 後加權平均）：
1. 文字風格相似度：用 TF-IDF (char n-gram，對中文效果較好) 對每位觀眾的
   未過濾留言即時串接文本向量化，算 cosine similarity。
2. 結構化特徵相似度：比較 hour_distribution（發言時段）、
   punctuation_rate / question_rate / exclaim_rate、avg_message_length
   等數值特徵的歐式距離，轉換成相似度。

儲存空間優化（v5）：
- 計算時即時從 ChatMessage 撈取有效留言串接，AuthorProfile.features 不落地儲存 ngram_text。
- 相似度以 uint8（0~255 SmallInteger）儲存，節省儲存空間。
- 每位觀眾只快取前 TOP_K_PER_AUTHOR（60）名最相似對象，避免 O(n^2) 全矩陣膨脹。
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.database import DATABASE_URL
from app.models.models import Author, AuthorProfile, AuthorSimilarity, Stream, ChatMessage

_IS_SQLITE = DATABASE_URL.startswith("sqlite")

MIN_MESSAGES_FOR_SIMILARITY = 5  # 留言數低於此門檻的觀眾不納入相似度計算
TEXT_WEIGHT = 0.7
STRUCTURAL_WEIGHT = 0.3
TOP_K_PER_AUTHOR = 60  # 每位觀眾只保留前 60 名相似度最高者


def _structural_vector(features: dict) -> np.ndarray:
    """把結構化特徵組成一個數值向量，供歐式距離計算。"""
    hour_dist = features.get("hour_distribution") or [0.0] * 24
    vec = list(hour_dist) + [
        features.get("punctuation_rate", 0.0),
        features.get("question_rate", 0.0),
        features.get("exclaim_rate", 0.0),
        min(features.get("avg_message_length", 0.0) / 50.0, 1.0),
    ]
    return np.array(vec, dtype=float)


def _fetch_ngram_texts(db: Session, channel_db_id: int, author_ids: list[int]) -> dict[int, str]:
    """
    即時從 chat_messages 撈出每位候選觀眾「所有未被過濾的留言」串接成一段文本，
    供 TF-IDF 使用。算完不落地存資料庫，避免重複備份留言全文。
    """
    if not author_ids:
        return {}
    rows = (
        db.query(ChatMessage.author_id, ChatMessage.message_text)
        .join(Stream, ChatMessage.stream_id == Stream.id)
        .filter(
            Stream.channel_id == channel_db_id,
            ChatMessage.author_id.in_(author_ids),
            ChatMessage.is_filtered == False,  # noqa: E712
        )
        .all()
    )
    parts: dict[int, list[str]] = defaultdict(list)
    for author_id, text in rows:
        if text:
            parts[author_id].append(text)
    return {aid: " ".join(texts) for aid, texts in parts.items()}


def compute_channel_similarities(db: Session, channel_db_id: int) -> dict:
    """
    計算某頻道所有符合條件觀眾的兩兩相似度，只把每位觀眾前 TOP_K_PER_AUTHOR 名
    轉為 uint8 (0~255) 寫入 AuthorSimilarity 快取。
    回傳 { "author_count": int, "pair_count": int }
    """
    channel_author_ids = (
        db.query(ChatMessage.author_id)
        .join(Stream, ChatMessage.stream_id == Stream.id)
        .filter(Stream.channel_id == channel_db_id)
        .distinct()
        .subquery()
    )

    authors_with_profiles = (
        db.query(Author, AuthorProfile)
        .join(
            AuthorProfile,
            AuthorProfile.author_id == Author.id,
        )
        .filter(Author.id.in_(channel_author_ids))
        .all()
    )

    # 篩選符合最低留言數門檻的觀眾
    candidates = []
    for author, profile in authors_with_profiles:
        msg_count = (profile.features or {}).get("message_count", 0)
        if msg_count >= MIN_MESSAGES_FOR_SIMILARITY:
            candidates.append((author, profile))

    if len(candidates) < 2:
        return {"author_count": len(candidates), "pair_count": 0}

    author_ids = [a.id for a, _ in candidates]

    # 即時撈取留言全文做 TF-IDF（不佔 DB 空間）
    ngram_map = _fetch_ngram_texts(db, channel_db_id, author_ids)
    ngram_texts = [ngram_map.get(aid, "") for aid in author_ids]
    structural_vecs = np.array([_structural_vector(p.features or {}) for _, p in candidates])

    # 文字相似度：char n-gram TF-IDF
    vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3), min_df=1, max_features=5000)
    try:
        tfidf_matrix = vectorizer.fit_transform(ngram_texts)
        text_sim_matrix = cosine_similarity(tfidf_matrix)
    except ValueError:
        text_sim_matrix = np.zeros((len(candidates), len(candidates)))

    # 結構化相似度：用歐式距離轉換成相似度 (1 / (1 + distance))
    struct_sim_matrix = np.zeros((len(candidates), len(candidates)))
    for i in range(len(candidates)):
        for j in range(len(candidates)):
            dist = np.linalg.norm(structural_vecs[i] - structural_vecs[j])
            struct_sim_matrix[i][j] = 1.0 / (1.0 + dist)

    final_sim_matrix = TEXT_WEIGHT * text_sim_matrix + STRUCTURAL_WEIGHT * struct_sim_matrix

    # 每位觀眾只保留前 TOP_K_PER_AUTHOR 名，並將相似度縮放為 0~255 (uint8)
    pair_best: dict[tuple[int, int], int] = {}
    n = len(candidates)
    k = min(TOP_K_PER_AUTHOR, n - 1)
    for i in range(n):
        sims = [(j, final_sim_matrix[i][j]) for j in range(n) if j != i]
        sims.sort(key=lambda x: -x[1])
        for j, sim in sims[:k]:
            if math.isnan(sim):
                sim = 0.0
            # 轉換為 0~255 uint8 整數
            sim_uint8 = max(0, min(255, int(round(sim * 255.0))))
            a_id, b_id = author_ids[i], author_ids[j]
            lo, hi = (a_id, b_id) if a_id < b_id else (b_id, a_id)
            key = (lo, hi)
            # 取兩方向計算出的較高值
            if key not in pair_best or sim_uint8 > pair_best[key]:
                pair_best[key] = sim_uint8

    rows = [
        {"author_id_a": lo, "author_id_b": hi, "similarity": sim_val}
        for (lo, hi), sim_val in pair_best.items()
    ]

    _replace_similarities(db, author_ids, rows)

    return {"author_count": len(candidates), "pair_count": len(rows)}


def _replace_similarities(db: Session, candidate_author_ids: list[int], rows: list[dict]) -> None:
    """刪除舊資料並寫入 top-K 相似度列。"""
    if candidate_author_ids:
        db.execute(
            delete(AuthorSimilarity).where(
                AuthorSimilarity.author_id_a.in_(candidate_author_ids)
                | AuthorSimilarity.author_id_b.in_(candidate_author_ids)
            )
        )
    if not rows:
        db.commit()
        return

    table = AuthorSimilarity.__table__
    BATCH = 500
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        if _IS_SQLITE:
            stmt = sqlite_insert(table).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["author_id_a", "author_id_b"],
                set_={"similarity": stmt.excluded.similarity},
            )
        else:
            stmt = pg_insert(table).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["author_id_a", "author_id_b"],
                set_={"similarity": stmt.excluded.similarity},
            )
        db.execute(stmt)
    db.commit()


def get_most_similar_authors(db: Session, author_db_id: int, top_n: int = 5) -> list[tuple[int, float]]:
    """查快取表，取得與指定觀眾最相似的其他觀眾（將 uint8 轉回 0.0~1.0 浮點數）。"""
    rows_a = (
        db.query(AuthorSimilarity.author_id_b, AuthorSimilarity.similarity)
        .filter(AuthorSimilarity.author_id_a == author_db_id)
        .all()
    )
    rows_b = (
        db.query(AuthorSimilarity.author_id_a, AuthorSimilarity.similarity)
        .filter(AuthorSimilarity.author_id_b == author_db_id)
        .all()
    )
    combined = list(rows_a) + list(rows_b)
    combined.sort(key=lambda x: -x[1])
    # uint8 (0~255) 轉換回 0.0~1.0
    return [(aid, round(float(sim_int) / 255.0, 4)) for aid, sim_int in combined[:top_n]]
