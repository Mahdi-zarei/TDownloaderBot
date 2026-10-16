"""yt-dlp / gallery-dl subprocesses: probing, downloads, progress, timeouts, error mapping."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import shlex
import shutil
import signal
import struct
import sys
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import MB, Settings
from .models import (
    AudioFile,
    DownloadError,
    Kind,
    MediaInfo,
    MediaItem,
    PostFiles,
    Probe,
    Progress,
    Quality,
    Reason,
    Stage,
    VideoFile,
)
from .sources import CookiesPolicy, Mode, SourceRule, Tool

log = logging.getLogger(__name__)

_STDERR_TAIL = 200
_LINE_MAX = 2000
_THROTTLE = 0.4
_MAX_QUALITIES = 8
_SELFTEST_TIMEOUT = 180.0
_PHOTO_EXTS = frozenset({"jpg", "jpeg", "png", "webp", "heic"})
_VIDEO_EXTS = frozenset({"mp4", "mov", "webm", "mkv"})
_AUDIO_EXTS = frozenset({"m4a", "mp3", "aac", "ogg", "oga", "opus", "wav", "flac", "weba", "mka"})
_SAFE_FORMAT_ID = re.compile(r"[\w.\-]+")
_SHORTCODE = re.compile(r"/(?:p|reels?|tv)/([\w-]+)")

_FASTSTART = "Merger+ffmpeg_o:-movflags +faststart"
_AUDIO_FORMAT = "ba[ext=m4a]/ba[acodec^=mp4a]/ba[ext=mp3]/ba/b"
_PP_MARKERS = ("[Merger]", "[ExtractAudio]", "[VideoRemuxer]", "[VideoConvertor]", "[Fixup")
_PROGRESS_TAG = "@@dl|"
_PROGRESS_TEMPLATE = "download:" + _PROGRESS_TAG + "|".join(
    f"%({key})s"
    for key in (
        "progress.status",
        "progress.downloaded_bytes",
        "progress.total_bytes",
        "progress.total_bytes_estimate",
        "progress.speed",
        "info.format_id",
        "info.playlist_index",
        "info.n_entries",
    )
)
_GDL_FILENAME = "{num:>02}_{media_id}.{extension}"
_GDL_SLEEP = "1.0-2.5"  # Instagram's own default is 6-12 s per request
_TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
_CURL_ERRORS = {
    5: "proxy not resolved",
    6: "host not resolved",
    7: "connection failed",
    28: "timed out",
    35: "TLS error",
    52: "empty reply",
    56: "connection reset",
    97: "proxy handshake failed",
}


def _rx(*parts: str) -> re.Pattern[str]:
    return re.compile("|".join(parts), re.IGNORECASE)


# Checked in order against tool output; the flag marks errors a retry with cookies may fix.
_ERROR_RULES: tuple[tuple[re.Pattern[str], Reason, bool], ...] = (
    (_rx(r"drm[- ]?protected", r"protected by drm", r"\bwidevine\b"), Reason.DRM, False),
    (_rx(r"sign in to confirm you.?re not a bot"), Reason.BLOCKED, True),
    (_rx(r"private video", r"account is private", r"posts are private", r"\bis private\b"), Reason.PRIVATE, False),
    (_rx(r"sign in to confirm your age", r"age[- ]restricted", r"inappropriate for some users"), Reason.LOGIN_REQUIRED, True),
    (_rx(r"members[- ]only", r"join this channel", r"channel.?s members"), Reason.LOGIN_REQUIRED, True),
    (
        _rx(
            r"rate-limit reached or login required", r"login required", r"redirect to login page",
            r"locked behind the login", r"logged[- ]in", r"authenticated cookies", r"account credentials required",
            r"registered users", r"http error 401", r"401 unauthorized",
        ),
        Reason.LOGIN_REQUIRED,
        True,
    ),
    (
        _rx(
            r"http error 403", r"403 forbidden", r"http error 429", r"429 too many", r"too many requests",
            r"rate[- ]?limit", r"redirect to challenge page", r"challengeerror", r"try again later",
        ),
        Reason.BLOCKED,
        False,
    ),
    (
        _rx(
            r"no video formats found", r"there is no video in this post", r"requested format is not available",
            r"no formats found", r"no media found",
        ),
        Reason.NO_MEDIA,
        False,
    ),
    (
        _rx(
            r"video unavailable", r"is unavailable", r"is not available", r"isn.?t available", r"no longer available",
            r"been removed", r"was removed", r"could not be found", r"notfounderror", r"http error 404",
            r"404 not found", r"in your country", r"from your location", r"geo.?restrict",
        ),
        Reason.UNAVAILABLE,
        False,
    ),
    (_rx(r"unsupported url"), Reason.UNSUPPORTED, False),
    (_rx(r"larger than max-filesize"), Reason.TOO_LARGE, False),
    (_rx(r"live event", r"premieres? in\b", r"premiere will begin", r"\bis (?:currently )?live\b"), Reason.LIVE, False),
)

# Most informative first, for picking one error when every post tool failed.
_REASON_RANK = (
    Reason.PRIVATE, Reason.DRM, Reason.UNAVAILABLE, Reason.LOGIN_REQUIRED, Reason.BLOCKED, Reason.LIVE, Reason.TOO_LARGE,
    Reason.TOO_LONG, Reason.UNSUPPORTED, Reason.PLAYLIST, Reason.TIMEOUT, Reason.FAILED, Reason.NO_MEDIA,
)


@dataclass(slots=True)
class SelftestResult:
    source: str  # rule name
    egress: str  # egress name as given
    ok: bool
    seconds: float
    reason: str = ""  # Reason value on failure
    detail: str = ""  # last lines of tool output on failure


class _ToolError(DownloadError):
    def __init__(
        self, reason: Reason, detail: str = "", *, auth: bool = False, user_text: str | None = None, cause: str = ""
    ):
        super().__init__(reason, detail, user_text=user_text, cause=cause)
        self.auth = auth  # a retry with cookies may help


@dataclass(slots=True)
class _Run:
    cmd: list[str]
    code: int = -1
    stdout: list[str] = field(default_factory=list)
    stderr: deque[str] = field(default_factory=lambda: deque[str](maxlen=_STDERR_TAIL))
    notes: list[str] = field(default_factory=list)  # stdout lines that explain a missing file

    def detail(self) -> str:
        return _redact("\n".join(["$ " + shlex.join(self.cmd), *self.notes, *self.stderr]))


# ---------------------------------------------------------------- helpers


def _redact(text: str) -> str:
    return re.sub(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@", r"\1***@", text)


def _tmpl(path: Path) -> str:
    """A path as a literal inside a yt-dlp output template."""
    return str(path).replace("%", "%%")


def _num(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _human(n: float) -> str:
    if n >= 1024 * MB:
        return f"{n / (1024 * MB):.2f} GB"
    if n >= 100 * MB:
        return f"{n / MB:.0f} MB"
    if n >= MB:
        return f"{n / MB:.1f} MB"
    return f"{n / 1024:.0f} KB"


def _ext(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


def _kind(path: Path) -> Kind | None:
    ext = _ext(path)
    return Kind.PHOTO if ext in _PHOTO_EXTS else Kind.VIDEO if ext in _VIDEO_EXTS else None


def _lead_int(name: str) -> int:
    m = re.match(r"\d+", name)
    return int(m[0]) if m else 1 << 30


def _tail(text: str, n: int) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


def _truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _yyyymmdd(value: Any) -> str | None:
    m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", str(value or ""))
    return "".join(m.groups()) if m else None


def _read_lines(path: Path) -> list[str]:
    try:
        return [line for line in path.read_text("utf-8", errors="replace").splitlines() if line.strip()]
    except OSError:
        return []


def _parse_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return _parse_json(path.read_text("utf-8"))
    except OSError:
        return {}


def _reset_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _deadline(seconds: float) -> float:
    return asyncio.get_running_loop().time() + seconds


def _remaining(deadline: float) -> float:
    return deadline - asyncio.get_running_loop().time()


def _classify(lines: Iterable[str]) -> tuple[Reason, bool] | None:
    text = "\n".join(lines)
    for rx, reason, auth in _ERROR_RULES:
        if rx.search(text):
            return reason, auth
    return None


def _diagnose(stderr: Iterable[str]) -> tuple[Reason, bool]:
    lines = list(stderr)
    errors = [line for line in lines if "ERROR" in line or "[error]" in line]
    return _classify(errors) or _classify(lines) or (Reason.FAILED, False)


# yt-dlp: "ERROR: [soundcloud] 1552744678: This video is DRM protected"; gallery-dl: "[instagram][error] ..."
_ERROR_LINE = re.compile(r"^(?:ERROR:|\[[\w:.-]+\]\[error\]|\[error\])\s*(.+)$", re.IGNORECASE)
_EXTRACTOR_PREFIX = re.compile(r"^\[[^\]]+\]\s*(?:[\w-]+:\s+)?")
# Advice aimed at the person running the tool, not at the bot's users.
_TOOL_ADVICE = re.compile(
    r"\s*(?:;\s*)?(?:please report this issue|use --|see\s+https?://|also see|confirm you are on the latest).*$",
    re.IGNORECASE,
)
_PAREN_NOISE = re.compile(r"\s*\((?:caused by|https?://)[^)]*\)\s*", re.IGNORECASE)


def _cause(stderr: Iterable[str]) -> str:
    """The site's own explanation from the last error line, cleaned up for users; "" when there is none."""
    for line in reversed(list(stderr)):
        if m := _ERROR_LINE.match(line.strip()):
            text = _TOOL_ADVICE.sub("", _EXTRACTOR_PREFIX.sub("", m[1]))
            text = _PAREN_NOISE.sub(" ", text).strip(" .;:")
            if text:
                return _truncate(_redact(text), 200)
    return ""


