from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import sys
import time
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramServerError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, User

from . import admin, handlers, manage, tag_editor
from .alerts import Alerts
from .config import Settings
from .context import Ctx
from .db import DB
from .delivery import Delivery
from .downloader import Downloader
from .jobs import JobManager
from .metrics import Metrics
from .sources import Sources

log = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="start", description="Start"),
    BotCommand(command="help", description="How it works"),
    BotCommand(command="cancel", description="Cancel your downloads"),
]
ADMIN_COMMANDS = [
    BotCommand(command="admin", description="Admin commands"),
    BotCommand(command="stats", description="Usage statistics"),
    BotCommand(command="users", description="Users and their usage"),
    BotCommand(command="user", description="One user's details"),
    BotCommand(command="jobs", description="Running and queued jobs"),
    BotCommand(command="failures", description="Recent failures"),
    BotCommand(command="uncache", description="Forget the cached file for a link"),
    BotCommand(command="broadcast", description="Message all users"),
    BotCommand(command="selftest", description="Test every source through every egress"),
    BotCommand(command="ban", description="Ban a user"),
    BotCommand(command="unban", description="Unban a user"),
]
API_WAIT = 120
HOUSEKEEPING_INTERVAL = 30


def sweep_jobs_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


async def wait_for_api(bot: Bot) -> User:
    """The local Bot API server may come up after us; retry get_me for a while."""
    deadline = time.monotonic() + API_WAIT
    delay = 1.0
    while True:
        try:
            return await bot.get_me()
        except TelegramUnauthorizedError:
            log.error(
                "The Bot API server rejected the token. Before moving to the local server, log the bot out "
                "of the cloud API once: https://api.telegram.org/bot<token>/logOut — and make sure the "
                "local server has api_id and api_hash set."
            )
            raise
        except (TelegramNetworkError, TelegramServerError) as e:
            if time.monotonic() + delay > deadline:
                raise
            log.info("Bot API not ready (%s); retrying in %.0fs", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)


async def set_commands(bot: Bot, settings: Settings) -> None:
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    try:
        await bot.set_my_commands(USER_COMMANDS + ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=settings.admin_id))
    except TelegramBadRequest as e:
        log.warning("could not set admin commands (has the admin started the bot?): %s", e.message)


async def housekeeping(ctx: Ctx) -> None:
    while True:
        await asyncio.sleep(HOUSEKEEPING_INTERVAL)
        try:
            await handlers.expire_pickers(ctx)
            tag_editor.expire_sessions(ctx)
        except Exception:
            log.exception("housekeeping failed")


async def shutdown(ctx: Ctx) -> None:
    ctx.closing = True
    pending = [j.task for j in ctx.jobs.all() if j.task is not None and not j.task.done()]
    for job in ctx.jobs.all():
        ctx.jobs.cancel(job.id)
    background = list(ctx.tasks)
    for task in background:
        task.cancel()
    if pending or background:
        await asyncio.wait([*pending, *background], timeout=15)


async def main() -> None:
    settings = Settings()  # pyright: ignore[reportCallIssue]
    logging.basicConfig(
        level=settings.log_level.upper(),
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sweep_jobs_dir(settings.jobs_dir)
    db = await DB.open(settings.db_path)
    bot = Bot(
        settings.bot_token,
        session=AiohttpSession(api=TelegramAPIServer.from_base(settings.bot_api_url, is_local=True)),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    ctx: Ctx | None = None
    runner = None
    try:
        me = await wait_for_api(bot)
        log.info("running as @%s", me.username)
        await set_commands(bot, settings)

        sources = Sources(settings)
        for rule in sources.rules:
            if problem := rule.cookies_problem():
                log.warning("%s cookies: %s", rule.name, problem)
        jobs = JobManager(
            global_limit=settings.global_downloads,
            user_running=settings.user_running,
            user_queued=settings.user_queued,
            source_limits={r.name: r.max_concurrent for r in sources.rules if r.max_concurrent},
        )
        metrics = Metrics()
        ctx = Ctx(
            settings=settings, bot=bot, db=db, jobs=jobs, sources=sources, downloader=Downloader(settings),
            delivery=Delivery(bot, settings), metrics=metrics, alerts=Alerts(bot, settings),
        )
        if settings.metrics_port:
            runner = await metrics.serve(
                settings.metrics_host, settings.metrics_port, lambda: (jobs.running_count, jobs.queued_count)
            )

        dp = Dispatcher(storage=MemoryStorage())
        dp["ctx"] = ctx
        access = handlers.AccessMiddleware()
        dp.message.outer_middleware(access)
        dp.callback_query.outer_middleware(access)
        dp.errors.register(handlers.on_error)
        dp.include_routers(admin.router, manage.router, tag_editor.router, handlers.router)

        ctx.spawn(housekeeping(ctx), name="housekeeping")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), close_bot_session=False)
    finally:
        if ctx is not None:
            await shutdown(ctx)
        if runner is not None:
            await runner.cleanup()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
