"""Playlist and album picker, and the batch that sends the picked tracks one by one.

Each track runs as an ordinary silent job (its own DB row, cache entry and concurrency limits);
the picker message turns into the batch's status message.
"""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from dataclasses import dataclass, field

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import pipeline
from .context import Ctx
from .jobs import Job
from .models import DownloadError, MediaRef, TrackList
from .sources import SourceRule

log = logging.getLogger(__name__)
router = Router(name="playlist")

PAGE_SIZE = 10
LABEL_LIMIT = 56
FAILURES_SHOWN = 30


class ListCb(CallbackData, prefix="pl"):
    t: str  # session token
    a: str  # "t" toggle track i, "p" page i, "all", "none", "go" (picked), "go_all", "x" dismiss, "stop", "-" no-op
    i: int = 0


@dataclass(eq=False)
class ListSession:
    token: str
    tracks: TrackList
    user_id: int
    chat_id: int
    message_id: int  # the picker, then the batch's status
    reply_to: int
    selected: set[int] = field(default_factory=set)
    page: int = 0
    touched: float = field(default_factory=time.monotonic)
    # The batch, once started.
    task: asyncio.Task[None] | None = None
    picked: list[int] = field(default_factory=list)
    current: int | None = None
    job: Job | None = None
    waiting: bool = False  # for room in the user's queue
    sent: int = 0
    failed: list[tuple[int, str]] = field(default_factory=list)  # (track index, why)
    shown: str | None = None


_sessions: dict[str, ListSession] = {}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _label(s: ListSession, i: int) -> str:
    t = s.tracks.items[i]
    return f"{t.title} — {t.artist}" if t.artist else t.title


def _title(s: ListSession) -> str:
    return f"<b>{pipeline.link(s.tracks.url, pipeline.truncate(s.tracks.title, 200))}</b>"


def _button(s: ListSession, text: str, action: str, i: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=ListCb(t=s.token, a=action, i=i).pack())


# Picker


async def open_list(ctx: Ctx, message: Message, user_id: int, rule: SourceRule, ref: MediaRef) -> None:
    assert rule.fetch_list is not None
    status = await message.reply("🔎 Reading the track list…")
    try:
        tracks = await rule.fetch_list(ref.url)
    except Exception as e:
        await pipeline.lookup_failed(
            ctx, rule, ref, "list", e, user_id=user_id, chat_id=message.chat.id, message_id=status.message_id,
            what="Reading the track list",
        )
        return
    s = ListSession(secrets.token_urlsafe(6), tracks, user_id, message.chat.id, status.message_id, message.message_id)
    _sessions[s.token] = s
    await pipeline.edit_message(ctx.bot, s.chat_id, s.message_id, *_picker_view(s))