def _read_sizes(path: Path) -> dict[str, float | None]:
    """format_id -> expected bytes, from the before_dl print of requested_formats."""
    lines = _read_lines(path)
    try:
        parts = json.loads(lines[-1]) if lines else None
    except ValueError:
        return {}
    if not isinstance(parts, list):
        return {}
    return {
        str(f.get("format_id")): _num(f.get("filesize")) or _num(f.get("filesize_approx"))
        for f in parts
        if isinstance(f, dict)
    }


def _moov_first(path: Path) -> bool | None:
    """Whether the MP4's moov box precedes mdat; None when the layout can't be read."""
    try:
        with path.open("rb") as f:
            while len(header := f.read(8)) == 8:
                size, kind = struct.unpack(">I4s", header)
                if kind == b"moov":
                    return True
                if kind == b"mdat":
                    return False
                if size == 1:
                    size = struct.unpack(">Q", f.read(8))[0] - 8
                if size < 8:
                    return None
                f.seek(size - 8, os.SEEK_CUR)
    except (OSError, struct.error):
        return None
    return None


def _media_outputs(workdir: Path) -> list[str]:
    w = _tmpl(workdir)
    return ["-o", f"{w}/media.%(ext)s", "-o", f"thumbnail:{w}/thumb.%(ext)s", "-o", "pl_thumbnail:", "-o", "pl_infojson:"]


def _read_info(workdir: Path, url: str) -> MediaInfo:
    data = _load_json(workdir / "media.info.json")
    info = MediaInfo.from_ytdlp(data) if data else MediaInfo(id="", title="Untitled", webpage_url=url)
    info.webpage_url = info.webpage_url or url
    return info


def _collect_media(folder: Path) -> list[MediaItem]:
    if not folder.is_dir():
        return []
    files = sorted((p for p in folder.iterdir() if p.is_file()), key=lambda p: (_lead_int(p.name), p.name))
    return [MediaItem(p, kind) for p in files if (kind := _kind(p))]


