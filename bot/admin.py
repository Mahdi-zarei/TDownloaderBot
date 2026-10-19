"""Admin router: /selftest, /ban, /unban. Statistics and user management live in manage.py."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil
import time

from aiogram import Router
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import CallbackQuery, Message

from . import matching, pipeline, playlist, tag_editor
from .config import Settings
from .context import Ctx
from .downloader import SelftestResult
from .models import DownloadError

log = logging.getLogger(__name__)
router = Router(name="admin")

_selftest_lock = asyncio.Lock()


class IsAdmin(Filter):
    async def __call__(self, event: Message | CallbackQuery, ctx: Ctx) -> bool:
        return event.from_user is not None and event.from_user.id == ctx.settings.admin_id


router.message.filter(IsAdmin())


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


async def stop_user_work(ctx: Ctx, user_id: int) -> int:
    """Cancels the user's playlists and jobs and closes their tag editors; returns how many were stopped."""
    lists = await playlist.close_user(ctx, user_id)
    return lists + await tag_editor.close_user(ctx, user_id) + ctx.jobs.cancel_user(user_id)


# Selftest


def _row(source: str, egress: str, result: SelftestResult | None) -> str:
    name = f"{html.escape(source)} · {html.escape(Settings.egress_label(egress))}"
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
        line = f"{html.escape(Settings.egress_label(egress))}: <code>{shown}</code>"
        if egress == "warp" and shown.startswith("error"):
            line += "\n    is the warp service running? It needs <code>COMPOSE_PROFILES=warp</code> in .env"
        lines.append(line)
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
    rows = {
        (src, eg): f"⏳ {html.escape(src)} · {html.escape(s.egress_label(eg))}" for src, _ in targets for eg in egresses
    }
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
                workdir = base / f"{source}-{egresses.index(egress)}"  # egress may be a URL, not a file name
                return source, egress, await ctx.downloader.selftest(rule, url, egress, workdir)
            except Exception:
                log.exception("selftest %s via %s crashed", source, s.egress_label(egress))
                return source, egress, None

    try:
        for done in asyncio.as_completed([one(src, url, eg) for src, url in targets for eg in egresses]):
            source, egress, result = await done
            rows[(source, egress)] = _row(source, egress, result)
            await pipeline.edit_message(ctx.bot, status.chat.id, status.message_id, render(), retry=False)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    rows[("spotify", "match")] = await _spotify_row(ctx)
    rows[("spotify", "list")] = await _spotify_list_row(ctx)
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


async def _spotify_list_row(ctx: Ctx) -> str:
    """Reading a playlist's tracks; each track then downloads like the row above."""
    rule = ctx.sources.get("spotify_list")
    assert rule.fetch_list is not None
    started = time.monotonic()
    try:
        tracks = await rule.fetch_list(ctx.settings.selftest_spotify_list)
    except DownloadError as e:
        return f"❌ spotify · playlist · {html.escape(e.reason)}"
    except Exception:
        log.exception("selftest spotify playlist crashed")
        return "❌ spotify · playlist · error"
    return f"✅ spotify · playlist · {len(tracks.items)} tracks · {time.monotonic() - started:.1f}s"


# Bans


async def _resolve_user(ctx: Ctx, command: CommandObject) -> int | None:
    """A numeric id as given (the user may not have used the bot yet), or a known @username."""
    arg = (command.args or "").strip()
    if re.fullmatch(r"-?\d+", arg):
        return int(arg)
    row = await ctx.db.find_user(arg) if arg else None
    return row["tg_id"] if row else None


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, ctx: Ctx) -> None:
    user_id = await _resolve_user(ctx, command)
    if user_id is None:
        await message.reply("Usage: /ban &lt;user id or @username&gt;")
        return
    if user_id == ctx.settings.admin_id:
        await message.reply("You can't ban yourself.")
        return
    await ctx.db.set_banned(user_id, True)
    n = await stop_user_work(ctx, user_id)
    suffix = f", cancelled {n} job{'s' if n != 1 else ''}" if n else ""
    await message.reply(f"🚫 Banned <code>{user_id}</code>{suffix}.")


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject, ctx: Ctx) -> None:
    user_id = await _resolve_user(ctx, command)
    if user_id is None:
        await message.reply("Usage: /unban &lt;user id or @username&gt;")
        return
    await ctx.db.set_banned(user_id, False)
    await message.reply(f"✅ Unbanned <code>{user_id}</code>.")
