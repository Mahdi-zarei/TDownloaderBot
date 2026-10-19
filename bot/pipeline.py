"""Job bodies, the shared runner, the status ticker and small Telegram helpers."""

from __future__ import annotations

import asyncio
import html
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters

from . import matching, spotify, tagging
from .alerts import scrub
from .context import Ctx
from .db import JobStatus
from .jobs import Job, QueueFull
from .models import (
    DownloadError,
    MediaInfo,
    MediaRef,
    PostFiles,
    Progress,
    Quality,
    Reason,
    Sent,
    Stage,
    Tags,
    too_long_text,
)
from .sources import SourceRule

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
GENERIC_FAILURE = "⚠️ Something went wrong. The admin has been notified."
CANCELLED_TEXT = "✖ Cancelled."
RESTART_TEXT = "✖ Stopped: the bot is restarting. Send the link again."
EXPIRED_TEXT = "Expired — send the link again."
EXPIRED_POPUP = "This has expired — send the link again."

STAGE_LABEL = {
    Stage.QUEUED: "⏳ Queued",
    Stage.STARTING: "⏳ Starting…",
    Stage.DOWNLOADING: "⬇️ Downloading…",
    Stage.PROCESSING: "⚙️ Processing…",
    Stage.UPLOADING: "📤 Uploading…",
}

Markup = InlineKeyboardMarkup | None
View = Callable[[Job], tuple[str, Markup]]
Flow = Callable[[Ctx, Job, Path], Awaitable[Sent | None]]


class CancelJob(CallbackData, prefix="cx"):
    job_id: int


def cancel_kb(job_id: int) -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(text="✖ Cancel", callback_data=CancelJob(job_id=job_id).pack())
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


# Telegram helpers: edits and deletes of our own messages may fail harmlessly.


async def edit_message(
    bot: Bot, chat_id: int, message_id: int, text: str, markup: Markup = None, *, retry: bool = True
) -> bool:
    for attempt in range(2):
        try:
            await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
            return True
        except TelegramRetryAfter as e:
            if not retry or attempt or e.retry_after > 30:
                log.debug("edit %s/%s throttled for %ss", chat_id, message_id, e.retry_after)
                return False
            await asyncio.sleep(e.retry_after)
        except TelegramBadRequest as e:
            if "not modified" in e.message:
                return True
            log.debug("edit %s/%s failed: %s", chat_id, message_id, e.message)
            return False
        except TelegramForbiddenError:
            return False
    return False


async def delete_message(bot: Bot, chat_id: int, message_id: int) -> bool:
    try:
        return await bot.delete_message(chat_id, message_id)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.debug("delete %s/%s failed: %s", chat_id, message_id, e)
        return False


async def answer(cb: CallbackQuery, text: str | None = None, *, alert: bool = False) -> None:
    try:
        await cb.answer(text, show_alert=alert)
    except TelegramBadRequest as e:  # the query is too old
        log.debug("callback answer failed: %s", e.message)


def reply_params(message_id: int | None) -> ReplyParameters | None:
    return ReplyParameters(message_id=message_id, allow_sending_without_reply=True) if message_id else None


async def quiet(aw: Awaitable[Any]) -> None:
    """Bookkeeping that must not turn a delivered file into a reported failure."""
    try:
        await aw
    except Exception:
        log.exception("bookkeeping failed")


# Text


def u16len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def truncate(text: str, limit: int) -> str:
    """Cuts to `limit` UTF-16 units (Telegram's measure), ending with an ellipsis when cut."""
    if u16len(text) <= limit:
        return text
    if limit <= 1:
        return "…"[:limit]
    out = text[: limit - 1]
    while u16len(out) > limit - 1:
        out = out[:-1]
    return out.rstrip() + "…"


def link(url: str, text: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(text)}</a>'


def progress_bar(fraction: float, width: int = 10) -> str:
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return "▰" * filled + "▱" * (width - filled)


