"""Sending files to Telegram through the local Bot API server: send method, thumbnails, albums, size checks."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import (
    SendAnimation,
    SendAudio,
    SendDocument,
    SendMediaGroup,
    SendPhoto,
    SendVideo,
    TelegramMethod,
)
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo, MediaUnion, Message, ReplyParameters
from PIL import Image, ImageOps

from .config import Settings
from .models import DownloadError, Kind, MediaItem, PostFiles, Reason, Sent, VideoFile
from .tagging import IMAGE_ERRORS, THUMB_SIDE, PreparedAudio, make_thumbnail, run_tool, to_rgb

log = logging.getLogger(__name__)

ALBUM_MAX = 10
SEND_TRIES = 3
PHOTO_MAX_BYTES = 10 * 1024 * 1024
PHOTO_MAX_SIDES = 10_000  # width + height
PHOTO_MAX_RATIO = 20
PHOTO_FORMATS = {"JPEG", "PNG", "WEBP"}


@dataclass(slots=True)
class _VideoMeta:
    duration: int | None
    width: int | None
    height: int | None
    thumb: FSInputFile | None


class Delivery:
    def __init__(self, bot: Bot, settings: Settings):
        self.bot = bot
        self.settings = settings

    async def send_video(
        self, chat_id: int, video: VideoFile, *, caption: str, workdir: Path, reply_to: int | None = None
    ) -> Sent:
        size = self._checked_size(video.path)
        meta = await _video_meta(video.path, video.thumb, workdir / f"tg-thumb-{video.path.stem}.jpg", video.info.duration)
        msg = await self._send(
            SendVideo(
                chat_id=chat_id,
                video=_uri(video.path),
                caption=caption or None,
                duration=meta.duration,
                width=meta.width,
                height=meta.height,
                thumbnail=meta.thumb,
                supports_streaming=True,
            ),
            reply_to,
        )
        kind, file_id = _media_of(msg)
        return Sent(kind=kind, file_id=file_id, size_bytes=size, caption=caption)

    async def send_audio(
        self, chat_id: int, audio: PreparedAudio, *, caption: str, reply_to: int | None = None
    ) -> Sent:
        size = self._checked_size(audio.path)
        msg = await self._send(
            SendAudio(
                chat_id=chat_id,
                audio=_uri(audio.path),
                caption=caption or None,
                title=audio.tags.title or None,
                performer=audio.tags.artist or None,
                duration=round(audio.duration) if audio.duration else None,
                thumbnail=FSInputFile(audio.thumb) if audio.thumb and audio.thumb.is_file() else None,
            ),
            reply_to,
        )
        kind, file_id = _media_of(msg)
        return Sent(kind=kind, file_id=file_id, size_bytes=size, caption=caption)

    async def send_post(
        self, chat_id: int, post: PostFiles, *, caption: str, workdir: Path, reply_to: int | None = None
    ) -> Sent:
        items: list[MediaItem] = []
        for item in post.items:
            if (size := item.path.stat().st_size) > self.settings.max_upload_bytes:
                log.info("skipping %s: %d bytes is over the upload limit", item.path.name, size)
            else:
                items.append(item)
        if not items:
            raise DownloadError(Reason.TOO_LARGE, f"all {len(post.items)} post items are over the upload limit")

        if len(items) == 1:
            item = items[0]
            if item.kind is not Kind.PHOTO:
                video = VideoFile(path=item.path, info=post.info)
                return await self.send_video(chat_id, video, caption=caption, workdir=workdir, reply_to=reply_to)
            path = await asyncio.to_thread(_fit_photo, item.path, workdir / "tg-photo-0.jpg")
            msg = await self._send(SendPhoto(chat_id=chat_id, photo=_uri(path), caption=caption or None), reply_to)
            kind, file_id = _media_of(msg)
            return Sent(kind=kind, file_id=file_id, size_bytes=path.stat().st_size, caption=caption)

        media: list[MediaUnion] = []
        total = 0
        for i, item in enumerate(items):
            item_caption = (caption or None) if i == 0 else None
            if item.kind is Kind.PHOTO:
                path = await asyncio.to_thread(_fit_photo, item.path, workdir / f"tg-photo-{i}.jpg")
                media.append(InputMediaPhoto(media=_uri(path), caption=item_caption))
            else:
                path = item.path
                meta = await _video_meta(path, None, workdir / f"tg-thumb-{i}.jpg", None)
                media.append(
                    InputMediaVideo(
                        media=_uri(path),
                        caption=item_caption,
                        thumbnail=meta.thumb,
                        width=meta.width,
                        height=meta.height,
                        duration=meta.duration,
                        supports_streaming=True,
                    )
                )
            total += path.stat().st_size

        entries: list[dict[str, str]] = []
        for msg in await self._send_album(chat_id, media, reply_to):
            kind, file_id = _media_of(msg)
            entries.append({"type": "photo" if kind is Kind.PHOTO else "video", "file_id": file_id})
        return Sent(kind=Kind.ALBUM, file_id=json.dumps(entries), size_bytes=total, caption=caption)

    async def send_cached(self, chat_id: int, sent: Sent, *, reply_to: int | None = None) -> None:
        """Re-sends by file_id. TelegramBadRequest (e.g. a stale file_id) propagates to the caller."""
        caption = sent.caption or None
        method: TelegramMethod[Message]
        match sent.kind:
            case Kind.ALBUM:
                media: list[MediaUnion] = [
                    InputMediaPhoto(media=e["file_id"], caption=caption if i == 0 else None)
                    if e["type"] == "photo"
                    else InputMediaVideo(media=e["file_id"], caption=caption if i == 0 else None, supports_streaming=True)
                    for i, e in enumerate(json.loads(sent.file_id))
                ]
                await self._send_album(chat_id, media, reply_to)
                return
            case Kind.VIDEO:
                method = SendVideo(chat_id=chat_id, video=sent.file_id, caption=caption, supports_streaming=True)
            case Kind.AUDIO:
                method = SendAudio(chat_id=chat_id, audio=sent.file_id, caption=caption)
            case Kind.PHOTO:
                method = SendPhoto(chat_id=chat_id, photo=sent.file_id, caption=caption)
            case Kind.ANIMATION:
                method = SendAnimation(chat_id=chat_id, animation=sent.file_id, caption=caption)
            case Kind.DOCUMENT:
                method = SendDocument(chat_id=chat_id, document=sent.file_id, caption=caption)
        await self._send(method, reply_to)

    def _checked_size(self, path: Path) -> int:
        size = path.stat().st_size
        if size > self.settings.max_upload_bytes:
            raise DownloadError(Reason.TOO_LARGE, f"{path.name} is {size} bytes")
        return size

    async def _send_album(self, chat_id: int, media: list[MediaUnion], reply_to: int | None) -> list[Message]:
        msgs: list[Message] = []
        for chunk in _chunks(media):
            msgs += await self._send(SendMediaGroup(chat_id=chat_id, media=chunk), reply_to)
        return msgs

    async def _send[T](self, method: TelegramMethod[T], reply_to: int | None) -> T:
        """The one place that sets reply parameters, the long upload timeout and the flood-control retry."""
        if reply_to:
            reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
            method = method.model_copy(update={"reply_parameters": reply})
        tries = 1
        while True:
            try:
                return await self.bot(method, request_timeout=self.settings.job_timeout)
            except TelegramRetryAfter as e:
                if tries >= SEND_TRIES:
                    raise
                tries += 1
                log.warning("flood control on %s, retrying in %d s", type(method).__name__, e.retry_after)
                await asyncio.sleep(e.retry_after)


def _uri(path: Path) -> str:
    return path.resolve().as_uri()


def _chunks[T](items: list[T]) -> list[list[T]]:
    # Near-equal chunks of at most ALBUM_MAX: sendMediaGroup needs 2+ items, so 11 goes as 6 + 5, not 10 + 1.
    n = len(items)
    k = -(-n // ALBUM_MAX)
    return [items[i * n // k : (i + 1) * n // k] for i in range(k)]


def _media_of(msg: Message) -> tuple[Kind, str]:
    if msg.video:
        return Kind.VIDEO, msg.video.file_id
    if msg.audio:
        return Kind.AUDIO, msg.audio.file_id
    if msg.photo:
        return Kind.PHOTO, msg.photo[-1].file_id
    if msg.animation:  # checked before document, which Telegram also sets for animations
        return Kind.ANIMATION, msg.animation.file_id
    if msg.document:
        return Kind.DOCUMENT, msg.document.file_id
    raise DownloadError(Reason.FAILED, f"Telegram answered without media (message {msg.message_id})")


def _num(value: Any) -> float | None:
    try:
        return float(value) or None
    except (TypeError, ValueError):
        return None


async def _ffprobe(path: Path) -> tuple[float | None, int | None, int | None]:
    """Duration, display width and height of the first video stream."""
    try:
        out = await run_tool(
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-show_format",
            "-of", "json", str(path),
        )
        data = json.loads(out)
    except (DownloadError, OSError, ValueError) as e:
        log.warning("ffprobe failed on %s: %s", path.name, e)
        return None, None, None
    stream = (data.get("streams") or [{}])[0]
    duration = _num((data.get("format") or {}).get("duration")) or _num(stream.get("duration"))
    width, height = stream.get("width"), stream.get("height")
    rotation = next(
        (sd["rotation"] for sd in stream.get("side_data_list") or [] if "rotation" in sd),
        (stream.get("tags") or {}).get("rotate"),
    )
    if width and height and abs(int(_num(rotation) or 0)) % 180 == 90:
        width, height = height, width
    return duration, width, height


async def _video_meta(video: Path, thumb_src: Path | None, thumb_dst: Path, fallback_duration: float | None) -> _VideoMeta:
    duration, width, height = await _ffprobe(video)
    duration = duration or fallback_duration
    thumb = await _video_thumb(video, thumb_src, thumb_dst, duration)
    return _VideoMeta(
        duration=round(duration) if duration else None,
        width=width,
        height=height,
        thumb=FSInputFile(thumb) if thumb else None,
    )


async def _video_thumb(video: Path, src: Path | None, dst: Path, duration: float | None) -> Path | None:
    if src and src.is_file():
        try:
            return await asyncio.to_thread(make_thumbnail, src, dst)
        except IMAGE_ERRORS as e:
            log.warning("thumbnail %s is unusable, grabbing a frame: %s", src.name, e)
    # A frame at 10% of the video; short clips with a wrong duration fall back to the first frame.
    for at in dict.fromkeys((duration * 0.1 if duration else 1.0, 0.0)):
        dst.unlink(missing_ok=True)
        try:
            await run_tool(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.2f}", "-i", str(video),
                "-frames:v", "1", "-vf", f"scale={THUMB_SIDE}:{THUMB_SIDE}:force_original_aspect_ratio=decrease",
                "-q:v", "4", str(dst),
            )
        except (DownloadError, OSError) as e:
            log.warning("frame grab at %.2fs failed on %s: %s", at, video.name, e)
            continue
        if dst.is_file() and dst.stat().st_size:
            return dst
    return None


def _fit_photo(src: Path, dst: Path) -> Path:
    """src when sendPhoto takes it as is, else a JPEG at dst within sendPhoto's size and shape limits."""
    try:
        with Image.open(src) as im:
            w, h = im.size
            if (
                im.format in PHOTO_FORMATS
                and src.stat().st_size <= PHOTO_MAX_BYTES
                and w + h <= PHOTO_MAX_SIDES
                and max(w, h) <= PHOTO_MAX_RATIO * min(w, h)
            ):
                return src
            img = to_rgb(ImageOps.exif_transpose(im))
    except IMAGE_ERRORS as e:
        log.warning("can't inspect %s, sending it as is: %s", src.name, e)
        return src

    w, h = img.size
    ratio = PHOTO_MAX_RATIO - 0.5  # slack for rounding when scaling down below
    if max(w, h) > ratio * min(w, h):
        # Downscaling can't fix the aspect ratio, so pad the short side.
        short = math.ceil(max(w, h) / ratio)
        size = (w, short) if w >= h else (short, h)
        canvas = Image.new("RGB", size, (255, 255, 255))
        canvas.paste(img, ((size[0] - w) // 2, (size[1] - h) // 2))
        img = canvas
    if (sides := img.width + img.height) > PHOTO_MAX_SIDES:
        scale = PHOTO_MAX_SIDES / sides
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.Resampling.LANCZOS)
    while True:
        for quality in (90, 80, 70):
            img.save(dst, "JPEG", quality=quality, optimize=True)
            if dst.stat().st_size <= PHOTO_MAX_BYTES:
                log.info("re-encoded %s to %s (%dx%d, q%d)", src.name, dst.name, img.width, img.height, quality)
                return dst
        img = img.resize((max(1, img.width * 3 // 4), max(1, img.height * 3 // 4)), Image.Resampling.LANCZOS)
