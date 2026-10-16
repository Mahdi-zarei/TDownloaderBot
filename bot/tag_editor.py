"""Custom-tags dialog: a card with the tags and download status, edited field by field.

The card is the status message of a download-only job. The job resolves `TagSession.ready`
and keeps its workdir; Done tags and sends the file outside the job, and the session then
reports the outcome on the same DB row.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Document, InlineKeyboardButton, InlineKeyboardMarkup, Message, PhotoSize

from . import pipeline, tagging
from .context import Ctx
from .db import JobStatus
from .jobs import Job
from .models import AudioFile, DownloadError, MediaInfo, MediaRef, Probe, Reason, Sent, Stage, Tags
from .sources import SourceRule

log = logging.getLogger(__name__)
router = Router(name="tag_editor")

TEXT_FIELDS = ("title", "artist", "album", "year")
PROMPTS = {
    "title": "Send the new title.",
    "artist": "Send the new artist, or - to clear it.",
    "album": "Send the album name, or - to clear it.",
    "year": "Send the year, e.g. 2021, or - to clear it.",
    "cover": "Send a photo for the cover, or pick one:",
}
COVER_LABELS = {
    "square": "Thumbnail · square",
    "full": "Thumbnail · full frame",
    "none": "No cover",
    "custom": "Your image",
}
COVER_ACTIONS = {"c_sq": "square", "c_full": "full", "c_none": "none"}
EXPIRED = "This editor expired"
MAX_COVER_BYTES = 20 * 1024 * 1024
_SUFFIX_RE = re.compile(r"\.[a-z0-9]{1,5}")


class TagEdit(StatesGroup):
    waiting = State()


class TagCb(CallbackData, prefix="tg"):
    t: str  # session token
    a: str  # field name, cover choice, "fmt", "reset", "done" or "cancel"


@dataclass(eq=False)
class TagSession:
    token: str
    user_id: int
    chat_id: int
    message_id: int  # the card
    reply_to: int
    rule: SourceRule
    ref: MediaRef
    info: MediaInfo
    state: FSMContext
    defaults: Tags
    tags: Tags
    cover: str = "square"
    cover_path: Path | None = None
    fmt: str = "m4a"
    job: Job | None = None
    workdir: Path | None = None
    ready: asyncio.Future[AudioFile] = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    phase: str = "editing"  # editing -> finishing (Done, waiting for the download) -> sending
    prompt_id: int | None = None
    touched: float = field(default_factory=time.monotonic)
    finish_task: asyncio.Task[None] | None = None
    closed: bool = False
    finalized: bool = False  # the job row's outcome has been written

    def audio(self) -> AudioFile | None:
        f = self.ready
        return f.result() if f.done() and not f.cancelled() and f.exception() is None else None


_sessions: dict[str, TagSession] = {}


# Card


def _status_line(ctx: Ctx, s: TagSession, job: Job) -> str:
    if s.phase == "sending":
        return pipeline.progress_text(job.progress)
    if s.audio() is not None:
        return "✅ Audio ready — tap Done to send."
    line = pipeline.queued_text(ctx, job) or pipeline.progress_text(job.progress)
    return f"Finishing download…\n{line}" if s.phase == "finishing" else line


def _button(s: TagSession, text: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=TagCb(t=s.token, a=action).pack())


def _keyboard(s: TagSession) -> InlineKeyboardMarkup:
    if s.phase != "editing":
        layout = [[("✖ Cancel", "cancel")]]
    else:
        layout = [
            [("Title", "title"), ("Artist", "artist"), ("Album", "album")],
            [("Year", "year"), ("Cover", "cover"), (f"Format: {s.fmt.upper()}", "fmt")],
            [("Reset", "reset"), ("✅ Done", "done"), ("✖ Cancel", "cancel")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=[[_button(s, text, a) for text, a in row] for row in layout])


def _card(ctx: Ctx, s: TagSession, job: Job) -> tuple[str, InlineKeyboardMarkup]:
    def val(v: str) -> str:
        return html.escape(v) if v else "—"

    t = s.tags
    text = "\n".join([
        "🎛 <b>Audio · custom tags</b>",
        f"<b>Title:</b> {val(t.title)}",
        f"<b>Artist:</b> {val(t.artist)}",
        f"<b>Album:</b> {val(t.album)}",
        f"<b>Year:</b> {val(t.year)}",
        f"<b>Cover:</b> {COVER_LABELS[s.cover]}",
        f"<b>Format:</b> {s.fmt.upper()}",
        "",
        _status_line(ctx, s, job),
    ])
    return text, _keyboard(s)


async def _render(ctx: Ctx, s: TagSession) -> None:
    if s.job is not None and not s.closed:
        await pipeline.show(ctx, s.job, *_card(ctx, s, s.job))


# Lifecycle


async def open_editor(
    ctx: Ctx, state: FSMContext, *, rule: SourceRule, ref: MediaRef, probe: Probe, user_id: int, chat_id: int,
    message_id: int, reply_to: int,
) -> None:
    """Turns the picker message into a tag card and starts downloading the audio."""
    defaults = tagging.default_tags(probe.info)
    s = TagSession(
        token=secrets.token_urlsafe(6), user_id=user_id, chat_id=chat_id, message_id=message_id,
        reply_to=reply_to, rule=rule, ref=ref, info=probe.info, state=state, defaults=defaults,
        tags=replace(defaults),
    )
    _sessions[s.token] = s

    async def flow(ctx: Ctx, job: Job, workdir: Path) -> Sent | None:
        audio = await ctx.downloader.download_audio(job.rule, job.ref.url, workdir, job.progress)
        if not s.ready.done():
            s.ready.set_result(audio)
        return None

    try:
        job = await pipeline.enqueue(
            ctx, rule=rule, ref=ref, fmt="audio-custom", user_id=user_id, chat_id=chat_id, reply_to=reply_to,
            flow=flow, status_message_id=message_id, view=lambda j: _card(ctx, s, j), data={"keep_dir": True},
        )
    except BaseException:
        _sessions.pop(s.token, None)
        raise
    if job is None or job.task is None:
        _sessions.pop(s.token, None)
        return
    s.job = job
    s.workdir = ctx.settings.jobs_dir / str(job.id)
    job.task.add_done_callback(lambda _: _download_done(ctx, s))


def _download_done(ctx: Ctx, s: TagSession) -> None:
    if s.closed:
        return
    if s.audio() is not None:
        if s.phase == "editing":
            ctx.spawn(_render(ctx, s), name="tag-render")
        return
    # Failed, timed out or cancelled: the runner already updated the card and the job row.
    if not s.ready.done():
        s.ready.cancel()
    s.finalized = True
    ctx.spawn(_close(ctx, s, None), name="tag-close")


async def _close(ctx: Ctx, s: TagSession, text: str | None) -> None:
    """Ends the session: stops its work, optionally replaces the card with `text`, cleans up."""
    if s.closed:
        return
    s.closed = True
    _sessions.pop(s.token, None)
    me = asyncio.current_task()
    waits: list[asyncio.Task[None]] = []
    for task in (s.finish_task, s.job.task if s.job else None):
        if task is not None and task is not me and not task.done():
            task.cancel()
            waits.append(task)
    if waits:
        await asyncio.wait(waits, timeout=15)
    if not s.ready.done():
        s.ready.cancel()

    job = s.job
    if job is not None and not s.finalized and s.audio() is not None:
        s.finalized = True
        await pipeline.quiet(ctx.db.job_finish(job.id, JobStatus.CANCELLED))
        ctx.metrics.job_finished(job.rule.name, JobStatus.CANCELLED, egress=job.egress)
    if text:
        if job is not None:
            await pipeline.show(ctx, job, text)
        else:
            await pipeline.edit_message(ctx.bot, s.chat_id, s.message_id, text)
    await _drop_prompt(ctx, s)
    await _release_state(s)
    if s.workdir is not None:
        shutil.rmtree(s.workdir, ignore_errors=True)


async def close_user(ctx: Ctx, user_id: int) -> int:
    sessions = [s for s in _sessions.values() if s.user_id == user_id]
    await asyncio.gather(*(_close(ctx, s, pipeline.CANCELLED_TEXT) for s in sessions))
    return len(sessions)


def expire_sessions(ctx: Ctx) -> None:
    now = time.monotonic()
    for s in list(_sessions.values()):
        if s.phase == "editing" and now - s.touched > ctx.settings.tag_editor_ttl:
            ctx.spawn(_close(ctx, s, "Expired."), name="tag-expire")


def _expired(ctx: Ctx, s: TagSession) -> bool:
    return s.phase == "editing" and time.monotonic() - s.touched > ctx.settings.tag_editor_ttl


async def _finish(ctx: Ctx, s: TagSession) -> None:
    job = s.job
    assert job is not None
    await _render(ctx, s)
    await asyncio.wait({s.ready})
    audio = s.audio()
    if audio is None or s.closed:
        return  # the download failed; _download_done closes the session

    s.phase = "sending"
    job.progress.set(Stage.PROCESSING)
    await _render(ctx, s)
    cover_src = {"square": audio.thumb, "full": audio.thumb, "custom": s.cover_path}.get(s.cover)
    timeout = asyncio.timeout(ctx.settings.job_timeout)
    try:
        async with timeout:
            prepared = await tagging.prepare_audio(
                audio, s.tags, fmt=s.fmt, cover_src=cover_src, crop=s.cover == "square"
            )
            job.progress.set(Stage.UPLOADING)
            await _render(ctx, s)
            sent = await ctx.delivery.send_audio(
                s.chat_id, prepared, caption=pipeline.audio_caption(job.rule, audio.info, job.ref.url),
                reply_to=s.reply_to,
            )
    except Exception as e:
        failure = e
        if isinstance(e, TimeoutError) and timeout.expired():
            failure = DownloadError(Reason.TIMEOUT, f"tagging and upload exceeded {ctx.settings.job_timeout}s")
        s.finalized = True
        await pipeline.report_failure(ctx, job, failure)
    else:
        s.finalized = True
        await pipeline.finish_success(ctx, job, sent)
    await _close(ctx, s, None)


# Prompts and FSM state


async def _drop_prompt(ctx: Ctx, s: TagSession) -> None:
    if s.prompt_id is not None:
        prompt_id, s.prompt_id = s.prompt_id, None
        await pipeline.delete_message(ctx.bot, s.chat_id, prompt_id)


async def _waiting_token(state: FSMContext) -> str | None:
    if await state.get_state() != TagEdit.waiting.state:
        return None
    return (await state.get_data()).get("token")


async def _release_state(s: TagSession, state: FSMContext | None = None) -> None:
    """Clears the FSM state if it is waiting for input for this session."""
    state = state or s.state
    try:
        if await _waiting_token(state) == s.token:
            await state.clear()
    except Exception:
        log.exception("could not clear tag editor state")


def _cover_keyboard(s: TagSession) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_button(s, COVER_LABELS[mode], action)] for action, mode in COVER_ACTIONS.items()
    ])


async def _ask(ctx: Ctx, s: TagSession, state: FSMContext, field_name: str) -> None:
    await _drop_prompt(ctx, s)
    other = _sessions.get(await _waiting_token(state) or "")
    if other is not None and other is not s:
        await _drop_prompt(ctx, other)
    markup = _cover_keyboard(s) if field_name == "cover" else None
    prompt = await ctx.bot.send_message(s.chat_id, PROMPTS[field_name], reply_markup=markup)
    s.prompt_id = prompt.message_id
    await state.set_state(TagEdit.waiting)
    await state.set_data({"token": s.token, "field": field_name})


async def _hint(ctx: Ctx, s: TagSession, message: Message, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """Shows what went wrong in the prompt itself and removes the rejected message."""
    await pipeline.delete_message(ctx.bot, message.chat.id, message.message_id)
    if s.prompt_id is None or not await pipeline.edit_message(ctx.bot, s.chat_id, s.prompt_id, text, markup):
        s.prompt_id = (await ctx.bot.send_message(s.chat_id, text, reply_markup=markup)).message_id


async def _take_cover(ctx: Ctx, s: TagSession, message: Message) -> str | None:
    """Downloads a sent photo or image file as the cover; returns an error hint on failure."""
    target: PhotoSize | Document
    suffix = ".jpg"
    if message.photo:
        target = message.photo[-1]
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        target = message.document
        if (target.file_size or 0) > MAX_COVER_BYTES:
            return "That image is too large. Send one under 20 MB."
        name_suffix = Path(target.file_name or "").suffix.lower()
        suffix = name_suffix if _SUFFIX_RE.fullmatch(name_suffix) else ".img"
    else:
        return "Send a photo or an image file, or pick an option."
    assert s.workdir is not None
    path = s.workdir / f"cover-{message.message_id}{suffix}"
    try:
        s.workdir.mkdir(parents=True, exist_ok=True)
        await ctx.bot.download(target, destination=path)
    except Exception:
        log.exception("cover download failed")
        return "Couldn't read that image. Try another one."
    s.cover, s.cover_path = "custom", path
    return None


def _not_command(message: Message) -> bool:
    return not (message.text or "").startswith("/")


@router.message(TagEdit.waiting, _not_command)
async def on_value(message: Message, ctx: Ctx, state: FSMContext) -> None:
    data = await state.get_data()
    s = _sessions.get(data.get("token", ""))
    field_name = data.get("field")
    if s is None or s.closed or s.phase != "editing" or field_name not in PROMPTS:
        await state.clear()
        raise SkipHandler  # treat it as an ordinary message
    s.touched = time.monotonic()

    if field_name == "cover":
        if (error := await _take_cover(ctx, s, message)) is not None:
            await _hint(ctx, s, message, f"{error}\n\n{PROMPTS['cover']}", _cover_keyboard(s))
            return
    else:
        value = (message.text or "").strip()
        if value == "-" and field_name != "title":
            value = ""
        elif not value or value == "-":
            await _hint(ctx, s, message, f"Send the {field_name} as text.", None)
            return
        elif field_name == "year" and not re.fullmatch(r"\d{4}", value):
            await _hint(ctx, s, message, "Send the year as 4 digits, e.g. 2021, or - to clear it.", None)
            return
        setattr(s.tags, field_name, value[:256])

    await state.clear()
    await _drop_prompt(ctx, s)
    await pipeline.delete_message(ctx.bot, message.chat.id, message.message_id)
    await _render(ctx, s)


@router.callback_query(TagCb.filter())
async def on_button(cb: CallbackQuery, callback_data: TagCb, ctx: Ctx, state: FSMContext) -> None:
    s = _sessions.get(callback_data.t)
    if s is None or s.closed:
        await pipeline.answer(cb, EXPIRED)
        return
    if _expired(ctx, s):
        await pipeline.answer(cb, EXPIRED)
        await _close(ctx, s, "Expired.")
        return
    if s.job is None:
        await pipeline.answer(cb, "One moment…")
        return
    action = callback_data.a
    if action == "cancel":
        await pipeline.answer(cb, "Cancelled")
        await _close(ctx, s, pipeline.CANCELLED_TEXT)
        return
    if s.phase != "editing":
        await pipeline.answer(cb, "Already sending…")
        return
    s.touched = time.monotonic()

    if action in PROMPTS:
        await pipeline.answer(cb)
        await _ask(ctx, s, state, action)
        return
    if action in COVER_ACTIONS:
        s.cover = COVER_ACTIONS[action]
    elif action == "fmt":
        s.fmt = "mp3" if s.fmt == "m4a" else "m4a"
    elif action == "reset":
        s.tags, s.cover, s.cover_path, s.fmt = replace(s.defaults), "square", None, "m4a"
    elif action == "done":
        s.phase = "finishing"
        s.finish_task = ctx.spawn(_finish(ctx, s), name=f"tag-finish-{s.token}")
    else:
        await pipeline.answer(cb)
        return
    await pipeline.answer(cb)
    if action != "fmt":
        await _release_state(s, state)
        await _drop_prompt(ctx, s)
    if action != "done":
        await _render(ctx, s)
