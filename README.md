# WhoDunChat (v5)

用觀眾過去在直播聊天室的留言，考考主播「這句話是誰講的？」的猜謎遊戲。

---

## 🌟 v5 重點功能與架構升級

1. **⚡ 多執行緒並行抓取（ThreadPoolExecutor + SQLite WAL）**：
   - `chat_fetch_service.py` 支援多場直播並行抓取（預設 3 條，可在進階選項自訂 1~10 條），大幅縮短抓取時間。
   - 每個執行緒獨立使用 `SessionLocal()`，搭配 SQLite WAL 模式（`PRAGMA journal_mode=WAL`）與 Savepoint 機制，防止多執行緒寫入鎖定與衝突。
   - **防限速全域降速保護閥**：任一執行緒遇到 HTTP 429 或驗證限速時，自動觸發全域降速，將後續佇列切換為序列模式並加大冷卻時間。
2. **🎨 YouTube 官方表情符號與自訂 Emote 支援**：
   - 內建 101 種 YouTube 官方表符對照表（`youtubeemoji.json`），前端自動渲染為 YouTube 官方圖片。
   - 頻道自訂會員表符（`:custom:`）作為獨立字串以膠囊徽章渲染。
   - 為 jieba 註冊表符字典，分析時精確統計觀眾最愛用的表情符號（`top_emojis`）與口頭禪。
3. **⏱️ 結果頁面時間戳跳轉（YouTube Replay Linking）**：
   - 結果頁面新增「題目詳細回顧清單」，顯示每題留言、正解觀眾、玩家選擇與正誤標籤。
   - 附帶 YouTube 直播時間戳連結（如 `▶ 01:23:45 直播回放`），點擊可直接在新分頁跳轉至該留言出現時的直播影片時間點！
4. **💰 Super Chat 超級留言專屬「酷 炫 特 效」**：
   - 題目若來自超級留言，作答卡片上方會呈現動態炫彩流光 Super Chat 橫幅（`💰 超級留言 NT$ ...`）與金光氛圍呼吸燈！
   - 作答結果回顧清單中標記炫彩發光 SC 徽章與專屬粒子動效。
5. **💾 極致資料庫瘦身（Data Slimming）**：
   - **過濾留言清空原文**：被過濾的留言（`is_filtered=True`）原文清空（設為 `None`），只保留 `normalized_text` 做比對，大幅減少 DB 空間佔用。
   - **相似度以 `uint8 (0~255)` 儲存**：`AuthorSimilarity.similarity` 改為 `SmallInteger` 儲存，且每位觀眾只保留前 60 名最相似對象，避免 $O(n^2)$ 矩陣膨脹。
   - **`features` 不冗餘存 `ngram_text`**：相似度計算時即時從 DB 撈取有效留言串接，算完即丟。
   - **移除 `raw_json` 與 `name_history`**。
6. **☁️ 雲端部署最佳化（Miget / Render 全面支援）**：
   - 內建 `Dockerfile` 與 `Procfile`，完美適配 Miget（app.miget.com）免費容器託管與永久免費 PostgreSQL。
   - `database.py` 啟用 `pool_pre_ping=True`，避免免費資料庫閒置睡眠喚醒時斷線。

---

## 專案結構

```
whodunchat/
├── app/
│   ├── main.py                    # FastAPI 入口
│   ├── database.py                # DB 連線設定（SQLite WAL / Postgres pool_pre_ping）
│   ├── models/models.py           # ORM models (v5 瘦身結構)
│   ├── data/
│   │   └── youtubeemoji.json      # 101 種 YouTube 官方表符映射
│   ├── routers/
│   │   ├── pipeline_router.py     # 抓取與表情 API
│   │   └── quiz_router.py         # 出題與答題 API
│   └── services/
│       ├── emoji_service.py       # 表情符號解析與 jieba 註冊
│       ├── parallel_fetch_service.py # 多執行緒並行抓取引擎 + 限速保護閥
│       ├── ytdlp_service.py       # yt-dlp：頻道解析、直播列表、暱稱反查
│       ├── chat_fetch_service.py  # chat-downloader：抓聊天室 + Savepoint 去重
│       ├── filter_service.py      # 清洗：剔除模板/複製文 + 黑名單 + 清空原文
│       ├── name_resolve_service.py# 並行補完觀眾 display name
│       ├── analysis_service.py    # 觀眾風格特徵分析
│       ├── similarity_service.py  # 觀眾相似度計算（uint8 + Top-60 快取）
│       ├── quiz_service.py        # 出題邏輯（難度分級、時間戳、SC金額）
│       ├── job_manager.py         # 背景任務管理
│       └── pipeline_service.py    # 串接以上所有步驟
├── static/                        # 前端 SPA（黑底白字極簡風 + SC 酷炫動效）
├── tests/
│   ├── test_pipeline_logic.py     # 核心業務邏輯模擬測試
│   └── test_parallel_fetch.py     # 多執行緒併發寫入測試
├── Dockerfile                     # Miget / Docker 容器化部署
├── Procfile                       # Migetpacks / Buildpack 啟動設定
├── requirements.txt               # 包含核心依賴與 psycopg[binary]
├── start.bat                      # Windows 一鍵啟動腳本
├── render.yaml                    # Render 免費 Blueprint 設定
└── README.md
```

