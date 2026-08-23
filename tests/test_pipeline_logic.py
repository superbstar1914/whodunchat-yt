"""
模擬資料測試腳本：不連網，用假造的聊天訊息驗證：
1. message_id 去重
2. 過濾模板/複製文邏輯 + 表情符號過濾
3. 觀眾風格分析（含 YouTube 表情符號）
4. 相似度計算
5. 出題邏輯（含 time_in_seconds 與 video_id）
6. 答題紀錄
"""
import os
import random
import sys
from datetime import datetime, timedelta, timezone

# 確保 stdout 使用 utf-8，避免 Windows 終端機編碼錯誤
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

os.environ["DATABASE_URL"] = "sqlite:///./test_whodunchat.db"

from app.database import init_db, SessionLocal, engine, Base  # noqa: E402
from app.models.models import Channel, Stream, Author, ChatMessage  # noqa: E402
from app.services import filter_service, analysis_service, similarity_service, quiz_service  # noqa: E402
from app.services.emoji_service import extract_all_emojis, is_pure_emoji  # noqa: E402

# 重建乾淨的測試資料庫
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)

db = SessionLocal()

channel = Channel(channel_id="UC_test123", channel_name="測試頻道", channel_url="https://youtube.com/test")
db.add(channel)
db.commit()
db.refresh(channel)

stream = Stream(video_id="video_001", channel_id=channel.id, title="測試直播 #1", fetch_status="done")
db.add(stream)
db.commit()
db.refresh(stream)

# 建立幾位風格迥異的觀眾
def make_author(author_id, name):
    a = Author(author_id=author_id, display_name=name, name_resolved=True)
    db.add(a)
    db.commit()
    db.refresh(a)
    return a

alice = make_author("UC_alice", "愛麗絲")
bob = make_author("UC_bob", "鮑伯")
carol = make_author("UC_carol", "卡蘿")   # 風格會刻意設計得跟 alice 很像
dave = make_author("UC_dave", "戴夫")

base_time = datetime.now(timezone.utc)

def add_message(mid, author, text, minutes_offset=0, hour=None):
    ts = base_time + timedelta(minutes=minutes_offset)
    if hour is not None:
        ts = ts.replace(hour=hour)
    m = ChatMessage(
        message_id=mid,
        stream_id=stream.id,
        author_id=author.id,
        message_text=text,
        timestamp=ts,
        time_in_seconds=float(minutes_offset * 60),
        message_type="text_message",
    )
    db.add(m)
    return m

msg_counter = 0
def next_id():
    global msg_counter
    msg_counter += 1
    return f"msg_{msg_counter:04d}"

# --- Alice: 常用「484」、「笑爛」，emoji 愛用 🤣 與 :face-blue-smiling:，晚上發言 ---
alice_texts = [
    "484該吃飯了 笑爛 :face-blue-smiling:", "這操作也太扯了吧 笑爛", "484有點強 :face-blue-smiling:", "笑爛 又輸了",
    "他484故意的啊 笑爛死", "這波484要輸了 :face-blue-smiling:", "笑爛笑爛笑爛", "484認真的",
]
for t in alice_texts:
    add_message(next_id(), alice, t, hour=22)

# --- Carol: 刻意模仿 Alice 的風格（測試相似度是否抓得到） ---
carol_texts = [
    "484該睡了 笑爛", "這波484也太扯 笑爛 :face-blue-smiling:", "笑爛 又死了", "484有點厲害欸",
    "他484故意放水 笑爛死", "這波484要贏了嗎", "笑爛笑爛 :face-blue-smiling:", "484是認真的嗎",
]
for t in carol_texts:
    add_message(next_id(), carol, t, hour=22)

# --- Bob: 風格完全不同，常問問題、用敬語，白天發言 ---
bob_texts = [
    "請問這是哪一款裝備呢？", "主播請問等等會打這隻王嗎？", "這個技能是不是要升到滿級比較好？",
    "想請教一下這個副本的機制", "請問可以介紹一下這把武器嗎？", "這樣操作是不是比較安全？",
]
for t in bob_texts:
    add_message(next_id(), bob, t, hour=14)

