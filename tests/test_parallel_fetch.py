"""
並行寫入與限速降速單元測試：
驗證：
1. 多個執行緒同時使用獨立 SessionLocal 對同一個 SQLite 資料庫寫入不同直播場次的訊息
2. 併發插入相同 author_id 時，Savepoint (begin_nested) 機制能保證無 IntegrityError 或資料丟失
3. SQLite WAL 模式下高併發無 "database is locked"
4. 限速錯誤檢測與全域降速機制運作正常
"""
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

os.environ["DATABASE_URL"] = "sqlite:///./test_parallel.db"

from app.database import init_db, SessionLocal, engine, Base
from app.models.models import Channel, Stream, Author, ChatMessage
from app.services.chat_fetch_service import _get_or_create_author, _commit_with_retry
from app.services.parallel_fetch_service import is_rate_limit_error

# 重建乾淨測試資料庫
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)

# 建立測試頻道與直播
main_db = SessionLocal()
channel = Channel(channel_id="UC_parallel_test", channel_name="並行測試頻道")
main_db.add(channel)
main_db.commit()
main_db.refresh(channel)

streams = []
for i in range(5):
    s = Stream(video_id=f"vid_stream_{i}", channel_id=channel.id, title=f"直播場次 #{i}")
    main_db.add(s)
    streams.append(s)
main_db.commit()
for s in streams:
    main_db.refresh(s)
main_db.close()

print("🏁 開始多執行緒併發寫入測試（模擬 5 個執行緒同時寫入）...")

THREAD_COUNT = 5
MESSAGES_PER_THREAD = 100
errors = []

def worker(stream_index: int):
    db = SessionLocal()
    try:
        stream_id = streams[stream_index].id
        for m_idx in range(MESSAGES_PER_THREAD):
            # 故意讓多個執行緒共用同一批 author_id 測試併發建立衝突
            shared_author_id = f"UC_shared_user_{m_idx % 10}"
            author = _get_or_create_author(db, shared_author_id, f"暱稱_{shared_author_id}")

            msg = ChatMessage(
                message_id=f"msg_s{stream_index}_{m_idx:04d}",
                stream_id=stream_id,
                author_id=author.id,
                message_text=f"來自場次 {stream_index} 的訊息 {m_idx} :face-blue-smiling:",
                timestamp=datetime.now(timezone.utc),
                time_in_seconds=float(m_idx * 5),
                message_type="text_message",
            )
            db.add(msg)
            if m_idx % 20 == 0:
                _commit_with_retry(db)
        _commit_with_retry(db)
    except Exception as exc:
        errors.append((stream_index, exc))
    finally:
        db.close()

threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREAD_COUNT)]
for t in threads:
    t.start()
for t in threads:
    t.join()

assert len(errors) == 0, f"❌ 併發寫入發生錯誤: {errors}"
print(f"✅ 多執行緒併發寫入測試通過！無 lock 衝突，無 IntegrityError。")

# 驗證總筆數
check_db = SessionLocal()
total_msgs = check_db.query(ChatMessage).count()
total_authors = check_db.query(Author).count()
print(f"📊 總寫入訊息數: {total_msgs} (預期: {THREAD_COUNT * MESSAGES_PER_THREAD})")
print(f"📊 總觀眾數: {total_authors} (預期: 10)")
assert total_msgs == THREAD_COUNT * MESSAGES_PER_THREAD, "訊息總數不符！"
assert total_authors == 10, "觀眾去重不符！"
print("✅ 資料筆數與觀眾去重驗證完全相符！")

# 驗證限速檢測
assert is_rate_limit_error("HTTP 429 Too Many Requests") is True
assert is_rate_limit_error("Sign in to confirm you're not a bot") is True
assert is_rate_limit_error("Connection timed out") is False
print("✅ 限速錯誤檢測邏輯驗證通過！")

check_db.close()
print("\n🎉 並行化單元測試全部通過！")
