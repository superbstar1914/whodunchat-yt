"""
WhoDunChat 主應用程式入口。

本地啟動：uvicorn app.main:app --reload --port 8000
Render 上：由 render.yaml / Procfile 指定啟動指令（見專案根目錄）
"""
import logging
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.database import init_db
from app.routers import pipeline_router, quiz_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="WhoDunChat")

app.include_router(pipeline_router.router)
app.include_router(quiz_router.router)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# 掛載靜態資源（CSS/JS），路徑用 /static 前綴避免跟 API 路由衝突
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