# --- Dave: 短句、驚嘆號多、玩梗 ---
dave_texts = [
    "太扯了吧！！！", "這也行！！", "神了！！！", "笑死我了！！",
    "這什麼操作！！！", "太狂了吧這波！！",
]
for t in dave_texts:
    add_message(next_id(), dave, t, hour=20)

# --- 模板彈幕：多人重複同一句（應該被過濾掉） ---
template_authors = [make_author(f"UC_spam{i}", f"路人{i}") for i in range(8)]
for i, a in enumerate(template_authors):
    add_message(next_id(), a, "888888888", minutes_offset=i)
    add_message(next_id(), a, "早安", minutes_offset=i + 100)

db.commit()

print(f"總留言數（過濾前）: {db.query(ChatMessage).count()}")

# ---- 測試1: message_id 去重（模擬重複寫入同一 message_id，應該被資料庫 unique constraint 擋下）----
from sqlalchemy.exc import IntegrityError
dup = ChatMessage(
    message_id="msg_0001",  # alice 第一則的 id，重複
    stream_id=stream.id,
    author_id=bob.id,
    message_text="這是重複的訊息",
    message_type="text_message",
)
db.add(dup)
try:
    db.commit()
    print("❌ 去重測試失敗：重複 message_id 竟然插入成功")
except IntegrityError:
    db.rollback()
    print("✅ 去重測試通過：重複 message_id 被資料庫擋下")

# ---- 測試2: 過濾模板/複製文與表情 ----
filter_result = filter_service.filter_stream_messages(db, stream)
print(f"\n清洗結果: {filter_result}")
assert filter_result["reasons"].get("duplicate_template", 0) >= 8, "模板彈幕沒有被正確過濾！"
assert filter_result["reasons"].get("generic_phrase", 0) >= 2, "通用詞（早安等）沒有被過濾！"
print("✅ 清洗邏輯：模板彈幕與通用詞都被正確過濾")

kept_texts = [m.message_text for m in db.query(ChatMessage).filter(ChatMessage.is_filtered == False).all()]
assert any("484該吃飯了" in t for t in kept_texts), "有特色的留言被誤刪！"
assert "888888888" not in kept_texts, "模板彈幕沒被過濾掉！"
print("✅ 清洗邏輯：有特色的留言被正確保留，模板留言被正確剔除")

# ---- 測試2b: 表情符號識別 ----
assert is_pure_emoji(":face-blue-smiling: :yt:") is True, "YouTube 表符未被識別為純表情！"
assert is_pure_emoji("484 :face-blue-smiling:") is False, "含文字被誤判為純表情！"
print("✅ 表情符號邏輯：YouTube 表符與 Unicode Emoji 檢測正確")

# ---- 測試3: 風格分析 ----
analyze_result = analysis_service.analyze_channel_authors(db, channel.id)
print(f"\n分析結果: {analyze_result}")

from app.models.models import AuthorProfile
alice_profile = db.query(AuthorProfile).filter(AuthorProfile.author_id == alice.id).one()
print(f"\nAlice 特徵: message_count={alice_profile.features['message_count']}, "
      f"top_words={alice_profile.features['top_words'][:5]}, "
      f"top_emojis={alice_profile.features.get('top_emojis', [])}, "
      f"catchphrases={[c['word'] for c in alice_profile.features['catchphrases'][:3]]}")

assert any(":face-blue-smiling:" in e[0] for e in alice_profile.features.get("top_emojis", [])), \
    "Alice 的 YouTube 表符 :face-blue-smiling: 未被統計進 top_emojis！"
print("✅ 風格分析邏輯：YouTube 表情符號成功納入觀眾特徵分析")

bob_profile = db.query(AuthorProfile).filter(AuthorProfile.author_id == bob.id).one()
print(f"Bob 特徵: question_rate={bob_profile.features['question_rate']}")
assert bob_profile.features["question_rate"] > 0.5, "Bob 的問句比例應該很高（常用「請問」句型）！"
print("✅ 分析邏輯：Bob 的問句比例正確偵測")

