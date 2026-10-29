"""Key/value application settings stored in the `settings` table."""

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting

PASSWORD_HASH = "password_hash"  # noqa: S105 - a settings key, not a password
SECRET_KEY = "secret_key"  # noqa: S105 - a settings key, not a secret


async def get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(Setting, key)
    return None if row is None else row.value


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


async def delete_setting(session: AsyncSession, key: str) -> None:
    await session.execute(delete(Setting).where(Setting.key == key))
