"""CheckRunner against a mocked Greenhouse board, on a real SQLite database."""

import random
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.engine import create_engine, create_session_factory
from app.db.models import CheckRun, Match, Monitor, Posting
from app.scraping.base import FetchContext
from app.scraping.http import PoliteClient
from app.services.checks import (
    MAX_BACKOFF_HOURS,
    CheckRunner,
    MonitorSnapshot,
    mark_interrupted_runs,
    next_check_delay,
)
from tests.scraping_helpers import make_client

API = "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs"
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def job(job_id: int, title: str) -> dict[str, Any]:
    return {
        "id": job_id,
        "title": title,
        "absolute_url": f"https://job-boards.greenhouse.io/examplecorp/jobs/{job_id}",
        "location": {"name": "Denver, CO"},
    }


class Board:
    """A fake Greenhouse board whose job list the test edits between checks."""

    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status)
        return httpx.Response(200, json={"jobs": self.jobs, "meta": {"total": len(self.jobs)}})


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def board() -> Iterator[Board]:
    board = Board()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith=API).mock(side_effect=board)
        yield board


@pytest.fixture
async def http() -> AsyncIterator[PoliteClient]:
    async with make_client(max_retries=0) as client:
        yield client


@pytest.fixture
async def factory(migrated_settings: Settings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_settings.db_path)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make_runner(
    factory: async_sessionmaker[AsyncSession], http: PoliteClient, clock: Clock
) -> CheckRunner:
    def context(snapshot: MonitorSnapshot) -> FetchContext:
        return FetchContext(
            client=http, respect_robots=snapshot.respect_robots, render_mode="static"
        )

    return CheckRunner(factory, context, clock=clock, rng=random.Random(42))


async def add_monitor(factory: async_sessionmaker[AsyncSession], **fields: Any) -> int:
    values: dict[str, Any] = {
        "name": "Example Corp",
        "url": "https://boards.greenhouse.io/examplecorp",
        "adapter": "greenhouse",
        "adapter_config": {"token": "examplecorp"},
        "criteria": {"keywords_any": ["engineer"]},
        "interval_hours": 12,
    }
    values.update(fields)
    async with factory() as db:
        monitor = Monitor(**values)
        db.add(monitor)
        await db.commit()
        return monitor.id


async def count(factory: async_sessionmaker[AsyncSession], model: type[Any], *where: Any) -> int:
    async with factory() as db:
        return (
            await db.execute(select(func.count()).select_from(model).where(*where))
        ).scalar_one()


async def posting(factory: async_sessionmaker[AsyncSession], external_id: str) -> Posting:
    async with factory() as db:
        return (await db.scalars(select(Posting).where(Posting.external_id == external_id))).one()


async def get_monitor(factory: async_sessionmaker[AsyncSession], monitor_id: int) -> Monitor:
    async with factory() as db:
        monitor = await db.get(Monitor, monitor_id)
        assert monitor is not None
        return monitor


# --- the plan's acceptance scenario ------------------------------------------------------------


async def test_baseline_new_match_removal_and_restart(
    board: Board,
    factory: async_sessionmaker[AsyncSession],
    http: PoliteClient,
    clock: Clock,
    migrated_settings: Settings,
) -> None:
    runner = make_runner(factory, http, clock)
    monitor_id = await add_monitor(factory)

    # 1. Baseline: everything stored, nothing alerted.
    board.jobs = [job(1, "Backend Engineer"), job(2, "Product Designer")]
    result = await runner.run(monitor_id)
    assert (result.status, result.baseline, result.postings_found) == ("ok", True, 2)
    assert (result.new_postings, result.new_matches) == (2, 0)
    assert await count(factory, Match) == 0
    monitor = await get_monitor(factory, monitor_id)
    assert monitor.baseline_done
    assert monitor.next_check_at is not None
    delay = monitor.next_check_at - T0
    assert timedelta(hours=12 * 0.9) <= delay <= timedelta(hours=12 * 1.1)

    # 2. One new matching posting and one new non-matching posting: exactly one alert.
    clock.advance(hours=12)
    board.jobs += [job(3, "Data Engineer"), job(4, "Recruiter")]
    result = await runner.run(monitor_id)
    assert (result.baseline, result.new_postings, result.new_matches) == (False, 2, 1)
    async with factory() as db:
        match = (await db.scalars(select(Match))).one()
        matched = await db.get(Posting, match.posting_id)
    assert matched is not None
    assert matched.title == "Data Engineer"
    assert match.reasons == ["title contains 'engineer'"]

    # 3. The posting disappears: still active after one miss, inactive after two.
    board.jobs = [j for j in board.jobs if j["id"] != 3]
    clock.advance(hours=12)
    await runner.run(monitor_id)
    gone = await posting(factory, "3")
    assert (gone.missing_count, gone.is_active) == (1, True)
    clock.advance(hours=12)
    await runner.run(monitor_id)
    gone = await posting(factory, "3")
    assert (gone.missing_count, gone.is_active) == (2, False)

    # 4. "Restart": a fresh engine and runner on the same database file. The posting comes back
    #    and nothing is alerted twice.
    engine = create_engine(migrated_settings.db_path)
    try:
        restarted = make_runner(create_session_factory(engine), http, clock)
        board.jobs.append(job(3, "Data Engineer"))
        clock.advance(hours=12)
        result = await restarted.run(monitor_id)
        assert (result.new_postings, result.new_matches) == (0, 0)
    finally:
        await engine.dispose()
    back = await posting(factory, "3")
    assert (back.is_active, back.missing_count) == (True, 0)
    assert back.first_seen_at == T0 + timedelta(hours=12)  # original first sighting kept
    assert await count(factory, Match) == 1
    assert await count(factory, CheckRun, CheckRun.status == "ok") == 5


