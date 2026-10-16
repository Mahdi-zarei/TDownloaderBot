from __future__ import annotations

import html
import logging
import time
import traceback
from collections import Counter
from typing import Any

from aiogram import Bot

from .config import Settings
from .models import DownloadError

log = logging.getLogger(__name__)

LIMIT = 4096
_CONTEXT_ORDER = ("user", "source", "url", "job")


class Alerts:
    """Error reports to the admin, at most one per `admin_alert_interval` per kind."""

    def __init__(self, bot: Bot, settings: Settings):
        self.bot = bot
        self.settings = settings
        self._last: dict[str, float] = {}
        self._suppressed: Counter[str] = Counter()

    async def error(self, kind: str, text: str, exc: BaseException | None = None, **context: Any) -> None:
        try:
            log.error("%s: %s %s", kind, text, context, exc_info=exc)
            now = time.monotonic()
            last = self._last.get(kind)
            if last is not None and now - last < self.settings.admin_alert_interval:
                self._suppressed[kind] += 1
                return
            self._last[kind] = now
            suppressed = self._suppressed.pop(kind, 0)
            await self.bot.send_message(self.settings.admin_id, self._render(kind, text, exc, suppressed, context))
        except Exception:
            log.exception("failed to send admin alert %s", kind)

    @staticmethod
    def _render(kind: str, text: str, exc: BaseException | None, suppressed: int, context: dict[str, Any]) -> str:
        lines = [f"🚨 <b>{html.escape(kind)}</b>"]
        if suppressed:
            lines[0] += f" (+{suppressed} similar suppressed)"
        lines.append(html.escape(text[:500]))
        keys = [k for k in _CONTEXT_ORDER if k in context] + [k for k in context if k not in _CONTEXT_ORDER]
        for key in keys:
            value = context[key]
            if value is not None and value != "":
                lines.append(f"{html.escape(key)}: <code>{html.escape(str(value)[:300])}</code>")
        header = "\n".join(lines)

        if isinstance(exc, DownloadError) and exc.detail:
            detail = exc.detail
        elif exc is not None:
            detail = "".join(traceback.format_exception(exc))
        else:
            detail = ""
        if not detail.strip():
            return header[:LIMIT]

        # Escaped length is an upper bound of what Telegram counts after parsing entities.
        budget = LIMIT - len(header) - len("\n<pre></pre>") - 2
        body = html.escape(detail.strip())
        if budget < 100:
            return header[:LIMIT]
        if len(body) > budget:
            body = "…" + body[-(budget - 1):]
        return f"{header}\n<pre>{body}</pre>"
