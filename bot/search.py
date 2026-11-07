"""Search by text: YouTube Music songs, YouTube videos and SoundCloud tracks.

A tapped result runs exactly like a link the user sent; the results message stays open so several can
be picked. Queries are never logged or stored, and alerts mask them.
"""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from aiogram import Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, User

from . import handlers, matching, pipeline
from .context import Ctx
from .models import DownloadError, Reason
from .sources import Mode

log = logging.getLogger(__name__)
router = Router(name="search")

LIMIT = 15
PAGE_SIZE = 5
LABEL_LIMIT = 56
QUERY_MIN, QUERY_MAX = 2, 200
SEARCH_SLOTS = 3
REPICK_GUARD = 10  # seconds in which another tap on the same result counts as a double tap
EXPIRED_POPUP = "This search has expired — send it again."
USAGE_TEXT = "Type what you're looking for, e.g. <code>/search daft punk one more time</code>. Plain text works too."


@dataclass(frozen=True, slots=True)
class Result:
    url: str
    title: str
    by: str  # artists, channel or uploader
    duration: float | None


async def _songs(ctx: Ctx, query: str) -> list[Result]:
    proxy = ctx.settings.resolve_egress(ctx.sources.get("youtube").egress)
    found = await asyncio.wait_for(asyncio.to_thread(matching.ytm_search, query, proxy, LIMIT), matching.YTM_TIMEOUT)
    return [
        Result(
            f"https://music.youtube.com/watch?v={r['videoId']}", r["title"],
            ", ".join(a["name"] for a in r.get("artists") or [] if a.get("name")), r.get("duration_seconds"),
        )
        for r in found
        if r.get("videoId") and r.get("title")
    ]


async def _youtube(ctx: Ctx, query: str) -> list[Result]:
    entries = await ctx.downloader.search(ctx.sources.get("youtube"), query, LIMIT)
    return [
        Result(
            f"https://www.youtube.com/watch?v={e['id']}", e.get("title") or "Untitled",
            e.get("channel") or e.get("uploader") or "", e.get("duration"),
        )
        for e in entries
        if e.get("live_status") not in ("is_live", "is_upcoming")
    ]


async def _soundcloud(ctx: Ctx, query: str) -> list[Result]:
    entries = await ctx.downloader.search(ctx.sources.get("soundcloud"), query, LIMIT, prefix="scsearch")
    return [
        Result(e["webpage_url"], e.get("title") or "Untitled", e.get("uploader") or "", e.get("duration"))
        for e in entries
        if e.get("webpage_url")
    ]


@dataclass(frozen=True, slots=True)
class Tab:
    key: str
    label: str
    site: str
    rule: str  # the source rule whose egress the search goes through
    audio: bool  # a picked result goes straight to audio instead of the quality picker
    hint: str
    fetch: Callable[[Ctx, str], Awaitable[list[Result]]]


TABS = (
    Tab("songs", "🎵 Songs", "YouTube Music", "youtube", True, "Tap a song to get it as audio.", _songs),
    Tab("youtube", "▶️ YouTube", "YouTube", "youtube", False, "Tap a video to pick a quality or audio.", _youtube),
    Tab("soundcloud", "☁️ SoundCloud", "SoundCloud", "soundcloud", False, "Tap a track to get it as audio.", _soundcloud),
)


class SearchCb(CallbackData, prefix="sr"):
    t: str  # session token
    a: str  # "s" show tab i, "g" get result i of tab k, "p" page i, "x" close, "-" no-op
    i: int = 0
    k: int = 0


@dataclass(eq=False)
class Session:
    token: str
    query: str
    user_id: int
    chat_id: int
    reply_to: int  # the user's message; downloads reply to it
    message_id: int = 0  # the results message
    tab: int = 0
    page: int = 0
    results: dict[int, list[Result]] = field(default_factory=dict)
    errors: dict[int, str] = field(default_factory=dict)
    loading: int | None = None  # the tab being searched
    picked: dict[str, float] = field(default_factory=dict)  # result url -> when it was tapped
    touched: float = field(default_factory=time.monotonic)


_sessions: dict[str, Session] = {}
_busy: set[int] = set()  # users with a search running; each user searches one thing at a time
_slots = asyncio.Semaphore(SEARCH_SLOTS)


# View


