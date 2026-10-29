"""Run one check of one monitor: fetch, diff against stored postings, record new matches."""

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import CheckRun, Match, Monitor, Posting, utcnow
from app.matching.criteria import Criteria
from app.matching.matcher import PostingView, matches
from app.scraping.base import AdapterConfig, BlockedError, FetchContext, FetchError, RawPosting
from app.scraping.detect import detect
from app.scraping.fetch import FetchOutcome, fetch_postings
from app.scraping.urls import fingerprint

log = logging.getLogger(__name__)

MISSING_CHECKS_BEFORE_INACTIVE = 2
BACKOFF_AFTER_FAILURES = 5
MAX_BACKOFF_HOURS = 7 * 24
JITTER = 0.10
MAX_ERROR_LENGTH = 2000


@dataclass(frozen=True)
class MonitorSnapshot:
    """The monitor fields a check needs, copied out so no ORM object outlives its session."""

    id: int
    url: str
    adapter: str | None
    adapter_config: dict[str, Any]
    render_mode: str
    respect_robots: bool
    criteria: dict[str, Any]

    @classmethod
    def of(cls, monitor: Monitor) -> "MonitorSnapshot":
        return cls(
            id=monitor.id,
            url=monitor.url,
            adapter=monitor.adapter,
            adapter_config=dict(monitor.adapter_config or {}),
            render_mode=monitor.render_mode,
            respect_robots=monitor.respect_robots,
            criteria=dict(monitor.criteria or {}),
        )


@dataclass(frozen=True)
class CheckResult:
    monitor_id: int
    status: str  # ok, warning, error, blocked, skipped
    postings_found: int = 0
    new_postings: int = 0
    new_matches: int = 0
    baseline: bool = False
    message: str | None = None


ContextFactory = Callable[[MonitorSnapshot], FetchContext]
Clock = Callable[[], datetime]


def next_check_delay(
    interval_hours: int, consecutive_failures: int, rng: random.Random
) -> timedelta:
    """The interval, doubled for each failure from the 5th on (up to 7 days), with ±10% jitter."""
    hours = float(interval_hours)
    if consecutive_failures >= BACKOFF_AFTER_FAILURES:
        doublings = consecutive_failures - BACKOFF_AFTER_FAILURES + 1
        hours = min(hours * 2**doublings, max(MAX_BACKOFF_HOURS, interval_hours))
    return timedelta(hours=hours * rng.uniform(1 - JITTER, 1 + JITTER))


def is_backing_off(monitor: Monitor) -> bool:
    return monitor.consecutive_failures >= BACKOFF_AFTER_FAILURES


async def mark_interrupted_runs(db: AsyncSession, now: datetime) -> int:
    """Close check runs left 'running' by a crash or restart."""
    result = await db.execute(
        update(CheckRun)
        .where(CheckRun.status == "running")
        .values(
            status="error",
            error="Interrupted: the app restarted during this check.",
            finished_at=now,
        )
    )
    await db.commit()
    return result.rowcount  # type: ignore[attr-defined, no-any-return]


class CheckRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        context_factory: ContextFactory,
        *,
        clock: Clock = utcnow,
        rng: random.Random | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.context_factory = context_factory
        self.clock = clock
        self.rng = rng or random.Random()  # noqa: S311 - scheduling jitter, not security
        self._running: set[int] = set()

    def is_running(self, monitor_id: int) -> bool:
        return monitor_id in self._running

    @property
    def running(self) -> frozenset[int]:
        return frozenset(self._running)

    async def run(self, monitor_id: int) -> CheckResult:
        if monitor_id in self._running:
            return CheckResult(monitor_id, "skipped", message="A check is already running.")
        self._running.add(monitor_id)
        try:
            return await self._run(monitor_id)
        finally:
            self._running.discard(monitor_id)

    async def _run(self, monitor_id: int) -> CheckResult:
        started = time.monotonic()
        async with self.session_factory() as db:
            monitor = await db.get(Monitor, monitor_id)
            if monitor is None:
                return CheckResult(monitor_id, "skipped", message="Monitor no longer exists.")
            snapshot = MonitorSnapshot.of(monitor)
            run = CheckRun(monitor_id=monitor_id, started_at=self.clock(), status="running")
            db.add(run)
            await db.commit()
            run_id = run.id

        outcome: FetchOutcome | None = None
        status, message = "ok", None
        try:
            Criteria.from_json(snapshot.criteria)  # fail early if the saved criteria are bad
            ctx = self.context_factory(snapshot)
            if snapshot.adapter:
                cfg = AdapterConfig(snapshot.adapter, dict(snapshot.adapter_config))
                outcome = await fetch_postings(cfg, snapshot.url, ctx)
            else:
                outcome = await detect(snapshot.url, ctx)
        except ValidationError as exc:
            status, message = "error", f"The saved criteria are invalid: {exc.errors()[0]['msg']}"
        except BlockedError as exc:
            status, message = "blocked", str(exc)
        except FetchError as exc:
            status, message = "error", str(exc)
        except Exception as exc:
            log.exception("Check of monitor %d failed unexpectedly", monitor_id)
            status, message = "error", f"Unexpected error: {exc!r}"

        async with self.session_factory() as db:
            now = self.clock()
            run = await db.get(CheckRun, run_id)
            monitor = await db.get(Monitor, monitor_id)
            if run is None or monitor is None:  # deleted while we were fetching
                await db.commit()
                return CheckResult(monitor_id, "skipped", message="Monitor was deleted.")

            result = CheckResult(monitor_id, status, message=message)
            if outcome is not None:
                result = await self._apply(db, monitor, outcome, now)
            self._finish(monitor, run, result, now, started)
            await db.commit()

        log.info(
            "Checked monitor %d: %s, %d postings, %d new, %d new matches%s",
            monitor_id,
            result.status,
            result.postings_found,
            result.new_postings,
            result.new_matches,
            f" ({result.message})" if result.message else "",
        )
        return result

    async def _apply(
        self, db: AsyncSession, monitor: Monitor, outcome: FetchOutcome, now: datetime
    ) -> CheckResult:
        criteria = Criteria.from_json(monitor.criteria)
        adapter = outcome.config.adapter
        if monitor.adapter is None:  # first check auto-detected the site
            monitor.adapter = adapter
            monitor.adapter_config = outcome.config.to_json()

        rows = (await db.scalars(select(Posting).where(Posting.monitor_id == monitor.id))).all()
        existing = {row.fingerprint: row for row in rows}
        previously_active = sum(1 for row in rows if row.is_active)

        seen: set[str] = set()
        new_rows: list[tuple[Posting, RawPosting]] = []
        for raw in outcome.postings:
            key = fingerprint(adapter, raw.external_id, raw.url)
            if key in seen:
                continue
            seen.add(key)
            row = existing.get(key)
            if row is None:
                row = Posting(
                    monitor_id=monitor.id,
                    fingerprint=key,
                    first_seen_at=now,
                    title=raw.title,
                    url=raw.url,
                )
                db.add(row)
                new_rows.append((row, raw))
            _copy_fields(row, raw)
            row.last_seen_at = now
            row.missing_count = 0
            row.is_active = True
        await db.flush()  # assigns ids to new postings

        baseline = not monitor.baseline_done
        alert = not baseline or monitor.alert_existing
        new_matches = 0
        for row, raw in new_rows:
            result = matches(PostingView.of(raw), criteria, now=now)
            if result.matched and alert:
                db.add(
                    Match(
                        monitor_id=monitor.id,
                        posting_id=row.id,
                        reasons=result.reasons,
                        matched_at=now,
                    )
                )
                new_matches += 1

        message = None
        if not outcome.postings and previously_active:
            # A page that suddenly lists nothing is more likely broken than empty; keep history.
            message = (
                f"Found 0 postings (previously {previously_active}). The page may have changed; "
                "nothing was marked as removed."
            )
        else:
            for key, row in existing.items():
                if key not in seen and row.is_active:
                    row.missing_count += 1
                    if row.missing_count >= MISSING_CHECKS_BEFORE_INACTIVE:
                        row.is_active = False

        monitor.baseline_done = True
        return CheckResult(
            monitor.id,
            "warning" if message else "ok",
            postings_found=len(seen),
            new_postings=len(new_rows),
            new_matches=new_matches,
            baseline=baseline,
            message=message,
        )

    def _finish(
        self, monitor: Monitor, run: CheckRun, result: CheckResult, now: datetime, started: float
    ) -> None:
        failed = result.status in ("error", "blocked")
        monitor.consecutive_failures = monitor.consecutive_failures + 1 if failed else 0
        monitor.last_check_at = now
        monitor.last_status = result.status
        monitor.last_error = (result.message or "")[:MAX_ERROR_LENGTH] or None
        monitor.next_check_at = now + next_check_delay(
            monitor.interval_hours, monitor.consecutive_failures, self.rng
        )

        run.finished_at = now
        run.status = "ok" if result.status == "warning" else result.status
        run.adapter_used = monitor.adapter
        run.postings_found = result.postings_found
        run.new_postings = result.new_postings
        run.new_matches = result.new_matches
        run.error = monitor.last_error
        run.duration_ms = int((time.monotonic() - started) * 1000)


def _copy_fields(row: Posting, raw: RawPosting) -> None:
    row.external_id = raw.external_id
    row.title = raw.title
    row.url = raw.url
    row.location = raw.location
    row.department = raw.department
    row.employment_type = raw.employment_type
    row.remote = raw.remote
    row.posted_at = raw.posted_at
    row.description_text = raw.description_text
