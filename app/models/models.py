"""
WhoDunChat 資料庫模型 (v5)

設計重點：
- ChatMessage.message_id 唯一，做去重的依據
- message_text: 未過濾留言保留原文；被過濾的留言原文清空（設為 None）只留 normalized_text 省空間
- amount: 超級留言（Super Chat）金額紀錄
- time_in_seconds: 影片內相對秒數，用於結果回顧時間戳連結
- AuthorSimilarity: 相似度以 uint8 (SmallInteger, 0~255) 快取每位觀眾 Top-K 最相似名單
- AuthorProfile: 存放統計特徵，不冗餘儲存 ngram_text
"""
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Integer, SmallInteger, Float, Boolean, DateTime, ForeignKey,
    Text, UniqueConstraint, Index, JSON
)
from sqlalchemy.orm import relationship

from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class Channel(Base):
    __tablename__ = "channels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(String, unique=True, nullable=False, index=True)  # UC...
    channel_name = Column(String, nullable=True)
    channel_url = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    streams = relationship("Stream", back_populates="channel", cascade="all, delete-orphan")


class Stream(Base):
    __tablename__ = "streams"

    id = Column(Integer, primary_key=True, autoincrement=True)
    video_id = Column(String, unique=True, nullable=False, index=True)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=False)
    title = Column(String, nullable=True)
    published_at = Column(DateTime, nullable=True)

    # 抓取狀態: pending / fetching / done / error
    fetch_status = Column(String, default="pending", nullable=False)
    fetch_error = Column(Text, nullable=True)
    message_count = Column(Integer, default=0)

    created_at = Column(DateTime, default=utcnow)
    fetched_at = Column(DateTime, nullable=True)

    channel = relationship("Channel", back_populates="streams")
    messages = relationship("ChatMessage", back_populates="stream", cascade="all, delete-orphan")


class Author(Base):
    __tablename__ = "authors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    author_id = Column(String, unique=True, nullable=False, index=True)  # YouTube channel id of the commenter
    display_name = Column(String, nullable=True)  # 最新已知暱稱
    # 僅代表 yt-dlp 已成功解析過；聊天室抓到的 name 只是暫存 fallback。
    name_resolved = Column(Boolean, default=False)

    total_message_count = Column(Integer, default=0)
    filtered_message_count = Column(Integer, default=0)  # 通過清洗、可用來出題的留言數

    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    messages = relationship("ChatMessage", back_populates="author")
    profile = relationship("AuthorProfile", back_populates="author", uselist=False,
                            cascade="all, delete-orphan")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(String, unique=True, nullable=False, index=True)  # chat-downloader message_id，去重用

    stream_id = Column(Integer, ForeignKey("streams.id"), nullable=False, index=True)
    author_id = Column(Integer, ForeignKey("authors.id"), nullable=False, index=True)

    # 未過濾留言保留原文；被過濾的留言原文清空（設為 None）省空間，只留 normalized_text
    message_text = Column(Text, nullable=True)
    normalized_text = Column(Text, nullable=True, index=True)  # 清洗/正規化後的文字，用來判斷重複
    timestamp = Column(DateTime, nullable=True)  # 留言時間（絕對時間，若有的話）
    time_in_seconds = Column(Float, nullable=True)  # 影片內的相對秒數，用於結果回顧時間戳連結

    message_type = Column(String, nullable=True)  # text_message / superchat / paid_message 等
    amount = Column(String, nullable=True)  # 若為超級留言，金額字串 (例如 "NT$ 300")

    # 清洗結果
    is_filtered = Column(Boolean, default=False, nullable=False)  # True = 被剔除，不用於出題
    filter_reason = Column(String, nullable=True)  # duplicate_template / mass_spam / too_short / emoji_only ...

    created_at = Column(DateTime, default=utcnow)

    stream = relationship("Stream", back_populates="messages")
    author = relationship("Author", back_populates="messages")

    __table_args__ = (
        Index("ix_messages_stream_normalized", "stream_id", "normalized_text"),
    )


class AuthorProfile(Base):
    """觀眾風格特徵分析結果，重新分析時整包覆蓋。"""
    __tablename__ = "author_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    author_id = Column(Integer, ForeignKey("authors.id"), unique=True, nullable=False)

    # 特徵向量與統計摘要，存成 JSON，彈性大，方便之後擴充特徵不用改 schema
    features = Column(JSON, nullable=False, default=dict)

    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    author = relationship("Author", back_populates="profile")


class AuthorSimilarity(Base):
    """觀眾兩兩風格相似度快取，以 uint8 (0~255) 儲存，出題時直接查表。"""
    __tablename__ = "author_similarities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    author_id_a = Column(Integer, ForeignKey("authors.id"), nullable=False, index=True)
    author_id_b = Column(Integer, ForeignKey("authors.id"), nullable=False, index=True)
    similarity = Column(SmallInteger, nullable=False)  # 0~255 (uint8)，除以 255.0 即為 0.0~1.0 相似度

    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("author_id_a", "author_id_b", name="uq_author_pair"),
        Index("ix_sim_a_score", "author_id_a", "similarity"),
    )


class QuizRecord(Base):
    """出題紀錄，可用來之後統計主播答對率、避免重複出題等。"""
    __tablename__ = "quiz_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=False)
    message_id = Column(Integer, ForeignKey("chat_messages.id"), nullable=False)
    correct_author_id = Column(Integer, ForeignKey("authors.id"), nullable=False)
    option_author_ids = Column(JSON, nullable=False)  # list[int]，選項（含正確答案）
    difficulty = Column(Float, nullable=True)  # 用相似度算出的難度分數

    answered_author_id = Column(Integer, nullable=True)  # 使用者選的答案
    is_correct = Column(Boolean, nullable=True)

    created_at = Column(DateTime, default=utcnow)
