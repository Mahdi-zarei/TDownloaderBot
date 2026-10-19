"""The only module that knows about sites: URL matching, media ids, egress, cookies and tools."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import spotify
from .config import Settings
from .models import DownloadError, MediaRef, Reason, TrackList, TrackMeta


class Mode(StrEnum):
    PICK = "pick"  # probe formats, offer video qualities plus audio
    AUDIO = "audio"  # straight to audio with default tags
    POST = "post"  # every item in the post, photos and videos
    MATCH = "match"  # metadata from this source, audio matched on another (SourceRule.match_via)
    LIST = "list"  # a playlist or album: the user picks tracks, each handled as its own link


class CookiesPolicy(StrEnum):
    NEVER = "never"
    ALWAYS = "always"
    ON_AUTH_ERROR = "on_auth_error"  # retry once with cookies after a sign-in / bot-check error


class Tool(StrEnum):
    YTDLP = "yt-dlp"
    GALLERYDL = "gallery-dl"


@dataclass(frozen=True, slots=True)
class SourceRule:
    name: str
    label: str
    mode: Mode
    egress: str  # "direct", a named egress, or a proxy URL; resolve with Settings.resolve_egress
    parse: Callable[[str], MediaRef | None]
    cookies: Path | None = None
    cookies_policy: CookiesPolicy = CookiesPolicy.NEVER
    # Any of these cookies in the file means a logged-in session.
    session_cookies: tuple[str, ...] = ()
    max_concurrent: int | None = None
    sleep_requests: float | None = None
    # Tools to try in order for a POST url; the first that yields media wins.
    post_tools: Callable[[str], tuple[Tool, ...]] | None = None
    # MATCH mode: reads the track's metadata, and names the rule whose download path fetches the audio.
    fetch_meta: Callable[[str], Awaitable[TrackMeta]] | None = None
    match_via: str | None = None
    # LIST mode: reads the list's tracks.
    fetch_list: Callable[[str], Awaitable[TrackList]] | None = None
    # Shares another rule's concurrency limit (jobs that really download from that site).
    limit_group: str | None = None

    def cookies_file(self) -> Path | None:
        return self.cookies if self.cookies and self.cookies.is_file() else None

    def cookies_problem(self) -> str | None:
        """Why the cookies file can't provide a logged-in session; None when it looks usable."""
        if self.cookies_policy is CookiesPolicy.NEVER or self.cookies is None:
            return None
        if not self.cookies.is_file():
            return f"no cookies file at {self.cookies}"
        if not self.session_cookies:
            return None
        try:
            lines = self.cookies.read_text("utf-8", errors="replace").splitlines()
        except OSError as e:
            return f"can't read {self.cookies}: {e.strerror}"
        # Netscape rows: domain, subdomains, path, secure, expiry, name, value. HttpOnly rows start
        # with "#HttpOnly_", so they look like comments but are real cookies.
        expiries = [
            int(f[4]) if f[4].isdigit() else 0
            for f in (line.split("\t") for line in lines)
            if len(f) == 7 and f[5] in self.session_cookies
        ]
        names = " / ".join(self.session_cookies)
        if not expiries:
            return f"{self.cookies.name} has no {names} cookie: the account isn't logged in"
        if all(0 < e < time.time() for e in expiries):
            return f"the {names} cookie in {self.cookies.name} has expired: export fresh cookies"
        return None


def _split(url: str):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    segs = [s for s in parts.path.split("/") if s]
    return parts, host, segs


_YT_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "youtube-nocookie.com"}
_YT_ID = re.compile(r"[0-9A-Za-z_-]{11}")
_YT_PATH_ID = {"shorts", "live", "embed", "v", "e"}


def _youtube(url: str) -> MediaRef | None:
    parts, host, segs = _split(url)
    if host not in _YT_HOSTS:
        return None
    vid = None
    if host == "youtu.be":
        vid = segs[0] if segs else None
    elif segs[:1] == ["watch"]:
        vid = (parse_qs(parts.query).get("v") or [None])[0]
    elif len(segs) >= 2 and segs[0] in _YT_PATH_ID:
        vid = segs[1]
    if vid is None or not _YT_ID.fullmatch(vid):
        raise DownloadError(Reason.PLAYLIST if segs else Reason.UNSUPPORTED, url)
    return MediaRef("youtube", vid, f"https://www.youtube.com/watch?v={vid}")