def progress_text(p: Progress) -> str:
    lines = [STAGE_LABEL.get(p.stage, str(p.stage))]
    if p.fraction is not None:
        f = max(0.0, min(1.0, p.fraction))
        lines.append(f"{progress_bar(f)} {f:.0%}")
    if p.detail:
        lines.append(f"<i>{html.escape(p.detail)}</i>")
    return "\n".join(lines)


def queued_text(ctx: Ctx, job: Job) -> str | None:
    """Queue line while the job waits for a slot, else None."""
    if job.running or job.progress.stage is not Stage.QUEUED:
        return None
    ahead = ctx.jobs.queued_ahead(job)
    return f"⏳ Queued · {ahead} ahead" if ahead else "⏳ Queued"


# Reasons where the site's own words add something; for the others our text says it all, or the site's
# text would mislead (a bot check tells users to sign in, which they can't do here).
SHOW_CAUSE = frozenset({Reason.PRIVATE, Reason.UNAVAILABLE, Reason.LOGIN_REQUIRED, Reason.NO_MEDIA, Reason.FAILED})


def failure_text(e: DownloadError) -> str:
    """The user's failure message: our reason, the site's explanation when useful, and whether the admin knows."""
    lines = ["⚠️ " + html.escape(e.user_text)]
    if e.cause and e.reason in SHOW_CAUSE:
        lines.append(f"<i>Details: {html.escape(e.cause)}</i>")
    if not e.expected:
        lines.append("The admin has been notified.")
    return "\n".join(lines)


def queue_full_text(ctx: Ctx, user_id: int) -> str:
    n = len(ctx.jobs.user_jobs(user_id))
    return f"You already have {n} download{'s' if n != 1 else ''} in progress. Wait for one to finish, or use /cancel."


def video_caption(info: MediaInfo, url: str) -> str:
    return link(info.webpage_url or url, truncate(info.title, CAPTION_LIMIT))


def audio_caption(rule: SourceRule, info: MediaInfo, url: str) -> str:
    label = info.extractor if rule.name == "generic" and info.extractor not in ("", "Generic") else rule.label
    return link(info.webpage_url or url, label)


def post_caption(rule: SourceRule, post: PostFiles, url: str) -> str:
    author = (post.info.uploader or "").strip()
    who = f"@{author.lstrip('@')}" if author and " " not in author else (author or rule.label)
    head = link(post.info.webpage_url or url, truncate(who, 64))
    text = post.caption.strip()
    room = CAPTION_LIMIT - u16len(truncate(who, 64)) - 1
    if not text or room < 10:
        return head
    return f"{head}\n{html.escape(truncate(text, room))}"


# Status message


def status_view(ctx: Ctx) -> View:
    def view(job: Job) -> tuple[str, Markup]:
        return queued_text(ctx, job) or progress_text(job.progress), cancel_kb(job.id)

    return view


async def show(ctx: Ctx, job: Job, text: str, markup: Markup = None, *, retry: bool = True) -> None:
    """Edits the job's status message unless it already shows exactly this."""
    if job.status_message_id is None or job.data.get("_shown") == (text, markup):
        return
    if await edit_message(ctx.bot, job.chat_id, job.status_message_id, text, markup, retry=retry):
        job.data["_shown"] = (text, markup)


async def _tick(ctx: Ctx, job: Job, view: View) -> None:
    while True:
        try:
            text, markup = view(job)
            await show(ctx, job, text, markup, retry=False)
        except Exception:
            log.exception("status ticker for job %s", job.id)
        await asyncio.sleep(ctx.settings.progress_interval)


# Submitting and running jobs