def _first_json(folder: Path) -> dict[str, Any]:
    for p in sorted(folder.glob("*.json"), key=lambda p: (_lead_int(p.name), p.name)):
        if data := _load_json(p):
            return data
    return {}


def _post_files(
    rule: SourceRule, url: str, items: list[MediaItem], *, caption: Any, uploader: Any, date: Any, media_id: Any
) -> PostFiles:
    text = str(caption or "").strip()
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    m = _SHORTCODE.search(url)
    info = MediaInfo(
        id=m[1] if m else str(media_id or ""),
        title=_truncate(first, 64) or f"{rule.label} post",
        webpage_url=url,
        extractor=rule.label,
        uploader=str(uploader) if uploader else None,
        upload_date=_yyyymmdd(date),
    )
    return PostFiles(items=items, info=info, caption=text)


def _most_informative(errors: list[tuple[Tool, DownloadError]]) -> DownloadError:
    if not errors:
        return DownloadError(Reason.NO_MEDIA)
    rank = {reason: i for i, reason in enumerate(_REASON_RANK)}
    _, best = min(errors, key=lambda te: rank.get(te[1].reason, len(rank)))
    detail = "\n\n".join(f"[{tool}] {e.detail}" for tool, e in errors)
    return DownloadError(best.reason, detail, user_text=best.user_text, cause=best.cause)


# ---------------------------------------------------------------- video formats


def _usable(f: dict[str, Any]) -> bool:
    if f.get("has_drm") or f.get("ext") == "mhtml" or f.get("protocol") == "mhtml":
        return False
    note = str(f.get("format_note") or "").lower()
    if "storyboard" in note or "premium" in note:
        return False
    return not (f.get("vcodec") == "none" and f.get("acodec") == "none")


def _is_video(f: dict[str, Any]) -> bool:
    vcodec = f.get("vcodec")
    if vcodec == "none":
        return False
    if vcodec or f.get("height") or f.get("width"):
        return True
    return f.get("ext") not in _AUDIO_EXTS and f.get("video_ext") != "none"


def _video_rank(f: dict[str, Any]) -> tuple[bool, float, bool, float]:
    return (
        f.get("dynamic_range") in (None, "SDR"),
        _num(f.get("fps")) or 0.0,
        f.get("protocol") in ("https", "http"),
        _num(f.get("tbr")) or 0.0,
    )


def _audio_rank(f: dict[str, Any]) -> tuple[bool, float, float]:
    drc = "drc" in f"{f.get('format_id')} {f.get('format_note')}".lower()
    return (not drc, _num(f.get("language_preference")) or 0.0, _num(f.get("abr")) or _num(f.get("tbr")) or 0.0)


def _best_audio(formats: list[dict[str, Any]]) -> dict[str, Any] | None:
    audios = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
    if not audios:
        return None
    aac = [f for f in audios if str(f.get("acodec") or "").startswith("mp4a") or f.get("ext") == "m4a"]
    return max(aac or audios, key=_audio_rank)


def _size(f: dict[str, Any], duration: float | None) -> tuple[int | None, bool]:
    """Bytes and whether the number is exact (filesize) rather than an estimate."""
    if n := _num(f.get("filesize")):
        return int(n), True
    if n := _num(f.get("filesize_approx")):
        return int(n), False
    kbps = _num(f.get("tbr")) or (_num(f.get("vbr")) or 0.0) + (_num(f.get("abr")) or 0.0)
    if kbps and duration:
        return int(kbps * 125 * duration), False
    return None, False


def _size_label(size: int | None, exact: bool) -> str:
    return f" · {'' if exact else '~'}{_human(size)}" if size else ""


def _quality(height: int, group: list[dict[str, Any]], audio: dict[str, Any] | None, duration: float | None) -> Quality:
    avc = [f for f in group if str(f.get("vcodec") or "").startswith("avc1") and f.get("ext") == "mp4"]
    video = max(avc or group, key=_video_rank)
    pair = audio if video.get("acodec") == "none" else None

    vsize, vexact = _size(video, duration)
    asize, aexact = _size(pair, duration) if pair else (0, True)
    size = None if vsize is None else vsize + (asize or 0)
    exact = vexact and aexact and asize is not None

    width = video.get("width")
    res = min(height, width) if isinstance(width, int) and width > 0 else height
    fps = _num(video.get("fps"))
    label = f"{res}p{round(fps) if fps and fps > 30 else ''}{_size_label(size, exact)}"

    ids = [str(video.get("format_id") or "")] + ([str(pair.get("format_id") or "")] if pair else [])
    chain = ["+".join(ids)] if all(_SAFE_FORMAT_ID.fullmatch(i) for i in ids) else []
    if avc:
        chain.append(f"bv*[height={height}][vcodec^=avc1]+ba[acodec^=mp4a]")
    else:
        chain.append(f"bv*[height={height}]+ba[acodec^=mp4a]")
    chain += [f"bv*[height={height}]+ba", f"b[height<={height}]"]
    return Quality(key=f"{height}p", label=label, height=height, format_spec="/".join(chain), size_bytes=size)


def _single_entry(d: dict[str, Any]) -> dict[str, Any]:
    while d.get("_type") in ("playlist", "multi_video"):
        entries = [e for e in d.get("entries") or () if isinstance(e, dict)]
        if len(entries) != 1:
            raise DownloadError(Reason.PLAYLIST if entries else Reason.NO_MEDIA, str(d.get("webpage_url") or ""))
        d = entries[0]
    return d


# ---------------------------------------------------------------- subprocess


def _child_env() -> dict[str, str]:
    return {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if sys.platform == "win32":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)


def _emit(sink: Callable[[str], None], raw: bytearray) -> None:
    line = raw.decode("utf-8", "replace").rstrip("\r")
    if "\r" in line:
        line = line.rsplit("\r", 1)[-1]
    try:
        sink(line)
    except Exception:
        log.debug("output handler failed on %r", line[:200], exc_info=True)


