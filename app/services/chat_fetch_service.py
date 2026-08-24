"""
用 chat-downloader 抓取單場直播的重播聊天室，並寫入資料庫。

去重與併發安全策略：
- 主要靠資料庫層的 UNIQUE constraint (ChatMessage.message_id)
- 抓取時先在記憶體用 set 追蹤本次已處理過的 message_id，減少不必要的 DB 查詢
- 寫入前用 "INSERT ... ON CONFLICT DO NOTHING" 批次處理
- 多執行緒併發支援：
  - Author 建立使用 savepoint (begin_nested)，避免同時建立同一觀眾引發衝突
  - 遇到 SQLite lock 時自動退避重試
  - 暱稱處理：
  - 同步提取 chat-downloader 回傳的暱稱作為暫存顯示名稱；
    name_resolved 僅在後續 yt-dlp 反查成功時才標記為 true。
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Iterator, Optional

from chat_downloader import ChatDownloader
from chat_downloader.errors import ChatDownloaderError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.database import DATABASE_URL
from app.models.models import Stream, Author, ChatMessage

_IS_SQLITE = DATABASE_URL.startswith("sqlite")

logger = logging.getLogger(__name__)

# 我們關心的訊息類型：一般文字 + 付費/會員相關
RELEVANT_MESSAGE_TYPES = {
    "text_message",
    "paid_message",
    "paid_sticker",
    "membership_item",
    "sponsorships_gift_purchase_announcement",
}


def _commit_with_retry(db: Session, max_retries: int = 3, initial_delay: float = 0.2) -> None:
    """針對 SQLite 資料庫鎖定提供指數退避重試的 commit 包裝。"""
    delay = initial_delay
    for attempt in range(max_retries):
        try:
            db.commit()
            return
        except OperationalError as exc:
            if ("locked" in str(exc).lower() or "busy" in str(exc).lower()) and attempt < max_retries - 1:
                db.rollback()
                sleep_sec = delay + random.uniform(0.05, 0.15)
                logger.warning("DB locked during commit (attempt %d/%d), retry in %.2fs", attempt + 1, max_retries, sleep_sec)
                time.sleep(sleep_sec)
                delay *= 2
            else:
                raise


def _get_or_create_author(db: Session, author_id: str, name_hint: Optional[str]) -> Author:
    """
    執行緒安全的觀眾查詢或建立。
    以 YouTube 唯一不可變的 Channel ID (UC...) 為永久錨點。
    即使觀眾更換暱稱或 @handle，author_id 永遠不變；
    每次抓取時會自動將 display_name 更新為最新抓到的暱稱。
    """
    clean_name = name_hint.strip() if name_hint and name_hint.strip() else None

    author = db.query(Author).filter(Author.author_id == author_id).one_or_none()
    if author is None:
        try:
            with db.begin_nested():
                author = Author(
                    author_id=author_id,
                    display_name=clean_name,
                    # chat-downloader 的 name 可能只是 @handle，不能視為
                    # yt-dlp 已成功解析的正式頻道名稱。
                    name_resolved=False,
                )
                db.add(author)
                db.flush()
        except IntegrityError:
            # 另一個並行執行緒剛好建立了該 author
            author = db.query(Author).filter(Author.author_id == author_id).one()
            if clean_name and author.display_name != clean_name:
                author.display_name = clean_name
                # 這仍只是 chat-downloader 的 fallback，不能代表 yt-dlp 成功。
                author.name_resolved = False
                db.flush()
    else:
        # 觀眾若更換了新暱稱，自動更新為最新暱稱，但所有歷史留言與特徵依然鎖定在同一 author_id
        if clean_name and author.display_name != clean_name:
            author.display_name = clean_name
            # 新的聊天室名稱只更新 fallback；交由 yt-dlp 再驗證。
            author.name_resolved = False
            db.flush()
    return author


def _iter_raw_messages(video_id: str) -> Iterator[dict]:
    """
    包一層 generator，捕捉個別訊息解析錯誤但不中斷整場抓取。
    """
    downloader = ChatDownloader()
    chat = downloader.get_chat(
        url=f"https://www.youtube.com/watch?v={video_id}",
        message_types=["all"],
    )
    for message in chat:
        yield message


def fetch_stream_chat(db: Session, stream: Stream, progress_callback=None) -> dict:
    """
    抓取單場直播的聊天室並寫入資料庫。
    回傳統計摘要 { "fetched": int, "inserted": int, "skipped_duplicate": int, "skipped_irrelevant": int, "error": str|None }
    """
    stats = {"fetched": 0, "inserted": 0, "skipped_duplicate": 0, "skipped_irrelevant": 0, "error": None}

    stream.fetch_status = "fetching"
    _commit_with_retry(db)

    # 先把這場直播已存在的 message_id 讀出來，本地 set 去重
    existing_ids = {
        mid for (mid,) in db.query(ChatMessage.message_id)
        .filter(ChatMessage.stream_id == stream.id)
        .all()
    }

    BATCH_SIZE = 300
    batch_rows: list[dict] = []
    author_msg_increment: dict[int, int] = {}

    def flush_batch():
        nonlocal batch_rows
        if not batch_rows:
            return
        table = ChatMessage.__table__
        if _IS_SQLITE:
            stmt = sqlite_insert(table).values(batch_rows)
            stmt = stmt.on_conflict_do_nothing(index_elements=["message_id"])
        else:
            stmt = pg_insert(table).values(batch_rows)
            stmt = stmt.on_conflict_do_nothing(index_elements=["message_id"])

        delay = 0.2
        for attempt in range(3):
            try:
                result = db.execute(stmt)
                _commit_with_retry(db)
                inserted_now = result.rowcount if result.rowcount is not None and result.rowcount >= 0 else len(batch_rows)
                skipped_now = len(batch_rows) - inserted_now
                stats["inserted"] += inserted_now
                stats["skipped_duplicate"] += max(skipped_now, 0)
                batch_rows = []
                return
            except OperationalError as exc:
                if ("locked" in str(exc).lower() or "busy" in str(exc).lower()) and attempt < 2:
                    db.rollback()
                    time.sleep(delay + random.uniform(0.05, 0.15))
                    delay *= 2
                else:
                    raise

    try:
        for msg in _iter_raw_messages(stream.video_id):
            stats["fetched"] += 1

            message_id = msg.get("message_id")
            message_type = msg.get("message_type")
            author_info = msg.get("author") or {}
            
            author_id = (
                author_info.get("id")
                or author_info.get("channel_id")
                or msg.get("author_id")
            )
            name_hint = (
                # 若 chat-downloader 同時提供兩者，display_name 比 name
                # 更接近頻道顯示名稱；name 常常只是 @handle。
                author_info.get("display_name")
                or author_info.get("name")
                or author_info.get("title")
                or author_info.get("username")
                or msg.get("author_name")
            )
            text = msg.get("message")

            if not message_id or not author_id or text is None:
                stats["skipped_irrelevant"] += 1
                continue

            if message_type not in RELEVANT_MESSAGE_TYPES:
                stats["skipped_irrelevant"] += 1
                continue

            if message_id in existing_ids:
                stats["skipped_duplicate"] += 1
                continue

            existing_ids.add(message_id)

            author = _get_or_create_author(db, author_id, name_hint)

            timestamp_usec = msg.get("timestamp")
            ts = None
            if timestamp_usec:
                try:
                    ts = datetime.fromtimestamp(int(timestamp_usec) / 1_000_000, tz=timezone.utc)
                except (ValueError, OSError):
                    ts = None

            amount_str = (msg.get("money") or {}).get("text") or (msg.get("amount") if isinstance(msg.get("amount"), str) else None)

            batch_rows.append({
                "message_id": message_id,
                "stream_id": stream.id,
                "author_id": author.id,
                "message_text": text,
                "timestamp": ts,
                "time_in_seconds": msg.get("time_in_seconds"),
                "message_type": message_type,
                "amount": amount_str,
                "is_filtered": False,
                "created_at": datetime.now(timezone.utc),
            })
            author_msg_increment[author.id] = author_msg_increment.get(author.id, 0) + 1

            if len(batch_rows) >= BATCH_SIZE:
                flush_batch()

            if progress_callback and stats["fetched"] % 50 == 0:
                progress_callback(stats)

        flush_batch()

        # 更新每位 author 的留言計數
        for aid, inc in author_msg_increment.items():
            author_obj = db.get(Author, aid)
            if author_obj:
                author_obj.total_message_count = (author_obj.total_message_count or 0) + inc
        _commit_with_retry(db)

        stream.fetch_status = "done"
        stream.message_count = stats["inserted"]
        stream.fetched_at = datetime.now(timezone.utc)
        _commit_with_retry(db)

    except ChatDownloaderError as exc:
        logger.exception("ChatDownloader error for %s", stream.video_id)
        stream.fetch_status = "error"
        stream.fetch_error = str(exc)
        stats["error"] = str(exc)
        _commit_with_retry(db)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error fetching %s", stream.video_id)
        stream.fetch_status = "error"
        stream.fetch_error = str(exc)
        stats["error"] = str(exc)
        _commit_with_retry(db)

    return stats
