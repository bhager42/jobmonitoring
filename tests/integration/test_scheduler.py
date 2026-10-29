"""Scheduler timing (fake clock), concurrency, and daily maintenance."""

import asyncio
import random
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.engine import create_engine, create_session_factory
from app.db.models import CheckRun, Match, Monitor, Posting
from app.scraping.base import FetchContext
from app.scraping.http import PoliteClient
from app.services.app_settings import get_setting
from app.services.checks import CheckResult, CheckRunner, MonitorSnapshot
from app.services.maintenance import (
    LAST_RUN_KEY,
    maintenance_due,
    prune,
    run_maintenance_if_due,
)
from app.services.scheduler import Scheduler
from tests.integration.test_checks import Board, Clock, add_monitor, job
from tests.scraping_helpers import make_client

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
API = "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs"


@pytest.fixture
async def factory(migrated_settings: Settings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_settings.db_path)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def board() -> Iterator[Board]:
    board = Board()
    board.jobs = [job(1, "Backend Engineer")]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith=API).mock(side_effect=board)
        yield board


@pytest.fixture
async def http() -> AsyncIterator[PoliteClient]:
    async with make_client(max_retries=0) as client:
        yield client


async def next_check(factory: async_sessionmaker[AsyncSession], monitor_id: int) -> datetime:
    async with factory() as db:
        monitor = await db.get(Monitor, monitor_id)
        assert monitor is not None and monitor.next_check_at is not None
        return monitor.next_check_at


# --- timing with a fake clock ------------------------------------------------------------------


async def test_checks_run_when_due(
    board: Board, factory: async_sessionmaker[AsyncSession], http: PoliteClient
) -> None:
    clock = Clock()

    def context(snapshot: MonitorSnapshot) -> FetchContext:
        return FetchContext(client=http, render_mode="static", respect_robots=False)

    runner = CheckRunner(factory, context, clock=clock, rng=random.Random(1))
    scheduler = Scheduler(factory, runner, max_concurrent=2, clock=clock)
    hourly = await add_monitor(factory, interval_hours=1)
    daily = await add_monitor(factory, interval_hours=24)
    paused = await add_monitor(factory, enabled=False)

    # New monitors have no next_check_at, so both enabled ones run straight away.
    assert sorted(await scheduler.tick()) == [hourly, daily]
    await scheduler.wait_idle()
    first_hourly = await next_check(factory, hourly)
    first_daily = await next_check(factory, daily)
    assert timedelta(minutes=54) <= first_hourly - T0 <= timedelta(minutes=66)
    assert timedelta(hours=21.6) <= first_daily - T0 <= timedelta(hours=26.4)

    # Nothing is due a minute later.
    clock.advance(minutes=1)
    assert await scheduler.tick() == []

    # Just before the hourly monitor is due: still nothing. At its time: only it runs.
    clock.now = first_hourly - timedelta(seconds=1)
    assert await scheduler.tick() == []
    clock.now = first_hourly
    assert await scheduler.tick() == [hourly]
    await scheduler.wait_idle()
    assert await next_check(factory, hourly) > first_hourly

    # Much later: both are due again; the paused monitor never ran.
    clock.now = T0 + timedelta(days=2)
    assert sorted(await scheduler.tick()) == [hourly, daily]
    await scheduler.wait_idle()
    async with factory() as db:
        paused_runs = await db.execute(
            select(func.count()).select_from(CheckRun).where(CheckRun.monitor_id == paused)
        )
        assert paused_runs.scalar_one() == 0


# --- concurrency -------------------------------------------------------------------------------


