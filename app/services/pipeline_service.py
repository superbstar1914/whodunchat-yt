"""
Pipeline 整合模組：把 7 個步驟串成一條龍，供背景 job 呼叫。

流程：
1. resolve_channel   - 解析頻道 ID
2. list_streams      - 列出過往直播
3. fetch_chat        - 並行抓聊天室（ThreadPoolExecutor + SQLite WAL + 限速保護閥）
4. filter            - 清洗過濾模板/複製文留言 + 黑名單
5. resolve_names     - 補完觀眾 display name（維持序列 + 節流）
6. analyze           - 分析觀眾風格特徵
7. similarity        - 計算觀眾兩兩相似度

每個階段都會更新 job 的 stage / progress，方便前端顯示目前進度。
資料庫 session 在每個階段各自開關，避免長時間佔用同一個 session。
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.models import Channel, Stream
from app.services import ytdlp_service, filter_service
from app.services import name_resolve_service, analysis_service, similarity_service
from app.services import parallel_fetch_service
from app.services.job_manager import update_job

logger = logging.getLogger(__name__)


def run_full_pipeline(
    job_id: str,
    channel_input: str,
    max_streams: Optional[int] = None,
    name_resolve_limit: Optional[int] = None,
    max_concurrent_fetches: int = 3,
) -> None:
    """
    完整跑一次 pipeline。供 job_manager.run_job_in_background 呼叫。
    """
    db: Session = SessionLocal()
    try:
        # --- Stage 1: resolve channel ---
        update_job(job_id, stage="resolve_channel", progress={})
        info = ytdlp_service.resolve_channel_id(channel_input)

        channel = db.query(Channel).filter(Channel.channel_id == info["channel_id"]).one_or_none()
        if channel is None:
            channel = Channel(
                channel_id=info["channel_id"],
                channel_name=info["channel_name"],
                channel_url=info["channel_url"],
            )
            db.add(channel)
            db.commit()
            db.refresh(channel)
        else:
            channel.channel_name = info["channel_name"] or channel.channel_name
            db.commit()

        update_job(job_id, meta={"channel_db_id": channel.id, "channel_name": channel.channel_name})

        # --- Stage 2: list past live streams ---
        update_job(job_id, stage="list_streams")
        stream_infos = ytdlp_service.list_past_live_streams(channel.channel_id, max_items=max_streams)

        streams: list[Stream] = []
        for s_info in stream_infos:
            stream = db.query(Stream).filter(Stream.video_id == s_info["video_id"]).one_or_none()
            if stream is None:
                stream = Stream(
                    video_id=s_info["video_id"],
                    channel_id=channel.id,
                    title=s_info["title"],
                    published_at=s_info["published_at"],
                )
                db.add(stream)
                db.commit()
                db.refresh(stream)
            streams.append(stream)

        update_job(job_id, progress={"total_streams": len(streams)})

        # --- Stage 3: fetch chat in parallel ---
        update_job(job_id, stage="fetch_chat")
        
        # 分離已完成與待抓取的場次（支援冪等性跳過）
        pending_stream_ids = [s.id for s in streams if s.fetch_status != "done"]
        already_done_count = len(streams) - len(pending_stream_ids)

        parallel_fetch_service.parallel_fetch_streams(
            job_id=job_id,
            stream_ids=pending_stream_ids,
            max_concurrent=max_concurrent_fetches,
            initial_completed=already_done_count,
            total_streams_count=len(streams),
        )

        # 重新載入最新 stream 狀態
        db.expire_all()
        streams = db.query(Stream).filter(Stream.channel_id == channel.id).all()

        # --- Stage 4: filter ---
        update_job(job_id, stage="filter")
        filter_result = filter_service.filter_all_streams(db, streams)
        filter_service.filter_duplicate_across_channel(db, channel.id)
        update_job(job_id, progress={"filter_result": filter_result})

        # --- Stage 5: resolve display names (並行) ---
        update_job(job_id, stage="resolve_names")

        def _name_progress_cb(done, total, current_author_id):
            update_job(job_id, progress={"names_done": done, "names_total": total})

        name_stats = name_resolve_service.resolve_pending_authors(
            db,
            limit=name_resolve_limit,
            max_workers=max_concurrent_fetches,
            progress_callback=_name_progress_cb,
        )
        update_job(job_id, progress={"name_stats": name_stats})

        # --- Stage 6: analyze ---
        update_job(job_id, stage="analyze")

        def _analyze_progress_cb(done, total):
            update_job(job_id, progress={"analyze_done": done, "analyze_total": total})

        analyze_result = analysis_service.analyze_channel_authors(
            db, channel.id, progress_callback=_analyze_progress_cb,
        )
        update_job(job_id, progress={"analyze_result": analyze_result})

        # --- Stage 7: similarity ---
        update_job(job_id, stage="similarity")
        sim_result = similarity_service.compute_channel_similarities(db, channel.id)
        update_job(job_id, progress={"similarity_result": sim_result})

        update_job(job_id, stage="done", meta={
            "channel_db_id": channel.id,
            "channel_name": channel.channel_name,
        })

    finally:
        db.close()
