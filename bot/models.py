from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class Kind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    PHOTO = "photo"
    ALBUM = "album"
    DOCUMENT = "document"  # a video Telegram stored as a plain file
    ANIMATION = "animation"  # a silent video Telegram turned into a GIF-like animation


class Stage(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    UPLOADING = "uploading"


class Reason(StrEnum):
    UNSUPPORTED = "unsupported"
    PLAYLIST = "playlist"
    PRIVATE = "private"
    UNAVAILABLE = "unavailable"
    LOGIN_REQUIRED = "login_required"
    BLOCKED = "blocked"
    TOO_LARGE = "too_large"
    TOO_LONG = "too_long"
    LIVE = "live"
    NO_MEDIA = "no_media"
    DRM = "drm"
    TIMEOUT = "timeout"
    FAILED = "failed"


USER_TEXT: dict[Reason, str] = {
    Reason.UNSUPPORTED: "This link isn't supported.",
    Reason.PLAYLIST: "Playlists, albums, sets, profiles and channels from this site aren't supported. Send a single item.",
    Reason.PRIVATE: "This media is private.",
    Reason.UNAVAILABLE: "This media is unavailable (removed or region-locked).",
    Reason.LOGIN_REQUIRED: "This media needs a signed-in account (age-restricted, members-only, or the site blocked guests).",
    Reason.BLOCKED: "The site blocked the download. Try again later.",
    Reason.TOO_LARGE: "The file is larger than the bot's upload limit.",
    Reason.TOO_LONG: "This is longer than the bot's length limit.",
    Reason.LIVE: "Live streams can't be downloaded.",
    Reason.NO_MEDIA: "No downloadable media found at this link.",
    Reason.DRM: "This is DRM-protected (for example a SoundCloud Go+ track or other paid content), so it can't be downloaded.",
    Reason.TIMEOUT: "The download took too long and was stopped.",
    Reason.FAILED: "The download failed.",
}


def too_long_text(limit: int) -> str:
    if limit >= 3600 and limit % 1800 == 0:
        span = f"{limit / 3600:g}-hour"
    elif limit % 60 == 0:
        span = f"{limit // 60}-minute"
    else:
        span = f"{limit}-second"
    return f"This is longer than the {span} limit."


# Failures caused by the link itself; everything else is reported to the admin.
EXPECTED_REASONS = frozenset(
    {Reason.UNSUPPORTED, Reason.PLAYLIST, Reason.PRIVATE, Reason.TOO_LARGE, Reason.TOO_LONG, Reason.LIVE, Reason.DRM}
)


class DownloadError(Exception):
    """A failure with a user-facing reason.

    `detail` carries the full tool output for the admin; `cause` is the site's own one-line
    explanation (e.g. "This video has been removed by the uploader"), safe to show users.
    """

    def __init__(self, reason: Reason, detail: str = "", *, user_text: str | None = None, cause: str = ""):
        super().__init__(f"{reason}: {detail[-1000:]}" if detail else str(reason))
        self.reason = reason
        self.detail = detail
        self.user_text = user_text or USER_TEXT[reason]
        self.cause = cause

    @property
    def expected(self) -> bool:
        return self.reason in EXPECTED_REASONS


@dataclass(frozen=True, slots=True)
class MediaRef:
    source: str  # SourceRule.name
    media_id: str
    url: str  # canonical URL handed to the downloader

    def cache_key(self, fmt: str) -> str:
        return f"{self.source}:{self.media_id}:{fmt}"


@dataclass(slots=True)
class MediaInfo:
    title: str
    webpage_url: str
    extractor: str = ""
    uploader: str | None = None  # channel / uploader display name
    duration: float | None = None
    upload_date: str | None = None  # YYYYMMDD
    is_live: bool = False
    track: str | None = None
    artist: str | None = None
    album: str | None = None
    release_year: int | None = None

    @classmethod
    def from_ytdlp(cls, d: dict[str, Any]) -> MediaInfo:
        artist = d.get("artist")
        if not artist and d.get("artists"):
            artist = ", ".join(d["artists"])
        return cls(
            title=d.get("title") or d.get("fulltitle") or "Untitled",
            webpage_url=d.get("webpage_url") or d.get("original_url") or "",
            extractor=d.get("extractor_key") or d.get("extractor") or "",
            uploader=d.get("channel") or d.get("uploader") or d.get("creator"),
            duration=d.get("duration"),
            upload_date=d.get("upload_date") or d.get("release_date"),
            is_live=bool(d.get("is_live")) or d.get("live_status") in ("is_live", "is_upcoming"),
            track=d.get("track"),
            artist=artist,
            album=d.get("album"),
            release_year=d.get("release_year"),
        )


@dataclass(frozen=True, slots=True)
class Quality:
    key: str  # format part of the cache key, e.g. "1080p" or "best"
    label: str  # button text, e.g. "1080p60 · 245 MB"
    format_spec: str  # value for yt-dlp -f
    size_bytes: int | None


@dataclass(slots=True)
class Probe:
    info: MediaInfo
    qualities: list[Quality]  # highest first, already filtered by the offer size limit
    has_audio: bool


@dataclass(slots=True)
class VideoFile:
    path: Path
    info: MediaInfo
    thumb: Path | None = None  # source thumbnail image (JPEG), any size


@dataclass(slots=True)
class AudioFile:
    path: Path  # always .m4a or .mp3
    info: MediaInfo
    thumb: Path | None = None  # source thumbnail image (JPEG), used to build the cover


@dataclass(slots=True)
class MediaItem:
    path: Path
    kind: Kind  # PHOTO or VIDEO


@dataclass(slots=True)
class PostFiles:
    items: list[MediaItem]  # in post order
    info: MediaInfo  # uploader = author
    caption: str = ""  # post text, plain


@dataclass(slots=True)
class TrackMeta:
    """A track known by its metadata only (Spotify); the audio is matched elsewhere."""

    url: str
    title: str
    artist: str  # display form, e.g. "The Weeknd, Ariana Grande"
    album: str = ""
    year: str = ""
    duration: float | None = None
    cover_url: str | None = None


@dataclass(slots=True)
class ListItem:
    url: str  # the track's own link, handled like a link the user sent
    title: str
    artist: str
    duration: float | None = None


@dataclass(slots=True)
class TrackList:
    """A playlist or album: its tracks, each downloaded as its own link."""

    url: str
    kind: str  # "playlist" or "album"
    title: str
    owner: str  # playlist owner or album artist
    items: list[ListItem]
    total: int  # tracks in the list; more than len(items) when the source shows only the first ones


@dataclass(slots=True)
class Tags:
    title: str = ""
    artist: str = ""
    album: str = ""
    year: str = ""


@dataclass(slots=True)
class Sent:
    """What was delivered; stored in media_cache and re-sent by file_id."""

    kind: Kind
    # For ALBUM: JSON list of {"type": "photo" | "video", "file_id": "..."} in post order.
    file_id: str
    size_bytes: int = 0
    caption: str = ""  # HTML, reused on cached re-sends


@dataclass(slots=True)
class Progress:
    """Mutable job progress. The downloader writes it; the status ticker reads it."""

    stage: Stage = Stage.QUEUED
    fraction: float | None = None  # 0..1, None when unknown
    detail: str = ""  # e.g. "52.1 MB of 120 MB · 3.4 MB/s"

    def set(self, stage: Stage, fraction: float | None = None, detail: str = "") -> None:
        self.stage, self.fraction, self.detail = stage, fraction, detail
