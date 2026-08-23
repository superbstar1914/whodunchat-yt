"""
用 yt-dlp 負責兩件事：
1. 取得頻道過往「已結束的直播」影片列表（不含一般影片、不含未開播的預告）
2. 用 author_id (channel id) 反查該觀眾目前的 display name

注意：
- yt-dlp 抓頻道列表用 extract_flat 減少開銷，避免逐支影片都完整解析
- 反查 display name 目前 YouTube 沒有官方「用 channel id 查暱稱」的輕量 API，
  作法是用 yt-dlp 打開該頻道的頻道頁 (https://www.youtube.com/channel/<id>)
  取得 channel 的 uploader/channel name。這對一般人帳號一樣適用，
  因為每個留言者的 author.id 本質上也是一個 YouTube 頻道 id。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import yt_dlp

logger = logging.getLogger(__name__)


def _base_ydl_opts(extra: Optional[dict] = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
        "extract_flat": True,
        "socket_timeout": 20,
    }
    if extra:
        opts.update(extra)
    return opts


def resolve_channel_id(channel_input: str) -> dict:
    """
    接受頻道網址 / @handle / channel id，回傳標準化資訊：
    { "channel_id": "UC...", "channel_name": "...", "channel_url": "..." }
    """
    url = channel_input.strip()
    if not url.startswith("http"):
        if url.startswith("@"):
            url = f"https://www.youtube.com/{url}"
        elif url.startswith("UC"):
            url = f"https://www.youtube.com/channel/{url}"
        else:
            url = f"https://www.youtube.com/@{url}"

    with yt_dlp.YoutubeDL(_base_ydl_opts({"playlist_items": "0"})) as ydl:
        info = ydl.extract_info(url, download=False)

    channel_id = info.get("channel_id") or info.get("id")
    channel_name = info.get("channel") or info.get("uploader") or info.get("title")
    channel_url = info.get("channel_url") or url

    if not channel_id:
        raise ValueError(f"無法解析頻道: {channel_input}")

    return {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "channel_url": channel_url,
    }


def list_past_live_streams(channel_id: str, max_items: Optional[int] = None) -> list[dict]:
    """
    取得該頻道「已結束的直播」列表。
    YouTube 頻道有個 /streams 分頁專門列直播（含進行中與已結束），
    我們用 extract_flat 先拿列表，過濾出已結束的（is_live == False 且有 duration）。

    回傳: [{ "video_id", "title", "published_at": datetime|None, "duration_seconds": int|None }, ...]
    """
    streams_url = f"https://www.youtube.com/channel/{channel_id}/streams"

    opts = _base_ydl_opts()
    if max_items:
        # playlistend 限制只掃前 N 筆，加速
        opts["playlistend"] = max_items

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(streams_url, download=False)

    entries = info.get("entries") or []
    results = []
    for e in entries:
        if not e:
            continue
        video_id = e.get("id")
        if not video_id:
            continue

        # extract_flat 模式下 live_status 可能是: is_live / was_live / is_upcoming / not_live
        live_status = e.get("live_status")
        if live_status == "is_upcoming":
            continue  # 還沒開播，跳過
        if live_status == "is_live":
            continue  # 正在直播中，跳過（重播還沒生成完整）

        duration = e.get("duration")
        timestamp = e.get("timestamp") or e.get("release_timestamp")
        published_at = datetime.utcfromtimestamp(timestamp) if timestamp else None

        results.append({
            "video_id": video_id,
            "title": e.get("title"),
            "published_at": published_at,
            "duration_seconds": int(duration) if duration else None,
        })

    return results


def resolve_display_name(author_id: str) -> Optional[str]:
    """
    用 author_id (YouTube channel id) 反查目前的 display name。
    抓不到就回傳 None。
    """
    url = f"https://www.youtube.com/channel/{author_id}"
    opts = _base_ydl_opts({"playlist_items": "0"})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None
        name = info.get("channel") or info.get("uploader") or info.get("channel_name") or info.get("title")
        if name:
            # 清除 YouTube 頻道標題常帶的尾綴（如 "- Videos", "- 影片" 等）
            for suffix in (" - Videos", " - 影片", " - Streams", " - 直播", " - Home", " - 首頁"):
                if name.endswith(suffix):
                    name = name[:-len(suffix)]
            name = name.strip()
            if name and not name.startswith("UC") and name != "YouTube":
                return name
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("resolve_display_name failed for %s: %s", author_id, exc)
        return None
