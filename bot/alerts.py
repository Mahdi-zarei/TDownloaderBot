from __future__ import annotations

import html
import logging
import re
import time
import traceback
from collections import Counter
from collections.abc import Iterable
from typing import Any

from aiogram import Bot

from .config import Settings
from .models import DownloadError

log = logging.getLogger(__name__)

LIMIT = 4096
_CONTEXT_ORDER = ("user", "source", "job", "link")

# Privacy: what users download stays out of alerts and logs. Links, search queries and the media id
# yt-dlp prints after its extractor tag ("[youtube] dQw4w9WgXcQ: ...") are masked.
_URL = re.compile(r"\bhttps?://\S+", re.IGNORECASE)
_SEARCH = re.compile(r"'ytsearch\d*:[^']*'|\bytsearch\d*:\S+", re.IGNORECASE)
_TOOL_TAGS = "download|info|debug|Merger|ExtractAudio|Fixup\\w*|VideoRemuxer|VideoConvertor|ThumbnailsConvertor|Metadata|MoveFiles"
_EXTRACTOR_ID = re.compile(rf"(\[(?!(?:{_TOOL_TAGS})\])[\w:.+-]+\]) [^\s:\[\]]+:(?=\s|$)")


def scrub(text: str, private: Iterable[str] = ()) -> str:
    """`text` with links, search queries and media ids masked; `private` adds exact strings to mask."""
    for s in private:
        if len(s) >= 4:
            text = re.sub(re.escape(s), "<id>", text, flags=re.IGNORECASE)
    text = _SEARCH.sub("<search>", text)
    text = _URL.sub("<link>", text)
    return _EXTRACTOR_ID.sub(r"\1 <id>:", text)


class Alerts:
    """Error reports to the admin, at most one per `admin_alert_interval` per kind."""

    def __init__(self, bot: Bot, settings: Settings):
        self.bot = bot
        self.settings = settings
        self._last: dict[str, float] = {}
        self._suppressed: Counter[str] = Counter()

    async def error(
        self, kind: str, text: str, exc: BaseException | None = None, *, private: Iterable[str] = (), **context: Any
    ) -> None:
        """`private`: strings to mask besides links, e.g. the media id of the failed request."""
        try:
            private = tuple(p for p in private if p)
            detail = scrub(self._detail(exc), private)
            context = {k: scrub(str(v), private) for k, v in context.items() if v is not None and v != ""}
            log.error("%s: %s %s%s", kind, scrub(text, private), context, f"\n{detail}" if detail else "")
            now = time.monotonic()
            last = self._last.get(kind)
            if last is not None and now - last < self.settings.admin_alert_interval:
                self._suppressed[kind] += 1
                return
            self._last[kind] = now
            suppressed = self._suppressed.pop(kind, 0)
            body = self._render(kind, scrub(text, private), detail, suppressed, context)
            await self.bot.send_message(self.settings.admin_id, body)
        except Exception:
            log.exception("failed to send admin alert %s", kind)

    @staticmethod
    def _detail(exc: BaseException | None) -> str:
        if isinstance(exc, DownloadError) and exc.detail:
            return exc.detail.strip()
        return "".join(traceback.format_exception(exc)).strip() if exc is not None else ""

    @staticmethod
    def _render(kind: str, text: str, detail: str, suppressed: int, context: dict[str, str]) -> str:
        lines = [f"🚨 <b>{html.escape(kind)}</b>"]
        if suppressed:
            lines[0] += f" (+{suppressed} similar suppressed)"
        lines.append(html.escape(text[:500]))
        keys = [k for k in _CONTEXT_ORDER if k in context] + [k for k in context if k not in _CONTEXT_ORDER]
        for key in keys:
            value = context[key]
            # Commands such as /link_27 stay tappable; everything else is shown verbatim.
            shown = html.escape(value[:300]) if value.startswith("/") else f"<code>{html.escape(value[:300])}</code>"
            lines.append(f"{html.escape(key)}: {shown}")
        header = "\n".join(lines)
        if not detail:
            return header[:LIMIT]

        # Escaped length is an upper bound of what Telegram counts after parsing entities.
        budget = LIMIT - len(header) - len("\n<pre></pre>") - 2
        body = html.escape(detail)
        if budget < 100:
            return header[:LIMIT]
        if len(body) > budget:
            body = "…" + body[-(budget - 1):]
        return f"{header}\n<pre>{body}</pre>"
