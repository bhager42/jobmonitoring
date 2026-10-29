"""Daily housekeeping: prune old history and take a database backup."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import CheckRun, Match, Posting
from app.services.app_settings import get_setting, set_setting
from app.services.backup import create_backup

log = logging.getLogger(__name__)

CHECK_RUN_RETENTION = timedelta(days=90)
POSTING_RETENTION = timedelta(days=180)
MAINTENANCE_INTERVAL = timedelta(days=1)
LAST_RUN_KEY = "maintenance_last_run"
BACKUP_PREFIX = "daily"


@dataclass(frozen=True)
class MaintenanceResult:
    check_runs_deleted: int
    postings_deleted: int
    backup: Path


async def prune(db: AsyncSession, now: datetime) -> tuple[int, int]:
    runs = await db.execute(
        delete(CheckRun).where(
            CheckRun.started_at < now - CHECK_RUN_RETENTION, CheckRun.status != "running"
        )
    )
    postings = await db.execute(
        delete(Posting).where(
            Posting.is_active.is_(False),
            Posting.last_seen_at < now - POSTING_RETENTION,
            ~exists(select(Match.id).where(Match.posting_id == Posting.id)),
        )
    )
    await db.commit()
    return runs.rowcount, postings.rowcount  # type: ignore[attr-defined]


async def maintenance_due(db: AsyncSession, now: datetime) -> bool:
    last = await get_setting(db, LAST_RUN_KEY)
    if last is None:
        return True
    try:
        return now - datetime.fromisoformat(last) >= MAINTENANCE_INTERVAL
    except ValueError:
        return True


async def run_maintenance(
    settings: Settings, factory: async_sessionmaker[AsyncSession], now: datetime
) -> MaintenanceResult:
    async with factory() as db:
        runs, postings = await prune(db, now)
    backup = await asyncio.to_thread(
        create_backup, settings.db_path, settings.backup_dir, BACKUP_PREFIX, settings.backup_keep
    )
    async with factory() as db:
        await set_setting(db, LAST_RUN_KEY, now.isoformat())
        await db.commit()
    log.info(
        "Maintenance: removed %d old check runs and %d old postings; backed up to %s",
        runs,
        postings,
        backup,
    )
    return MaintenanceResult(runs, postings, backup)


async def run_maintenance_if_due(
    settings: Settings, factory: async_sessionmaker[AsyncSession], now: datetime
) -> MaintenanceResult | None:
    async with factory() as db:
        if not await maintenance_due(db, now):
            return None
    return await run_maintenance(settings, factory, now)