async def _pump(stream: asyncio.StreamReader, sink: Callable[[str], None]) -> None:
    # Chunked reads: `yt-dlp -J` prints one multi-megabyte line, beyond StreamReader.readline's limit.
    pending = bytearray()
    while chunk := await stream.read(1 << 16):
        start = 0
        while (i := chunk.find(b"\n", start)) >= 0:
            pending += chunk[start:i]
            _emit(sink, pending)
            pending.clear()
            start = i + 1
        pending += chunk[start:]
    if pending:
        _emit(sink, pending)


async def _reap(proc: asyncio.subprocess.Process, pumps: asyncio.Future[Any]) -> None:
    try:
        await proc.wait()
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(pumps, 5)


async def _exec(
    cmd: list[str],
    *,
    timeout: float,
    on_line: Callable[[str], None] | None = None,
    keep_stdout: bool = False,
    cwd: Path | None = None,
) -> _Run:
    """Runs a command in its own process group; kills the whole group on timeout or cancellation."""
    run = _Run(cmd)
    if timeout <= 0:
        raise _ToolError(Reason.TIMEOUT, run.detail())
    log.debug("exec: %s", _redact(shlex.join(cmd)))

    def out(line: str) -> None:
        if keep_stdout:
            run.stdout.append(line)
        elif ("max-filesize" in line or "does not pass filter" in line) and len(run.notes) < 10:
            run.notes.append(line[:_LINE_MAX])
        if on_line:
            on_line(line)

    def err(line: str) -> None:
        run.stderr.append(line[:_LINE_MAX])
        if on_line:
            on_line(line)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=_child_env(),
            start_new_session=True,
        )
    except OSError as e:
        raise _ToolError(Reason.FAILED, f"{run.detail()}\n{cmd[0]}: {e}") from e
    assert proc.stdout is not None and proc.stderr is not None
    pumps = asyncio.gather(_pump(proc.stdout, out), _pump(proc.stderr, err))
    finished = timed_out = False
    try:
        try:
            async with asyncio.timeout(timeout):
                await asyncio.shield(pumps)
                run.code = await proc.wait()
            finished = True
        except TimeoutError:
            timed_out = True
    finally:
        if not finished:
            _kill_group(proc)
        await _reap(proc, pumps)
    if timed_out:
        raise _ToolError(Reason.TIMEOUT, run.detail())
    return run


async def _version(cmd: list[str]) -> str:
    if shutil.which(cmd[0]) is None:
        return "missing"
    try:
        run = await _exec(cmd, timeout=20, keep_stdout=True)
    except Exception:
        return "error"
    line = next((s.strip() for s in [*run.stdout, *run.stderr] if s.strip()), "")
    words = line.split()
    if line.startswith("ffmpeg version") and len(words) > 2:
        return words[2]
    if len(words) > 1 and words[0] == "deno":
        return words[1]
    return line[:40] or "unknown"


# ---------------------------------------------------------------- progress parsers


class _Tracker:
    """Turns yt-dlp progress-template lines into throttled Progress updates across all requested parts."""

    def __init__(self, progress: Progress, sizes_file: Path):
        self.progress = progress
        self.sizes_file = sizes_file
        self.item: str | None = None
        self.expected: dict[str, float | None] = {}
        self.done: dict[str, float] = {}
        self.total: dict[str, float] = {}
        self.last = 0.0
        self.processing = False

    def __call__(self, line: str) -> None:
        if line.startswith(_PROGRESS_TAG):
            self._update(line[len(_PROGRESS_TAG) :].split("|"))
        elif line.startswith(_PP_MARKERS) and not self.processing:
            self.processing = True
            self.progress.set(Stage.PROCESSING)

    def _update(self, fields: list[str]) -> None:
        if len(fields) != 8:
            return
        status, done_s, total_s, estimate_s, speed_s, fid, item, entries_s = fields
        done = _num(done_s)
        if done is None:
            return
        if item != self.item:
            self.item = item
            self.expected = _read_sizes(self.sizes_file)
            self.done.clear()
            self.total.clear()
        self.done[fid] = done
        if status == "finished":
            self.total[fid] = done
        elif size := _num(total_s) or _num(estimate_s):
            self.total[fid] = size
        now = time.monotonic()
        if status != "finished" and not self.processing and now - self.last < _THROTTLE:
            return
        self.last, self.processing = now, False

        # Parts not started yet count with their expected size; unknown sizes get the average of known ones.
        ids = list(self.expected) if fid in self.expected else []
        ids += [k for k in self.done if k not in ids]
        sizes = [self.total.get(k) or self.expected.get(k) for k in ids]
        known = [s for s in sizes if s]
        got = sum(self.done.values())
        total: float | None = None
        fraction: float | None = None
        if known:
            fill = sum(known) / len(known)
            total = sum(s or fill for s in sizes)
            got = sum(min(self.done.get(k, 0.0), s or fill) for k, s in zip(ids, sizes))
            fraction = got / total
        entries, index = _num(entries_s), _num(item)
        prefix = ""
        if entries and entries > 1 and index:
            fraction = (index - 1 + (fraction or 0.0)) / entries
            prefix = f"{index:.0f} of {entries:.0f} · "
        text = f"{_human(got)} of {_human(total)}" if total else _human(got)
        if speed := _num(speed_s):
            text += f" · {_human(speed)}/s"
        self.progress.set(Stage.DOWNLOADING, None if fraction is None else round(min(fraction, 1.0), 3), prefix + text)


class _ItemCounter:
    """Counts the files gallery-dl reports on stdout; per-file metadata carries the post's item count."""

    def __init__(self, progress: Progress, folder: Path):
        self.progress = progress
        self.prefix = str(folder)
        self.n = 0
        self.count: int | None = None

    def __call__(self, line: str) -> None:
        path = line.removeprefix("# ").strip()
        if not path.startswith(self.prefix) or _kind(Path(path)) is None:
            return
        self.n += 1
        if self.count is None:
            count = _load_json(Path(path + ".json")).get("count")
            self.count = count if isinstance(count, int) and count > 0 else None
        total = max(self.count, self.n) if self.count else None
        detail = f"{self.n} of {total} items" if total else f"{self.n} items"
        self.progress.set(Stage.DOWNLOADING, self.n / total if total else None, detail)


