"""Finding a metadata-only track's audio: YouTube Music song search first, plain YouTube search as fallback."""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .downloader import Downloader
from .models import DownloadError, Reason, TrackMeta
from .sources import SourceRule

log = logging.getLogger(__name__)

YTM_TIMEOUT = 25
MIN_TITLE = 0.6
MIN_SCORE = 0.6
# Words that mark a different rendition; penalized unless the original title has them too.
_RENDITION = frozenset({
    "live", "cover", "karaoke", "instrumental", "remix", "sped", "slowed", "nightcore", "reverb", "acoustic",
    "reaction", "tutorial", "lesson", "piano", "loop", "extended", "mashup", "8d", "edit", "version",
})
_STOP = frozenset({
    "feat", "ft", "featuring", "with", "prod", "by", "official", "audio", "video", "music", "lyrics", "lyric",
    "hd", "hq", "4k", "remastered", "remaster", "visualizer", "mv", "the", "a", "and", "x",
})


@dataclass(slots=True)
class Match:
    url: str
    title: str
    channel: str
    duration: float | None
    via: str  # "YouTube Music" or "YouTube"
    score: float


@dataclass(slots=True)
class _Candidate:
    video_id: str
    title: str
    channel: str  # uploader / joined artist names
    duration: float | None
    via: str


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())


def _tokens(text: str) -> set[str]:
    return {w for w in _words(text) if w not in _STOP}


def _coverage(wanted: set[str], have: set[str]) -> float:
    return len(wanted & have) / len(wanted) if wanted else 0.7


def score(meta: TrackMeta, c: _Candidate) -> tuple[float, float] | None:
    """(score, title coverage) for a candidate, or None when its duration rules it out."""
    dur = 0.5
    if meta.duration and c.duration:
        tolerance = max(5.0, 0.04 * meta.duration)
        diff = abs(meta.duration - c.duration)
        if diff > 3 * tolerance:
            return None
        dur = 1 - diff / (3 * tolerance)
    title_words = set(_words(meta.title))
    cand_words = set(_words(c.title))
    title = _coverage(_tokens(meta.title), cand_words)
    artist = _coverage(_tokens(meta.artist), cand_words | set(_words(c.channel)))
    penalty = 0.25 * len((_RENDITION & cand_words) - title_words)
    # Words the track doesn't have, e.g. an extra "ft. Justin Bieber" on a remix with the same length.
    extra = _tokens(c.title) - title_words - set(_words(meta.artist)) - _RENDITION
    penalty += min(0.3, 0.05 * len(extra))
    bonus = (0.1 if c.via == "YouTube Music" else 0) + (0.1 if c.channel.endswith(" - Topic") else 0)
    return 0.45 * title + 0.25 * artist + 0.30 * dur + bonus - penalty, title


def pick(meta: TrackMeta, candidates: list[_Candidate]) -> Match | None:
    best: Match | None = None
    for rank, c in enumerate(candidates):
        if (scored := score(meta, c)) is None:
            continue
        total, title = scored
        total -= 0.02 * rank  # search order is a weak signal too
        if title < MIN_TITLE or total < MIN_SCORE or (best and total <= best.score):
            continue
        host = "music.youtube.com" if c.via == "YouTube Music" else "www.youtube.com"
        best = Match(f"https://{host}/watch?v={c.video_id}", c.title, c.channel, c.duration, c.via, total)
    return best


def _ytm_search(query: str, proxy: str | None) -> list[dict[str, Any]]:
    from ytmusicapi import YTMusic  # optional: plain YouTube search still works without it

    proxies = {"http": proxy, "https": proxy} if proxy else None
    return YTMusic(proxies=proxies).search(query, filter="songs", limit=10)


async def _ytm_candidates(meta: TrackMeta, proxy: str | None) -> list[_Candidate]:
    try:
        results = await asyncio.wait_for(asyncio.to_thread(_ytm_search, f"{meta.artist} {meta.title}", proxy), YTM_TIMEOUT)
    except Exception as e:  # ytmusicapi breaks when YouTube Music changes; fall back quietly
        log.warning("YouTube Music search failed for %r: %s", meta.title, e)
        return []
    out = []
    for r in results:
        if r.get("videoId") and r.get("title"):
            artists = ", ".join(a.get("name", "") for a in r.get("artists") or [] if a.get("name"))
            out.append(_Candidate(r["videoId"], r["title"], artists, r.get("duration_seconds"), "YouTube Music"))
    return out


async def _yt_candidates(downloader: Downloader, rule: SourceRule, meta: TrackMeta) -> list[_Candidate]:
    # The " topic" variant surfaces official audio uploads that the plain query ranks below music videos.
    query = f"{meta.artist} - {meta.title}"
    results = await asyncio.gather(downloader.search(rule, query), downloader.search(rule, f"{query} topic"))
    entries = {e["id"]: e for batch in results for e in batch}
    return [
        _Candidate(e["id"], e.get("title") or "", e.get("channel") or e.get("uploader") or "", e.get("duration"), "YouTube")
        for e in entries.values()
    ]


async def find(settings: Settings, downloader: Downloader, rule: SourceRule, meta: TrackMeta) -> Match:
    """The best audio match for `meta`, searched through `rule`'s egress (YouTube's)."""
    proxy = settings.resolve_egress(rule.egress)
    tried: list[str] = []
    for via, fetch in (
        ("YouTube Music", lambda: _ytm_candidates(meta, proxy)),
        ("YouTube", lambda: _yt_candidates(downloader, rule, meta)),
    ):
        candidates = await fetch()
        if match := pick(meta, candidates):
            log.info("matched %r by %r to %s (%s, %.2f)", meta.title, meta.artist, match.url, match.via, match.score)
            return match
        tried.append(f"{via}: {len(candidates)} result(s), none close enough")
    raise DownloadError(
        Reason.NO_MEDIA,
        f"{meta.artist} - {meta.title} ({meta.duration}s): " + "; ".join(tried),
        user_text="Couldn't find this track on YouTube Music.",
    )
