"""`jobmonitor` command-line entry point."""

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from app import __version__
from app.config import Settings, get_settings

if TYPE_CHECKING:
    from app.scraping.base import FetchContext


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


def make_fetch_context(
    settings: Settings, *, respect_robots: bool, render_mode: str
) -> "FetchContext":
    from app.services.fetching import create_browser, create_http_client, make_context

    client = create_http_client(settings)
    return make_context(
        client,
        create_browser(settings, client),
        respect_robots=respect_robots,
        render_mode=render_mode,
    )


async def _probe(settings: Settings, args: argparse.Namespace) -> None:
    from app.scraping.base import FetchError
    from app.scraping.detect import detect

    ctx = make_fetch_context(
        settings, respect_robots=not args.ignore_robots, render_mode=args.render
    )
    css = {
        "item_selector": args.item_selector,
        "title_selector": args.title_selector,
        "link_selector": args.link_selector,
        "location_selector": args.location_selector,
    }
    try:
        outcome = await detect(args.url, ctx, css={k: v for k, v in css.items() if v} or None)
    except FetchError as exc:
        sys.exit(f"ERROR: {exc}")
    finally:
        if ctx.browser is not None:
            await ctx.browser.close()
        await ctx.client.aclose()

    params = json.dumps(outcome.config.params) if outcome.config.params else ""
    print(f"Adapter:   {outcome.config.adapter} {params}".rstrip())
    print(f"Rendered:  {'yes (browser)' if outcome.rendered else 'no (static HTML or API)'}")
    print(f"Postings:  {len(outcome.postings)}")
    for number, posting in enumerate(outcome.postings[: args.limit], start=1):
        details = [d for d in (posting.location, posting.department) if d]
        if posting.remote:
            details.append("remote")
        if posting.posted_at:
            details.append(f"posted {posting.posted_at:%Y-%m-%d}")
        print(f"{number:>4}. {posting.title}" + (f"  [{' · '.join(details)}]" if details else ""))
        print(f"      {posting.url}")
    if len(outcome.postings) > args.limit:
        print(f"      ... and {len(outcome.postings) - args.limit} more")


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Alembic logs every plugin it loads at INFO; keep its migration messages, drop the noise.
    logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)


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

    probe = sub.add_parser(
        "probe", help="Detect how to read a careers page and print the postings found"
    )
    probe.add_argument("url")
    probe.add_argument("--render", choices=("auto", "static", "browser"), default="auto")
    probe.add_argument("--ignore-robots", action="store_true", help="Do not obey robots.txt")
    probe.add_argument("--limit", type=int, default=20, help="How many postings to print")
    probe.add_argument("--item-selector", help="CSS selector for each posting")
    probe.add_argument("--title-selector", help="CSS selector for the title within an item")
    probe.add_argument("--link-selector", help="CSS selector for the link within an item")
    probe.add_argument("--location-selector", help="CSS selector for the location")

    args = parser.parse_args(argv)
    if args.command == "serve":
        serve(get_settings())
    elif args.command == "migrate":
        migrate(get_settings())
    elif args.command == "reset-password":
        reset_password(get_settings(), from_stdin=args.password_stdin)
    elif args.command == "probe":
        settings = get_settings()
        logging.basicConfig(level="WARNING", format="%(levelname)s %(name)s: %(message)s")
        asyncio.run(_probe(settings, args))


if __name__ == "__main__":
    main()