# ---------------------------------------------------------------- downloader


class Downloader:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._probe_sem = asyncio.Semaphore(settings.probe_concurrency)

    # ------------------------------------------------------------ public API

    async def probe(self, rule: SourceRule, url: str) -> Probe:
        """Metadata and video qualities via `yt-dlp -J`, without downloading media."""
        return await self._probe(rule, url, rule.egress)

    async def download_video(
        self, rule: SourceRule, url: str, quality: Quality, workdir: Path, progress: Progress
    ) -> VideoFile:
        deadline = _deadline(self.settings.job_timeout)
        args = [
            "-f", quality.format_spec, "--merge-output-format", "mp4", "--postprocessor-args", _FASTSTART,
            "--write-info-json", "--write-thumbnail", "--convert-thumbnails", "jpg",
            *_media_outputs(workdir), "--playlist-items", "1",
        ]
        files = await self._ytdlp_fetch(rule, url, workdir, progress, args, egress=rule.egress, deadline=deadline)
        path = await self._faststart(files[-1], progress, deadline)
        self._check_size(path)
        return VideoFile(path=path, info=_read_info(workdir, url), thumb=await self._thumb(workdir, deadline))

    async def download_audio(self, rule: SourceRule, url: str, workdir: Path, progress: Progress) -> AudioFile:
        deadline = _deadline(self.settings.job_timeout)
        args = [
            "-f", _AUDIO_FORMAT, "--write-info-json", "--write-thumbnail", "--convert-thumbnails", "jpg",
            *_media_outputs(workdir), "--playlist-items", "1",
        ]
        files = await self._ytdlp_fetch(rule, url, workdir, progress, args, egress=rule.egress, deadline=deadline)
        path = await self._to_audio(files[-1], progress, deadline)
        self._check_size(path)
        return AudioFile(path=path, info=_read_info(workdir, url), thumb=await self._thumb(workdir, deadline))

    async def download_post(self, rule: SourceRule, url: str, workdir: Path, progress: Progress) -> PostFiles:
        return await self._download_post(rule, url, workdir, progress, rule.egress, _deadline(self.settings.job_timeout))

    async def search(self, rule: SourceRule, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """YouTube search results (flat entries: id, title, duration, channel) via the rule's egress."""
        proxy = self._proxy(rule.egress)
        args = ["--flat-playlist", "-J", "--playlist-items", f"1:{limit}"]
        cmd = self._ytdlp_cmd(rule, proxy, None, args, f"ytsearch{limit}:{query}", playlist=True)
        async with self._probe_sem:
            with tempfile.TemporaryDirectory(prefix="search-") as tmp:
                run = await _exec(cmd, timeout=self.settings.probe_timeout, keep_stdout=True, cwd=Path(tmp))
        for line in reversed(run.stdout):
            if line.startswith("{") and (data := _parse_json(line)):
                return [e for e in data.get("entries") or [] if isinstance(e, dict) and e.get("id")]
        raise self._error(run)

    async def selftest(self, rule: SourceRule, url: str, egress: str, workdir: Path) -> SelftestResult:
        started = time.monotonic()

        def result(ok: bool, reason: str = "", detail: str = "") -> SelftestResult:
            return SelftestResult(rule.name, egress, ok, round(time.monotonic() - started, 1), reason, detail)

        try:
            async with asyncio.timeout(_SELFTEST_TIMEOUT):
                deadline = _deadline(_SELFTEST_TIMEOUT)
                workdir.mkdir(parents=True, exist_ok=True)
                if rule.mode is Mode.POST:
                    await self._download_post(rule, url, workdir, Progress(), egress, deadline)
                else:
                    await self._probe(rule, url, egress)
                    args = ["-f", "wa/w", "-o", f"{_tmpl(workdir)}/selftest.%(ext)s", "--playlist-items", "1"]
                    await self._ytdlp_fetch(
                        rule, url, workdir, Progress(), args, egress=egress, deadline=deadline, guard=False
                    )
        except TimeoutError:
            return result(False, Reason.TIMEOUT.value, f"no result within {_SELFTEST_TIMEOUT:.0f} s")
        except DownloadError as e:
            return result(False, e.reason.value, _tail(e.detail, 5))
        except Exception as e:
            log.exception("selftest %s via %s crashed", rule.name, egress)
            return result(False, Reason.FAILED.value, repr(e))
        return result(True)

    async def egress_ip(self, egress: str) -> str:
        if shutil.which("curl") is None:
            return "error: curl not installed"
        try:
            proxy = self.settings.resolve_egress(egress)
            cmd = ["curl", "-s", "--max-time", "15", *(["-x", proxy] if proxy else []), _TRACE_URL]
            run = await _exec(cmd, timeout=20, keep_stdout=True)
        except DownloadError as e:
            return f"error: {e.reason.value}"
        except Exception as e:
            return f"error: {e}"
        if run.code != 0:
            return f"error: {_CURL_ERRORS.get(run.code, f'curl exit {run.code}')}"
        trace = {k.strip(): v.strip() for k, _, v in (line.partition("=") for line in run.stdout)}
        if not trace.get("ip"):
            return "error: unexpected response"
        parts = [trace["ip"]]
        if trace.get("warp") in ("on", "plus"):  # Cloudflare marks traffic from its WARP network
            parts.append(f"warp={trace['warp']}")
        if loc := trace.get("loc"):
            parts.append(loc)
        return " · ".join(parts)

    async def versions(self) -> dict[str, str]:
        cmds = {
            "yt-dlp": ["yt-dlp", "--version"],
            "gallery-dl": ["gallery-dl", "--version"],
            "ffmpeg": ["ffmpeg", "-version"],
            "deno": ["deno", "--version"],
        }
        found = await asyncio.gather(*(_version(cmd) for cmd in cmds.values()))
        return dict(zip(cmds, found))

    # ------------------------------------------------------------ shared plumbing

    def _proxy(self, egress: str) -> str | None:
        try:
            return self.settings.resolve_egress(egress)
        except ValueError as e:
            raise DownloadError(Reason.FAILED, str(e)) from e

    def _ytdlp_cmd(
        self,
        rule: SourceRule,
        proxy: str | None,
        cookies: Path | None,
        args: list[str],
        url: str,
        *,
        playlist: bool = False,
    ) -> list[str]:
        cmd = [
            "yt-dlp", "--ignore-config", "--newline", "--socket-timeout", "30", "--retries", "5",
            "--fragment-retries", "10", "--max-filesize", f"{self.settings.max_upload_mb}M",
        ]
        if not playlist:
            cmd.append("--no-playlist")
        if proxy:
            cmd += ["--proxy", proxy]
        if rule.sleep_requests:
            cmd += ["--sleep-requests", f"{rule.sleep_requests:g}"]
        if cookies:
            cmd += ["--cookies", str(cookies)]
        return [*cmd, *args, "--", url]

    async def _attempts[T](
        self, rule: SourceRule, scratch: Path, attempt: Callable[[Path | None], Awaitable[T]]
    ) -> T:
        """Runs `attempt` per the rule's cookies policy; ON_AUTH_ERROR retries once with cookies."""
        src = rule.cookies_file()
        if rule.cookies_policy is CookiesPolicy.ALWAYS:
            if src is None:
                log.warning("%s: cookies file %s is missing, continuing without", rule.name, rule.cookies)
                return await attempt(None)
            return await self._with_cookies(src, scratch, attempt)
        try:
            return await attempt(None)
        except _ToolError as first:
            if rule.cookies_policy is not CookiesPolicy.ON_AUTH_ERROR or not first.auth or src is None:
                raise
            log.info("%s: %s, retrying once with cookies", rule.name, first.reason)
            try:
                return await self._with_cookies(src, scratch, attempt)
            except DownloadError as second:
                detail = f"{first.detail}\n\n[retry with cookies]\n{second.detail}"
                raise _ToolError(second.reason, detail, user_text=second.user_text, cause=second.cause) from second

    @staticmethod
    async def _with_cookies[T](src: Path, scratch: Path, attempt: Callable[[Path | None], Awaitable[T]]) -> T:
        # The tools write the jar back, so each invocation gets a private copy and /secrets stays untouched.
        scratch.mkdir(parents=True, exist_ok=True)
        copy = scratch / "cookies.txt"
        fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as dst, src.open("rb") as s:
            shutil.copyfileobj(s, dst)
        try:
            return await attempt(copy)
        finally:
            copy.unlink(missing_ok=True)

    def _error(self, run: _Run, meta: Path | None = None) -> _ToolError:
        detail = run.detail()
        if meta is not None and any("does not pass filter" in n for n in run.notes):
            lines = _read_lines(meta)
            status, is_live, duration = ((lines[-1] if lines else "").split("|") + ["", ""])[:3]
            if status in ("is_live", "is_upcoming") or is_live == "True":
                return _ToolError(Reason.LIVE, detail)
            if (d := _num(duration)) and d > self.settings.max_duration:
                return self._too_long(d, detail)
        if any("max-filesize" in n for n in run.notes):
            return _ToolError(Reason.TOO_LARGE, detail)
        reason, auth = _diagnose(run.stderr)
        return _ToolError(reason, detail, auth=auth, cause=_cause(run.stderr))

    def _too_long(self, duration: float, detail: str = "") -> _ToolError:
        limit = self.settings.max_duration
        return _ToolError(
            Reason.TOO_LONG,
            f"duration {duration:.0f}s > {limit}s\n{detail}".strip(),
            user_text=f"This is longer than the {limit / 3600:g}-hour limit.",
        )

    def _check_size(self, path: Path) -> None:
        size = path.stat().st_size
        if size > self.settings.max_upload_bytes:
            raise DownloadError(Reason.TOO_LARGE, f"{path.name}: {size} bytes > {self.settings.max_upload_bytes}")

    # ------------------------------------------------------------ yt-dlp

    async def _probe(self, rule: SourceRule, url: str, egress: str) -> Probe:
        proxy = self._proxy(egress)
        async with self._probe_sem:
            deadline = _deadline(self.settings.probe_timeout)
            with tempfile.TemporaryDirectory(prefix="probe-") as tmp:
                scratch = Path(tmp)

                async def attempt(cookies: Path | None) -> dict[str, Any]:
                    # Two items are enough to tell a playlist from a single entry.
                    cmd = self._ytdlp_cmd(rule, proxy, cookies, ["-J", "--playlist-items", "1:2"], url)
                    run = await _exec(cmd, timeout=_remaining(deadline), keep_stdout=True, cwd=scratch)
                    for line in reversed(run.stdout):
                        if line.startswith("{") and (data := _parse_json(line)):
                            return data
                    raise self._error(run)

                data = await self._attempts(rule, scratch, attempt)
        return self._build_probe(data)

    def _build_probe(self, data: dict[str, Any]) -> Probe:
        d = _single_entry(data)
        info = MediaInfo.from_ytdlp(d)
        if info.is_live:
            raise DownloadError(Reason.LIVE, info.webpage_url)
        duration = _num(d.get("duration"))
        if duration and duration > self.settings.max_duration:
            raise self._too_long(duration, info.webpage_url)
        listed = [f for f in (d.get("formats") or [d]) if isinstance(f, dict)]
        formats = [f for f in listed if _usable(f)]
        if not formats:
            if any(f.get("has_drm") for f in listed):
                raise DownloadError(Reason.DRM, f"{info.webpage_url}: every format is DRM-protected")
            raise DownloadError(Reason.NO_MEDIA, f"{info.webpage_url}: no usable formats")
        has_audio = any(f.get("acodec") != "none" for f in formats)
        qualities = self._qualities(d, formats, duration)
        if not qualities and not has_audio:
            raise DownloadError(
                Reason.TOO_LARGE, f"{info.webpage_url}: every quality is over {self.settings.offer_limit_mb} MB"
            )
        return Probe(info=info, qualities=qualities, has_audio=has_audio)

    def _qualities(self, d: dict[str, Any], formats: list[dict[str, Any]], duration: float | None) -> list[Quality]:
        videos = [f for f in formats if _is_video(f)]
        if not videos:
            return []
        limit = self.settings.offer_limit_bytes
        by_height: dict[int, list[dict[str, Any]]] = {}
        for f in videos:
            h = f.get("height")
            if isinstance(h, int) and h >= 144:
                by_height.setdefault(h, []).append(f)
        if not by_height:
            size, exact = _size(d, duration)  # top level describes yt-dlp's own default pick
            best = Quality("best", "Best quality" + _size_label(size, exact), None, "bv*+ba/b", size)
            return [best] if size is None or size <= limit else []
        audio = _best_audio(formats)
        out: list[Quality] = []
        for h in sorted(by_height, reverse=True):
            q = _quality(h, by_height[h], audio, duration)
            if q.size_bytes is None or q.size_bytes <= limit:
                out.append(q)
        return out[:_MAX_QUALITIES]

    async def _ytdlp_fetch(
        self,
        rule: SourceRule,
        url: str,
        workdir: Path,
        progress: Progress,
        args: list[str],
        *,
        egress: str,
        deadline: float,
        guard: bool = True,
        playlist: bool = False,
    ) -> list[Path]:
        """A yt-dlp download; returns the final files in download order. `guard` refuses live and too-long media."""
        proxy = self._proxy(egress)
        workdir.mkdir(parents=True, exist_ok=True)
        files_txt, sizes_txt, meta_txt = workdir / ".files", workdir / ".sizes", workdir / ".meta"
        full = [
            *args,
            "--no-simulate",
            "--progress",  # keeps progress lines even if a print option makes yt-dlp quiet
            "--progress-template", _PROGRESS_TEMPLATE,
            "--print-to-file", "before_dl:%(requested_formats.:.{format_id,filesize,filesize_approx})j", _tmpl(sizes_txt),
            "--print-to-file", "after_move:%(filepath)s", _tmpl(files_txt),
        ]
        if guard:
            # pre_process runs before the match filter, so a rejection can be explained from this line.
            full += [
                "--match-filters", f"!is_live & live_status!=?is_upcoming & duration<=?{self.settings.max_duration}",
                "--print-to-file", "pre_process:%(live_status)s|%(is_live)s|%(duration)s", _tmpl(meta_txt),
            ]

        async def attempt(cookies: Path | None) -> list[Path]:
            for p in (files_txt, sizes_txt, meta_txt):
                p.unlink(missing_ok=True)
            cmd = self._ytdlp_cmd(rule, proxy, cookies, full, url, playlist=playlist)
            run = await _exec(cmd, timeout=_remaining(deadline), on_line=_Tracker(progress, sizes_txt), cwd=workdir)
            files = list(dict.fromkeys(p for p in map(Path, _read_lines(files_txt)) if p.is_file()))
            if not files:
                raise self._error(run, meta_txt if guard else None)
            if run.code:
                log.warning("yt-dlp exited %d with %d file(s) for a %s download", run.code, len(files), rule.name)
            return files

        progress.set(Stage.DOWNLOADING)
        return await self._attempts(rule, workdir, attempt)

    async def _faststart(self, path: Path, progress: Progress, deadline: float) -> Path:
        """Makes a streamable MP4 (moov first) by remuxing only; keeps the original when that fails."""
        if _ext(path) == "mp4" and _moov_first(path) is not False:
            return path
        progress.set(Stage.PROCESSING)
        tmp = path.with_name(f"{path.stem}.remux.mp4")
        cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
            "-map", "0:v?", "-map", "0:a?", "-dn", "-sn", "-c", "copy", "-movflags", "+faststart", str(tmp),
        ]
        run = await _exec(cmd, timeout=_remaining(deadline))
        if run.code == 0 and tmp.is_file() and tmp.stat().st_size:
            final = path.with_suffix(".mp4")
            tmp.replace(final)
            if final != path:
                path.unlink(missing_ok=True)
            return final
        tmp.unlink(missing_ok=True)
        log.warning("faststart remux failed for %s: %s", path.name, " | ".join(run.stderr)[-500:])
        return path

    async def _to_audio(self, path: Path, progress: Progress, deadline: float) -> Path:
        """Keeps M4A/AAC and MP3 untouched; copies AAC/MP3 out of other containers; encodes the rest to AAC."""
        probe = ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name"]
        run = await _exec([*probe, "-of", "default=nw=1:nk=1", str(path)], timeout=_remaining(deadline), keep_stdout=True)
        codec = next((s.strip() for s in run.stdout if s.strip()), None) if run.code == 0 else None
        if codec is None:
            raise _ToolError(Reason.NO_MEDIA, f"{path.name}: no audio stream\n{run.detail()}")
        if (_ext(path), codec) in (("m4a", "aac"), ("mp3", "mp3")):
            return path
        if codec == "mp3":
            target, codec_args = "mp3", ["-c:a", "copy"]
        elif codec == "aac":
            target, codec_args = "m4a", ["-c:a", "copy", "-movflags", "+faststart"]
        else:
            target, codec_args = "m4a", ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
        progress.set(Stage.PROCESSING)
        out = path.with_name(f"audio.{target}")
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-map", "0:a:0", "-vn"]
        run = await _exec([*cmd, *codec_args, str(out)], timeout=_remaining(deadline))
        if run.code != 0 or not out.is_file():
            raise _ToolError(Reason.FAILED, run.detail())
        path.unlink(missing_ok=True)
        return out

    async def _thumb(self, workdir: Path, deadline: float) -> Path | None:
        jpg = workdir / "thumb.jpg"
        if jpg.is_file():
            return jpg
        other = next((p for p in workdir.glob("thumb.*") if _ext(p) in _PHOTO_EXTS), None)
        if other is None:
            return None
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(other), "-frames:v", "1", str(jpg)]
        try:
            run = await _exec(cmd, timeout=min(30.0, _remaining(deadline)))
        except DownloadError:
            return None
        return jpg if run.code == 0 and jpg.is_file() else None

    # ------------------------------------------------------------ posts

    async def _download_post(
        self, rule: SourceRule, url: str, workdir: Path, progress: Progress, egress: str, deadline: float
    ) -> PostFiles:
        tools = rule.post_tools(url) if rule.post_tools else (Tool.YTDLP,)
        errors: list[tuple[Tool, DownloadError]] = []
        for tool in tools:
            fetch = self._gallery_dl if tool is Tool.GALLERYDL else self._ytdlp_post
            try:
                post = await fetch(rule, url, workdir, progress, egress=egress, deadline=deadline)
            except DownloadError as e:
                log.info("%s post via %s failed: %s", rule.name, tool, e.reason)
                errors.append((tool, e))
                if e.reason is Reason.TIMEOUT:
                    break
                continue
            return await self._finish_post(post, progress, deadline)
        raise _most_informative(errors)

    async def _finish_post(self, post: PostFiles, progress: Progress, deadline: float) -> PostFiles:
        items: list[MediaItem] = []
        for item in post.items:
            path = await self._faststart(item.path, progress, deadline) if item.kind is Kind.VIDEO else item.path
            if path.stat().st_size > self.settings.max_upload_bytes:
                log.warning("dropping %s: over the upload limit", path.name)
                continue
            items.append(MediaItem(path, item.kind))
        if not items:
            raise DownloadError(Reason.TOO_LARGE, f"{post.info.webpage_url}: every item is over the upload limit")
        post.items = items
        return post

    async def _gallery_dl(
        self, rule: SourceRule, url: str, workdir: Path, progress: Progress, *, egress: str, deadline: float
    ) -> PostFiles:
        proxy = self._proxy(egress)
        folder = workdir / "post"
        base = [
            "gallery-dl", "--config-ignore", "-D", str(folder), "-f", _GDL_FILENAME, "--write-metadata",
            "--http-timeout", "30", "--retries", "3", "--sleep-request", _GDL_SLEEP,
            # Pre-merged MP4s: one plain HTTP download instead of DASH through the ytdl module.
            "-o", "extractor.instagram.videos=merged",
        ]
        if proxy:
            base += ["--proxy", proxy]

        async def attempt(cookies: Path | None) -> PostFiles:
            _reset_dir(folder)
            cmd = [*base, *(["--cookies", str(cookies)] if cookies else []), "--", url]
            run = await _exec(cmd, timeout=_remaining(deadline), on_line=_ItemCounter(progress, folder), cwd=workdir)
            items = _collect_media(folder)
            if not items:
                raise self._gallery_dl_error(run)
            if run.code:
                log.warning("gallery-dl exited %d with %d item(s) for a %s post", run.code, len(items), rule.name)
            meta = _first_json(folder)
            return _post_files(
                rule, url, items, caption=meta.get("description"), uploader=meta.get("username"),
                date=meta.get("date"), media_id=meta.get("post_shortcode") or meta.get("shortcode"),
            )

        progress.set(Stage.DOWNLOADING)
        return await self._attempts(rule, workdir, attempt)

    @staticmethod
    def _gallery_dl_error(run: _Run) -> _ToolError:
        reason, auth = _diagnose(run.stderr)
        if reason is Reason.FAILED:
            # The exit status ORs exception codes: 4 extraction/HTTP, 8 challenge, 16 auth, 32 input, 64 (older) no extractor.
            if run.code == 0:
                reason = Reason.NO_MEDIA
            elif run.code & 16:
                reason, auth = Reason.LOGIN_REQUIRED, True
            elif run.code & 8:
                reason = Reason.BLOCKED
            elif run.code & 64:
                reason = Reason.UNSUPPORTED
        return _ToolError(reason, run.detail(), auth=auth, cause=_cause(run.stderr))

    async def _ytdlp_post(
        self, rule: SourceRule, url: str, workdir: Path, progress: Progress, *, egress: str, deadline: float
    ) -> PostFiles:
        # A carousel comes back as a playlist: download every entry (no --no-playlist), skip the ones that fail.
        folder = workdir / "post"
        meta_txt = workdir / ".post.jsonl"
        _reset_dir(folder)
        meta_txt.unlink(missing_ok=True)
        args = [
            "-f", "bv*+ba/b", "--merge-output-format", "mp4", "--postprocessor-args", _FASTSTART,
            "--ignore-errors", "--playlist-items", "1:20",
            "-o", f"{_tmpl(folder)}/%(playlist_index|0)s_%(id)s.%(ext)s",
            "--print-to-file", "after_move:%(.{id,description,channel,uploader,upload_date})j", _tmpl(meta_txt),
        ]
        files = await self._ytdlp_fetch(
            rule, url, workdir, progress, args, egress=egress, deadline=deadline, guard=False, playlist=True
        )
        items = [MediaItem(p, kind) for p in files if (kind := _kind(p))]
        if not items:
            raise _ToolError(Reason.NO_MEDIA, "yt-dlp produced no photo or video files: " + ", ".join(p.name for p in files))
        metas = [m for m in map(_parse_json, _read_lines(meta_txt)) if m]
        meta = next((m for m in metas if m.get("description")), metas[0] if metas else {})
        return _post_files(
            rule, url, items, caption=meta.get("description"), uploader=meta.get("channel") or meta.get("uploader"),
            date=meta.get("upload_date"), media_id=meta.get("id"),
        )
