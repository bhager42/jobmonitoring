from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.engine import get_session
from app.db.models import Match, Monitor, Posting, Setting
from app.main import create_app
from tests.integration.conftest import login


@pytest.mark.parametrize(
    ("pragma", "expected"),
    [
        ("journal_mode", "wal"),
        ("foreign_keys", 1),
        ("busy_timeout", 5000),
        ("synchronous", 1),  # NORMAL
    ],
)
async def test_connection_pragmas(session: AsyncSession, pragma: str, expected: object) -> None:
    value = (await session.execute(text(f"PRAGMA {pragma}"))).scalar_one()
    assert value == expected


def _monitor(**overrides: object) -> Monitor:
    fields: dict[str, object] = {"name": "Acme", "url": "https://jobs.example.com/acme"}
    fields.update(overrides)
    return Monitor(**fields)


async def test_monitor_defaults(session: AsyncSession) -> None:
    monitor = _monitor()
    session.add(monitor)
    await session.commit()

    assert monitor.render_mode == "auto"
    assert monitor.interval_hours == 12
    assert monitor.enabled is True
    assert monitor.baseline_done is False
    assert monitor.criteria == {}
    assert monitor.created_at.tzinfo is UTC


async def test_deleting_monitor_cascades(session: AsyncSession) -> None:
    monitor = _monitor()
    session.add(monitor)
    await session.flush()
    posting = Posting(
        monitor_id=monitor.id, fingerprint="a" * 64, title="Engineer", url="https://x.test/1"
    )
    session.add(posting)
    await session.flush()
    session.add(Match(monitor_id=monitor.id, posting_id=posting.id, reasons=["title"]))
    await session.commit()

    await session.execute(text("DELETE FROM monitors WHERE id = :id"), {"id": monitor.id})
    await session.commit()

    assert (await session.execute(select(func.count()).select_from(Posting))).scalar_one() == 0
    assert (await session.execute(select(func.count()).select_from(Match))).scalar_one() == 0


async def test_duplicate_fingerprint_rejected(session: AsyncSession) -> None:
    monitor = _monitor()
    session.add(monitor)
    await session.flush()
    for _ in range(2):
        session.add(
            Posting(monitor_id=monitor.id, fingerprint="b" * 64, title="t", url="https://x.test")
        )
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.parametrize(
    "overrides", [{"interval_hours": 0}, {"interval_hours": 721}, {"render_mode": "magic"}]
)
async def test_monitor_check_constraints(
    session: AsyncSession, overrides: dict[str, object]
) -> None:
    session.add(_monitor(**overrides))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_datetimes_round_trip_as_utc(session: AsyncSession) -> None:
    denver = timezone(timedelta(hours=-6))
    when = datetime(2026, 11, 2, 9, 30, tzinfo=denver)
    session.add(_monitor(next_check_at=when))
    await session.commit()
    session.expunge_all()

    stored = (await session.execute(select(Monitor))).scalar_one().next_check_at
    assert stored == when
    assert stored is not None
    assert stored.tzinfo is UTC
    assert stored.hour == 15


async def test_naive_datetimes_rejected(session: AsyncSession) -> None:
    session.add(_monitor(next_check_at=datetime(2026, 1, 1)))  # noqa: DTZ001
    with pytest.raises(StatementError, match="naive datetime"):
        await session.commit()


def test_app_lifespan_provides_sessions(password_settings: Settings) -> None:
    app = create_app(password_settings, failed_login_delay=0, start_scheduler=False)

    @app.get("/test-db")
    async def read_db(session: AsyncSession = Depends(get_session)) -> dict[str, int]:  # noqa: B008
        session.add(Setting(key="k", value="v"))
        await session.commit()
        count = (await session.execute(select(func.count()).select_from(Setting))).scalar_one()
        return {"settings": count}

    with TestClient(app, follow_redirects=False) as client:
        login(client)
        # secret_key + password_hash from bootstrap, plus the row this route adds
        assert client.get("/test-db").json() == {"settings": 3}