async def send_from_cache(
    ctx: Ctx, rule: SourceRule, ref: MediaRef, fmt: str, *, user_id: int, chat_id: int, reply_to: int | None
) -> bool:
    """Re-sends a cached file by file_id; False when there is nothing (valid) cached."""
    key = ref.cache_key(fmt)
    sent = await ctx.db.get_cache(key)
    if sent is None:
        return False
    try:
        await ctx.delivery.send_cached(chat_id, sent, reply_to=reply_to)
    except TelegramBadRequest as e:
        log.info("dropping a stale %s cache entry: %s", rule.name, e.message)
        await ctx.db.delete_cache(key)
        return False
    ctx.metrics.cache_hit(rule.name)
    egress = ctx.settings.egress_label(rule.egress)

    async def record() -> None:
        job_id = await ctx.db.job_create(user_id, rule.name, fmt, egress=egress, status=JobStatus.CACHED)
        await ctx.db.job_finish(job_id, JobStatus.CACHED)

    await quiet(record())
    return True


async def enqueue(
    ctx: Ctx,
    *,
    rule: SourceRule,
    ref: MediaRef,
    fmt: str,
    user_id: int,
    chat_id: int,
    reply_to: int | None,
    flow: Flow,
    status_message_id: int | None = None,
    view: View | None = None,
    data: dict[str, Any] | None = None,
) -> Job | None:
    """Creates the job row and status message and submits the job; None when the user's queue is full."""
    if not ctx.jobs.can_submit(user_id):
        text = queue_full_text(ctx, user_id)
        if status_message_id is None:
            await ctx.bot.send_message(chat_id, text, reply_parameters=reply_params(reply_to))
        else:
            await edit_message(ctx.bot, chat_id, status_message_id, text)
        return None

    # Job.egress is for records and display only (DB, metrics, alerts); downloads use rule.egress.
    egress = ctx.settings.egress_label(rule.egress)
    job_id = await ctx.db.job_create(user_id, rule.name, fmt, egress=egress)
    job = Job(
        id=job_id, user_id=user_id, chat_id=chat_id, rule=rule, ref=ref, fmt=fmt, egress=egress,
        data={"reply_to": reply_to, **(data or {})},
    )
    render = view or status_view(ctx)
    text, markup = render(job)
    try:
        if status_message_id is None:
            msg = await ctx.bot.send_message(chat_id, text, reply_markup=markup, reply_parameters=reply_params(reply_to))
            status_message_id = msg.message_id
        else:
            await edit_message(ctx.bot, chat_id, status_message_id, text, markup)
    except Exception:
        await quiet(ctx.db.job_finish(job_id, JobStatus.FAILED, error="could not post the status message"))
        raise
    job.status_message_id = status_message_id
    job.data["_shown"] = (text, markup)
    try:
        ctx.jobs.submit(job, lambda j: run(ctx, j, flow, render))
    except QueueFull:  # another link of the same user won the race
        await edit_message(ctx.bot, chat_id, status_message_id, queue_full_text(ctx, user_id))
        await quiet(ctx.db.job_finish(job_id, JobStatus.CANCELLED, error="queue full"))
        return None
    return job


async def run(ctx: Ctx, job: Job, flow: Flow, view: View) -> None:
    """Shared job body: wait for a slot, run the flow under the job timeout, report the outcome.

    A flow returns the Sent it delivered, or None when it hands the job over to someone else
    (the tag editor) who then reports the outcome itself.
    """
    workdir = ctx.settings.jobs_dir / str(job.id)
    ticker = asyncio.create_task(_tick(ctx, job, view), name=f"ticker-{job.id}")
    try:
        try:
            sent = await _execute(ctx, job, flow, workdir)
        finally:
            ticker.cancel()
            await asyncio.wait({ticker})
        if sent is not None:
            await finish_success(ctx, job, sent)
    except asyncio.CancelledError:
        # Shielded so a second cancel (double tap, shutdown) can't leave the row and message stale.
        await asyncio.shield(ctx.spawn(report_cancel(ctx, job), name=f"cancel-{job.id}"))
        raise
    except Exception as e:
        await report_failure(ctx, job, e)
    finally:
        if not job.data.get("keep_dir"):
            shutil.rmtree(workdir, ignore_errors=True)