def _button(s: Session, text: str, action: str, i: int = 0, k: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=SearchCb(t=s.token, a=action, i=i, k=k).pack())


def _heading(s: Session) -> str:
    return f"🔎 <b>{html.escape(pipeline.truncate(s.query, 100))}</b>"


def _label(s: Session, i: int, r: Result) -> str:
    mark = "⬇️ " if r.url in s.picked else ""
    tail = f" — {pipeline.truncate(r.by, 20)}" if r.by else ""
    if d := handlers.fmt_duration(r.duration):
        tail += f" · {d}"
    return pipeline.truncate(f"{mark}{i + 1}. {r.title}", LABEL_LIMIT - pipeline.u16len(tail)) + tail


def _view(s: Session) -> tuple[str, InlineKeyboardMarkup]:
    tab = TABS[s.tab]
    lines = [_heading(s)]
    rows = [[_button(s, f"• {t.label} •" if i == s.tab else t.label, "s", i) for i, t in enumerate(TABS)]]
    results = s.results.get(s.tab)
    if s.loading == s.tab:
        lines.append(f"<i>Searching {tab.site}…</i>")
    elif s.tab in s.errors:
        lines.append(f"⚠️ {html.escape(s.errors[s.tab])}")
    elif results is None:
        lines.append(f"<i>Tap {tab.label} to search {tab.site}.</i>")
    elif not results:
        lines.append(f"Nothing found on {tab.site}.")
    else:
        lines.append(tab.hint)
        first = s.page * PAGE_SIZE
        last = min(first + PAGE_SIZE, len(results))
        rows += [[_button(s, _label(s, i, results[i]), "g", i, s.tab)] for i in range(first, last)]
        pages = -(-len(results) // PAGE_SIZE)
        if pages > 1:
            rows.append([
                _button(s, "◀", "p", (s.page - 1) % pages),
                _button(s, f"{s.page + 1} / {pages}", "-"),
                _button(s, "▶", "p", (s.page + 1) % pages),
            ])
    rows.append([_button(s, "✖ Close", "x")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(ctx: Ctx, s: Session) -> None:
    await pipeline.edit_message(ctx.bot, s.chat_id, s.message_id, *_view(s))


# Searching


def _failure_text(tab: Tab, e: Exception) -> str:
    if isinstance(e, TimeoutError) or (isinstance(e, DownloadError) and e.reason is Reason.TIMEOUT):
        return f"{tab.site} didn't answer in time. Tap {tab.label} to try again."
    if isinstance(e, DownloadError) and e.reason in (Reason.BLOCKED, Reason.LOGIN_REQUIRED):
        return f"{tab.site} blocked the search. Try again later."
    return f"The {tab.site} search failed. Tap {tab.label} to try again."


async def _fetch(ctx: Ctx, s: Session, k: int, *, announce: bool) -> None:
    """Searches tab k; the caller holds the user's _busy mark."""
    tab = TABS[k]
    s.loading = k
    s.errors.pop(k, None)
    if announce:
        await _show(ctx, s)
    try:
        async with _slots:
            found = await tab.fetch(ctx, s.query)
        s.results[k] = list({r.url: r for r in found}.values())[:LIMIT]
    except Exception as e:
        s.errors[k] = _failure_text(tab, e)
        reason = e.reason.value if isinstance(e, DownloadError) else type(e).__name__
        await ctx.alerts.error(
            f"search:{tab.key}:{reason}", f"{tab.site} search failed", e, private=[s.query], user=s.user_id,
            egress=ctx.settings.egress_label(ctx.sources.get(tab.rule).egress),
        )
    finally:
        s.loading = None
    if s.token in _sessions:  # not closed meanwhile
        await _show(ctx, s)


async def _start(ctx: Ctx, message: Message, user_id: int, query: str) -> None:
    if len(query) < QUERY_MIN:
        await message.reply(handlers.HINT_TEXT)
        return
    if len(query) > QUERY_MAX:
        await message.reply("That's too long to search for. Send a link, or a shorter search.")
        return
    if user_id in _busy:
        await message.reply("⏳ Your last search is still running. Send this one again in a moment.")
        return
    _busy.add(user_id)
    try:
        s = Session(secrets.token_urlsafe(6), query, user_id, message.chat.id, message.message_id, loading=0)
        text, markup = _view(s)
        s.message_id = (await message.reply(text, reply_markup=markup)).message_id
        _sessions[s.token] = s
        await _fetch(ctx, s, 0, announce=False)
    except TelegramForbiddenError:
        log.info("user %s blocked the bot", user_id)
    finally:
        _busy.discard(user_id)


def _query(message: Message) -> dict[str, str] | bool:
    """Filter: text without links or a command is a search; hands the query to the handler."""
    text = message.text
    if not text or text.startswith("/") or handlers.extract_urls(message):
        return False
    return {"query": " ".join(text.split())}


@router.message(Command("search"))
async def cmd_search(message: Message, ctx: Ctx, event_from_user: User, command: CommandObject) -> None:
    if not (query := " ".join((command.args or "").split())):
        await message.answer(USAGE_TEXT)
        return
    await _start(ctx, message, event_from_user.id, query)


@router.message(_query)
async def on_text(message: Message, ctx: Ctx, event_from_user: User, query: str) -> None:
    await _start(ctx, message, event_from_user.id, query)


# Buttons


def _expired(ctx: Ctx, s: Session) -> bool:
    return s.loading is None and time.monotonic() - s.touched > ctx.settings.picker_ttl


async def _expire(ctx: Ctx, s: Session) -> None:
    if _sessions.pop(s.token, None) is not None:
        text = f"{_heading(s)}\nExpired — send your search again."
        await pipeline.edit_message(ctx.bot, s.chat_id, s.message_id, text)


async def expire_sessions(ctx: Ctx) -> None:
    for s in list(_sessions.values()):
        if _expired(ctx, s):
            await _expire(ctx, s)


async def _switch(ctx: Ctx, s: Session, cb: CallbackQuery, k: int) -> None:
    if k in s.results:
        await pipeline.answer(cb)
        if k != s.tab:
            s.tab, s.page = k, 0
            await _show(ctx, s)
        return
    if s.user_id in _busy:
        await pipeline.answer(cb, "Wait for the current search to finish.")
        return
    _busy.add(s.user_id)
    try:
        s.tab, s.page = k, 0
        await pipeline.answer(cb)
        await _fetch(ctx, s, k, announce=True)
    finally:
        _busy.discard(s.user_id)


async def _pick(ctx: Ctx, s: Session, cb: CallbackQuery, k: int, i: int) -> None:
    results = s.results.get(k) or []
    if not 0 <= i < len(results):
        await pipeline.answer(cb)
        return
    r, now = results[i], time.monotonic()
    if r.url in s.picked and now - s.picked[r.url] < REPICK_GUARD:
        await pipeline.answer(cb, "Already on its way.")
        return
    tab = TABS[k]
    direct = tab.audio or ctx.sources.get(tab.rule).mode is not Mode.PICK
    if direct and not ctx.jobs.can_submit(s.user_id):
        await pipeline.answer(cb, pipeline.queue_full_text(ctx, s.user_id), alert=True)
        return
    s.picked[r.url] = now
    await pipeline.answer(cb)
    ctx.spawn(
        handlers.start_url(ctx, user_id=s.user_id, chat_id=s.chat_id, reply_to=s.reply_to, url=r.url, audio=tab.audio),
        name="search-pick",
    )
    await _show(ctx, s)


@router.callback_query(SearchCb.filter())
async def on_button(cb: CallbackQuery, callback_data: SearchCb, ctx: Ctx) -> None:
    s = _sessions.get(callback_data.t)
    if s is None or _expired(ctx, s):
        await pipeline.answer(cb, EXPIRED_POPUP)
        if s is not None:
            await _expire(ctx, s)
        return
    s.touched = time.monotonic()
    a, i = callback_data.a, callback_data.i
    if a == "x":
        _sessions.pop(s.token, None)
        await pipeline.answer(cb)
        await pipeline.delete_message(ctx.bot, s.chat_id, s.message_id)
    elif a == "s" and 0 <= i < len(TABS):
        await _switch(ctx, s, cb, i)
    elif a == "p" and 0 <= i * PAGE_SIZE < len(s.results.get(s.tab) or []):
        s.page = i
        await pipeline.answer(cb)
        await _show(ctx, s)
    elif a == "g" and 0 <= callback_data.k < len(TABS):
        await _pick(ctx, s, cb, callback_data.k, i)
    else:
        await pipeline.answer(cb)
