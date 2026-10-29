"""`jobmonitor` command-line entry point."""

import argparse
import asyncio
import getpass
import logging
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


async def _store_password(settings: Settings, password: str) -> None:
    from app.auth.passwords import hash_password
    from app.auth.sessions import delete_all_sessions
    from app.db.engine import create_engine, create_session_factory
    from app.services.app_settings import PASSWORD_HASH, set_setting

    engine = create_engine(settings.db_path)
    try:
        async with create_session_factory(engine)() as db:
            await set_setting(db, PASSWORD_HASH, hash_password(password))
            await delete_all_sessions(db)  # commits both changes
    finally:
        await engine.dispose()


def reset_password(settings: Settings, *, from_stdin: bool) -> None:
    from app.auth.passwords import PasswordPolicyError, validate_new_password

    migrate(settings)
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
        confirm = None
    else:
        password = getpass.getpass("New password: ")
        confirm = getpass.getpass("Confirm new password: ")
    try:
        validate_new_password(password, confirm)
    except PasswordPolicyError as exc:
        sys.exit(f"ERROR: {exc}")
    asyncio.run(_store_password(settings, password))
    print("Password changed. All sessions have been signed out.")


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def serve(settings: Settings) -> None:
    import uvicorn

    configure_logging(settings)
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
    reset = sub.add_parser("reset-password", help="Set a new password and sign out every session")
    reset.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the new password from standard input instead of prompting",
    )

    args = parser.parse_args(argv)
    if args.command == "serve":
        serve(get_settings())
    elif args.command == "migrate":
        migrate(get_settings())
    elif args.command == "reset-password":
        reset_password(get_settings(), from_stdin=args.password_stdin)


if __name__ == "__main__":
    main()
