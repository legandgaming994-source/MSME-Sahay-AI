"""In-process periodic sweep of queued/stuck documents.

On platforms where the document disk is attached to the web service only (e.g. Render), a separate cron service could not
reach the files, so the sweep runs inside the web process. Enabled by MSME_SWEEP_INTERVAL_SECONDS (0/unset = off; values
are clamped to 30..86400). Each tick runs the (blocking) sweep in a worker thread; a failing tick is logged and the loop
continues; the loop stops cleanly on shutdown. Correctness never depends on this loop: documents are claimed with a
database compare-and-set, so a sweep racing with an API-triggered process is safe.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Optional

log = logging.getLogger("msme.sweeper")
MIN_INTERVAL, MAX_INTERVAL = 30.0, 86400.0


def interval_from_env(environ=None) -> float:
    raw = (os.environ if environ is None else environ).get("MSME_SWEEP_INTERVAL_SECONDS", "")
    try:
        value = float(str(raw).strip() or 0)
    except ValueError:
        log.warning("ignoring invalid MSME_SWEEP_INTERVAL_SECONDS", extra={"event": "sweeper_config"})
        return 0.0
    if value <= 0:
        return 0.0
    return min(max(value, MIN_INTERVAL), MAX_INTERVAL)


class PeriodicSweeper:
    def __init__(self, run: Callable[[], dict], interval: float, first_delay: Optional[float] = None):
        self._run = run
        self.interval = interval
        self._first_delay = interval if first_delay is None else first_delay
        self._task: Optional[asyncio.Task] = None
        self.ticks = 0
        self.failures = 0

    async def tick(self) -> None:
        try:
            result = await asyncio.to_thread(self._run)
            self.ticks += 1
            if result and (result.get("processed") or result.get("failed")):
                log.info("sweep finished: %s", result, extra={"event": "sweep"})
        except Exception:  # noqa: BLE001 - a bad tick must never kill the loop
            self.failures += 1
            log.error("sweep failed", exc_info=True, extra={"event": "sweep_failed"})

    async def _loop(self) -> None:
        await asyncio.sleep(self._first_delay)
        while True:
            await self.tick()
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if self.interval > 0 and self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
