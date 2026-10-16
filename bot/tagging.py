"""Audio tags and cover art (mutagen MP4 / ID3, Pillow)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
from asyncio.subprocess import DEVNULL, PIPE
from dataclasses import dataclass
from pathlib import Path

from mutagen.id3 import APIC, TALB, TIT2, TPE1, TYER, PictureType
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image, ImageOps

from .models import AudioFile, DownloadError, MediaInfo, Reason, Tags

log = logging.getLogger(__name__)

AUDIO_FORMATS = ("m4a", "mp3")
COVER_SIDE = 1200
THUMB_SIDE = 320
THUMB_MAX_BYTES = 200 * 1024
# Errors Pillow raises for unreadable or hostile images.
IMAGE_ERRORS = (OSError, ValueError, Image.DecompressionBombError)

_ENCODERS = {
    "mp3": ("-c:a", "libmp3lame", "-q:a", "0"),
    "m4a": ("-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"),
}


@dataclass(slots=True)
class PreparedAudio:
    path: Path  # final tagged .m4a or .mp3
    tags: Tags
    thumb: Path | None  # small JPEG for sendAudio's `thumbnail`
    duration: float | None


def default_tags(info: MediaInfo) -> Tags:
    artist = (info.artist or info.uploader or "").strip().removesuffix(" - Topic")
    year = str(info.release_year) if info.release_year else (info.upload_date or "")[:4]
    return Tags(
        title=(info.track or info.title).strip(),
        artist=artist.strip(),
        album=(info.album or "").strip(),
        year=year,
    )


async def prepare_audio(
    audio: AudioFile, tags: Tags, *, fmt: str | None = None, cover_src: Path | None, crop: bool = True
) -> PreparedAudio:
    """fmt: None keeps the downloaded container (m4a or mp3); "m4a" / "mp3" converts when it differs."""
    path = await _convert(audio.path, fmt)
    cover = thumb = None
    if cover_src is not None:
        try:
            cover, thumb = await asyncio.to_thread(_make_cover, cover_src, path, crop)
        except IMAGE_ERRORS as e:
            log.warning("cover %s is unusable, sending without one: %s", cover_src, e)
    duration = await asyncio.to_thread(_write_tags, path, tags, cover)
    return PreparedAudio(path=path, tags=tags, thumb=thumb, duration=duration)


async def run_tool(*cmd: str) -> bytes:
    """Runs a command (ffmpeg, ffprobe) and returns its stdout; a non-zero exit raises DownloadError(FAILED)."""
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=DEVNULL, stdout=PIPE, stderr=PIPE)
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    if proc.returncode:
        raise DownloadError(Reason.FAILED, f"{cmd[0]} exited with {proc.returncode}: {err.decode(errors='replace')}")
    return out


def to_rgb(im: Image.Image) -> Image.Image:
    """RGB version for JPEG output; transparency is flattened onto white."""
    if im.mode == "RGB":
        return im
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        flat = Image.new("RGB", im.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        return flat
    return im.convert("RGB")


def save_thumbnail(im: Image.Image, dst: Path) -> Path:
    """Writes a JPEG within 320x320 and under 200 KB, the limits Telegram sets for thumbnails."""
    small = to_rgb(im.copy())
    small.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.Resampling.LANCZOS)
    data = b""
    for quality in (85, 70, 50, 30):
        data = _jpeg(small, quality)
        if len(data) < THUMB_MAX_BYTES:
            break
    dst.write_bytes(data)
    return dst


def make_thumbnail(src: Path, dst: Path) -> Path:
    with Image.open(src) as im:
        return save_thumbnail(ImageOps.exif_transpose(im), dst)


def _jpeg(im: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


async def _convert(src: Path, fmt: str | None) -> Path:
    have = src.suffix.lower().lstrip(".")
    want = (fmt or have).lower().lstrip(".")
    if want not in AUDIO_FORMATS:
        if fmt:
            raise ValueError(f"unsupported audio format {fmt!r}")
        want = "m4a"
    if want == have:
        return src
    dst = src.with_suffix(f".{want}")
    log.info("converting %s to %s", src.name, want)
    await run_tool(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        "-map", "0:a:0", "-map_metadata", "-1", *_ENCODERS[want], str(dst),
    )
    return dst


def _make_cover(src: Path, audio: Path, crop: bool) -> tuple[Path, Path]:
    with Image.open(src) as im:
        img = to_rgb(ImageOps.exif_transpose(im))
    if crop:
        w, h = img.size
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
    img.thumbnail((COVER_SIDE, COVER_SIDE), Image.Resampling.LANCZOS)
    cover = audio.with_name(f"{audio.stem}.cover.jpg")
    cover.write_bytes(_jpeg(img, 90))
    return cover, save_thumbnail(img, audio.with_name(f"{audio.stem}.thumb.jpg"))


def _write_tags(path: Path, tags: Tags, cover: Path | None) -> float | None:
    art = cover.read_bytes() if cover else None
    if path.suffix.lower() == ".mp3":
        return _tag_mp3(path, tags, art)
    return _tag_mp4(path, tags, art)


def _tag_mp4(path: Path, tags: Tags, art: bytes | None) -> float | None:
    f = MP4(path)
    if f.tags is None:
        f.add_tags()
    assert f.tags is not None
    f.tags.clear()
    for key, value in (("©nam", tags.title), ("©ART", tags.artist), ("©alb", tags.album), ("©day", tags.year)):
        if v := value.strip():
            f.tags[key] = [v]
    if art:
        f.tags["covr"] = [MP4Cover(art, imageformat=MP4Cover.FORMAT_JPEG)]
    f.save()
    return f.info.length or None


def _tag_mp3(path: Path, tags: Tags, art: bytes | None) -> float | None:
    f = MP3(path)
    if f.tags is None:
        f.add_tags()
    id3 = f.tags
    assert id3 is not None
    id3.clear()
    for frame, value in ((TIT2, tags.title), (TPE1, tags.artist), (TALB, tags.album), (TYER, tags.year)):
        if v := value.strip():
            id3.add(frame(encoding=3, text=[v]))
    if art:
        id3.add(APIC(encoding=0, mime="image/jpeg", type=PictureType.COVER_FRONT, desc="", data=art))
    # v2.3 for the widest player support; mutagen writes UTF-8 frames as UTF-16 there.
    id3.update_to_v23()
    f.save(v2_version=3, v1=0)
    return f.info.length or None
