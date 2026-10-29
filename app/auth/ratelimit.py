"""Login rate limiting: too many recent failures from one IP lock that IP out."""

from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LoginAttempt, utcnow

MAX_FAILURES = 5
WINDOW = timedelta(minutes=15)
RETENTION = timedelta(days=1)


async def is_locked_out(db: AsyncSession, ip: str) -> bool:
    """Locked when the IP has MAX_FAILURES failures in the last WINDOW since its last success."""
    now = utcnow()
    last_success = (
        await db.execute(
            select(func.max(LoginAttempt.attempted_at)).where(
                LoginAttempt.ip == ip, LoginAttempt.success.is_(True)
            )
        )
    ).scalar_one_or_none()
    since = now - WINDOW
    if last_success is not None:
        since = max(since, last_success)
    failures = (
        await db.execute(
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.ip == ip,
                LoginAttempt.success.is_(False),
                LoginAttempt.attempted_at > since,
            )
        )
    ).scalar_one()
    return failures >= MAX_FAILURES


async def record_attempt(db: AsyncSession, ip: str, *, success: bool) -> None:
    now = utcnow()
    db.add(LoginAttempt(ip=ip, attempted_at=now, success=success))
    await db.execute(delete(LoginAttempt).where(LoginAttempt.attempted_at < now - RETENTION))
    await db.commit()
