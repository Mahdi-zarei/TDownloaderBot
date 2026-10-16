from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable

from aiohttp import web


def _labels(**kw: str) -> str:
    def esc(v: str) -> str:
        return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    return "{" + ",".join(f'{k}="{esc(str(v))}"' for k, v in kw.items()) + "}"


class Metrics:
    """In-process counters exposed in Prometheus text format on /metrics."""

    def __init__(self) -> None:
        self.started = time.time()
        self.jobs: Counter[tuple[str, str]] = Counter()  # (source, status)
        self.failures: Counter[tuple[str, str, str]] = Counter()  # (source, egress, reason)
        self.bytes: Counter[str] = Counter()  # source
        self.cache_hits: Counter[str] = Counter()  # source

    def job_finished(self, source: str, status: str, *, egress: str = "", reason: str = "", nbytes: int = 0) -> None:
        self.jobs[(source, status)] += 1
        if reason:
            self.failures[(source, egress, reason)] += 1
        if nbytes:
            self.bytes[source] += nbytes

    def cache_hit(self, source: str) -> None:
        self.cache_hits[source] += 1

    def render(self, running: int, queued: int) -> str:
        out = [
            "# TYPE tgdl_jobs_total counter",
            *(f"tgdl_jobs_total{_labels(source=s, status=st)} {n}" for (s, st), n in self.jobs.items()),
            "# TYPE tgdl_failures_total counter",
            *(
                f"tgdl_failures_total{_labels(source=s, egress=e, reason=r)} {n}"
                for (s, e, r), n in self.failures.items()
            ),
            "# TYPE tgdl_bytes_sent_total counter",
            *(f"tgdl_bytes_sent_total{_labels(source=s)} {n}" for s, n in self.bytes.items()),
            "# TYPE tgdl_cache_hits_total counter",
            *(f"tgdl_cache_hits_total{_labels(source=s)} {n}" for s, n in self.cache_hits.items()),
            "# TYPE tgdl_jobs_running gauge",
            f"tgdl_jobs_running {running}",
            "# TYPE tgdl_jobs_queued gauge",
            f"tgdl_jobs_queued {queued}",
            "# TYPE tgdl_start_time_seconds gauge",
            f"tgdl_start_time_seconds {self.started:.0f}",
        ]
        return "\n".join(out) + "\n"

    async def serve(self, port: int, gauges: Callable[[], tuple[int, int]]) -> web.AppRunner:
        """Starts the /metrics server; `gauges` returns (running, queued). Caller cleans up the runner."""

        async def handle(_: web.Request) -> web.Response:
            running, queued = gauges()
            return web.Response(text=self.render(running, queued), content_type="text/plain", charset="utf-8")

        app = web.Application()
        app.router.add_get("/metrics", handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        return runner
