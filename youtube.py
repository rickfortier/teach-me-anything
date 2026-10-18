"""YouTube helpers: Data API search and stats, plus quota-free oEmbed checks.
Only official APIs: no scraping of transcripts or pages (YouTube's Terms forbid it)."""
from __future__ import annotations

import html
import os
import re
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
ISO_DURATION = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


class YouTubeError(Exception):
    friendly = True      # message is safe and readable as-is


class QuotaExceeded(YouTubeError):
    def __init__(self):
        super().__init__("YouTube's daily search quota is used up. It resets at midnight Pacific time; "
                         "use \"Retry failed parts\" after that to add the videos.")


class JudgeUnavailable(YouTubeError):
    def __init__(self, reason: str):
        super().__init__("The videos found couldn't be checked against the lesson right now "
                         f"({reason}). Use \"Retry failed parts\" later to add them.")


def _is_quota_error(exc) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    text = str(exc).lower()
    return status in (403, 429) and ("quota" in text or "ratelimit" in text)


def client():
    from course_builder import ensure_env
    load_dotenv(ensure_env())
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise YouTubeError("YOUTUBE_API_KEY missing (set it in .env)")
    from googleapiclient.discovery import build
    return build("youtube", "v3", developerKey=key, cache_discovery=False)


VIDEO_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|embed/|live/|v/)|youtu\.be/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")


def video_id(url: str) -> str | None:
    """The 11-character id from a YouTube watch/embed/youtu.be link (Shorts are not accepted)."""
    m = VIDEO_ID.search(url or "")
    return m.group(1) if m else None


def oembed(vid: str) -> dict | None:
    """Title, channel and thumbnail for a public, embeddable video; None if the
    video doesn't exist, is private, or can't be embedded. Uses no API quota."""
    import requests
    resp = requests.get("https://www.youtube.com/oembed", timeout=20, params={
        "url": f"https://www.youtube.com/watch?v={vid}", "format": "json"})
    if resp.status_code in (400, 401, 403, 404):
        return None
    resp.raise_for_status()
    data = resp.json()
    return {"id": vid, "title": data.get("title", ""), "channel": data.get("author_name", ""),
            "thumbnail": data.get("thumbnail_url", ""), "url": f"https://www.youtube.com/watch?v={vid}"}


def format_seconds(seconds: int) -> str:
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_duration(iso: str) -> str:
    m = ISO_DURATION.fullmatch(iso)
    if not m:
        return iso
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    if h:
        return f"{h}:{mi:02d}:{s:02d}"
    return f"{mi}:{s:02d}"


def duration_to_seconds(iso: str) -> int:
    m = ISO_DURATION.fullmatch(iso)
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def format_views(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def search(yt, query: str, max_results: int = 8) -> list[dict]:
    """Embeddable English videos for `query`. Costs 100 quota units."""
    try:
        resp = yt.search().list(
            q=query, part="snippet", type="video", maxResults=max_results,
            videoEmbeddable="true", relevanceLanguage="en", safeSearch="moderate",
        ).execute()
    except Exception as exc:
        if _is_quota_error(exc):
            raise QuotaExceeded() from None
        raise
    videos = []
    for item in resp.get("items", []):
        vid = item.get("id", {}).get("videoId")
        if not vid:
            continue
        sn = item.get("snippet", {})
        # search() returns HTML-escaped snippet text ("&amp;", "&#39;").
        videos.append({
            "id": vid,
            "title": html.unescape(sn.get("title", "")),
            "channel": html.unescape(sn.get("channelTitle", "")),
            "description": html.unescape(sn.get("description", "")),
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    return videos


def enrich_with_stats(yt, videos: list[dict]) -> None:
    """Add duration, views, thumbnail, embeddable, captions and licence in place
    (1 quota unit per 50 videos, from a pool separate from search)."""
    by_id = {v["id"]: v for v in videos}
    ids = list(by_id)
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        try:
            resp = yt.videos().list(id=",".join(batch), part="contentDetails,statistics,snippet,status").execute()
        except Exception as exc:
            if _is_quota_error(exc):
                raise QuotaExceeded() from None
            raise
        for item in resp.get("items", []):
            v = by_id.get(item["id"])
            if v is None:
                continue
            iso = item["contentDetails"]["duration"]
            v["duration"] = parse_duration(iso)
            v["duration_seconds"] = duration_to_seconds(iso)
            view_count = int(item.get("statistics", {}).get("viewCount", 0))
            v["views_raw"] = view_count
            v["views"] = format_views(view_count)
            v["thumbnail"] = item["snippet"]["thumbnails"].get("medium", {}).get("url", "")
            if item["snippet"].get("description"):        # the full text, with any chapter list (search cuts it short)
                v["description"] = html.unescape(item["snippet"]["description"])
            status = item.get("status") or {}
            v["embeddable"] = status.get("embeddable", True) and status.get("privacyStatus", "public") == "public"
            v["license"] = status.get("license", "youtube")
            v["captions"] = item["contentDetails"].get("caption") == "true"
            v.setdefault("title", html.unescape(item["snippet"].get("title", "")))
            v.setdefault("channel", html.unescape(item["snippet"].get("channelTitle", "")))