---

## 本地快速啟動

### Windows 一鍵啟動：
直接雙擊 `start.bat`，會自動檢查環境、建立虛擬環境、安裝依賴並啟動伺服器於 `http://localhost:8000`。

### 手動啟動（Windows / macOS / Linux）：
```bash
# 1. 建立並啟用虛擬環境
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 2. 安裝依賴
pip install -r requirements.txt

# 3. 啟動伺服器
uvicorn app.main:app --reload --port 8000
```
打開瀏覽器訪問 `http://localhost:8000` 即可開始使用。

---

## 雲端免費部署指南

### 方案 A：部署到 Miget（推薦，完全免費且資料庫永不過期）

[Miget](https://app.miget.com) 提供永久免費的 PostgreSQL（1 GiB 空間 / 256 MiB RAM）與應用程式託管服務：

1. **建立免費 PostgreSQL**：
   - 登入 [app.miget.com](https://app.miget.com)。
   - 左側選單進入 **Services**，點擊 **Create Service**。
   - 選擇 **PostgreSQL**，方案選擇 **Miget Free**。
   - 開啟 **Public Access** 並建立。
   - 複製取得的連線字串（`postgresql://...`）。
2. **部署 Web 應用**：
   - 將專案 Push 至您的 GitHub Repo。
   - 在 Miget Dashboard 點擊 **New Application**，選擇 **GitHub** 並綁定此 Repo。
   - Build 方式選擇 **Migetpacks** 或 **Dockerfile** 皆可（專案已內建配置）。
   - 在 Environment Variables 新增：
     - `DATABASE_URL`：貼上第一步取得的 PostgreSQL 連線字串。
   - 點擊 Deploy，完成後即可透過 Miget 提供的網址訪問！

---

### 方案 B：部署到 Render（Render Web + Miget Postgres）

1. 將專案 Push 至 GitHub Repo。
2. 登入 [Render Dashboard](https://dashboard.render.com)，點擊 **New** → **Blueprint**，選擇您的 GitHub Repo（會自動讀取 `render.yaml`）。
3. 部署完成後，到 Web Service 的 **Environment** 分頁手動加入：
   - `DATABASE_URL`：貼上 Miget 給的 PostgreSQL 連線字串。
4. 儲存後 Render 會自動重新部署，並連線至外部免費資料庫。

### 觀眾風格分析（analysis_service.py）
用 jieba 斷詞抽取常用詞、口頭禪（用詞頻率相對全頻道平均的比值），加上 emoji 使用、標點/問句/驚嘆習慣、24小時發言時段分布。

### 相似度計算（similarity_service.py）
混合兩種訊號：
- 70% 權重：TF-IDF (character n-gram) cosine similarity，抓「用字遣詞」層面
- 30% 權重：結構化特徵（時段分布、標點習慣等）的歐式距離轉相似度

已用模擬資料驗證：刻意設計成風格相近的兩位測試觀眾，相似度計算結果為 0.84，遠高於其他組合的 0.1 左右，符合預期。

### 出題邏輯（quiz_service.py）
隨機挑一則有效留言，用相似度快取表找出與正確答案最相似的觀眾當干擾選項，藉此增加辨識難度。若相似觀眾不足會用隨機觀眾補滿選項數。

## 之後可以擴充的方向

- 用 LLM 對「模板/複製文」做更細緻的語意判斷（目前是規則式）
- 出題時依照「最近是否出過同一位觀眾」做輪替，避免重複出現同一人
- 難度分級（簡單題用低相似度組合、困難題用高相似度組合）
- 統計主播的答對率、對每位觀眾的辨識準確度
- 真正的背景 job queue（目前是簡化版 in-process thread，適合單機小規模使用；若之後要處理大頻道/多使用者併發，建議升級成 Celery + Redis）
