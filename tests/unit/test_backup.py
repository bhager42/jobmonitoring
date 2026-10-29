import sqlite3
from pathlib import Path

from app.services.backup import create_backup, prune_backups, vacuum_into


def _make_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1), (2)")
    conn.close()


def test_vacuum_into_creates_consistent_copy(tmp_path: Path) -> None:
    db = tmp_path / "src.db"
    _make_db(db)
    dest = vacuum_into(db, tmp_path / "nested" / "copy.db")
    with sqlite3.connect(dest) as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone() == (2,)
    conn.close()


def test_create_backup_prunes_oldest(tmp_path: Path) -> None:
    db = tmp_path / "src.db"
    _make_db(db)
    backups = tmp_path / "backups"
    made = [create_backup(db, backups, "daily", keep=2) for _ in range(3)]

    remaining = sorted(backups.iterdir())
    assert remaining == sorted(made[1:])


def test_prune_only_touches_matching_prefix(tmp_path: Path) -> None:
    for name in ("daily-1.db", "daily-2.db", "pre-migrate-1.db", "notes.txt"):
        (tmp_path / name).touch()
    prune_backups(tmp_path, "daily", keep=1)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "daily-2.db",
        "notes.txt",
        "pre-migrate-1.db",
    ]