async def _execute(ctx: Ctx, job: Job, flow: Flow, workdir: Path) -> Sent | None:
    async with ctx.jobs.slot(job):
        await ctx.db.job_started(job.id)
        timeout = asyncio.timeout(ctx.settings.job_timeout)
        try:
            async with timeout:
                return await flow(ctx, job, workdir)
        except TimeoutError as e:
            if timeout.expired():
                raise DownloadError(Reason.TIMEOUT, f"job exceeded {ctx.settings.job_timeout}s") from e
            raise


async def finish_success(ctx: Ctx, job: Job, sent: Sent) -> None:
    await quiet(ctx.db.job_finish(job.id, JobStatus.DONE, nbytes=sent.size_bytes))
    ctx.metrics.job_finished(job.rule.name, JobStatus.DONE, egress=job.egress, nbytes=sent.size_bytes)
    if job.status_message_id is not None:
        await delete_message(ctx.bot, job.chat_id, job.status_message_id)


async def report_cancel(ctx: Ctx, job: Job) -> None:
    await show(ctx, job, RESTART_TEXT if ctx.closing else CANCELLED_TEXT)
    await quiet(ctx.db.job_finish(job.id, JobStatus.CANCELLED))
    ctx.metrics.job_finished(job.rule.name, JobStatus.CANCELLED, egress=job.egress)


def classify_failure(source: str, exc: Exception, ref: MediaRef) -> tuple[str, str, str, str, bool]:
    """(job status, stored error, metrics reason, alert kind, whether to alert) for a failure."""
    if isinstance(exc, DownloadError):
        status = JobStatus.TIMEOUT if exc.reason is Reason.TIMEOUT else JobStatus.FAILED
        error = f"{exc.reason}: {exc.detail[-2000:]}" if exc.detail else str(exc.reason)
        reason, kind, alert = str(exc.reason), f"{source}:{exc.reason}", not exc.expected
    else:
        status, error = JobStatus.FAILED, f"{type(exc).__name__}: {str(exc)[-2000:]}"
        reason, kind, alert = str(Reason.FAILED), f"{source}:{type(exc).__name__}", True
    # The failed request's link is kept in its own column for /link; the error text stays link-free.
    return status, scrub(error, [ref.media_id]), reason, kind, alert


async def report_failure(ctx: Ctx, job: Job, exc: Exception) -> None:
    source = job.rule.name
    status, error, reason, kind, alert = classify_failure(source, exc, job.ref)
    await show(ctx, job, failure_text(exc) if isinstance(exc, DownloadError) else GENERIC_FAILURE)
    await quiet(ctx.db.job_finish(job.id, status, error=error, url=job.ref.url))
    ctx.metrics.job_finished(source, status, egress=job.egress, reason=reason)
    if alert:
        await ctx.alerts.error(
            kind, f"Job failed ({status}, {job.fmt})", exc, private=[job.ref.media_id],
            user=job.user_id, source=source, job=job.id, link=f"/link_{job.id}", egress=job.egress,
            cookies=job.rule.cookies_problem(),
        )


async def record_failure(
    ctx: Ctx, rule: SourceRule, ref: MediaRef, fmt: str, user_id: int, exc: Exception
) -> tuple[int | None, bool]:
    """Logs a failure that happened outside a job (reading formats) as a failed request.

    Returns (job id, whether the admin should be alerted); the id is None if the row couldn't be written.
    """
    status, error, reason, _, alert = classify_failure(rule.name, exc, ref)
    egress = ctx.settings.egress_label(rule.egress)
    ctx.metrics.job_finished(rule.name, status, egress=egress, reason=reason)
    try:
        job_id = await ctx.db.job_create(user_id, rule.name, fmt, egress=egress, status=status)
        await ctx.db.job_finish(job_id, status, error=error, url=ref.url)
    except Exception:
        log.exception("could not record a %s failure", rule.name)
        return None, alert
    return job_id, alert


