"""`jobmonitor` command-line entry point."""

import argparse
import os
import sys
import tempfile
from pathlib import Path

from app import __version__
from app.config import Settings, get_settings


def ensure_data_dir_writable(data_dir: Path) -> None:
    """Exit with a helpful message if the data directory cannot be written."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=data_dir, prefix=".write-test-"):
            pass
    except OSError as exc:
        sys.exit(
            f"ERROR: data directory {data_dir} is not writable by UID {os.getuid()} "
            f"GID {os.getgid()} ({exc.strerror}).\n"
            "On TrueNAS, give the 'apps' user (568) Modify permission on the dataset "
            "mounted at /data."
        )


def migrate(settings: Settings) -> None:
    from app.db.migrate import DatabaseTooNewError, upgrade_to_head

    ensure_data_dir_writable(settings.data_dir)
    try:
        result = upgrade_to_head(settings)
    except DatabaseTooNewError as exc:
        sys.exit(f"ERROR: {exc}")
    if not result.changed:
        print(f"Database is up to date (revision {result.to_revision}).")
        return
    if result.backup is not None:
        print(f"Backed up database to {result.backup}")
    print(f"Migrated database from {result.from_revision or 'empty'} to {result.to_revision}.")


def serve(settings: Settings) -> None:
    import uvicorn

    migrate(settings)
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - must listen on all interfaces inside the container
        port=settings.port,
        workers=1,
        log_level=settings.log_level,
        proxy_headers=settings.trust_proxy,
        forwarded_allow_ips=settings.forwarded_allow_ips if settings.trust_proxy else None,
        server_header=False,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jobmonitor", description="Job Monitor")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Apply database migrations, then run the web server")
    sub.add_parser("migrate", help="Apply database migrations and exit")

    args = parser.parse_args(argv)
    if args.command == "serve":
        serve(get_settings())
    elif args.command == "migrate":
        migrate(get_settings())


if __name__ == "__main__":
    main()
