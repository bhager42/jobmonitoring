"""Alembic environment. Migrations run synchronously with the stdlib sqlite3 driver."""

from alembic import context
from sqlalchemy import Connection, create_engine

from app.config import get_settings
from app.db.models import Base

config = context.config
target_metadata = Base.metadata


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite needs table rebuilds for most ALTERs
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection: Connection | None = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        return
    url = config.get_main_option("sqlalchemy.url") or f"sqlite:///{get_settings().db_path}"
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            _configure(conn)
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline (--sql) migrations are not supported.")
run_migrations_online()
