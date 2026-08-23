"""
簡易 in-process 背景任務管理器。

設計說明：
- 這不是 Celery/Redis 等級的正式 job queue，而是用 Python threading 做的輕量版本，
  適合這個專案的規模（單一使用者、單機部署）。
- 用一個全域 dict 存 job 狀態，前端輪詢 GET /api/jobs/{job_id} 取得進度。
- Render 的 free/starter web service 是單一 process、可能會因為閒置被喚醒/重啟，
  長時間 job 建議搭配「背景執行緒 + 定期 log 進度」而不是依賴外部 queue，
  這也是為什麼先用這個簡化版本；如果之後頻道規模變大導致 job 常跑超過
  Render 的 request timeout，才需要升級成真正的 worker（見 README 備註）。

流程階段（stage）：
  resolve_channel -> list_streams -> fetch_chat -> filter -> resolve_names -> analyze -> similarity -> done
任何階段出錯會把 job 狀態設為 "error" 並記錄 error 訊息，但已完成的階段資料仍保留在資料庫中，
可以之後重跑（服務本身是 idempotent 的：去重與 upsert 保證重跑不會產生髒資料）。
"""
from __future__ import annotations

import threading
import traceback
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def create_job(job_type: str, meta: Optional[dict] = None) -> str:
    job_id = str(uuid.uuid4())
    with _LOCK:
        _JOBS[job_id] = {
            "job_id": job_id,
            "job_type": job_type,
            "status": "pending",  # pending / running / done / error
            "stage": None,
            "progress": {},
            "meta": meta or {},
            "error": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    return job_id


def update_job(job_id: str, **kwargs) -> None:
    with _LOCK:
        if job_id not in _JOBS:
            return
        _JOBS[job_id].update(kwargs)
        _JOBS[job_id]["updated_at"] = datetime.now(timezone.utc).isoformat()


def get_job(job_id: str) -> Optional[dict]:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def run_job_in_background(job_id: str, target: Callable[[], None]) -> None:
    def wrapper():
        update_job(job_id, status="running")
        try:
            target()
            update_job(job_id, status="done", stage="done")
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            update_job(job_id, status="error", error=f"{exc}\n{tb}")

    thread = threading.Thread(target=wrapper, daemon=True)
    thread.start()