_IG_HOSTS = {"instagram.com", "m.instagram.com", "instagr.am"}
_IG_POST = re.compile(r"^/(?:[\w.]+/)?(p|reels?|tv)/([\w-]+)")
_IG_STORY = re.compile(r"^/stories/([\w.]+)/(\d+)")


def _instagram(url: str) -> MediaRef | None:
    parts, host, segs = _split(url)
    if host not in _IG_HOSTS:
        return None
    if m := _IG_POST.match(parts.path):
        kind = "reel" if m[1].startswith("reel") else m[1]
        return MediaRef("instagram", m[2], f"https://www.instagram.com/{kind}/{m[2]}/")
    if m := _IG_STORY.match(parts.path):
        return MediaRef("instagram", f"story-{m[2]}", f"https://www.instagram.com/stories/{m[1]}/{m[2]}/")
    raise DownloadError(Reason.PLAYLIST if len(segs) == 1 else Reason.UNSUPPORTED, url)


def _instagram_tools(url: str) -> tuple[Tool, ...]:
    # gallery-dl handles photos and carousels; yt-dlp is the better reel/video extractor.
    if "/reel/" in url or "/tv/" in url:
        return Tool.YTDLP, Tool.GALLERYDL
    return Tool.GALLERYDL, Tool.YTDLP


_SC_HOSTS = {"soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"}
_SC_RESERVED = {
    "discover", "search", "stream", "you", "charts", "stations", "pages", "upload", "settings",
    "messages", "notifications", "people", "popular", "tags", "terms-of-use", "mobile", "jobs",
}
_SC_USER_PAGES = {
    "tracks", "likes", "reposts", "albums", "popular-tracks", "sets", "followers", "following",
    "comments", "spotlight", "toptracks",
}


def _soundcloud(url: str) -> MediaRef | None:
    parts, host, segs = _split(url)
    if host not in _SC_HOSTS:
        return None
    if host == "on.soundcloud.com":
        if not segs:
            raise DownloadError(Reason.UNSUPPORTED, url)
        return MediaRef("soundcloud", f"on-{segs[0]}", f"https://on.soundcloud.com/{segs[0]}")
    if not segs or segs[0].lower() in _SC_RESERVED:
        raise DownloadError(Reason.UNSUPPORTED, url)
    if len(segs) == 1 or segs[1].lower() in _SC_USER_PAGES:
        raise DownloadError(Reason.PLAYLIST, url)
    path = segs[:2]
    if len(segs) >= 3 and segs[2].startswith("s-"):
        path.append(segs[2])  # secret share link
    path_str = "/".join(path)
    return MediaRef("soundcloud", path_str.lower(), f"https://soundcloud.com/{path_str}")


_SP_HOSTS = {"open.spotify.com", "play.spotify.com"}
_SP_ID = re.compile(r"[0-9A-Za-z]{22}")
_SP_LISTS = {"album", "playlist"}
_SP_COLLECTIONS = {"artist", "user", "collection"}


def _spotify_segs(url: str) -> list[str] | None:
    _, host, segs = _split(url)
    if host not in _SP_HOSTS:
        return None
    return segs[1:] if segs and segs[0].startswith("intl-") else segs


def _spotify(url: str) -> MediaRef | None:
    _, host, segs = _split(url)
    if host in ("spotify.link", "spotify.app.link"):
        if not segs:
            raise DownloadError(Reason.UNSUPPORTED, url)
        return MediaRef("spotify", f"link-{segs[0]}", url)  # resolved to the track page when fetched
    if (segs := _spotify_segs(url)) is None:
        return None
    if len(segs) >= 2 and segs[0] == "track" and _SP_ID.fullmatch(segs[1]):
        return MediaRef("spotify", segs[1], f"https://open.spotify.com/track/{segs[1]}")
    if segs and segs[0] in _SP_COLLECTIONS:
        raise DownloadError(
            Reason.PLAYLIST, url,
            user_text="Spotify artists, profiles and Liked Songs aren't supported. Send a track, album or playlist.",
        )
    raise DownloadError(
        Reason.UNSUPPORTED, url, user_text="Only Spotify tracks, albums and playlists are supported (no podcasts)."
    )


