import sqlite3

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.config import Settings
from app.db.migrate import (
    PRE_MIGRATE_PREFIX,
    DatabaseTooNewError,
    alembic_config,
    current_revision,
    head_revision,
    upgrade_to_head,
)
from app.db.models import Base


def test_fresh_data_dir_is_migrated_to_head(settings: Settings) -> None:
    settings.data_dir.mkdir()
    result = upgrade_to_head(settings)

    head = head_revision(alembic_config(settings.db_path))
    assert result.from_revision is None
    assert result.to_revision == head
    assert result.changed
    assert result.backup is None  # nothing to back up on first run
    assert current_revision(settings.db_path) == head

    engine = create_engine(f"sqlite:///{settings.db_path}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert tables == set(Base.metadata.tables) | {"alembic_version"}


def test_second_run_is_a_noop(migrated_settings: Settings) -> None:
    mtime = migrated_settings.db_path.stat().st_mtime_ns
    result = upgrade_to_head(migrated_settings)

    assert not result.changed
    assert result.backup is None
    assert not migrated_settings.backup_dir.exists()
    assert migrated_settings.db_path.stat().st_mtime_ns == mtime


def test_models_match_migrations(migrated_settings: Settings) -> None:
    engine = create_engine(f"sqlite:///{migrated_settings.db_path}")
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            diff = compare_metadata(ctx, Base.metadata)
    finally:
        engine.dispose()
    assert diff == []


def test_backs_up_existing_database_before_migrating(migrated_settings: Settings) -> None:
    cfg = alembic_config(migrated_settings.db_path)
    command.downgrade(cfg, "base")
    with sqlite3.connect(migrated_settings.db_path) as conn:
        conn.execute("CREATE TABLE marker (x INTEGER)")
        conn.execute("INSERT INTO marker VALUES (42)")
    conn.close()

    result = upgrade_to_head(migrated_settings)

    assert result.changed
    assert result.backup is not None
    assert result.backup.parent == migrated_settings.backup_dir
    assert result.backup.name.startswith(f"{PRE_MIGRATE_PREFIX}-")
    with sqlite3.connect(result.backup) as backup:
        assert backup.execute("SELECT x FROM marker").fetchone() == (42,)
    backup.close()


def test_downgrade_to_base_and_back(migrated_settings: Settings) -> None:
    cfg = alembic_config(migrated_settings.db_path)
    command.downgrade(cfg, "base")
    assert current_revision(migrated_settings.db_path) is None
    command.upgrade(cfg, "head")
    assert current_revision(migrated_settings.db_path) == head_revision(cfg)


def test_refuses_database_from_newer_version(migrated_settings: Settings) -> None:
    with sqlite3.connect(migrated_settings.db_path) as conn:
        conn.execute("UPDATE alembic_version SET version_num = 'ffffffffffff'")
    conn.close()

    with pytest.raises(DatabaseTooNewError, match="ffffffffffff"):
        upgrade_to_head(migrated_settings)
    assert not migrated_settings.backup_dir.exists()


def test_upgrade_from_initial_schema_keeps_monitors(settings: Settings) -> None:
    """An install created before `alert_existing` existed upgrades in place, with a backup."""
    settings.data_dir.mkdir()
    cfg = alembic_config(settings.db_path)
    command.upgrade(cfg, "e7ea16393223")  # the initial schema
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO monitors (name, url, adapter_config, render_mode, criteria,"
            " interval_hours, enabled, respect_robots, notify, consecutive_failures,"
            " baseline_done, created_at, updated_at) VALUES ('Old', 'https://x.example/', '{}',"
            " 'auto', '{}', 12, 1, 1, 1, 0, 1, '2026-01-01', '2026-01-01')"
        )
    conn.close()

    result = upgrade_to_head(settings)

    assert result.from_revision == "e7ea16393223"
    assert result.backup is not None and result.backup.exists()
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("SELECT name, alert_existing FROM monitors").fetchall() == [("Old", 0)]
    conn.close()