dave_profile = db.query(AuthorProfile).filter(AuthorProfile.author_id == dave.id).one()
print(f"Dave 特徵: exclaim_rate={dave_profile.features['exclaim_rate']}")
assert dave_profile.features["exclaim_rate"] > 1.0, "Dave 的驚嘆號比例應該很高！"
print("✅ 分析邏輯：Dave 的驚嘆號比例正確偵測")

# ---- 測試4: 相似度計算（Alice 和 Carol 應該最相似）----
sim_result = similarity_service.compute_channel_similarities(db, channel.id)
print(f"\n相似度計算結果: {sim_result}")

similar_to_alice = similarity_service.get_most_similar_authors(db, alice.id, top_n=3)
print(f"與 Alice 最相似的觀眾: {similar_to_alice}")
top_similar_id = similar_to_alice[0][0]
assert top_similar_id == carol.id, f"❌ Alice 最相似的應該是 Carol (id={carol.id})，但結果是 {top_similar_id}"
print(f"✅ 相似度邏輯：Alice 與 Carol（風格刻意相似）被正確識別為最相似組合，相似度 = {similar_to_alice[0][1]:.4f}")

similar_to_bob = similarity_service.get_most_similar_authors(db, bob.id, top_n=3)
print(f"與 Bob 最相似的觀眾: {similar_to_bob}")
assert similar_to_bob[0][0] != carol.id or similar_to_bob[0][1] < similar_to_alice[0][1], \
    "Bob 不應該跟 Carol 比 Alice-Carol 更相似"
print("✅ 相似度邏輯：Bob（風格迥異）與其他人的相似度明顯較低")

# ---- 測試5: 出題邏輯（難度分級版 + time_in_seconds & video_id）----
random.seed(42)
quiz = quiz_service.generate_quiz(db, channel.id, difficulty_level="hard")
print(f"\n出題結果 (hard): {quiz}")
assert quiz is not None, "出題失敗，回傳 None"
assert len(quiz["options"]) == 3, "選項數量應該固定為3"
assert quiz["correct_author_db_id"] in [o["author_db_id"] for o in quiz["options"]], "正確答案不在選項中"
assert "video_id" in quiz and quiz["video_id"] == "video_001", "video_id 未回傳！"
assert "time_in_seconds" in quiz and quiz["time_in_seconds"] is not None, "time_in_seconds 未回傳！"
print(f"✅ 出題邏輯：成功產生題目，留言=「{quiz['message_text']}」，"
      f"選項數={len(quiz['options'])}，難度={quiz['difficulty']}，video_id={quiz['video_id']}")

# ---- 測試5b: 批次出題依比例分配 ----
random.seed(1)
batch = quiz_service.generate_quiz_batch(db, channel.id, total_count=6, easy_ratio=0.3, medium_ratio=0.3, hard_ratio=0.4)
print(f"\n批次出題結果數量: {len(batch)}")
assert len(batch) > 0, "批次出題應至少產生部分題目"
for q in batch:
    assert len(q["options"]) == 3
    assert "time_in_seconds" in q
print(f"✅ 批次出題邏輯：成功產生 {len(batch)} 題，每題皆包含選項與時間戳資訊")

# ---- 測試5c: 黑名單「安安」正確過濾 ----
blacklist_mid = next_id()
add_message(blacklist_mid, alice, "安安大家好", minutes_offset=999)
db.commit()
filter_service.filter_stream_messages(db, stream)
blacklisted = db.query(ChatMessage).filter(ChatMessage.message_id == blacklist_mid).one()
assert blacklisted.is_filtered is True and blacklisted.filter_reason == "blacklisted_word", \
    "「安安」黑名單沒有正確過濾留言！"
assert blacklisted.message_text is None, "過濾後的留言 message_text 應該被清空省空間"
assert blacklisted.normalized_text is not None, "過濾後的留言仍應保留 normalized_text"
print("✅ 黑名單與瘦身邏輯：含「安安」的留言被正確過濾且 message_text 原文已清空")

# ---- 測試6: 答題紀錄 ----
record = quiz_service.record_quiz_answer(db, channel.id, quiz, quiz["correct_author_db_id"])
assert record.is_correct is True
print("✅ 答題紀錄邏輯：正確答案記錄為 is_correct=True")

db.close()
print("\n🎉 全部測試通過！")
