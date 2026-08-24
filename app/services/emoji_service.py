"""
YouTube 表情符號與 Unicode Emoji 處理模組。

功能：
1. 載入 YouTube 官方表情符號對照表（youtubeemoji.json）。
2. 識別 YouTube 表符（如 :face-blue-smiling:、:_custom:）與 Unicode Emoji。
3. 將表符作為獨立詞彙註冊給 jieba，避免分詞時被拆碎。
4. 提供表情抽取與純表情檢測工具。
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

import jieba


_EMOJI_JSON_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "youtubeemoji.json"
)

# 載入 YouTube 官方表情字典
YOUTUBE_EMOJI_MAP: dict[str, str] = {}
if os.path.exists(_EMOJI_JSON_PATH):
    try:
        with open(_EMOJI_JSON_PATH, "r", encoding="utf-8") as f:
            YOUTUBE_EMOJI_MAP = json.load(f)
    except Exception:
        YOUTUBE_EMOJI_MAP = {}

# 正則比對：YouTube 表符標籤（例如 :face-blue-smiling:, :yt:, :_UC...: 等）
YT_EMOTE_PATTERN = re.compile(r":[a-zA-Z0-9_\-]+:")

# 正則比對：Unicode Emojis
UNICODE_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)


def init_jieba_emojis() -> None:
    """把 YouTube 官方與常見表符標籤註冊給 jieba，保證分詞時為獨立 Token。"""
    for code in YOUTUBE_EMOJI_MAP.keys():
        jieba.add_word(code)


# 模組載入時自動初始化一次
init_jieba_emojis()


def extract_all_emojis(text: str) -> list[str]:
    """抽取文字中所有的 Unicode Emoji 與 YouTube 表符標籤。"""
    if not text:
        return []
    yt_emotes = YT_EMOTE_PATTERN.findall(text)
    unicode_emojis = UNICODE_EMOJI_PATTERN.findall(text)
    return yt_emotes + unicode_emojis


def is_pure_emoji(text: str) -> bool:
    """檢查字串是否只包含表情符號（Unicode Emoji 或 YouTube 表符）與空白。"""
    if not text or not text.strip():
        return False
    # 先剔除 YouTube 表符
    stripped = YT_EMOTE_PATTERN.sub("", text)
    # 再剔除 Unicode Emoji
    stripped = UNICODE_EMOJI_PATTERN.sub("", stripped).strip()
    return stripped == ""
