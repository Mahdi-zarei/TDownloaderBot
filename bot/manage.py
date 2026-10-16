"""Admin statistics and management: /stats, /users, /user, /jobs, /failures, /uncache, /broadcast, /admin."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import pipeline
from .admin import IsAdmin, fmt_bytes, stop_user_work
from .context import Ctx
from .db import USER_SORTS, JobStatus, day_start
from .models import DownloadError

log = logging.getLogger(__name__)
router = Router(name="manage")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

PAGE_SIZE = 15
TEXT_BUDGET = 3800  # Telegram allows 4096; keeps room for a closing line
BROADCAST_TTL = 600
BROADCAST_DELAY = 0.05  # ~20 messages a second, under Telegram's bulk-sending limit

STATUS_ICON = {
    JobStatus.DONE: "✅", JobStatus.CACHED: "⚡", JobStatus.FAILED: "❌", JobStatus.TIMEOUT: "⌛",
    JobStatus.CANCELLED: "✖", JobStatus.RUNNING: "⬇️", JobStatus.QUEUED: "⏳",
}
SORT_LABEL = {"recent": "Recent", "jobs": "Most requests", "bytes": "Most data"}
PERIODS = {"today": "Today", "7d": "7 days", "30d": "30 days", "all": "All time"}

ADMIN_HELP = (
    "🛠 <b>Admin commands</b>\n"
    "/stats [today|7d|30d|all] · usage, top users, failures\n"
    "/users [recent|jobs|bytes] · everyone, with their usage\n"
    "/user &lt;id or @name&gt; · one user's history, ban, cancel (or tap /u_&lt;id&gt; in lists)\n"
    "/jobs · running and queued jobs, cancel any of them\n"
    "/failures [n] · recent failures with the site's error\n"
    "/uncache &lt;link&gt; · forget the cached file for a link, so it downloads fresh\n"
    "/broadcast · reply to a message to send a copy to all users, or /broadcast &lt;text&gt;\n"
    "/ban, /unban &lt;id or @name&gt;\n"
    "/selftest · every source through every egress"
)


class UsersCb(CallbackData, prefix="au"):
    sort: str
    page: int


class UserCb(CallbackData, prefix="ad"):
    uid: int
    a: str  # "ban", "unban", "cancel" or "refresh"


class StatsCb(CallbackData, prefix="as"):
    p: str


class JobsCb(CallbackData, prefix="aj"):
    a: str  # "cancel" or "refresh"
    id: int = 0


class BroadcastCb(CallbackData, prefix="ab"):
    t: str
    a: str  # "send" or "cancel"


# Formatting


def when(ts: int, fmt: str = "%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ts, UTC).strftime(fmt)


def ago(ts: int | None) -> str:
    if not ts:
        return "never"
    d = int(time.time()) - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    if d < 30 * 86400:
        return f"{d // 86400}d ago"
    return when(ts, "%Y-%m-%d")


def who(row: dict[str, Any] | None, uid: int) -> str:
    """@username, else the display name, else the id; links to the user's profile."""
    row = row or {}
    label = f"@{row['username']}" if row.get("username") else (row.get("name") or str(uid))
    return f'<a href="tg://user?id={uid}">{html.escape(pipeline.truncate(label, 32))}</a>'


def short_link(url: str) -> str:
    shown = re.sub(r"^https?://(www\.)?", "", url)
    return pipeline.link(url, pipeline.truncate(shown, 48))


def reason_of(error: str | None) -> str:
    return (error or "").split(":", 1)[0].strip()


def error_line(error: str | None) -> str:
    """The most telling line of a stored error: the tool's last ERROR line, else the first line."""
    lines = [line.strip() for line in (error or "").splitlines() if line.strip()]
    return next((line for line in reversed(lines) if "ERROR" in line or "[error]" in line), lines[0] if lines else "")


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def btn(text: str, data: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data.pack())


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


def fit(lines: list[str], more: list[str]) -> list[str]:
    """Appends entries while the message stays under Telegram's limit."""
    size = sum(len(line) + 1 for line in lines)
    for i, entry in enumerate(more):
        if size + len(entry) > TEXT_BUDGET:
            lines.append(f"… and {len(more) - i} more")
            break
        lines.append(entry)
        size += len(entry) + 1
    return lines


async def show(ctx: Ctx, cb: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None, note: str | None = None) -> None:
    if isinstance(cb.message, Message):
        await pipeline.edit_message(ctx.bot, cb.message.chat.id, cb.message.message_id, text, markup)
    await pipeline.answer(cb, note)


