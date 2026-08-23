"""
多執行緒並行抓取直播聊天室服務（Parallel Chat Fetch Service）。

設計重點：
1. 併發模型：使用 concurrent.futures.ThreadPoolExecutor 進行多場直播並行抓取。
2. 獨立 Session：每個執行緒獨立建立與關閉 SessionLocal()，避免跨執行緒共用 Session。
3. 隨機抖動（Jitter）：任務開始前加入 0.5s~1.5s 隨機延遲，防止流量尖峰同時打向 YouTube。
4. 全域防限速降速閥（Global Slowdown Circuit Breaker）：
   - 監聽 429 / Too Many Requests / Bot 驗證錯誤。
   - 一旦觸發，自動收緊為單執行緒序列執行並加大冷卻時間，保護後續任務。
5. 進度回報：
   - 格式：{"total_streams": N, "completed_streams": X, "failed_streams": Y, "in_progress": [標題,...]}
"""
from __future__ import annotations

import concurrent.futures
import logging
import random
import threading
import time
from typing import Callable, Optional

from app.database import SessionLocal
from app.models.models import Stream
from app.services import chat_fetch_service
from app.services.job_manager import update_job

logger = logging.getLogger(__name__)

RATE_LIMIT_KEYWORDS = ("429", "too many requests", "rate limit", "bot", "captcha", "sign in to confirm")


def is_rate_limit_error(error_str: str) -> bool:
    if not error_str:
        return False
    low = error_str.lower()
    return any(k in low for k in RATE_LIMIT_KEYWORDS)


def parallel_fetch_streams(
    job_id: str,
    stream_ids: list[int],
    max_concurrent: int = 3,
    initial_completed: int = 0,
    total_streams_count: Optional[int] = None,
) -> dict:
    """
    並行抓取指定 stream_ids 清單的聊天室。
    stream_ids: 需要抓取的 Stream id 列表（不含已完成的場次）
    initial_completed: 已經完成（跳過）的場次數量
    total_streams_count: 頻道直播總場次
    """
    total_streams = total_streams_count if total_streams_count is not None else (len(stream_ids) + initial_completed)
    completed_streams = initial_completed
    failed_streams = 0
    in_progress_titles: dict[int, str] = {}  # stream_id -> title

    progress_lock = threading.Lock()
    slowdown_active = threading.Event()
    slowdown_lock = threading.Lock()

    def report_progress():
        with progress_lock:
            prog = {
                "total_streams": total_streams,
                "completed_streams": completed_streams,
                "failed_streams": failed_streams,
                "in_progress": list(in_progress_titles.values()),
                "slowdown_active": slowdown_active.is_set(),
            }
        update_job(job_id, progress=prog)

    report_progress()

    if not stream_ids:
        return {
            "total": total_streams,
            "completed": completed_streams,
            "failed": failed_streams,
        }

    # 限制並行數在 1 到 10 之間，預設 3
    effective_concurrency = max(1, min(max_concurrent, 10))

    def worker_task(stream_id: int):
        nonlocal completed_streams, failed_streams

        # 隨機抖動 (Jitter)，避免多執行緒在同一毫秒同時送出請求
        time.sleep(random.uniform(0.5, 1.5))

        # 若已觸發全域降速，進行單執行緒排隊與冷卻
        if slowdown_active.is_set():
            time.sleep(random.uniform(2.0, 3.5))

        db = SessionLocal()
        try:
            stream = db.get(Stream, stream_id)
            if not stream:
                with progress_lock:
                    failed_streams += 1
                report_progress()
                return

            stream_title = stream.title or f"直播 #{stream.video_id}"
            with progress_lock:
                in_progress_titles[stream_id] = stream_title
            report_progress()

            def _single_stream_cb(stats):
                # 局部抓取中回報（不頻繁更新整體以免刷爆）
                pass

            # 如果在降速模式中，使用 slowdown_lock 強制單一執行緒抓取
            if slowdown_active.is_set():
                with slowdown_lock:
                    stats = chat_fetch_service.fetch_stream_chat(db, stream, progress_callback=_single_stream_cb)
            else:
                stats = chat_fetch_service.fetch_stream_chat(db, stream, progress_callback=_single_stream_cb)

            # 檢查是否有限速錯誤
            err = stats.get("error")
            if err and is_rate_limit_error(err):
                if not slowdown_active.is_set():
                    logger.warning("Rate limit detected in stream %s! Triggering global slowdown circuit breaker.", stream.video_id)
                    slowdown_active.set()

            with progress_lock:
                in_progress_titles.pop(stream_id, None)
                if stream.fetch_status == "done":
                    completed_streams += 1
                else:
                    failed_streams += 1
            report_progress()

        except Exception as exc:  # noqa: BLE001
            logger.exception("Worker failed on stream_id=%s", stream_id)
            err_str = str(exc)
            if is_rate_limit_error(err_str) and not slowdown_active.is_set():
                logger.warning("Rate limit exception in worker for stream %s! Triggering slowdown.", stream_id)
                slowdown_active.set()

            with progress_lock:
                in_progress_titles.pop(stream_id, None)
                failed_streams += 1
            report_progress()
        finally:
            db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=effective_concurrency) as executor:
        futures = [executor.submit(worker_task, sid) for sid in stream_ids]
        concurrent.futures.wait(futures)

    report_progress()

    return {
        "total": total_streams,
        "completed": completed_streams,
        "failed": failed_streams,
    }
