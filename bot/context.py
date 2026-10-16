from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot

from .alerts import Alerts
from .config import Settings
from .db import DB
from .delivery import Delivery
from .downloader import Downloader
from .jobs import JobManager
from .metrics import Metrics
from .sources import Sources

log = logging.getLogger(__name__)


@dataclass(eq=False)
class Ctx:
    """Everything handlers need; lives in the dispatcher's workflow data as `ctx`."""

    settings: Settings
    bot: Bot
    db: DB
    jobs: JobManager
    sources: Sources
    downloader: Downloader
    delivery: Delivery
    metrics: Metrics
    alerts: Alerts
    closing: bool = False  # set on shutdown so cancelled jobs can say why
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
        """Starts a background task, keeps a reference to it and logs a crash."""
        task = asyncio.create_task(coro, name=name)
        self.tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.error("background task %s crashed", task.get_name(), exc_info=exc)
