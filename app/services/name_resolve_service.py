"""
Display name 補完模組（步驟5）：
用 author.id 透過 yt_dlp_service.resolve_display_name 批次補上觀眾目前的暱稱。

設計重點：
- 聊天室抓取階段已盡量直接記錄暱稱，name_resolved == False 的觀眾才需要這裡補查
- 並行化：用 ThreadPoolExecutor 同時對多位觀眾查詢，預設 3 個 worker
  - yt-dlp 對每位觀眾各打一次 HTTP，I/O bound，適合執行緒並行
  - 每個 worker 查完後統一用全域 lock 回寫資料庫，避免 SQLite 鎖定
- 防限速：全域 429 檢測，觸發時自動降為序列模式並加大冷卻
- 單筆失敗不中斷整批，維持原有 display_name 當 fallback
"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.models import Author
from app.services.ytdlp_service import resolve_display_name

logger = logging.getLogger(__name__)

DEFAULT_WORKERS = 3
DEFAULT_SLEEP_SECONDS = 0.5   # 並行時每 worker 完成後的基礎間隔（比序列短）
_db_write_lock = threading.Lock()

# 全域限速旗標（同 parallel_fetch_service 設計）
_slowdown_active = threading.Event()


def _is_rate_limit_error(msg: str) -> bool:
    m = msg.lower()
    return any(k in m for k in ("429", "too many requests", "sign in", "bot", "rate limit", "confirm you"))


def _resolve_one(author_id: str, author_db_id: int) -> tuple[int, str | None, str | None]:
    """
    在背景執行緒中查詢單一觀眾的 display_name。
    回傳 (author_db_id, resolved_name_or_None, error_or_None)
    """
    if _slowdown_active.is_set():
        # 限速降速模式：額外等待
        time.sleep(random.uniform(2.0, 3.5))

    try:
        name = resolve_display_name(author_id)
        return (author_db_id, name, None)
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        if _is_rate_limit_error(err):
            _slowdown_active.set()
            logger.warning("Name resolve: 偵測到限速，啟動降速保護閥")
        return (author_db_id, None, err)


def resolve_pending_authors(
    db: Session,
    limit: Optional[int] = None,
    max_workers: int = DEFAULT_WORKERS,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    progress_callback=None,
) -> dict:
    """
    並行補完尚未解析暱稱的觀眾。

    limit: 最多處理幾筆（None = 不限制）
    max_workers: 最多同時幾個執行緒查詢 YouTube（預設 3）
    progress_callback(done, total, current_author_id) 可選

    回傳 { "processed": int, "resolved": int, "failed": int, "fallback_used": int }
    """
    _slowdown_active.clear()

    query = db.query(Author).filter(Author.name_resolved == False)  # noqa: E712
    if limit:
        query = query.limit(limit)
    authors = query.all()

    # 把需要查詢的 (author_id, db_id) 列出來，main db session 先暫時不用
    tasks: list[tuple[str, int]] = [(a.author_id, a.id) for a in authors]
    total = len(tasks)
    stats = {"processed": 0, "resolved": 0, "failed": 0, "fallback_used": 0}

    if total == 0:
        return stats

    logger.info("name_resolve: 開始並行補完，共 %d 位觀眾，max_workers=%d", total, max_workers)

    # 控制整體 QPS：用 semaphore 讓最多 max_workers 個請求同時進行
    semaphore = threading.Semaphore(max_workers)
    results: dict[int, tuple[str | None, str | None]] = {}  # db_id -> (name, error)

    def _worker(author_id: str, db_id: int):
        with semaphore:
            res = _resolve_one(author_id, db_id)
            with _db_write_lock:
                results[res[0]] = (res[1], res[2])

    futures = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for author_id, db_id in tasks:
            futures.append(pool.submit(_worker, author_id, db_id))
            # 小抖動：每提交一個任務後稍候，避免瞬間同時發出大量請求
            time.sleep(random.uniform(0.1, 0.3))

        for idx, future in enumerate(as_completed(futures)):
            future.result()  # 讓任何例外浮現到日誌

            if progress_callback:
                author_id_hint = tasks[idx][0] if idx < len(tasks) else ""
                progress_callback(idx + 1, total, author_id_hint)

    # 全部查詢結束後，統一回寫資料庫（減少 lock 競爭）
    write_db: Session = SessionLocal()
    try:
        for db_id, (name, error) in results.items():
            author = write_db.get(Author, db_id)
            if author is None:
                continue

            if name:
                author.display_name = name
                author.name_resolved = True
                stats["resolved"] += 1
            else:
                if author.display_name:
                    stats["fallback_used"] += 1
                # 失敗不能標記成 resolved，否則之後永遠不會重試；
                # display_name 仍保留聊天室抓到的 fallback。
                author.name_resolved = False
                stats["failed"] += 1

            stats["processed"] += 1

        write_db.commit()
    finally:
        write_db.close()

    logger.info(
        "name_resolve 完成：processed=%d resolved=%d failed=%d fallback=%d",
        stats["processed"], stats["resolved"], stats["failed"], stats["fallback_used"],
    )
    return stats


def get_display_name_or_fallback(author: Author) -> str:
    """
    給前端顯示用：優先用已解析的 display_name，
    若無則顯示截短的 author_id。
    """
    if author.display_name:
        return author.display_name
    return f"(未知觀眾 {author.author_id[:8]}...)"
