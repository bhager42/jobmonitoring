"""SQLite backups using `VACUUM INTO`, which produces a consistent copy of a live database."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def vacuum_into(db_path: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()
    return dest


def create_backup(db_path: Path, backup_dir: Path, prefix: str, keep: int) -> Path:
    """Back up the database to `<backup_dir>/<prefix>-<timestamp>.db` and prune older copies."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    dest = vacuum_into(db_path, backup_dir / f"{prefix}-{stamp}.db")
    prune_backups(backup_dir, prefix, keep)
    return dest


def prune_backups(backup_dir: Path, prefix: str, keep: int) -> None:
    backups = sorted(backup_dir.glob(f"{prefix}-*.db"))
    for old in backups[: max(len(backups) - keep, 0)]:
        old.unlink()