def _picker_view(s: ListSession) -> tuple[str, InlineKeyboardMarkup]:
    tl, n = s.tracks, len(s.tracks.items)
    about = (tl.kind.capitalize(), tl.owner, _plural(n, "track"))
    lines = [_title(s), html.escape(" · ".join(x for x in about if x))]
    if tl.total > n:
        lines.append(f"<i>Only the first {n} of its {tl.total} tracks can be downloaded.</i>")
    lines += ["", f"Tap tracks to pick them, or download all. Picked: {len(s.selected)}"]

    def track(i: int) -> InlineKeyboardButton:
        mark = "✅" if i in s.selected else "▫️"
        return _button(s, f"{mark} {i + 1}. {pipeline.truncate(_label(s, i), LABEL_LIMIT)}", "t", i)

    first = s.page * PAGE_SIZE
    rows = [[track(i)] for i in range(first, min(first + PAGE_SIZE, n))]
    pages = -(-n // PAGE_SIZE)
    if pages > 1:
        rows.append([
            _button(s, "◀", "p", (s.page - 1) % pages),
            _button(s, f"{s.page + 1} / {pages}", "-"),
            _button(s, "▶", "p", (s.page + 1) % pages),
        ])
    rows.append([_button(s, "☑️ Pick all", "all"), _button(s, "Clear", "none")])
    rows.append([_button(s, f"⬇️ All {n}", "go_all"), _button(s, f"⬇️ Picked {len(s.selected)}", "go")])
    rows.append([_button(s, "✖ Cancel", "x")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _expired(ctx: Ctx, s: ListSession) -> bool:
    return s.task is None and time.monotonic() - s.touched > ctx.settings.picker_ttl


async def _expire(ctx: Ctx, s: ListSession) -> None:
    if _sessions.pop(s.token, None) is not None:
        await pipeline.edit_message(ctx.bot, s.chat_id, s.message_id, pipeline.EXPIRED_TEXT)


async def expire_sessions(ctx: Ctx) -> None:
    for s in list(_sessions.values()):
        if _expired(ctx, s):
            await _expire(ctx, s)


@router.callback_query(ListCb.filter())
async def on_button(cb: CallbackQuery, callback_data: ListCb, ctx: Ctx) -> None:
    s = _sessions.get(callback_data.t)
    action, i = callback_data.a, callback_data.i
    if s is None:
        await pipeline.answer(cb, pipeline.EXPIRED_POPUP)
        return
    if s.task is not None:
        if action == "stop" and not s.task.done():
            s.task.cancel()
            await pipeline.answer(cb, "Cancelling…")
        else:
            await pipeline.answer(cb)
        return
    if _expired(ctx, s):
        await pipeline.answer(cb, pipeline.EXPIRED_POPUP)
        await _expire(ctx, s)
        return
    s.touched = time.monotonic()
    n = len(s.tracks.items)

    if action == "x":
        _sessions.pop(s.token, None)
        await pipeline.answer(cb)
        await pipeline.delete_message(ctx.bot, s.chat_id, s.message_id)
        return
    if action in ("go", "go_all"):
        await _start(ctx, s, cb, sorted(s.selected) if action == "go" else list(range(n)))
        return
    if action == "t" and 0 <= i < n:
        s.selected ^= {i}
    elif action == "p" and 0 <= i * PAGE_SIZE < n:
        s.page = i
    elif action == "all":
        s.selected = set(range(n))
    elif action == "none":
        s.selected.clear()
    else:
        await pipeline.answer(cb)
        return
    await pipeline.answer(cb)
    await pipeline.edit_message(ctx.bot, s.chat_id, s.message_id, *_picker_view(s))


async def _start(ctx: Ctx, s: ListSession, cb: CallbackQuery, picked: list[int]) -> None:
    if not picked:
        await pipeline.answer(cb, "Tap tracks to pick them first.")
        return
    if any(o.task is not None and o.user_id == s.user_id for o in _sessions.values()):
        text = "Another playlist or album is still downloading. Wait for it to finish, or use /cancel."
        await pipeline.answer(cb, text, alert=True)
        return
    s.picked = picked
    s.task = ctx.spawn(_run(ctx, s), name=f"list-{s.token}")
    await pipeline.answer(cb)


# Batch


def _progress_view(ctx: Ctx, s: ListSession) -> tuple[str, InlineKeyboardMarkup]:
    done, total = s.sent + len(s.failed), len(s.picked)
    counts = f"{pipeline.progress_bar(done / total)} {done} of {total}"
    if s.failed:
        counts += f" · ⚠️ {len(s.failed)} not sent"
    lines = [f"⬇️ {_title(s)}", counts]
    if s.current is not None:
        lines += ["", f"{s.current + 1}. {html.escape(pipeline.truncate(_label(s, s.current), 200))}"]
        if s.job is not None:
            lines.append(pipeline.queued_text(ctx, s.job) or pipeline.progress_text(s.job.progress))
        elif s.waiting:
            lines.append("⏳ Waiting for your other downloads…")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[[_button(s, "✖ Cancel", "stop")]])


async def _show_progress(ctx: Ctx, s: ListSession) -> None:
    text, markup = _progress_view(ctx, s)
    if text != s.shown and await pipeline.edit_message(ctx.bot, s.chat_id, s.message_id, text, markup, retry=False):
        s.shown = text


def _summary(s: ListSession, head: str, note: str = "") -> str:
    lines = [f"{head} {_title(s)}", f"{s.sent} of {_plural(len(s.picked), 'track')} sent."]
    if note:
        lines.append(note)
    if s.failed:
        lines += ["", "<b>Not sent:</b>"]
        groups: dict[str, list[int]] = {}
        for i, why in s.failed:
            groups.setdefault(why, []).append(i)
        room = FAILURES_SHOWN
        for why, indexes in groups.items():
            if room <= 0:
                break
            lines.append(f"<i>{html.escape(why)}</i>")
            lines += [
                f"{i + 1}. {pipeline.link(s.tracks.items[i].url, pipeline.truncate(_label(s, i), LABEL_LIMIT))}"
                for i in indexes[:room]
            ]
            room -= len(indexes)
        if (hidden := len(s.failed) - FAILURES_SHOWN) > 0:
            lines.append(f"…and {hidden} more.")
    return "\n".join(lines)


async def _run(ctx: Ctx, s: ListSession) -> None:
    try:
        await _show_progress(ctx, s)
        for i in s.picked:
            s.current = i
            if not await _send_track(ctx, s, i):
                await _stop(ctx, s)
                return
        s.current = None
        await _report(ctx, s)
    except asyncio.CancelledError:
        # Shielded so a second cancel can't leave the current track's job running or the message stale.
        await asyncio.shield(ctx.spawn(_stop(ctx, s), name=f"list-stop-{s.token}"))
        raise
    except TelegramForbiddenError:
        log.info("user %s blocked the bot", s.user_id)
    except Exception as e:
        await ctx.alerts.error(f"list:{type(e).__name__}", "Track list download crashed", e, user=s.user_id)
        await pipeline.edit_message(ctx.bot, s.chat_id, s.message_id, _summary(s, "⚠️", pipeline.GENERIC_FAILURE))
    finally:
        _sessions.pop(s.token, None)


async def _send_track(ctx: Ctx, s: ListSession, i: int) -> bool:
    """Sends track i like a link of its own; False when the batch has to stop."""
    try:
        rule, ref = ctx.sources.match(s.tracks.items[i].url)
    except DownloadError as e:
        s.failed.append((i, e.user_text))
        return True
    fmt, flow = pipeline.FLOWS[rule.mode]
    if await pipeline.send_from_cache(ctx, rule, ref, fmt, user_id=s.user_id, chat_id=s.chat_id, reply_to=None):
        s.sent += 1
        return True
    while (job := await pipeline.enqueue(
        ctx, rule=rule, ref=ref, fmt=fmt, user_id=s.user_id, chat_id=s.chat_id, reply_to=None, flow=flow, silent=True,
    )) is None:
        s.waiting = True
        await _show_progress(ctx, s)
        await ctx.jobs.wait_for_room(s.user_id)
    s.waiting, s.job = False, job
    task = job.task
    assert task is not None
    while not task.done():
        await _show_progress(ctx, s)
        await asyncio.wait({task}, timeout=ctx.settings.progress_interval)
    s.job = None

    if task.cancelled():
        # The admin cancelled this one track, or the bot is shutting down.
        if ctx.closing:
            return False
        s.failed.append((i, "Cancelled."))
        return True
    error = job.data.get("error")
    if isinstance(error, TelegramForbiddenError):
        return False
    if error is None:
        s.sent += 1
    else:
        s.failed.append((i, error.user_text if isinstance(error, DownloadError) else "Something went wrong."))
    return True


async def _report(ctx: Ctx, s: ListSession) -> None:
    """The summary goes below the sent tracks; the status message above them is removed."""
    text = _summary(s, "⚠️" if s.failed else "✅")
    try:
        await ctx.bot.send_message(s.chat_id, text, reply_parameters=pipeline.reply_params(s.reply_to))
    except (TelegramBadRequest, TelegramRetryAfter) as e:
        log.warning("list summary not sent: %s", e.message)
        await pipeline.edit_message(ctx.bot, s.chat_id, s.message_id, text)
        return
    await pipeline.delete_message(ctx.bot, s.chat_id, s.message_id)


async def _stop(ctx: Ctx, s: ListSession) -> None:
    task = s.job.task if s.job is not None else None
    if task is not None and not task.done():
        task.cancel()
        await asyncio.wait({task}, timeout=15)
    note = "Stopped: the bot is restarting. Send the link again for the rest." if ctx.closing else "Cancelled."
    await pipeline.edit_message(ctx.bot, s.chat_id, s.message_id, _summary(s, "✖", note))


async def close_user(ctx: Ctx, user_id: int) -> int:
    """Cancels the user's running lists; returns how many."""
    tasks = [s.task for s in _sessions.values() if s.user_id == user_id and s.task and not s.task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=15)
    return len(tasks)
