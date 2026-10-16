"""Admin router: /stats, /selftest, /ban, /unban."""

from __future__ import annotations

import asyncio
import html
import logging
import shutil
import time

from aiogram import Router
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import Message

from . import matching, pipeline, tag_editor
from .context import Ctx
from .db import JobStatus
from .downloader import SelftestResult
from .models import DownloadError

log = logging.getLogger(__name__)
router = Router(name="admin")

_STATUS_ORDER = (
    JobStatus.DONE, JobStatus.CACHED, JobStatus.FAILED, JobStatus.TIMEOUT,
    JobStatus.CANCELLED, JobStatus.RUNNING, JobStatus.QUEUED,
)
_selftest_lock = asyncio.Lock()


class IsAdmin(Filter):
    async def __call__(self, message: Message, ctx: Ctx) -> bool:
        return message.from_user is not None and message.from_user.id == ctx.settings.admin_id


router.message.filter(IsAdmin())


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


@router.message(Command("stats"))
async def cmd_stats(message: Message, ctx: Ctx) -> None:
    st = await ctx.db.stats_today()
    jobs: dict[str, int] = st["jobs"]
    statuses = [s for s in _STATUS_ORDER if jobs.get(s)] + [s for s in jobs if s not in _STATUS_ORDER]
    lines = [
        "📊 <b>Today</b> (UTC)",
        f"Users: {st['users_today']} active · {st['users_new']} new · {st['users_total']} total",
        "Jobs: " + (" · ".join(f"{html.escape(s)} {jobs[s]}" for s in statuses) or "none"),
        f"Sent: {fmt_bytes(st['bytes'])}",
        f"Now: {ctx.jobs.running_count} running · {ctx.jobs.queued_count} queued",
        f"Cache hits since start: {sum(ctx.metrics.cache_hits.values())}",
    ]
    if st["failures"]:
        lines.append("<b>Failures</b>")
        lines += [
            f"{html.escape(source)} · {html.escape(egress or 'direct')}: {n}" for source, egress, n in st["failures"]
        ]
    await message.answer("\n".join(lines))


# Selftest


def _row(source: str, egress: str, result: SelftestResult | None) -> str:
    name = f"{html.escape(source)} · {html.escape(egress)}"
    if result is None:
        return f"❌ {name} · error"
    if result.ok:
        return f"✅ {name} · {result.seconds:.1f}s"
    return f"❌ {name} · {html.escape(result.reason or 'failed')}"


async def _selftest_header(ctx: Ctx, egresses: list[str]) -> str:
    lines = ["🧪 <b>Selftest</b>"]
    try:
        versions = await ctx.downloader.versions()
        lines.append(" · ".join(f"{html.escape(k)} <code>{html.escape(v)}</code>" for k, v in versions.items()))
    except Exception:
        log.exception("selftest: versions failed")
        lines.append("versions: error")
    ips = await asyncio.gather(*(ctx.downloader.egress_ip(e) for e in egresses), return_exceptions=True)
    for egress, ip in zip(egresses, ips):
        shown = html.escape(ip) if isinstance(ip, str) else f"error ({html.escape(type(ip).__name__)})"
        lines.append(f"{html.escape(egress)}: <code>{shown}</code>")
    for rule in ctx.sources.rules:
        if rule.cookies is not None:
            problem = rule.cookies_problem()
            lines.append(f"cookies {html.escape(rule.name)}: " + (f"❌ {html.escape(problem)}" if problem else "✅"))
    return "\n".join(lines)


@router.message(Command("selftest"))
async def cmd_selftest(message: Message, ctx: Ctx) -> None:
    if _selftest_lock.locked():
        await message.reply("A selftest is already running.")
        return
    async with _selftest_lock:
        await _selftest(ctx, message)


async def _selftest(ctx: Ctx, message: Message) -> None:
    s = ctx.settings
    status = await message.reply("🧪 Selftest: starting…")
    egresses = s.egress_names
    targets = [("youtube", s.selftest_youtube), ("instagram", s.selftest_instagram), ("soundcloud", s.selftest_soundcloud)]
    rows = {(src, eg): f"⏳ {html.escape(src)} · {html.escape(eg)}" for src, _ in targets for eg in egresses}
    header = await _selftest_header(ctx, egresses)

    def render() -> str:
        return header + "\n\n" + "\n".join(rows.values())

    await pipeline.edit_message(ctx.bot, status.chat.id, status.message_id, render(), retry=False)
    base = s.jobs_dir / f"selftest-{int(time.time())}"
    limit = asyncio.Semaphore(2)

    async def one(source: str, url: str, egress: str) -> tuple[str, str, SelftestResult | None]:
        async with limit:
            try:
                rule = ctx.sources.get(source)
                return source, egress, await ctx.downloader.selftest(rule, url, egress, base / f"{source}-{egress}")
            except Exception:
                log.exception("selftest %s via %s crashed", source, egress)
                return source, egress, None

    try:
        for done in asyncio.as_completed([one(src, url, eg) for src, url in targets for eg in egresses]):
            source, egress, result = await done
            rows[(source, egress)] = _row(source, egress, result)
            await pipeline.edit_message(ctx.bot, status.chat.id, status.message_id, render(), retry=False)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    rows[("spotify", "match")] = await _spotify_row(ctx)
    await pipeline.edit_message(ctx.bot, status.chat.id, status.message_id, render())


async def _spotify_row(ctx: Ctx) -> str:
    """Metadata plus matching only: the download itself is the youtube row above."""
    rule = ctx.sources.get("spotify")
    assert rule.fetch_meta is not None and rule.match_via is not None
    started = time.monotonic()
    try:
        meta = await rule.fetch_meta(ctx.settings.selftest_spotify)
        match = await matching.find(ctx.settings, ctx.downloader, ctx.sources.get(rule.match_via), meta)
    except DownloadError as e:
        return f"❌ spotify · match · {html.escape(e.reason)}"
    except Exception:
        log.exception("selftest spotify match crashed")
        return "❌ spotify · match · error"
    found = html.escape(pipeline.truncate(f"{match.title} — {match.channel}", 60))
    return f"✅ spotify · match via {html.escape(match.via)} · {time.monotonic() - started:.1f}s\n    {found}"


# Bans


def _parse_id(command: CommandObject) -> int | None:
    arg = (command.args or "").strip()
    return int(arg) if arg.lstrip("-").isdigit() else None


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, ctx: Ctx) -> None:
    user_id = _parse_id(command)
    if user_id is None:
        await message.reply("Usage: /ban &lt;user id&gt;")
        return
    if user_id == ctx.settings.admin_id:
        await message.reply("You can't ban yourself.")
        return
    await ctx.db.set_banned(user_id, True)
    n = await tag_editor.close_user(ctx, user_id) + ctx.jobs.cancel_user(user_id)
    suffix = f", cancelled {n} job{'s' if n != 1 else ''}" if n else ""
    await message.reply(f"🚫 Banned <code>{user_id}</code>{suffix}.")


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject, ctx: Ctx) -> None:
    user_id = _parse_id(command)
    if user_id is None:
        await message.reply("Usage: /unban &lt;user id&gt;")
        return
    await ctx.db.set_banned(user_id, False)
    await message.reply(f"✅ Unbanned <code>{user_id}</code>.")