# Flows


async def _cache(ctx: Ctx, job: Job, sent: Sent) -> None:
    await quiet(ctx.db.put_cache(job.ref.cache_key(job.fmt), sent))


async def video_flow(ctx: Ctx, job: Job, workdir: Path) -> Sent:
    quality: Quality = job.data["quality"]
    video = await ctx.downloader.download_video(job.rule, job.ref.url, quality, workdir, job.progress)
    job.progress.set(Stage.UPLOADING)
    sent = await ctx.delivery.send_video(
        job.chat_id, video, caption=video_caption(video.info, job.ref.url), workdir=workdir,
        reply_to=job.data.get("reply_to"),
    )
    await _cache(ctx, job, sent)
    return sent


async def audio_flow(ctx: Ctx, job: Job, workdir: Path) -> Sent:
    audio = await ctx.downloader.download_audio(job.rule, job.ref.url, workdir, job.progress)
    job.progress.set(Stage.PROCESSING)
    tags = tagging.default_tags(audio.info)
    prepared = await tagging.prepare_audio(audio, tags, fmt=None, cover_src=audio.thumb, crop=True)
    job.progress.set(Stage.UPLOADING)
    sent = await ctx.delivery.send_audio(
        job.chat_id, prepared, caption=audio_caption(job.rule, audio.info, job.ref.url),
        reply_to=job.data.get("reply_to"),
    )
    await _cache(ctx, job, sent)
    return sent


async def match_flow(ctx: Ctx, job: Job, workdir: Path) -> Sent:
    """MATCH mode (Spotify): the source's metadata and cover, the audio of the best YouTube match."""
    rule = job.rule
    assert rule.fetch_meta is not None and rule.match_via is not None
    via = ctx.sources.get(rule.match_via)
    job.progress.set(Stage.STARTING, detail=f"Reading the {rule.label} track…")
    meta = await rule.fetch_meta(job.ref.url)
    if meta.duration and meta.duration > ctx.settings.max_duration:
        raise DownloadError(
            Reason.TOO_LONG, f"{meta.duration:.0f}s", user_text=too_long_text(ctx.settings.max_duration)
        )
    job.progress.set(Stage.STARTING, detail="Finding it on YouTube Music…")
    match = await matching.find(ctx.settings, ctx.downloader, via, meta)
    audio = await ctx.downloader.download_audio(via, match.url, workdir, job.progress)
    job.progress.set(Stage.PROCESSING)
    workdir.mkdir(parents=True, exist_ok=True)
    cover = await spotify.fetch_cover(meta.cover_url, workdir / "source-cover.jpg") if meta.cover_url else None
    tags = Tags(title=meta.title, artist=meta.artist, album=meta.album, year=meta.year)
    prepared = await tagging.prepare_audio(audio, tags, fmt=None, cover_src=cover or audio.thumb, crop=True)
    job.progress.set(Stage.UPLOADING)
    caption = f"{link(meta.url or job.ref.url, rule.label)} · {link(match.url, match.via)}"
    sent = await ctx.delivery.send_audio(job.chat_id, prepared, caption=caption, reply_to=job.data.get("reply_to"))
    await _cache(ctx, job, sent)
    return sent


async def post_flow(ctx: Ctx, job: Job, workdir: Path) -> Sent:
    post = await ctx.downloader.download_post(job.rule, job.ref.url, workdir, job.progress)
    job.progress.set(Stage.UPLOADING)
    sent = await ctx.delivery.send_post(
        job.chat_id, post, caption=post_caption(job.rule, post, job.ref.url), workdir=workdir,
        reply_to=job.data.get("reply_to"),
    )
    await _cache(ctx, job, sent)
    return sent
