"""
Database connection setup.
Uses the DATABASE_URL environment variable to switch between SQLite (local
development) and Postgres (Render production).

Local default: sqlite:///./whodunchat.db
On Render: DATABASE_URL is injected automatically by Render's managed
Postgres. We rewrite the scheme to use the psycopg (v3) driver, since
psycopg2-binary's precompiled wheels lag behind new Python releases and
can fail to install locally on newer Python versions. See
requirements-render.txt for why psycopg[binary] is installed separately
only on Render.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./whodunchat.db")

# Render's Postgres URL usually starts with postgres:// or postgresql://.
# Rewrite either to postgresql+psycopg:// so SQLAlchemy uses the psycopg (v3)
# driver instead of defaulting to psycopg2 (which we don't install locally).
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

# pool_pre_ping: 連線前先 ping，避免外部免費資料庫（如 Miget Postgres）閒置睡眠喚醒時斷線
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)

# SQLite 併發優化：開啟 WAL 模式與 busy_timeout，允許讀寫並行並大幅降低鎖定衝突
if DATABASE_URL.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables if they don't exist yet."""
    from app.models import models  # noqa: F401  (ensures models are registered on Base.metadata)
    Base.metadata.create_all(bind=engine)
