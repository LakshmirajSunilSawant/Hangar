"""Resource usage per app — PRD Milestone 5, sized for the box it runs on.

The PRD names Prometheus, Grafana and Loki. All three are the right answer at a
scale this project does not have, and the wrong answer on a 12GB VM that is
also running Postgres, Caddy, the control plane and every app: the monitoring
stack would be the largest tenant on the machine.

What an owner actually wants from that milestone is a small number of things —
is my app near its memory cap, is it spinning the CPU, has it been restarting —
and Docker already knows all of it. So this reads `docker stats` on an interval
and keeps a short history in memory.

In memory, deliberately, like the idle tracker: samples every 15 seconds across
a handful of apps would otherwise be a steady write load on a free-tier disk to
store data nobody looks at after the afternoon. The cost is that history starts
empty after a restart, which the dashboard says plainly rather than drawing a
flat line and implying the app was quiet.

Reading stats is not free. Docker computes a CPU delta by sampling twice about
a second apart, so a pass costs roughly a second per app; the collector runs
them concurrently and on its own thread, never in a request.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

from . import config, store
from .store import AppStatus

log = logging.getLogger("hangar.metrics")

# Enough breadth to keep one pass quick without asking much of a 2-OCPU box.
MAX_CONCURRENT_READS = 4


@dataclass(frozen=True)
class Sample:
    """One reading for one app."""

    at: float                    # unix seconds, so the browser can plot it
    cpu_percent: float           # of one core; 100.0 means one core saturated
    memory_mb: float
    memory_limit_mb: float

    @property
    def memory_percent(self) -> float:
        if self.memory_limit_mb <= 0:
            return 0.0
        return round(self.memory_mb / self.memory_limit_mb * 100, 1)

    def as_dict(self) -> dict:
        return {**asdict(self), "memory_percent": self.memory_percent}


def read_sample(raw: dict) -> Sample | None:
    """Turn one Docker stats payload into a Sample.

    Pure, because the arithmetic is the part worth testing and a live daemon is
    a poor place to test arithmetic. Returns None when Docker hands back a
    reading it cannot complete — which it does for a container that stopped
    between listing it and asking about it.
    """
    cpu = raw.get("cpu_stats") or {}
    precpu = raw.get("precpu_stats") or {}
    memory = raw.get("memory_stats") or {}

    cpu_total = (cpu.get("cpu_usage") or {}).get("total_usage")
    pre_total = (precpu.get("cpu_usage") or {}).get("total_usage")
    system = cpu.get("system_cpu_usage")
    pre_system = precpu.get("system_cpu_usage")
    if None in (cpu_total, pre_total, system, pre_system):
        return None

    cpu_delta = cpu_total - pre_total
    system_delta = system - pre_system
    # First reading after a container starts has no previous sample to diff
    # against; reporting 0% is more honest than dividing by zero.
    if system_delta <= 0 or cpu_delta < 0:
        cpu_percent = 0.0
    else:
        # online_cpus is what `docker stats` uses; fall back to the per-CPU
        # array, which older daemons report instead.
        cores = cpu.get("online_cpus") or len(
            (cpu.get("cpu_usage") or {}).get("percpu_usage") or []
        ) or 1
        cpu_percent = round(cpu_delta / system_delta * cores * 100, 2)

    usage = memory.get("usage") or 0
    # Page cache counts toward `usage` but is reclaimable, so subtracting it
    # gives the number that actually predicts an OOM kill. This is what
    # `docker stats` shows in its MEM USAGE column.
    cache = (memory.get("stats") or {}).get("inactive_file", 0)
    resident = max(usage - cache, 0)

    return Sample(
        at=time.time(),
        cpu_percent=cpu_percent,
        memory_mb=round(resident / (1024 * 1024), 1),
        memory_limit_mb=round((memory.get("limit") or 0) / (1024 * 1024), 1),
    )


class History:
    """A bounded ring of samples per app. Thread-safe."""

    def __init__(self, depth: int = 120) -> None:
        self.depth = depth
        self._series: dict[str, deque[Sample]] = {}
        self._lock = threading.Lock()

    def record(self, app_id: str, sample: Sample) -> None:
        with self._lock:
            series = self._series.get(app_id)
            if series is None:
                series = self._series[app_id] = deque(maxlen=self.depth)
            series.append(sample)

    def samples(self, app_id: str) -> list[Sample]:
        with self._lock:
            return list(self._series.get(app_id, ()))

    def latest(self, app_id: str) -> Sample | None:
        samples = self.samples(app_id)
        return samples[-1] if samples else None

    def forget(self, app_id: str) -> None:
        with self._lock:
            self._series.pop(app_id, None)

    def clear(self) -> None:
        with self._lock:
            self._series.clear()


HISTORY = History()


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------


def _stats_for(app_id: str) -> Sample | None:
    """Ask Docker about one container. Never raises."""
    from .runtime import _container_name, client

    try:
        container = client().containers.get(_container_name(app_id))
        return read_sample(container.stats(stream=False))
    except Exception as exc:  # noqa: BLE001 - one bad app must not stop the pass
        log.debug("no stats for %s: %s", app_id, exc)
        return None


def collect(history: History | None = None) -> int:
    """One pass over every running app. Returns how many samples were taken."""
    history = history or HISTORY
    with store.session() as sess:
        app_ids = [
            app.id
            for app in store.list_apps(sess)
            if app.status == AppStatus.RUNNING.value
        ]
    if not app_ids:
        return 0

    taken = 0
    # Concurrent because each read blocks about a second inside Docker; a
    # sequential pass over five apps would outlast a 15s interval.
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_READS) as pool:
        for app_id, sample in zip(app_ids, pool.map(_stats_for, app_ids)):
            if sample is not None:
                history.record(app_id, sample)
                taken += 1
    return taken


class Collector:
    """Background thread sampling every app on an interval."""

    def __init__(self, history: History | None = None) -> None:
        self.history = history or HISTORY
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        settings = config.settings()
        if not settings.metrics_enabled or self.running:
            return False
        self.history.depth = settings.metrics_history
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="hangar-metrics", daemon=True
        )
        self._thread.start()
        log.info(
            "metrics collector started: every %ss, keeping %s samples",
            settings.metrics_interval,
            settings.metrics_history,
        )
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        interval = config.settings().metrics_interval
        # Sample immediately so a freshly started control plane has something
        # to show, then settle into the interval.
        while True:
            try:
                collect(self.history)
            except Exception:  # noqa: BLE001 - a bad pass must not kill the thread
                log.exception("metrics pass failed")
            if self._stop.wait(interval):
                return


COLLECTOR = Collector()
