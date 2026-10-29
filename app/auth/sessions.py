"""Server-side login sessions. The cookie holds a random token; the database stores its SHA-256."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Session, utcnow

SLIDING_LIFETIME = timedelta(days=7)
ABSOLUTE_LIFETIME = timedelta(days=30)
# Refresh last_seen/expiry at most this often, to avoid a database write on every request.
TOUCH_INTERVAL = timedelta(minutes=5)

COOKIE_NAME = "jm_session"
SECURE_COOKIE_NAME = "__Host-jm_session"


def cookie_name(secure: bool) -> str:
    return SECURE_COOKIE_NAME if secure else COOKIE_NAME


def _session_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class NewSession:
    token: str
    id: str
    absolute_expiry: datetime


async def create_session(db: AsyncSession, *, user_agent: str | None, ip: str | None) -> NewSession:
    now = utcnow()
    token = secrets.token_urlsafe(32)
    session_id = _session_id(token)
    db.add(
        Session(
            id=session_id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + SLIDING_LIFETIME,
            user_agent=(user_agent or "")[:500] or None,
            ip=ip,
        )
    )
    await db.execute(
        delete(Session).where(
            or_(Session.expires_at <= now, Session.created_at <= now - ABSOLUTE_LIFETIME)
        )
    )
    await db.commit()
    return NewSession(token, session_id, now + ABSOLUTE_LIFETIME)


async def validate_session(db: AsyncSession, token: str | None) -> Session | None:
    """Return the live session for `token`, extending its sliding expiry; None if invalid."""
    if not token or len(token) > 100:
        return None
    row = await db.get(Session, _session_id(token))
    if row is None:
        return None
    now = utcnow()
    absolute_expiry = row.created_at + ABSOLUTE_LIFETIME
    if now >= row.expires_at or now >= absolute_expiry:
        await db.delete(row)
        await db.commit()
        return None
    if now - row.last_seen_at >= TOUCH_INTERVAL:
        row.last_seen_at = now
        row.expires_at = min(now + SLIDING_LIFETIME, absolute_expiry)
        await db.commit()
    return row


async def delete_session(db: AsyncSession, session_id: str) -> None:
    await db.execute(delete(Session).where(Session.id == session_id))
    await db.commit()


async def delete_all_sessions(db: AsyncSession) -> None:
    await db.execute(delete(Session))
    await db.commit()
