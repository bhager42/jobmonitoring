"""Notifications: one digest per check with new matches, none on the baseline (plan Phase 6)."""

import json
import random
import threading
from collections.abc import AsyncIterator, Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.engine import create_engine, create_session_factory
from app.db.models import Match, Monitor, NotificationTarget
from app.scraping.base import FetchContext
from app.scraping.http import PoliteClient
from app.security.secrets import SecretBox
from app.services.app_settings import get_setting
from app.services.checks import CheckRunner, MonitorSnapshot
from app.services.notify import (
    LAST_ERROR_KEY,
    LAST_SENT_KEY,
    MAX_ITEMS,
    AppriseSender,
    Delivery,
    Message,
    Notifier,
    digest,
)
from tests.integration.test_checks import Board, Clock, add_monitor, job
from tests.scraping_helpers import make_client

NTFY = "ntfy://user:hunter2@ntfy.example/jobs"


class FakeSender:
    """Stands in for Apprise: records what would be sent."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[list[str], Message]] = []

    def validate(self, url: str) -> bool:
        return "://" in url

    def masked(self, url: str) -> str:
        return url.split("@")[-1]

    async def send(self, urls: Sequence[str], message: Message) -> Delivery:
        self.sent.append((list(urls), message))
        return Delivery(True) if self.ok else Delivery(False, "server said no")


@pytest.fixture
def board() -> Iterator[Board]:
    import respx

    board = Board()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith="https://boards-api.greenhouse.io/").mock(side_effect=board)
        yield board


@pytest.fixture
async def factory(migrated_settings: Settings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_settings.db_path)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
async def http() -> AsyncIterator[PoliteClient]:
    async with make_client(max_retries=0) as client:
        yield client


@pytest.fixture
def box(tmp_path: Path) -> SecretBox:
    return SecretBox(tmp_path / "notify.key")


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


@pytest.fixture
def runner(
    factory: async_sessionmaker[AsyncSession],
    http: PoliteClient,
    box: SecretBox,
    sender: FakeSender,
) -> CheckRunner:
    def context(_snapshot: MonitorSnapshot) -> FetchContext:
        return FetchContext(client=http, render_mode="static", respect_robots=False)

    notifier = Notifier(factory, box, sender)
    return CheckRunner(
        factory,
        context,
        clock=Clock(),
        rng=random.Random(3),
        on_new_matches=notifier.notify_matches,
    )


async def add_target(
    factory: async_sessionmaker[AsyncSession], box: SecretBox, url: str = NTFY, **fields: Any
) -> None:
    async with factory() as db:
        db.add(
            NotificationTarget(
                name=fields.pop("name", "Phone"), apprise_url_encrypted=box.encrypt(url), **fields
            )
        )
        await db.commit()


async def notified(factory: async_sessionmaker[AsyncSession]) -> list[bool]:
    async with factory() as db:
        rows = (await db.scalars(select(Match).order_by(Match.id))).all()
    return [m.notified_at is not None for m in rows]


# --- the plan's acceptance criteria -------------------------------------------------------------


async def test_one_digest_per_check_and_none_on_baseline(
    board: Board,
    factory: async_sessionmaker[AsyncSession],
    runner: CheckRunner,
    box: SecretBox,
    sender: FakeSender,
) -> None:
    await add_target(factory, box)
    monitor_id = await add_monitor(factory)

    board.jobs = [job(1, "Backend Engineer")]  # matches, but it's the baseline
    await runner.run(monitor_id)
    assert sender.sent == []

    board.jobs += [job(2, "Data Engineer"), job(3, "Platform Engineer"), job(4, "Recruiter")]
    result = await runner.run(monitor_id)
    assert result.new_matches == 2
    assert len(sender.sent) == 1  # one digest, not one message per posting
    urls, message = sender.sent[0]
    assert urls == [NTFY]
    assert message.title == "2 new matches at Example Corp"
    assert "[Data Engineer](https://job-boards.greenhouse.io/examplecorp/jobs/2)" in message.body
    assert "Platform Engineer" in message.body
    assert "Recruiter" not in message.body
    assert await notified(factory) == [True, True]

    await runner.run(monitor_id)  # nothing new
    assert len(sender.sent) == 1
    async with factory() as db:
        assert await get_setting(db, LAST_SENT_KEY) is not None


async def test_alert_existing_baseline_still_does_not_notify(
    board: Board,
    factory: async_sessionmaker[AsyncSession],
    runner: CheckRunner,
    box: SecretBox,
    sender: FakeSender,
) -> None:
    await add_target(factory, box)
    monitor_id = await add_monitor(factory, alert_existing=True)
    board.jobs = [job(1, "Backend Engineer")]
    result = await runner.run(monitor_id)
    assert result.new_matches == 1  # it is in the matches list...
    assert sender.sent == []  # ...but the first check never sends anything
    assert await notified(factory) == [False]


@pytest.mark.parametrize("setup", ["monitor_off", "no_targets", "target_disabled"])
async def test_nothing_sent_when_switched_off(
    board: Board,
    factory: async_sessionmaker[AsyncSession],
    runner: CheckRunner,
    box: SecretBox,
    sender: FakeSender,
    setup: str,
) -> None:
    if setup != "no_targets":
        await add_target(factory, box, enabled=setup != "target_disabled")
    monitor_id = await add_monitor(factory, notify=setup != "monitor_off")
    board.jobs = [job(1, "Designer")]
    await runner.run(monitor_id)
    board.jobs.append(job(2, "Backend Engineer"))
    assert (await runner.run(monitor_id)).new_matches == 1
    assert sender.sent == []
    assert await notified(factory) == [False]


async def test_failed_delivery_is_recorded_and_check_still_succeeds(
    board: Board,
    factory: async_sessionmaker[AsyncSession],
    runner: CheckRunner,
    box: SecretBox,
    sender: FakeSender,
) -> None:
    sender.ok = False
    await add_target(factory, box)
    monitor_id = await add_monitor(factory)
    board.jobs = [job(1, "Designer")]
    await runner.run(monitor_id)
    board.jobs.append(job(2, "Backend Engineer"))
    result = await runner.run(monitor_id)
    assert result.status == "ok"
    assert len(sender.sent) == 1
    assert await notified(factory) == [False]
    async with factory() as db:
        error = await get_setting(db, LAST_ERROR_KEY)
    assert error is not None and "server said no" in error


async def test_sender_crash_does_not_break_the_check(
    board: Board,
    factory: async_sessionmaker[AsyncSession],
    runner: CheckRunner,
    box: SecretBox,
    sender: FakeSender,
) -> None:
    async def explode(urls: Sequence[str], message: Message) -> Delivery:
        raise RuntimeError("network on fire")

    sender.send = explode  # type: ignore[method-assign]
    await add_target(factory, box)
    monitor_id = await add_monitor(factory)
    board.jobs = [job(1, "Designer")]
    await runner.run(monitor_id)
    board.jobs.append(job(2, "Backend Engineer"))
    assert (await runner.run(monitor_id)).status == "ok"


async def test_target_from_another_install_is_skipped(
    board: Board,
    factory: async_sessionmaker[AsyncSession],
    runner: CheckRunner,
    box: SecretBox,
    sender: FakeSender,
    tmp_path: Path,
) -> None:
    await add_target(factory, SecretBox(tmp_path / "old.key"), name="Old")
    await add_target(factory, box, url="json://hooks.example/new", name="New")
    monitor_id = await add_monitor(factory)
    board.jobs = [job(1, "Designer")]
    await runner.run(monitor_id)
    board.jobs.append(job(2, "Backend Engineer"))
    await runner.run(monitor_id)
    assert [urls for urls, _ in sender.sent] == [["json://hooks.example/new"]]


def test_digest_truncates_long_lists() -> None:
    class P:
        def __init__(self, n: int) -> None:
            self.title, self.url = f"Job {n}", f"https://x.test/{n}"
            self.location, self.remote = ("Denver", True) if n == 0 else (None, None)

    class M:
        def __init__(self, n: int) -> None:
            self.posting = P(n)

    message = digest("Acme", [M(n) for n in range(MAX_ITEMS + 5)])  # type: ignore[misc]
    assert message.title == f"{MAX_ITEMS + 5} new matches at Acme"
    assert "- [Job 0](https://x.test/0) (Denver, remote)" in message.body
    assert "…and 5 more" in message.body
    assert message.body.count("\n- [") == MAX_ITEMS - 1
    assert digest("Acme", [M(1)]).title == "1 new match at Acme"  # type: ignore[list-item]


async def test_monitor_notify_flag_defaults_on(factory: async_sessionmaker[AsyncSession]) -> None:
    monitor_id = await add_monitor(factory)
    async with factory() as db:
        monitor = await db.get(Monitor, monitor_id)
        assert monitor is not None and monitor.notify is True


# --- the real Apprise, against a local webhook ----------------------------------------------------


@pytest.fixture
def webhook() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"json://user:hunter2@127.0.0.1:{server.server_address[1]}/hook", received
    server.shutdown()
    server.server_close()


async def test_apprise_delivers_to_a_real_endpoint(
    webhook: tuple[str, list[dict[str, Any]]],
) -> None:
    url, received = webhook
    sender = AppriseSender()
    assert sender.validate(url)
    assert not sender.validate("not a url")
    masked = sender.masked(url)
    assert "hunter2" not in masked and masked.startswith("json://user:")
    delivery = await sender.send([url], Message("2 new matches at Acme", "- [Job](https://x.test)"))
    assert delivery.ok
    assert received[0]["title"] == "2 new matches at Acme"
    assert "[Job](https://x.test)" in received[0]["message"]


async def test_apprise_reports_failures() -> None:
    delivery = await AppriseSender().send(["json://127.0.0.1:9/nothing"], Message("t", "b"))
    assert not delivery.ok
    assert delivery.error is not None and "FAILURE" in delivery.error