# --- other behaviour ---------------------------------------------------------------------------


async def test_alert_existing_on_baseline(
    board: Board, factory: async_sessionmaker[AsyncSession], http: PoliteClient, clock: Clock
) -> None:
    monitor_id = await add_monitor(factory, alert_existing=True)
    board.jobs = [job(1, "Backend Engineer"), job(2, "Designer")]
    result = await make_runner(factory, http, clock).run(monitor_id)
    assert (result.baseline, result.new_matches) == (True, 1)


async def test_zero_results_do_not_wipe_history(
    board: Board, factory: async_sessionmaker[AsyncSession], http: PoliteClient, clock: Clock
) -> None:
    runner = make_runner(factory, http, clock)
    monitor_id = await add_monitor(factory)
    board.jobs = [job(1, "Backend Engineer")]
    await runner.run(monitor_id)

    board.jobs = []
    for _ in range(3):
        result = await runner.run(monitor_id)
        assert result.status == "warning"
        assert result.message is not None and "previously 1" in result.message
    kept = await posting(factory, "1")
    assert (kept.is_active, kept.missing_count) == (True, 0)
    monitor = await get_monitor(factory, monitor_id)
    assert monitor.last_status == "warning"
    assert monitor.consecutive_failures == 0
    async with factory() as db:
        last = (await db.scalars(select(CheckRun).order_by(CheckRun.id.desc()))).first()
    assert last is not None and last.status == "ok" and last.error is not None


@pytest.mark.parametrize(("http_status", "expected"), [(500, "error"), (403, "blocked")])
async def test_failures_are_recorded(
    board: Board,
    factory: async_sessionmaker[AsyncSession],
    http: PoliteClient,
    clock: Clock,
    http_status: int,
    expected: str,
) -> None:
    runner = make_runner(factory, http, clock)
    monitor_id = await add_monitor(factory)
    board.jobs = [job(1, "Backend Engineer")]
    await runner.run(monitor_id)

    board.status = http_status
    result = await runner.run(monitor_id)
    assert result.status == expected
    monitor = await get_monitor(factory, monitor_id)
    assert (monitor.last_status, monitor.consecutive_failures) == (expected, 1)
    assert monitor.last_error is not None and str(http_status) in monitor.last_error
    kept = await posting(factory, "1")
    assert kept.missing_count == 0  # a failed check says nothing about what was removed
    async with factory() as db:
        runs = (await db.scalars(select(CheckRun).order_by(CheckRun.id))).all()
    assert [r.status for r in runs] == ["ok", expected]
    assert runs[-1].finished_at is not None and runs[-1].duration_ms is not None

    board.status = 200
    await runner.run(monitor_id)
    assert (await get_monitor(factory, monitor_id)).consecutive_failures == 0


async def test_backoff_after_five_failures(
    board: Board, factory: async_sessionmaker[AsyncSession], http: PoliteClient, clock: Clock
) -> None:
    monitor_id = await add_monitor(factory, consecutive_failures=4)
    board.status = 500
    await make_runner(factory, http, clock).run(monitor_id)
    monitor = await get_monitor(factory, monitor_id)
    assert monitor.consecutive_failures == 5
    assert monitor.next_check_at is not None
    delay = monitor.next_check_at - T0
    assert timedelta(hours=24 * 0.9) <= delay <= timedelta(hours=24 * 1.1)  # 12 h doubled


