"""Spotify track metadata and playlist / album track lists from the public pages (no API key)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .models import DownloadError, ListItem, Reason, TrackList, TrackMeta

log = logging.getLogger(__name__)

# Spotify serves full Open Graph / music:* tags to link-preview bots; a browser user agent
# gets a tiny "open in app" page without them.
USER_AGENT = "TelegramBot (like TwitterBot)"
TIMEOUT = aiohttp.ClientTimeout(total=30)
COVER_MAX_BYTES = 10 * 1024 * 1024
# The embed player lists at most this many tracks of a playlist or album.
EMBED_MAX = 100
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_TRACK_URI = re.compile(r"spotify:track:([0-9A-Za-z]{22})")


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


def _meta_tags(html: str) -> dict[str, list[str]]:
    p = _MetaTags()
    p.feed(html)
    return p.tags


def parse_track_page(html: str, url: str) -> TrackMeta:
    tags = _meta_tags(html)

    def first(key: str) -> str:
        return (tags.get(key) or [""])[0]

    kind = first("og:type")
    if kind in ("music.album", "music.playlist"):
        # A short link that opens a list rather than a track.
        noun = "album" if kind == "music.album" else "playlist"
        raise DownloadError(
            Reason.PLAYLIST, f"{url}: og:type {kind}",
            user_text=f"This link opens a Spotify {noun}. Send its full link to pick tracks: {first('og:url') or url}",
        )
    if kind in ("music.musician", "profile"):
        raise DownloadError(Reason.PLAYLIST, f"{url}: og:type {kind}")
    title = first("og:title")
    if kind != "music.song" or not title:
        raise DownloadError(Reason.UNSUPPORTED, f"{url}: not a Spotify track page (og:type {kind or 'missing'})")

    artist = ", ".join(dict.fromkeys(a for a in tags.get("music:musician_description", []) if a))
    # og:description reads "Artists · Album · Song · Year".
    parts = [s.strip() for s in first("og:description").split(" · ")]
    album = ""
    if len(parts) >= 4 and parts[-2].casefold() == "song":
        album = " · ".join(parts[1:-2]) if parts[0] == artist or not artist else parts[-3]
        artist = artist or parts[0]
    release = first("music:release_date")
    year = release[:4] if re.match(r"\d{4}", release) else (parts[-1] if parts and re.fullmatch(r"\d{4}", parts[-1]) else "")
    duration = float(d) if (d := first("music:duration")).isdigit() else None
    return TrackMeta(
        url=first("og:url") or url,
        title=title,
        artist=artist,
        album=album,
        year=year,
        duration=duration,
        cover_url=first("og:image") or None,
    )


def parse_embed(html: str, url: str, kind: str) -> TrackList:
    """Reads the track list the embed player page carries as JSON."""
    m = _NEXT_DATA.search(html)
    try:
        props = json.loads(m[1])["props"]["pageProps"] if m else {}
    except (ValueError, KeyError, TypeError) as e:
        raise DownloadError(Reason.FAILED, f"{url}: unreadable embed data ({type(e).__name__})") from e
    if props.get("status") == 404:
        raise DownloadError(Reason.PRIVATE, f"{url}: embed 404", user_text=f"This {kind} is private or no longer exists.")
    entity = ((props.get("state") or {}).get("data") or {}).get("entity")
    if not isinstance(entity, dict) or not isinstance(entity.get("trackList"), list):
        raise DownloadError(Reason.FAILED, f"{url}: no track list in the embed page (status {props.get('status')})")

    entries = entity["trackList"]
    items = []
    for t in entries:
        # Podcast episodes and local files have no track id.
        if not isinstance(t, dict) or not (track := _TRACK_URI.fullmatch(t.get("uri") or "")) or not t.get("title"):
            continue
        # The subtitle joins artists with ",\xa0".
        artist = ", ".join(a.strip() for a in (t.get("subtitle") or "").split(",") if a.strip())
        duration = t["duration"] / 1000 if isinstance(t.get("duration"), int) and t["duration"] > 0 else None
        items.append(ListItem(f"https://open.spotify.com/track/{track[1]}", t["title"].strip(), artist, duration))
    if not items:
        raise DownloadError(
            Reason.NO_MEDIA, f"{url}: {len(entries)} entries, no tracks", user_text=f"This {kind} has no tracks."
        )
    return TrackList(
        url=url,
        kind=kind,
        title=(entity.get("name") or entity.get("title") or "").strip() or kind.capitalize(),
        owner=(entity.get("subtitle") or "").replace("\xa0", " ").strip(),
        items=items,
        total=len(entries) if len(entries) >= EMBED_MAX else len(items),
    )


async def _get(url: str) -> tuple[str, str]:
    """The page's text and final URL after redirects."""
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT) as s:
            async with s.get(url, allow_redirects=True) as r:
                if r.status == 404:
                    raise DownloadError(Reason.UNAVAILABLE, f"{url}: HTTP 404")
                if r.status in (403, 429):
                    raise DownloadError(Reason.BLOCKED, f"{url}: HTTP {r.status}")
                if r.status >= 400:
                    raise DownloadError(Reason.FAILED, f"{url}: HTTP {r.status}")
                return await r.text(errors="replace"), str(r.url)
    except (aiohttp.ClientError, TimeoutError) as e:
        raise DownloadError(Reason.FAILED, f"{url}: {type(e).__name__}: {e}") from e


async def fetch_track(url: str) -> TrackMeta:
    """Reads a track page; follows short links (spotify.link) to it."""
    html, final = await _get(url)
    return parse_track_page(html, final)


async def fetch_list(url: str) -> TrackList:
    """Reads a playlist or album (an open.spotify.com/{playlist|album}/{id} link) through its embed page."""
    kind, list_id = urlsplit(url).path.rstrip("/").split("/")[-2:]
    html, _ = await _get(f"https://open.spotify.com/embed/{kind}/{list_id}")
    tracks = parse_embed(html, url, kind)
    if tracks.total >= EMBED_MAX:
        # The embed stops at EMBED_MAX; the regular page knows the real size.
        try:
            page, _ = await _get(url)
            count = (_meta_tags(page).get("music:song_count") or [""])[0]
            tracks.total = max(tracks.total, int(count)) if count.isdigit() else tracks.total
        except DownloadError as e:
            log.warning("could not read the %s size: %s", kind, e.reason)
    return tracks


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
