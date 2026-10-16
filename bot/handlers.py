"""User-facing router: commands, link messages, the quality picker and cancel buttons."""

from __future__ import annotations

import html
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiogram import BaseMiddleware, Router
from aiogram.enums import ChatType, MessageEntityType
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    Chat,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    User,
)

from . import pipeline, tag_editor
from .context import Ctx
from .models import USER_TEXT, DownloadError, MediaRef, Probe, Quality, Reason
from .sources import Mode, SourceRule

log = logging.getLogger(__name__)
router = Router(name="user")

START_TEXT = (
    "👋 Send me a link and I'll send the file back.\n\n"
    "Works with YouTube, Instagram, SoundCloud, Spotify tracks and most other video sites. /help for details."
)
HELP_TEXT = (
    "<b>How it works</b>\n"
    "Send a link, or several in one message.\n\n"
    "• <b>YouTube</b> and other sites: pick a quality, 🎵 Audio, or 🎛 Audio · custom tags "
    "to set title, artist, album, year, cover and format.\n"
    "• <b>Instagram</b>: every photo and video of the post, reel or story.\n"
    "• <b>SoundCloud</b>: the track as audio.\n"
    "• <b>Spotify</b>: single tracks, found on YouTube Music and tagged with Spotify's title, artist, album and cover.\n\n"
    "⚡ marks formats that arrive instantly from cache.\n"
    "/cancel stops your downloads."
)
HINT_TEXT = "Send me a link from YouTube, Instagram, SoundCloud, Spotify or another video site. /help"
EXPIRED_TEXT = "Expired — send the link again."

_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_TRAILING = ".,;:!?)]}>\"'»”’"