# /admin


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    await message.answer(ADMIN_HELP)


# /stats


def _since(period: str) -> int:
    now = int(time.time())
    return {"today": day_start(), "7d": now - 7 * 86400, "30d": now - 30 * 86400}.get(period, 0)


async def _stats_view(ctx: Ctx, period: str) -> tuple[str, InlineKeyboardMarkup]:
    st = await ctx.db.stats(_since(period))
    jobs: dict[str, int] = st["jobs"]
    delivered = jobs.get(JobStatus.DONE, 0) + jobs.get(JobStatus.CACHED, 0)
    failed = jobs.get(JobStatus.FAILED, 0) + jobs.get(JobStatus.TIMEOUT, 0)
    rate = f" · {delivered / (delivered + failed):.0%} success" if delivered + failed else ""
    lines = [
        f"📊 <b>{PERIODS[period]}</b>" + (" (since 00:00 UTC)" if period == "today" else ""),
        f"Users: {st['users_active']} active · {st['users_new']} new · {st['users_total']} total"
        + (f" · {st['users_banned']} banned" if st["users_banned"] else ""),
        f"Requests: {sum(jobs.values())} · ✅ {jobs.get(JobStatus.DONE, 0)} · ⚡ {jobs.get(JobStatus.CACHED, 0)}"
        f" · ❌ {failed} · ✖ {jobs.get(JobStatus.CANCELLED, 0)}{rate}",
        f"Sent: {fmt_bytes(st['bytes'])}",
        f"Now: {ctx.jobs.running_count} running · {ctx.jobs.queued_count} queued",
        f"Cache: {plural(st['cache_files'], 'file')} · {fmt_bytes(st['cache_bytes'])}",
    ]
    if st["by_source"]:
        lines += ["", "<b>By source</b>"]
        lines += [
            f"{html.escape(r['source'])}: {r['n']} · {r['delivered']} ok"
            + (f" · {r['failed']} failed" if r["failed"] else "") + f" · {fmt_bytes(r['bytes'])}"
            for r in st["by_source"]
        ]
    if st["top_users"]:
        lines += ["", "<b>Top users</b>"]
        lines += [
            f"{i}. {who(r, r['tg_id'])} · {r['n']} · {fmt_bytes(r['b'])} /u_{r['tg_id']}"
            for i, r in enumerate(st["top_users"], 1)
        ]
    if st["reasons"]:
        lines += ["", "<b>Failure reasons</b>", " · ".join(f"{html.escape(r['reason'] or '?')} {r['n']}" for r in st["reasons"])]
    if st["failures"]:
        lines += ["", "<b>Failures by egress</b>"]
        lines += [f"{html.escape(r['source'])} · {html.escape(r['egress'] or 'direct')}: {r['n']}" for r in st["failures"]]
    periods = [btn(("• " if p == period else "") + label, StatsCb(p=p)) for p, label in PERIODS.items()]
    return "\n".join(lines), kb([periods])


@router.message(Command("stats"))
async def cmd_stats(message: Message, command: CommandObject, ctx: Ctx) -> None:
    period = (command.args or "today").strip().lower()
    text, markup = await _stats_view(ctx, period if period in PERIODS else "today")
    await message.answer(text, reply_markup=markup)


@router.callback_query(StatsCb.filter())
async def on_stats(cb: CallbackQuery, callback_data: StatsCb, ctx: Ctx) -> None:
    await show(ctx, cb, *await _stats_view(ctx, callback_data.p if callback_data.p in PERIODS else "today"))


# /users


