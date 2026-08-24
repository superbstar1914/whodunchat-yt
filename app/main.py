"""
WhoDunChat 主應用程式入口。

本地啟動：uvicorn app.main:app --reload --port 8000
Render 上：由 render.yaml / Procfile 指定啟動指令（見專案根目錄）
"""
import logging
import os
import jieba

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.database import init_db
from app.routers import pipeline_router, quiz_router
from app.database import SessionLocal, DATABASE_URL

from sqlalchemy import text

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="WhoDunChat")

app.include_router(pipeline_router.router)
app.include_router(quiz_router.router)

jieba.dt.cache_file = os.path.join(os.path.dirname(__file__), "jieba.cache")
jieba.initialize()

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.get("/api/db-status")
def db_status():
    try:
        db = SessionLocal()
        from app.models.models import Channel, Author, ChatMessage
        
        channels = db.query(Channel).all()
        author_count = db.query(Author).count()
        author_with_name = db.query(Author).filter(Author.display_name != None).count()
        msg_count = db.query(ChatMessage).count()
        db.close()
        return {
            "database_type": "sqlite" if "sqlite" in DATABASE_URL else "postgresql",
            "channels": [{"id": c.id, "name": c.channel_name} for c in channels],
            "author_count": author_count,
            "author_with_display_name": author_with_name,   # ★ 這個是關鍵
            "author_without_display_name": author_count - author_with_name,
            "message_count": msg_count,
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# 掛載靜態資源（CSS/JS），路徑用 /static 前綴避免跟 API 路由衝突
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
