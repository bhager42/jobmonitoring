"""Start checks when monitors are due, a few at a time, and run daily maintenance."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Monitor, utcnow
from app.services.checks import CheckRunner

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 10.0

Maintenance = Callable[[datetime], Awaitable[object]]


class Scheduler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        runner: CheckRunner,
        *,
        max_concurrent: int,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        clock: Callable[[], datetime] = utcnow,
        maintenance: Maintenance | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.runner = runner
        self.poll_interval = poll_interval
        self.clock = clock
        self.maintenance = maintenance
        self._slots = asyncio.Semaphore(max_concurrent)
        self._queued: set[int] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._loop_task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    # --- public API ---------------------------------------------------------------------------

    def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self, grace_seconds: float = STOP_TIMEOUT_SECONDS) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        if self._tasks:
            _done, pending = await asyncio.wait(set(self._tasks), timeout=grace_seconds)
            for task in pending:  # interrupted runs are closed on the next start
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def wake(self) -> None:
        """Re-check for due monitors now instead of at the next poll (e.g. after an edit)."""
        self._wake.set()

    def is_busy(self, monitor_id: int) -> bool:
        return monitor_id in self._queued or self.runner.is_running(monitor_id)

    def request_check(self, monitor_id: int) -> bool:
        """'Check now'. False if a check of this monitor is already queued or running."""
        if self.is_busy(monitor_id):
            return False
        self._start(monitor_id)
        return True

    async def tick(self) -> list[int]:
        """Queue every due monitor that isn't already queued; run maintenance if due."""
        now = self.clock()
        started = [
            monitor_id for monitor_id in await self.due(now) if self.request_check(monitor_id)
        ]
        if self.maintenance is not None:
            try:
                await self.maintenance(now)
            except Exception:
                log.exception("Maintenance failed")
        return started

    async def due(self, now: datetime) -> list[int]:
        async with self.session_factory() as db:
            rows = await db.scalars(
                select(Monitor.id)
                .where(
                    Monitor.enabled.is_(True),
                    or_(Monitor.next_check_at.is_(None), Monitor.next_check_at <= now),
                )
                .order_by(Monitor.next_check_at.is_not(None), Monitor.next_check_at, Monitor.id)
            )
            return list(rows.all())

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*set(self._tasks), return_exceptions=True)

    # --- internals ----------------------------------------------------------------------------

    def _start(self, monitor_id: int) -> None:
        self._queued.add(monitor_id)
        task = asyncio.get_running_loop().create_task(self._run(monitor_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, monitor_id: int) -> None:
        try:
            async with self._slots:
                await self.runner.run(monitor_id)
        except Exception:
            log.exception("Check of monitor %d crashed", monitor_id)
        finally:
            self._queued.discard(monitor_id)

    async def _loop(self) -> None:
        log.info("Scheduler started (polling every %.0fs)", self.poll_interval)
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Scheduler tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
            self._wake.clear()