async def _users_view(ctx: Ctx, sort: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    rows, total = await ctx.db.users_page(sort, page * PAGE_SIZE, PAGE_SIZE)
    pages = max(1, -(-total // PAGE_SIZE))
    lines = [f"👥 <b>Users</b> · {total} total · {SORT_LABEL[sort].lower()} first"]
    entries = []
    for i, r in enumerate(rows, page * PAGE_SIZE + 1):
        usage = [f"{r['delivered']} ok"] + ([f"{r['failed']} failed"] if r["failed"] else [])
        usage += [fmt_bytes(r["bytes"]), ago(r["last_seen"])]
        flag = " 🚫" if r["banned"] else ""
        entries.append(f"{i}. {who(r, r['tg_id'])}{flag} · {' · '.join(usage)} /u_{r['tg_id']}")
    fit(lines, entries or ["No users yet."])
    sorts = [btn(("• " if s == sort else "") + label, UsersCb(sort=s, page=0)) for s, label in SORT_LABEL.items()]
    nav = [btn("◀", UsersCb(sort=sort, page=page - 1))] if page > 0 else []
    nav.append(btn(f"{page + 1}/{pages}", UsersCb(sort=sort, page=page)))
    if page + 1 < pages:
        nav.append(btn("▶", UsersCb(sort=sort, page=page + 1)))
    return "\n".join(lines), kb([sorts, nav])


@router.message(Command("users"))
async def cmd_users(message: Message, command: CommandObject, ctx: Ctx) -> None:
    sort = (command.args or "recent").strip().lower()
    text, markup = await _users_view(ctx, sort if sort in USER_SORTS else "recent", 0)
    await message.answer(text, reply_markup=markup)


@router.callback_query(UsersCb.filter())
async def on_users(cb: CallbackQuery, callback_data: UsersCb, ctx: Ctx) -> None:
    sort = callback_data.sort if callback_data.sort in USER_SORTS else "recent"
    await show(ctx, cb, *await _users_view(ctx, sort, max(0, callback_data.page)))


# /user


def _job_line(j: dict[str, Any]) -> str:
    parts = [f"{STATUS_ICON.get(j['status'], '•')} {when(j['created_at'])}", html.escape(f"{j['source']} {j['format']}".strip())]
    if j["bytes"]:
        parts.append(fmt_bytes(j["bytes"]))
    if j["status"] in (JobStatus.FAILED, JobStatus.TIMEOUT) and (reason := reason_of(j["error"])):
        parts.append(html.escape(reason))
    return " · ".join(parts) + " · " + short_link(j["url"])


async def _user_view(ctx: Ctx, uid: int) -> tuple[str, InlineKeyboardMarkup] | None:
    d = await ctx.db.user_detail(uid)
    if d is None:
        return None
    u, by_status = d["user"], d["by_status"]
    total = sum(n for n, _ in by_status.values())
    name = (u.get("name") or "").strip()
    head = f"👤 {who(u, uid)}" + (f" ({html.escape(name)})" if name and u.get("username") else "")
    head += f" <code>{uid}</code>" + (" 🚫 banned" if u["banned"] else "")
    counts = "".join(f" · {icon} {by_status[s][0]}" for s, icon in STATUS_ICON.items() if s in by_status)
    lines = [
        head,
        f"First seen {when(u['first_seen'], '%Y-%m-%d')} · last seen {ago(u['last_seen'])}",
        f"Requests: {total} (today {d['today']}){counts}",
        f"Sent: {fmt_bytes(sum(b for _, b in by_status.values()))}",
    ]
    if d["by_source"]:
        lines.append("By source: " + " · ".join(f"{html.escape(r['source'])} {r['n']}" for r in d["by_source"]))
    live = ctx.jobs.user_jobs(uid)
    if live:
        running = sum(j.running for j in live)
        lines.append(f"Now: {running} running · {len(live) - running} queued")
    if d["recent"]:
        lines += ["", "<b>Recent</b>"]
        fit(lines, [_job_line(j) for j in d["recent"]])
    buttons = []
    if uid != ctx.settings.admin_id:
        buttons.append(btn("✅ Unban", UserCb(uid=uid, a="unban")) if u["banned"] else btn("🚫 Ban", UserCb(uid=uid, a="ban")))
    if live:
        buttons.append(btn(f"✖ Cancel {len(live)}", UserCb(uid=uid, a="cancel")))
    buttons.append(btn("🔄", UserCb(uid=uid, a="refresh")))
    return "\n".join(lines), kb([buttons])


async def _reply_user(message: Message, ctx: Ctx, uid: int) -> None:
    view = await _user_view(ctx, uid)
    if view is None:
        await message.reply("No such user. They appear here after their first message to the bot.")
        return
    await message.answer(view[0], reply_markup=view[1])


@router.message(Command("user"))
async def cmd_user(message: Message, command: CommandObject, ctx: Ctx) -> None:
    row = await ctx.db.find_user(command.args or "")
    if row is None:
        await message.reply("Usage: /user &lt;id or @username&gt; (see /users)" if not command.args else "No such user.")
        return
    await _reply_user(message, ctx, row["tg_id"])


@router.message(Command(re.compile(r"u_(\d+)")))
async def cmd_user_short(message: Message, command: CommandObject, ctx: Ctx) -> None:
    assert command.regexp_match is not None
    await _reply_user(message, ctx, int(command.regexp_match[1]))


@router.callback_query(UserCb.filter())
async def on_user(cb: CallbackQuery, callback_data: UserCb, ctx: Ctx) -> None:
    uid, action, note = callback_data.uid, callback_data.a, None
    if action == "ban" and uid != ctx.settings.admin_id:
        await ctx.db.set_banned(uid, True)
        n = await stop_user_work(ctx, uid)
        note = "Banned" + (f", cancelled {plural(n, 'job')}" if n else "")
    elif action == "unban":
        await ctx.db.set_banned(uid, False)
        note = "Unbanned"
    elif action == "cancel":
        note = f"Cancelled {plural(await stop_user_work(ctx, uid), 'job')}"
        await asyncio.sleep(0.5)  # let the cancelled jobs leave the queue before re-rendering
    view = await _user_view(ctx, uid)
    if view is None:
        await pipeline.answer(cb, "No such user.")
        return
    await show(ctx, cb, *view, note=note)


# /jobs


async def _jobs_view(ctx: Ctx) -> tuple[str, InlineKeyboardMarkup]:
    jobs = sorted(ctx.jobs.all(), key=lambda j: j.id)
    users = await ctx.db.users_by_ids(list({j.user_id for j in jobs}))
    lines = [f"⚙️ <b>Jobs</b> · {ctx.jobs.running_count} running · {ctx.jobs.queued_count} queued"]
    entries = []
    for j in jobs:
        p = j.progress
        state = pipeline.STAGE_LABEL.get(p.stage, str(p.stage)) if j.running else "⏳ Queued"
        if j.running and p.fraction is not None:
            state += f" {p.fraction:.0%}"
        entries.append(
            f"#{j.id} {state} · {html.escape(f'{j.rule.name} {j.fmt}')} · {who(users.get(j.user_id), j.user_id)} "
            f"/u_{j.user_id}\n    {short_link(j.ref.url)}"
        )
    fit(lines, entries or ["Nothing running."])
    cancels = [btn(f"✖ #{j.id}", JobsCb(a="cancel", id=j.id)) for j in jobs[:24]]
    rows = [cancels[i : i + 4] for i in range(0, len(cancels), 4)]
    return "\n".join(lines), kb([*rows, [btn("🔄 Refresh", JobsCb(a="refresh"))]])


@router.message(Command("jobs"))
async def cmd_jobs(message: Message, ctx: Ctx) -> None:
    text, markup = await _jobs_view(ctx)
    await message.answer(text, reply_markup=markup)


@router.callback_query(JobsCb.filter())
async def on_jobs(cb: CallbackQuery, callback_data: JobsCb, ctx: Ctx) -> None:
    note = None
    if callback_data.a == "cancel":
        note = f"Cancelling #{callback_data.id}" if ctx.jobs.cancel(callback_data.id) else "That job already finished"
        await asyncio.sleep(0.5)
    await show(ctx, cb, *await _jobs_view(ctx), note=note)


# /failures


@router.message(Command("failures"))
async def cmd_failures(message: Message, command: CommandObject, ctx: Ctx) -> None:
    arg = (command.args or "").strip()
    rows = await ctx.db.recent_failures(min(max(int(arg), 1), 30) if arg.isdigit() else 10)
    if not rows:
        await message.answer("No failures recorded. 🎉")
        return
    users = await ctx.db.users_by_ids(list({r["tg_id"] for r in rows}))
    entries = []
    for r in rows:
        entry = (
            f"{STATUS_ICON.get(r['status'], '❌')} {when(r['created_at'])} · {html.escape(r['source'])} · "
            f"{html.escape(r['egress'] or 'direct')} · <b>{html.escape(r['reason'] or '?')}</b> · "
            f"{who(users.get(r['tg_id']), r['tg_id'])} /u_{r['tg_id']}\n    {short_link(r['url'])}"
        )
        if (line := error_line(r["error"])) and line != r["reason"]:
            entry += f"\n    <i>{html.escape(pipeline.truncate(line, 160))}</i>"
        entries.append(entry)
    await message.answer("\n".join(fit([f"❌ <b>Last {plural(len(rows), 'failure')}</b>"], entries)))


# /uncache


@router.message(Command("uncache"))
async def cmd_uncache(message: Message, command: CommandObject, ctx: Ctx) -> None:
    url = (command.args or "").strip()
    if not url:
        await message.reply("Usage: /uncache &lt;link&gt; · the next request for it downloads fresh.")
        return
    try:
        rule, ref = ctx.sources.match(url)
    except DownloadError as e:
        await message.reply(html.escape(e.user_text))
        return
    n = await ctx.db.delete_cache_prefix(f"{ref.source}:{ref.media_id}:")
    target = f"{html.escape(rule.label)} <code>{html.escape(ref.media_id)}</code>"
    await message.reply(f"🗑 Removed {plural(n, 'cached file')} for {target}." if n else f"Nothing cached for {target}.")


# /broadcast


@dataclass(slots=True)
class _Broadcast:
    chat_id: int
    preview_id: int
    message_id: int | None  # copy this message, keeping its formatting and media
    text: str | None  # or send this plain text
    created: float


_broadcasts: dict[str, _Broadcast] = {}
_broadcast_lock = asyncio.Lock()


async def _recipients(ctx: Ctx) -> list[int]:
    return [uid for uid in await ctx.db.broadcast_ids() if uid != ctx.settings.admin_id]


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject, ctx: Ctx) -> None:
    source, text = message.reply_to_message, (command.args or "").strip()
    if source is None and not text:
        await message.reply(
            "Reply to a message with /broadcast to send a copy of it (formatting and media kept), "
            "or use /broadcast &lt;text&gt;."
        )
        return
    recipients = await _recipients(ctx)
    if not recipients:
        await message.reply("No users to message yet.")
        return
    token = secrets.token_urlsafe(6)
    body = "" if source else f"\n\n{html.escape(text)}"
    preview = await message.reply(
        f"📣 Send {'that message' if source else 'this'} to <b>{plural(len(recipients), 'user')}</b>? "
        f"Banned users are skipped.{body}",
        reply_markup=kb([[btn("📣 Send", BroadcastCb(t=token, a="send")), btn("Cancel", BroadcastCb(t=token, a="cancel"))]]),
    )
    now = time.monotonic()
    for key in [k for k, b in _broadcasts.items() if now - b.created > BROADCAST_TTL]:
        _broadcasts.pop(key, None)
    _broadcasts[token] = _Broadcast(
        message.chat.id, preview.message_id, source.message_id if source else None, None if source else text, now
    )


@router.callback_query(BroadcastCb.filter())
async def on_broadcast(cb: CallbackQuery, callback_data: BroadcastCb, ctx: Ctx) -> None:
    b = _broadcasts.pop(callback_data.t, None)
    if b is None or time.monotonic() - b.created > BROADCAST_TTL:
        await show(ctx, cb, "This broadcast expired. Send /broadcast again.", None)
        return
    if callback_data.a != "send":
        await show(ctx, cb, "Broadcast cancelled.", None)
        return
    if _broadcast_lock.locked():
        _broadcasts[callback_data.t] = b
        await pipeline.answer(cb, "Another broadcast is still sending.", alert=True)
        return
    await show(ctx, cb, "📣 Sending…", None)
    ctx.spawn(_run_broadcast(ctx, b), name="broadcast")


async def _run_broadcast(ctx: Ctx, b: _Broadcast) -> None:
    async with _broadcast_lock:
        recipients = await _recipients(ctx)
        sent = blocked = failed = 0
        for n, uid in enumerate(recipients, 1):
            for attempt in range(2):
                try:
                    if b.message_id is not None:
                        await ctx.bot.copy_message(uid, b.chat_id, b.message_id)
                    else:
                        await ctx.bot.send_message(uid, html.escape(b.text or ""))
                    sent += 1
                except TelegramRetryAfter as e:
                    if attempt == 0:
                        await asyncio.sleep(e.retry_after)
                        continue
                    failed += 1
                except TelegramForbiddenError:
                    blocked += 1
                except TelegramBadRequest:  # e.g. the chat no longer exists
                    failed += 1
                except Exception:
                    log.exception("broadcast to %s failed", uid)
                    failed += 1
                break
            if n % 25 == 0 and n < len(recipients):
                await pipeline.edit_message(ctx.bot, b.chat_id, b.preview_id, f"📣 Sending… {n}/{len(recipients)}", retry=False)
            await asyncio.sleep(BROADCAST_DELAY)
        summary = f"📣 Broadcast done: {sent} delivered"
        summary += (f" · {blocked} blocked the bot" if blocked else "") + (f" · {failed} failed" if failed else "")
        await pipeline.edit_message(ctx.bot, b.chat_id, b.preview_id, summary)
