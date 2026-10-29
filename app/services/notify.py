"""Send one notification digest per check that finds new matches, via Apprise."""

import asyncio
import importlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from app.db.models import Match, Monitor, NotificationTarget, utcnow
from app.security.secrets import SecretBox, SecretUnavailableError
from app.services.app_settings import set_setting

log = logging.getLogger(__name__)

MAX_ITEMS = 20
LAST_SENT_KEY = "notify_last_sent"
LAST_ERROR_KEY = "notify_last_error"

# Apprise logs whole URLs (with tokens) at debug level; keep it quiet.
logging.getLogger("apprise").setLevel(logging.WARNING)


@dataclass(frozen=True)
class Message:
    title: str
    body: str  # Markdown; Apprise converts it for services that only take plain text


@dataclass(frozen=True)
class Delivery:
    ok: bool
    error: str | None = None


class Sender(Protocol):
    def validate(self, url: str) -> bool: ...

    def masked(self, url: str) -> str: ...

    async def send(self, urls: Sequence[str], message: Message) -> Delivery: ...


def _apprise() -> Any:
    """Apprise, imported lazily and typed loosely (its own hints are partially untyped)."""
    return importlib.import_module("apprise")


class AppriseSender:
    """The real sender. Apprise is synchronous, so delivery runs in a worker thread."""

    def validate(self, url: str) -> bool:
        return bool(_apprise().Apprise().add(url))

    def masked(self, url: str) -> str:
        app = _apprise().Apprise()
        if not app.add(url):
            return "(not a valid notification URL)"
        return str(app[0].url(privacy=True)).split("?", 1)[0]  # drop Apprise's option noise

    async def send(self, urls: Sequence[str], message: Message) -> Delivery:
        apprise = _apprise()

        def deliver() -> Delivery:
            app = apprise.Apprise()
            for url in urls:
                app.add(url)
            result = app.notify(
                title=message.title, body=message.body, body_format=apprise.NotifyFormat.MARKDOWN
            )
            if result:
                return Delivery(True)
            status = getattr(result.status, "name", str(result.status))
            failed = getattr(result, "failed_count", None)
            detail = f"{failed} target(s) failed" if failed else "delivery failed"
            return Delivery(False, f"{detail} ({status}). Check the app log for details.")

        return await asyncio.to_thread(deliver)


def digest(monitor_name: str, matches: Sequence[Match]) -> Message:
    count = len(matches)
    title = f"{count} new match{'es' if count != 1 else ''} at {monitor_name}"
    lines: list[str] = []
    for match in matches[:MAX_ITEMS]:
        posting = match.posting
        details = [d for d in (posting.location, "remote" if posting.remote else None) if d]
        suffix = f" ({', '.join(details)})" if details else ""
        lines.append(f"- [{posting.title}]({posting.url}){suffix}")
    if count > MAX_ITEMS:
        lines.append(f"- …and {count - MAX_ITEMS} more")
    lines.append("")
    lines.append("Open Job Monitor to review them.")
    return Message(title, "\n".join(lines))


class Notifier:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        secrets: SecretBox,
        sender: Sender,
    ) -> None:
        self.session_factory = session_factory
        self.secrets = secrets
        self.sender = sender

    async def enabled_urls(self, db: AsyncSession) -> list[str]:
        targets = (
            await db.scalars(select(NotificationTarget).where(NotificationTarget.enabled.is_(True)))
        ).all()
        urls: list[str] = []
        for target in targets:
            try:
                urls.append(self.secrets.decrypt(target.apprise_url_encrypted))
            except SecretUnavailableError:
                log.warning("Skipping notification target %r: %s", target.name, "cannot decrypt")
        return urls

    async def notify_matches(
        self, monitor_id: int, match_ids: Sequence[int], baseline: bool
    ) -> None:
        """Called by the check runner after a check that created matches. Never raises."""
        if baseline or not match_ids:
            return  # the first check of a monitor never notifies
        try:
            await self._notify(monitor_id, match_ids)
        except Exception:
            log.exception("Notification for monitor %d failed", monitor_id)

    async def _notify(self, monitor_id: int, match_ids: Sequence[int]) -> None:
        async with self.session_factory() as db:
            monitor = await db.get(Monitor, monitor_id)
            if monitor is None or not monitor.notify:
                return
            urls = await self.enabled_urls(db)
            if not urls:
                return
            matches = (
                await db.scalars(
                    select(Match)
                    .options(joinedload(Match.posting))
                    .where(Match.id.in_(match_ids), Match.notified_at.is_(None))
                    .order_by(Match.id)
                )
            ).all()
            if not matches:
                return
            message = digest(monitor.name, matches)

        delivery = await self.sender.send(urls, message)
        now = utcnow()
        async with self.session_factory() as db:
            if delivery.ok:
                await db.execute(
                    update(Match)
                    .where(Match.id.in_([m.id for m in matches]))
                    .values(notified_at=now)
                )
                await set_setting(db, LAST_SENT_KEY, now.isoformat())
                log.info(
                    "Sent a notification for %d new match(es) at monitor %d",
                    len(matches),
                    monitor_id,
                )
            else:
                await set_setting(db, LAST_ERROR_KEY, f"{now.isoformat()} {delivery.error}")
                log.warning("Notification for monitor %d failed: %s", monitor_id, delivery.error)
            await db.commit()

    async def send_test(self, url: str) -> Delivery:
        return await self.sender.send(
            [url],
            Message(
                "Job Monitor test",
                "Notifications are working. New matches will arrive like this.",
            ),
        )


def parse_timestamped(value: str | None) -> tuple[datetime | None, str]:
    """Split a '<iso time> <text>' setting into its parts."""
    if not value:
        return None, ""
    stamp, _, text = value.partition(" ")
    try:
        return datetime.fromisoformat(stamp), text
    except ValueError:
        return None, value
