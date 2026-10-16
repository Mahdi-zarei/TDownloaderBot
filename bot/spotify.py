"""Spotify track metadata from the public track page (no API key)."""

from __future__ import annotations

import asyncio
import logging
import re
from html.parser import HTMLParser
from pathlib import Path

import aiohttp

from .models import DownloadError, Reason, TrackMeta

log = logging.getLogger(__name__)

# Spotify serves full Open Graph / music:* tags to link-preview bots; a browser user agent
# gets a tiny "open in app" page without them.
USER_AGENT = "TelegramBot (like TwitterBot)"
TIMEOUT = aiohttp.ClientTimeout(total=30)
TRACK_ID = re.compile(r"/track/([0-9A-Za-z]{22})")
COVER_MAX_BYTES = 10 * 1024 * 1024


class _MetaTags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        a = dict(attrs)
        key, content = a.get("property") or a.get("name"), a.get("content")
        if key and content is not None:
            self.tags.setdefault(key, []).append(content.strip())


def parse_track_page(html: str, url: str) -> TrackMeta:
    p = _MetaTags()
    p.feed(html)

    def first(key: str) -> str:
        return (p.tags.get(key) or [""])[0]

    kind = first("og:type")
    if kind in ("music.album", "music.playlist", "music.musician", "profile"):
        raise DownloadError(Reason.PLAYLIST, f"{url}: og:type {kind}")
    title = first("og:title")
    if kind != "music.song" or not title:
        raise DownloadError(Reason.UNSUPPORTED, f"{url}: not a Spotify track page (og:type {kind or 'missing'})")

    artist = ", ".join(dict.fromkeys(a for a in p.tags.get("music:musician_description", []) if a))
    # og:description reads "Artists · Album · Song · Year".
    parts = [s.strip() for s in first("og:description").split(" · ")]
    album = ""
    if len(parts) >= 4 and parts[-2].casefold() == "song":
        album = " · ".join(parts[1:-2]) if parts[0] == artist or not artist else parts[-3]
        artist = artist or parts[0]
    release = first("music:release_date")
    year = release[:4] if re.match(r"\d{4}", release) else (parts[-1] if parts and re.fullmatch(r"\d{4}", parts[-1]) else "")
    duration = float(d) if (d := first("music:duration")).isdigit() else None
    canonical = first("og:url") or url
    m = TRACK_ID.search(canonical) or TRACK_ID.search(url)
    return TrackMeta(
        id=m[1] if m else "",
        url=canonical,
        title=title,
        artist=artist,
        album=album,
        year=year,
        duration=duration,
        cover_url=first("og:image") or None,
    )


async def fetch_track(url: str) -> TrackMeta:
    """Reads a track page; follows short links (spotify.link) to it."""
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT) as s:
            async with s.get(url, allow_redirects=True) as r:
                if r.status == 404:
                    raise DownloadError(Reason.UNAVAILABLE, f"{url}: HTTP 404")
                if r.status in (403, 429):
                    raise DownloadError(Reason.BLOCKED, f"{url}: HTTP {r.status}")
                if r.status >= 400:
                    raise DownloadError(Reason.FAILED, f"{url}: HTTP {r.status}")
                html, final = await r.text(errors="replace"), str(r.url)
    except (aiohttp.ClientError, TimeoutError) as e:
        raise DownloadError(Reason.FAILED, f"{url}: {type(e).__name__}: {e}") from e
    return parse_track_page(html, final)


async def fetch_cover(url: str, dest: Path) -> Path | None:
    """Downloads the cover art; None on any failure (the video thumbnail is used instead)."""
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT) as s:
            async with s.get(url) as r:
                r.raise_for_status()
                data = await r.content.read(COVER_MAX_BYTES + 1)
        if not data or len(data) > COVER_MAX_BYTES:
            return None
        await asyncio.to_thread(dest.write_bytes, data)
        return dest
    except (aiohttp.ClientError, TimeoutError, OSError) as e:
        log.warning("cover download failed: %s", e)
        return None