class AccessMiddleware(BaseMiddleware):
    """Private chats only; banned users are ignored and an allowlist, when set, is enforced."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        chat: Chat | None = data.get("event_chat")
        if user is None or chat is None or chat.type != ChatType.PRIVATE:
            return None
        ctx: Ctx = data["ctx"]
        if await ctx.db.touch_user(user.id, user.username):
            return None
        if not ctx.settings.is_allowed(user.id):
            if isinstance(event, Message):
                await event.answer("This bot is private.")
            elif isinstance(event, CallbackQuery):
                await pipeline.answer(event, "This bot is private.", alert=True)
            return None
        return await handler(event, data)


async def on_error(event: ErrorEvent, ctx: Ctx) -> bool:
    update = event.update
    user = (update.message and update.message.from_user) or (
        update.callback_query and update.callback_query.from_user
    )
    await ctx.alerts.error(
        f"handler:{type(event.exception).__name__}", "Unhandled error in an update handler", event.exception,
        user=user.id if user else None, update=update.update_id,
    )
    return True


# Commands


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, ctx: Ctx, state: FSMContext, event_from_user: User) -> None:
    editors = await tag_editor.close_user(ctx, event_from_user.id)
    jobs = ctx.jobs.cancel_user(event_from_user.id)
    await state.clear()
    n = editors + jobs
    await message.answer(f"✖ Cancelled {n} job{'s' if n != 1 else ''}." if n else "Nothing to cancel.")


@router.callback_query(pipeline.CancelJob.filter())
async def on_cancel_job(
    cb: CallbackQuery, callback_data: pipeline.CancelJob, ctx: Ctx, event_from_user: User
) -> None:
    if ctx.jobs.cancel(callback_data.job_id, user_id=event_from_user.id):
        await pipeline.answer(cb, "Cancelling…")
    else:
        await pipeline.answer(cb, "This job has already finished.")


# Links


def extract_urls(message: Message) -> list[str]:
    if message.text is not None:
        text, entities = message.text, message.entities
    else:
        text, entities = message.caption or "", message.caption_entities
    found: list[str] = []
    for e in entities or []:
        if e.type == MessageEntityType.URL:
            found.append(e.extract_from(text))
        elif e.type == MessageEntityType.TEXT_LINK and e.url:
            found.append(e.url)
    if not found:
        found = [m.group().rstrip(_TRAILING) for m in _URL_RE.finditer(text)]
    urls = (u.strip() for u in found)
    # Keep http(s) and scheme-less links ("youtu.be/…"); drop mailto:, tg: and the like.
    return list(dict.fromkeys(u for u in urls if u and (u.lower().startswith(("http://", "https://")) or not _SCHEME_RE.match(u))))


@router.message()
async def on_message(message: Message, ctx: Ctx, event_from_user: User) -> None:
    urls = extract_urls(message)
    if not urls:
        await message.answer(HINT_TEXT)
        return
    limit = ctx.settings.max_links_per_message
    if len(urls) > limit:
        await message.reply(f"Only the first {limit} links are handled.")
        urls = urls[:limit]
    for url in urls:
        ctx.spawn(_handle_link(ctx, message, event_from_user.id, url, multi=len(urls) > 1), name="link")


async def _handle_link(ctx: Ctx, message: Message, user_id: int, url: str, *, multi: bool) -> None:
    try:
        try:
            rule, ref = ctx.sources.match(url)
        except DownloadError as e:
            prefix = f"<code>{html.escape(pipeline.truncate(url, 80))}</code>: " if multi else ""
            await message.reply(prefix + html.escape(e.user_text))
            return
        if rule.mode is Mode.PICK:
            await _open_picker(ctx, message, user_id, rule, ref)
            return
        fmt, flow = {
            Mode.AUDIO: ("audio", pipeline.audio_flow),
            Mode.MATCH: ("audio", pipeline.match_flow),
            Mode.POST: ("post", pipeline.post_flow),
        }[rule.mode]
        chat_id = message.chat.id
        if await pipeline.send_from_cache(
            ctx, rule, ref, fmt, user_id=user_id, chat_id=chat_id, reply_to=message.message_id
        ):
            return
        await pipeline.enqueue(
            ctx, rule=rule, ref=ref, fmt=fmt, user_id=user_id, chat_id=chat_id,
            reply_to=message.message_id, flow=flow,
        )
    except TelegramForbiddenError:
        log.info("user %s blocked the bot", user_id)
    except Exception as e:
        await ctx.alerts.error(f"link:{type(e).__name__}", "Link handling crashed", e, user=user_id, url=url)
        try:
            await message.reply(pipeline.GENERIC_FAILURE)
        except Exception:
            pass


# Quality picker


class PickCb(CallbackData, prefix="pk"):
    t: str  # picker token
    a: str  # "v" video quality, "a" audio, "c" audio with custom tags, "x" dismiss
    i: int = 0  # quality index for "v"


@dataclass(slots=True, eq=False)
class Picker:
    rule: SourceRule
    ref: MediaRef
    probe: Probe
    user_id: int
    chat_id: int
    message_id: int
    reply_to: int
    created_at: float = field(default_factory=time.monotonic)


_pickers: dict[str, Picker] = {}


def fmt_duration(seconds: float | None) -> str | None:
    if not seconds:
        return None
    h, rest = divmod(int(round(seconds)), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


async def _open_picker(ctx: Ctx, message: Message, user_id: int, rule: SourceRule, ref: MediaRef) -> None:
    status = await message.reply("🔎 Reading formats…")
    chat_id = message.chat.id
    try:
        probe = await ctx.downloader.probe(rule, ref.url)
        if probe.info.is_live:
            raise DownloadError(Reason.LIVE, ref.url)
        if probe.info.duration and probe.info.duration > ctx.settings.max_duration:
            raise DownloadError(Reason.TOO_LONG, f"{probe.info.duration:.0f}s")
    except DownloadError as e:
        await pipeline.edit_message(ctx.bot, chat_id, status.message_id, pipeline.failure_text(e))
        if not e.expected:
            await ctx.alerts.error(
                f"{rule.name}:{e.reason}", f"Probe failed ({e.reason})", e, user=user_id, source=rule.name, url=ref.url,
                cookies=rule.cookies_problem(),
            )
        return
    except Exception as e:
        await pipeline.edit_message(ctx.bot, chat_id, status.message_id, pipeline.GENERIC_FAILURE)
        await ctx.alerts.error(
            f"{rule.name}:probe", "Probe crashed", e, user=user_id, source=rule.name, url=ref.url
        )
        return
    if not probe.qualities and not probe.has_audio:
        await pipeline.edit_message(
            ctx.bot, chat_id, status.message_id, "⚠️ " + html.escape(USER_TEXT[Reason.NO_MEDIA])
        )
        return

    token = secrets.token_urlsafe(6)
    picker = Picker(rule, ref, probe, user_id, chat_id, status.message_id, message.message_id)
    _pickers[token] = picker
    text, markup = await _picker_view(ctx, token, picker)
    await pipeline.edit_message(ctx.bot, chat_id, status.message_id, text, markup)


async def _picker_view(ctx: Ctx, token: str, p: Picker) -> tuple[str, InlineKeyboardMarkup]:
    info = p.probe.info
    lines = [f"<b>{pipeline.link(info.webpage_url or p.ref.url, pipeline.truncate(info.title, 200))}</b>"]
    meta = [html.escape(m) for m in (info.uploader, fmt_duration(info.duration)) if m]
    if meta:
        lines.append(" · ".join(meta))

    def button(text: str, action: str, i: int = 0) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=PickCb(t=token, a=action, i=i).pack())

    keys = [p.ref.cache_key(q.key) for q in p.probe.qualities] + [p.ref.cache_key("audio")]
    cached = await ctx.db.cached_keys(keys)

    def bolt(fmt: str) -> str:
        return "⚡ " if p.ref.cache_key(fmt) in cached else ""

    videos = [button(bolt(q.key) + q.label, "v", i) for i, q in enumerate(p.probe.qualities)]
    rows = [videos[i : i + 2] for i in range(0, len(videos), 2)]
    if p.probe.has_audio:
        rows.append([button(bolt("audio") + "🎵 Audio", "a"), button("🎛 Audio · custom tags", "c")])
    rows.append([button("✖", "x")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _picker_expired(ctx: Ctx, p: Picker) -> bool:
    return time.monotonic() - p.created_at > ctx.settings.picker_ttl


async def _expire_picker(ctx: Ctx, token: str) -> None:
    if (p := _pickers.pop(token, None)) is not None:
        await pipeline.edit_message(ctx.bot, p.chat_id, p.message_id, EXPIRED_TEXT)


async def expire_pickers(ctx: Ctx) -> None:
    for token, p in list(_pickers.items()):
        if _picker_expired(ctx, p):
            await _expire_picker(ctx, token)


@router.callback_query(PickCb.filter())
async def on_pick(cb: CallbackQuery, callback_data: PickCb, ctx: Ctx, state: FSMContext) -> None:
    token, action = callback_data.t, callback_data.a
    p = _pickers.get(token)
    if p is None or _picker_expired(ctx, p):
        await pipeline.answer(cb, EXPIRED_TEXT)
        await _expire_picker(ctx, token)
        return
    if action == "x":
        _pickers.pop(token, None)
        await pipeline.answer(cb)
        await pipeline.delete_message(ctx.bot, p.chat_id, p.message_id)
        return

    quality: Quality | None = None
    if action == "v" and 0 <= callback_data.i < len(p.probe.qualities):
        quality = p.probe.qualities[callback_data.i]
        fmt = quality.key
    elif action in ("a", "c") and p.probe.has_audio:
        fmt = "audio"
    else:
        await pipeline.answer(cb)
        return

    _pickers.pop(token, None)  # taken before any await, so a double tap can't start two jobs
    if action != "c" and await pipeline.send_from_cache(
        ctx, p.rule, p.ref, fmt, user_id=p.user_id, chat_id=p.chat_id, reply_to=p.reply_to
    ):
        await pipeline.answer(cb)
        await pipeline.delete_message(ctx.bot, p.chat_id, p.message_id)
        return
    if not ctx.jobs.can_submit(p.user_id):
        _pickers[token] = p
        await pipeline.answer(cb, pipeline.queue_full_text(ctx, p.user_id), alert=True)
        return
    await pipeline.answer(cb)

    if action == "c":
        await tag_editor.open_editor(
            ctx, state, rule=p.rule, ref=p.ref, probe=p.probe, user_id=p.user_id, chat_id=p.chat_id,
            message_id=p.message_id, reply_to=p.reply_to,
        )
        return
    await pipeline.enqueue(
        ctx, rule=p.rule, ref=p.ref, fmt=fmt, user_id=p.user_id, chat_id=p.chat_id, reply_to=p.reply_to,
        flow=pipeline.video_flow if quality else pipeline.audio_flow, status_message_id=p.message_id,
        data={"quality": quality} if quality else None,
    )

