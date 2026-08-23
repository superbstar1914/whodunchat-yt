"""
出題相關 API：
GET  /api/channels/{channel_db_id}/quiz          產生一題新測驗
POST /api/channels/{channel_db_id}/quiz/answer    提交作答結果
GET  /api/channels/{channel_db_id}/authors        列出該頻道分析過的觀眾（供除錯/瀏覽）
GET  /api/authors/{author_db_id}                  查看單一觀眾的風格分析
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.models import Channel, Author, AuthorProfile
from app.services.quiz_service import generate_quiz, generate_quiz_batch, record_quiz_answer
from app.services.name_resolve_service import get_display_name_or_fallback
from app.services.analysis_service import get_author_summary_text

router = APIRouter(prefix="/api", tags=["quiz"])


@router.get("/channels/{channel_db_id}/quiz")
def get_quiz(channel_db_id: int, difficulty_level: str = "random"):
    db: Session = SessionLocal()
    try:
        channel = db.get(Channel, channel_db_id)
        if channel is None:
            raise HTTPException(status_code=404, detail="channel not found")

        quiz = generate_quiz(db, channel_db_id, difficulty_level=difficulty_level)
        if quiz is None:
            raise HTTPException(
                status_code=409,
                detail="這個頻道目前沒有足夠的留言資料可以出題，請先完成抓取流程。",
            )
        return quiz
    finally:
        db.close()


class QuizBatchRequest(BaseModel):
    total_count: int = 10
    easy_ratio: float = 0.3
    medium_ratio: float = 0.5
    hard_ratio: float = 0.2


@router.post("/channels/{channel_db_id}/quiz/batch")
def get_quiz_batch(channel_db_id: int, req: QuizBatchRequest):
    db: Session = SessionLocal()
    try:
        channel = db.get(Channel, channel_db_id)
        if channel is None:
            raise HTTPException(status_code=404, detail="channel not found")

        quizzes = generate_quiz_batch(
            db, channel_db_id,
            total_count=req.total_count,
            easy_ratio=req.easy_ratio,
            medium_ratio=req.medium_ratio,
            hard_ratio=req.hard_ratio,
        )
        if not quizzes:
            raise HTTPException(
                status_code=409,
                detail="這個頻道目前沒有足夠的留言資料可以出題，請先完成抓取流程。",
            )
        return {"quizzes": quizzes, "requested_count": req.total_count, "actual_count": len(quizzes)}
    finally:
        db.close()


class AnswerRequest(BaseModel):
    message_db_id: int
    correct_author_db_id: int
    option_author_db_ids: list[int]
    difficulty: Optional[float] = None
    answered_author_db_id: Optional[int] = None


@router.post("/channels/{channel_db_id}/quiz/answer")
def submit_answer(channel_db_id: int, req: AnswerRequest):
    db: Session = SessionLocal()
    try:
        quiz_dict = {
            "message_db_id": req.message_db_id,
            "correct_author_db_id": req.correct_author_db_id,
            "options": [{"author_db_id": aid} for aid in req.option_author_db_ids],
            "difficulty": req.difficulty,
        }
        record = record_quiz_answer(db, channel_db_id, quiz_dict, req.answered_author_db_id)
        return {
            "is_correct": record.is_correct,
            "correct_author_db_id": record.correct_author_id,
        }
    finally:
        db.close()


@router.get("/channels/{channel_db_id}/authors")
def list_authors(channel_db_id: int, min_messages: int = 1):
    db: Session = SessionLocal()
    try:
        from app.models.models import ChatMessage, Stream
        rows = (
            db.query(Author)
            .join(ChatMessage, ChatMessage.author_id == Author.id)
            .join(Stream, ChatMessage.stream_id == Stream.id)
            .filter(Stream.channel_id == channel_db_id)
            .distinct()
            .all()
        )
        result = []
        for a in rows:
            if (a.filtered_message_count or 0) < min_messages:
                continue
            result.append({
                "author_db_id": a.id,
                "display_name": get_display_name_or_fallback(a),
                "total_message_count": a.total_message_count,
                "filtered_message_count": a.filtered_message_count,
                "name_resolved": a.name_resolved,
            })
        result.sort(key=lambda x: -(x["filtered_message_count"] or 0))
        return result
    finally:
        db.close()


@router.get("/authors/{author_db_id}")
def get_author_detail(author_db_id: int):
    db: Session = SessionLocal()
    try:
        author = db.get(Author, author_db_id)
        if author is None:
            raise HTTPException(status_code=404, detail="author not found")
        profile = db.query(AuthorProfile).filter(AuthorProfile.author_id == author_db_id).one_or_none()
        return {
            "author_db_id": author.id,
            "display_name": get_display_name_or_fallback(author),
            "total_message_count": author.total_message_count,
            "filtered_message_count": author.filtered_message_count,
            "features": profile.features if profile else None,
            "summary_text": get_author_summary_text(author, profile),
        }
    finally:
        db.close()