def _spotify_list(url: str) -> MediaRef | None:
    segs = _spotify_segs(url)
    if not segs or len(segs) < 2 or segs[0] not in _SP_LISTS or not _SP_ID.fullmatch(segs[1]):
        return None
    return MediaRef("spotify_list", segs[1], f"https://open.spotify.com/{segs[0]}/{segs[1]}")


def _internal(host: str) -> bool:
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home.arpa")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            ip = ipaddress.ip_address(socket.inet_aton(host))  # also 127.1, 2130706433, 0x7f.1
        except (OSError, ValueError):
            return False
    return not ip.is_global


def _generic(url: str) -> MediaRef | None:
    parts, host, _ = _split(url)
    if parts.scheme not in ("http", "https") or not host:
        return None
    # Host networking: links to local services (the Bot API, metrics, cloud metadata) must not be fetched.
    if _internal((parts.hostname or "").rstrip(".")):
        raise DownloadError(Reason.UNSUPPORTED, url)
    return MediaRef("generic", hashlib.sha1(url.encode()).hexdigest()[:16], url)


def build_rules(s: Settings) -> list[SourceRule]:
    """Ordered rules; the first whose parser returns a ref wins. Generic must stay last."""
    return [
        SourceRule(
            name="youtube",
            label="YouTube",
            mode=Mode.PICK,
            egress=s.egress_youtube,
            parse=_youtube,
            cookies=s.youtube_cookies,
            cookies_policy=CookiesPolicy.ON_AUTH_ERROR,
            session_cookies=("SAPISID", "__Secure-3PAPISID"),
            max_concurrent=s.youtube_concurrency,
            sleep_requests=s.youtube_sleep_requests,
        ),
        SourceRule(
            name="instagram",
            label="Instagram",
            mode=Mode.POST,
            egress=s.egress_instagram,
            parse=_instagram,
            cookies=s.instagram_cookies,
            cookies_policy=CookiesPolicy.ALWAYS,
            session_cookies=("sessionid",),
            post_tools=_instagram_tools,
        ),
        SourceRule(
            name="soundcloud",
            label="SoundCloud",
            mode=Mode.AUDIO,
            egress=s.egress_soundcloud,
            parse=_soundcloud,
        ),
        SourceRule(
            name="spotify_list",
            label="Spotify",
            mode=Mode.LIST,
            # Only the track list is read here, straight from Spotify; each track then runs as a "spotify" job.
            egress="direct",
            parse=_spotify_list,
            fetch_list=spotify.fetch_list,
        ),
        SourceRule(
            name="spotify",
            label="Spotify",
            mode=Mode.MATCH,
            # The audio comes from YouTube, so the job runs on YouTube's egress and concurrency limit.
            egress=s.egress_youtube,
            parse=_spotify,
            fetch_meta=spotify.fetch_track,
            match_via="youtube",
            limit_group="youtube",
        ),
        SourceRule(
            name="generic",
            label="Other",
            mode=Mode.PICK,
            egress=s.egress_generic,
            parse=_generic,
        ),
    ]


class Sources:
    def __init__(self, settings: Settings):
        self.rules = build_rules(settings)
        self.by_name = {r.name: r for r in self.rules}

    def get(self, name: str) -> SourceRule:
        return self.by_name[name]

    def match(self, url: str) -> tuple[SourceRule, MediaRef]:
        """Raises DownloadError(UNSUPPORTED | PLAYLIST) for links that are out of scope."""
        url = url.strip()
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        for rule in self.rules:
            ref = rule.parse(url)
            if ref is not None:
                return rule, ref
        raise DownloadError(Reason.UNSUPPORTED, url)