@pytest.mark.parametrize(
    ("interval", "failures", "expected_hours"),
    [(12, 0, 12), (12, 4, 12), (12, 5, 24), (12, 6, 48), (12, 7, 96), (12, 8, 168), (12, 30, 168),
     (720, 9, 720)],
)  # fmt: skip
def test_next_check_delay(interval: int, failures: int, expected_hours: float) -> None:
    class NoJitter(random.Random):
        def uniform(self, a: float, b: float) -> float:
            return 1.0

    assert next_check_delay(interval, failures, NoJitter()) == timedelta(hours=expected_hours)
    assert expected_hours <= max(MAX_BACKOFF_HOURS, interval)


def test_jitter_stays_within_ten_percent() -> None:
    rng = random.Random(7)
    delays = [next_check_delay(10, 0, rng) for _ in range(500)]
    assert min(delays) >= timedelta(hours=9)
    assert max(delays) <= timedelta(hours=11)
    assert len(set(delays)) > 400  # actually random


async def test_first_check_detects_and_saves_adapter(
    board: Board, factory: async_sessionmaker[AsyncSession], http: PoliteClient, clock: Clock
) -> None:
    monitor_id = await add_monitor(factory, adapter=None, adapter_config={})
    board.jobs = [job(1, "Backend Engineer")]
    result = await make_runner(factory, http, clock).run(monitor_id)
    assert result.status == "ok"
    monitor = await get_monitor(factory, monitor_id)
    assert (monitor.adapter, monitor.adapter_config) == ("greenhouse", {"token": "examplecorp"})


async def test_invalid_saved_criteria(
    board: Board, factory: async_sessionmaker[AsyncSession], http: PoliteClient, clock: Clock
) -> None:
    monitor_id = await add_monitor(factory, criteria={"title_regex": "([bad"})
    result = await make_runner(factory, http, clock).run(monitor_id)
    assert result.status == "error"
    assert result.message is not None and "criteria are invalid" in result.message


async def test_unexpected_errors_are_contained(
    factory: async_sessionmaker[AsyncSession], clock: Clock
) -> None:
    def broken(_snapshot: MonitorSnapshot) -> FetchContext:
        raise RuntimeError("boom")

    monitor_id = await add_monitor(factory)
    result = await CheckRunner(factory, broken, clock=clock).run(monitor_id)
    assert result.status == "error"
    assert result.message is not None and "boom" in result.message


async def test_skips_when_already_running_or_missing(
    factory: async_sessionmaker[AsyncSession], http: PoliteClient, clock: Clock
) -> None:
    runner = make_runner(factory, http, clock)
    monitor_id = await add_monitor(factory)
    runner._running.add(monitor_id)
    assert (await runner.run(monitor_id)).status == "skipped"
    runner._running.clear()
    assert (await runner.run(9999)).status == "skipped"
    assert await count(factory, CheckRun) == 0


async def test_monitor_deleted_during_check(
    factory: async_sessionmaker[AsyncSession],
    http: PoliteClient,
    clock: Clock,
    migrated_settings: Settings,
) -> None:
    import sqlite3

    monitor_id = await add_monitor(factory)

    def delete_monitor_then_respond(request: httpx.Request) -> httpx.Response:
        # The user deletes the monitor while the site is still answering.
        conn = sqlite3.connect(migrated_settings.db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
        conn.commit()
        conn.close()
        return httpx.Response(200, json={"jobs": [job(1, "Engineer")]})

    with respx.mock() as mock:
        mock.get(url__startswith=API).mock(side_effect=delete_monitor_then_respond)
        result = await make_runner(factory, http, clock).run(monitor_id)
    assert result.status == "skipped"
    assert await count(factory, Posting) == 0
    assert await count(factory, CheckRun) == 0


async def test_mark_interrupted_runs(factory: async_sessionmaker[AsyncSession]) -> None:
    monitor_id = await add_monitor(factory)
    async with factory() as db:
        db.add(CheckRun(monitor_id=monitor_id, started_at=T0, status="running"))
        db.add(CheckRun(monitor_id=monitor_id, started_at=T0, status="ok"))
        await db.commit()
        assert await mark_interrupted_runs(db, T0 + timedelta(minutes=5)) == 1
    async with factory() as db:
        statuses = sorted(r.status for r in (await db.scalars(select(CheckRun))).all())
    assert statuses == ["error", "ok"]
