"""Run Alembic migrations, taking a backup of an existing database first."""

import logging
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import create_engine

from app.config import Settings
from app.services.backup import create_backup

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
PRE_MIGRATE_PREFIX = "pre-migrate"


class DatabaseTooNewError(RuntimeError):
    """The database was migrated by a newer version of the app."""


@dataclass(frozen=True)
class MigrationResult:
    from_revision: str | None
    to_revision: str
    backup: Path | None

    @property
    def changed(self) -> bool:
        return self.from_revision != self.to_revision


def alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def head_revision(cfg: Config) -> str:
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if head is None:
        raise RuntimeError("no migrations found")
    return head


def current_revision(db_path: Path) -> str | None:
    if not db_path.exists():
        return None
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def _is_known_revision(cfg: Config, revision: str) -> bool:
    try:
        ScriptDirectory.from_config(cfg).get_revision(revision)
    except CommandError:
        return False
    return True


def upgrade_to_head(settings: Settings) -> MigrationResult:
    db_path = settings.db_path
    cfg = alembic_config(db_path)
    head = head_revision(cfg)
    current = current_revision(db_path)

    if current == head:
        return MigrationResult(current, head, None)

    if current is not None and not _is_known_revision(cfg, current):
        raise DatabaseTooNewError(
            f"database is at revision {current}, which this version of Job Monitor does not "
            "know. It was probably upgraded by a newer image; run that image, or restore a "
            f"backup from {settings.backup_dir}."
        )

    backup = None
    if db_path.exists() and db_path.stat().st_size > 0:
        backup = create_backup(
            db_path, settings.backup_dir, PRE_MIGRATE_PREFIX, keep=settings.backup_keep
        )
        log.info("Backed up database to %s before migrating", backup)

    command.upgrade(cfg, "head")
    log.info("Migrated database from %s to %s", current or "empty", head)
    return MigrationResult(current, head, backup)
