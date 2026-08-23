"""
Pipeline 相關 API：
POST /api/channels/fetch   啟動一個頻道的完整抓取流程（背景執行）
GET  /api/jobs/{job_id}    查詢 job 進度
GET  /api/channels         列出已抓取過的頻道
GET  /api/emojis           取得 YouTube 表情符號映射表
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.models import Channel, Stream
from app.services.job_manager import create_job, get_job, run_job_in_background
from app.services.pipeline_service import run_full_pipeline
from app.services.emoji_service import YOUTUBE_EMOJI_MAP

router = APIRouter(prefix="/api", tags=["pipeline"])


class FetchChannelRequest(BaseModel):
    channel: str = Field(..., description="頻道網址、@handle 或 channel id")
    max_streams: Optional[int] = Field(None, description="最多抓幾場直播，留空代表全部")
    name_resolve_limit: Optional[int] = Field(
        None, description="最多補完幾位觀眾的暱稱，留空代表全部（暱稱補完較慢，建議先設上限測試）"
    )
    max_concurrent_fetches: Optional[int] = Field(
        3, ge=1, le=10, description="同時最多幾場直播並行抓取，預設 3"
    )


@router.post("/channels/fetch")
def fetch_channel(req: FetchChannelRequest):
    job_id = create_job("full_pipeline", meta={"channel_input": req.channel})

    def target():
        run_full_pipeline(
            job_id=job_id,
            channel_input=req.channel,
            max_streams=req.max_streams,
            name_resolve_limit=req.name_resolve_limit,
            max_concurrent_fetches=req.max_concurrent_fetches or 3,
        )

    run_job_in_background(job_id, target)
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
def get_job_status(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/emojis")
def get_emojis():
    """回傳 YouTube 官方表情對照表。"""
    return YOUTUBE_EMOJI_MAP


@router.get("/channels")
def list_channels():
    db: Session = SessionLocal()
    try:
        channels = db.query(Channel).all()
        result = []
        for c in channels:
            stream_count = db.query(Stream).filter(Stream.channel_id == c.id).count()
            done_count = (
                db.query(Stream)
                .filter(Stream.channel_id == c.id, Stream.fetch_status == "done")
                .count()
            )
            result.append({
                "channel_db_id": c.id,
                "channel_id": c.channel_id,
                "channel_name": c.channel_name,
                "channel_url": c.channel_url,
                "stream_count": stream_count,
                "streams_fetched": done_count,
            })
        return result
    finally:
        db.close()


@router.get("/channels/{channel_db_id}/streams")
def list_channel_streams(channel_db_id: int):
    db: Session = SessionLocal()
    try:
        channel = db.get(Channel, channel_db_id)
        if channel is None:
            raise HTTPException(status_code=404, detail="channel not found")
        streams = db.query(Stream).filter(Stream.channel_id == channel_db_id).all()
        return [
            {
                "stream_db_id": s.id,
                "video_id": s.video_id,
                "title": s.title,
                "published_at": s.published_at.isoformat() if s.published_at else None,
                "fetch_status": s.fetch_status,
                "message_count": s.message_count,
                "fetch_error": s.fetch_error,
            }
            for s in streams
        ]
    finally:
        db.close()