class SlowRunner(CheckRunner):
    """Records how many checks run at once; each check waits for the test to release it."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(factory, lambda _snapshot: None)  # type: ignore[arg-type, return-value]
        self.active = 0
        self.peak = 0
        self.release = asyncio.Event()
        self.started: list[int] = []

    async def run(self, monitor_id: int) -> CheckResult:
        self._running.add(monitor_id)
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.append(monitor_id)
        try:
            await self.release.wait()
        finally:
            self.active -= 1
            self._running.discard(monitor_id)
        return CheckResult(monitor_id, "ok")


async def test_concurrency_limit_and_no_duplicates(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = [await add_monitor(factory) for _ in range(5)]
    runner = SlowRunner(factory)
    scheduler = Scheduler(factory, runner, max_concurrent=2, clock=lambda: T0)

    assert sorted(await scheduler.tick()) == ids
    await asyncio.sleep(0.05)
    assert runner.active == 2  # three are waiting for a slot

    # Still due (next_check_at isn't updated until a check finishes) but already queued.
    assert await scheduler.tick() == []
    assert not scheduler.request_check(ids[0])
    assert scheduler.is_busy(ids[4])

    runner.release.set()
    await scheduler.wait_idle()
    assert runner.peak == 2
    assert sorted(runner.started) == ids  # each exactly once
    assert not scheduler.is_busy(ids[4])
    assert scheduler.request_check(ids[0])  # free again
    await scheduler.wait_idle()


async def test_check_now(factory: async_sessionmaker[AsyncSession]) -> None:
    monitor_id = await add_monitor(factory, next_check_at=T0 + timedelta(days=1))
    runner = SlowRunner(factory)
    runner.release.set()
    scheduler = Scheduler(factory, runner, max_concurrent=1, clock=lambda: T0)
    assert await scheduler.tick() == []  # not due
    assert scheduler.request_check(monitor_id)
    await scheduler.wait_idle()
    assert runner.started == [monitor_id]


async def test_crashing_check_does_not_stop_the_scheduler(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    class Crashing(SlowRunner):
        async def run(self, monitor_id: int) -> CheckResult:
            raise RuntimeError("bug")

    monitor_id = await add_monitor(factory)
    scheduler = Scheduler(factory, Crashing(factory), max_concurrent=1, clock=lambda: T0)
    assert await scheduler.tick() == [monitor_id]
    await scheduler.wait_idle()
    assert not scheduler.is_busy(monitor_id)


async def test_background_loop_wakes_and_stops(factory: async_sessionmaker[AsyncSession]) -> None:
    runner = SlowRunner(factory)
    runner.release.set()
    scheduler = Scheduler(factory, runner, max_concurrent=1, poll_interval=3600, clock=lambda: T0)
    scheduler.start()
    await asyncio.sleep(0.05)  # first tick: nothing to do
    monitor_id = await add_monitor(factory)
    scheduler.wake()  # no need to wait an hour for the next poll
    for _ in range(100):
        if runner.started:
            break
        await asyncio.sleep(0.01)
    assert runner.started == [monitor_id]
    await scheduler.stop()


async def test_stop_cancels_stuck_checks(factory: async_sessionmaker[AsyncSession]) -> None:
    runner = SlowRunner(factory)  # never released
    scheduler = Scheduler(factory, runner, max_concurrent=1, clock=lambda: T0)
    await add_monitor(factory)
    await scheduler.tick()
    await asyncio.sleep(0.01)
    await scheduler.stop(grace_seconds=0.05)
    assert runner.active == 0


# --- maintenance -------------------------------------------------------------------------------


async def test_prune_keeps_recent_history_and_matched_postings(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    monitor_id = await add_monitor(factory)
    old = T0 - timedelta(days=200)

    def posting(n: int, *, active: bool, last_seen: datetime) -> Posting:
        return Posting(
            monitor_id=monitor_id,
            fingerprint=f"{n:064d}",
            title=f"Job {n}",
            url=f"https://x.test/{n}",
            first_seen_at=old,
            last_seen_at=last_seen,
            is_active=active,
        )

    async with factory() as db:
        stale = posting(1, active=False, last_seen=old)
        stale_but_matched = posting(2, active=False, last_seen=old)
        recently_removed = posting(3, active=False, last_seen=T0 - timedelta(days=10))
        old_but_active = posting(4, active=True, last_seen=old)
        db.add_all([stale, stale_but_matched, recently_removed, old_but_active])
        await db.flush()
        db.add(Match(monitor_id=monitor_id, posting_id=stale_but_matched.id, reasons=[]))
        db.add_all(
            [
                CheckRun(monitor_id=monitor_id, started_at=T0 - timedelta(days=91), status="ok"),
                CheckRun(monitor_id=monitor_id, started_at=T0 - timedelta(days=89), status="ok"),
                CheckRun(
                    monitor_id=monitor_id, started_at=T0 - timedelta(days=91), status="running"
                ),
            ]
        )
        await db.commit()
        assert await prune(db, T0) == (1, 1)
        titles = sorted((await db.scalars(select(Posting.title))).all())
    assert titles == ["Job 2", "Job 3", "Job 4"]


async def test_daily_backup_runs_once_a_day(
    migrated_settings: Settings, factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = migrated_settings.model_copy(update={"backup_keep": 2})
    first = await run_maintenance_if_due(settings, factory, T0)
    assert first is not None and first.backup.exists()
    assert await run_maintenance_if_due(settings, factory, T0 + timedelta(hours=23)) is None
    for day in (1, 2, 3):
        result = await run_maintenance_if_due(settings, factory, T0 + timedelta(days=day))
        assert result is not None
    backups = sorted(settings.backup_dir.glob("daily-*.db"))
    assert len(backups) == 2  # pruned to backup_keep
    async with factory() as db:
        assert await get_setting(db, LAST_RUN_KEY) == (T0 + timedelta(days=3)).isoformat()
        assert not await maintenance_due(db, T0 + timedelta(days=3, hours=1))


async def test_scheduler_runs_maintenance_and_survives_its_failure(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    calls: list[datetime] = []

    async def flaky(now: datetime) -> None:
        calls.append(now)
        raise OSError("disk full")

    runner = SlowRunner(factory)
    scheduler = Scheduler(factory, runner, max_concurrent=1, clock=lambda: T0, maintenance=flaky)
    assert await scheduler.tick() == []
    assert calls == [T0]
