from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from .models import MediaRef, Progress, Stage
from .sources import SourceRule


class QueueFull(Exception):
    pass


@dataclass(eq=False)
class Job:
    id: int  # jobs.id in the DB
    user_id: int
    chat_id: int
    rule: SourceRule
    ref: MediaRef
    fmt: str  # format part of the cache key: "1080p", "audio", "post", ...
    egress: str
    progress: Progress = field(default_factory=Progress)
    status_message_id: int | None = None
    task: asyncio.Task[None] | None = None
    running: bool = False
    data: dict[str, Any] = field(default_factory=dict)  # free-form per-flow state


JobFn = Callable[[Job], Coroutine[Any, Any, None]]


class JobManager:
    """Per-user, per-source and global concurrency for jobs.

    `submit` starts the job's coroutine right away; the coroutine enters `slot(job)` to wait
    for its turn, so cancelling a queued job raises CancelledError inside the job's own code
    and it can report that like any other outcome.
    """

    def __init__(self, *, global_limit: int, user_running: int, user_queued: int, source_limits: dict[str, int]):
        self._global = asyncio.Semaphore(global_limit)
        self._user_running = user_running
        self._user_cap = user_running + user_queued
        self._user_sems: dict[int, asyncio.Semaphore] = {}
        self._source_sems = {name: asyncio.Semaphore(n) for name, n in source_limits.items() if n}
        self._jobs: dict[int, Job] = {}

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def user_jobs(self, user_id: int) -> list[Job]:
        return [j for j in self._jobs.values() if j.user_id == user_id]

    def can_submit(self, user_id: int) -> bool:
        return len(self.user_jobs(user_id)) < self._user_cap

    @property
    def running_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j.running)

    @property
    def queued_count(self) -> int:
        return sum(1 for j in self._jobs.values() if not j.running)

    def queued_ahead(self, job: Job) -> int:
        """Rough queue position: waiting jobs submitted before this one."""
        return sum(1 for j in self._jobs.values() if not j.running and j.id < job.id)

    def submit(self, job: Job, fn: JobFn) -> asyncio.Task[None]:
        if not self.can_submit(job.user_id):
            raise QueueFull
        self._jobs[job.id] = job
        job.task = asyncio.create_task(fn(job), name=f"job-{job.id}")
        # A done-callback also fires for a task cancelled before it ever started running.
        job.task.add_done_callback(lambda _: self._jobs.pop(job.id, None))
        return job.task

    @asynccontextmanager
    async def slot(self, job: Job) -> AsyncIterator[None]:
        """Waits for the user's, the source's and the global slot, always in that order."""
        user_sem = self._user_sems.setdefault(job.user_id, asyncio.Semaphore(self._user_running))
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(user_sem)
            if source_sem := self._source_sems.get(job.rule.name):
                await stack.enter_async_context(source_sem)
            await stack.enter_async_context(self._global)
            job.running = True
            job.progress.set(Stage.STARTING)
            try:
                yield
            finally:
                job.running = False

    def cancel(self, job_id: int, user_id: int | None = None) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.task is None or job.task.done():
            return False
        if user_id is not None and job.user_id != user_id:
            return False
        job.task.cancel()
        return True

    def cancel_user(self, user_id: int) -> int:
        return sum(self.cancel(j.id) for j in self.user_jobs(user_id))
