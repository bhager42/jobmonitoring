"""Authenticated pages."""

import asyncio
import logging
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.auth.passwords import (
    PasswordPolicyError,
    hash_password,
    validate_new_password,
    verify_password,
)
from app.auth.ratelimit import is_locked_out, record_attempt
from app.auth.sessions import delete_all_sessions
from app.config import Settings
from app.db.engine import get_session
from app.db.models import Match, Monitor, NotificationTarget, utcnow
from app.security.secrets import SecretUnavailableError
from app.services.app_settings import PASSWORD_HASH, get_setting, set_setting
from app.services.backup import vacuum_into
from app.services.checks import is_backing_off
from app.services.maintenance import BACKUP_PREFIX
from app.services.notify import LAST_ERROR_KEY, LAST_SENT_KEY, Notifier, parse_timestamped
from app.web.assets import STATIC_DIR
from app.web.routes.auth import LOCKED_MESSAGE, client_ip, failed_attempt_delay, start_session
from app.web.routes.matches import unseen_count, with_details
from app.web.templating import templates

log = logging.getLogger(__name__)
router = APIRouter()

DbSession = Annotated[AsyncSession, Depends(get_session)]


DASHBOARD_LIMIT = 100


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: DbSession) -> Response:
    matches = (
        await db.scalars(
            with_details(select(Match))
            .where(Match.seen.is_(False), Match.dismissed.is_(False))
            .order_by(Match.matched_at.desc(), Match.id.desc())
            .limit(DASHBOARD_LIMIT)
        )
    ).all()
    monitors = (await db.scalars(select(Monitor).order_by(Monitor.name))).all()
    attention = [
        m
        for m in monitors
        if m.enabled and (m.last_status in ("error", "blocked", "warning") or is_backing_off(m))
    ]
    last_check = max((m.last_check_at for m in monitors if m.last_check_at), default=None)
    return templates.TemplateResponse(
        request,
        "pages/dashboard.html",
        {
            "matches": matches,
            "unseen": await unseen_count(db),
            "monitor_count": len(monitors),
            "enabled_count": sum(1 for m in monitors if m.enabled),
            "attention": attention,
            "last_check": last_check,
        },
    )


@dataclass(frozen=True)
class TargetView:
    id: int
    name: str
    masked_url: str
    enabled: bool
    readable: bool


async def notification_targets(request: Request, db: AsyncSession) -> list[TargetView]:
    notifier: Notifier = request.app.state.notifier
    views: list[TargetView] = []
    for target in (
        await db.scalars(select(NotificationTarget).order_by(NotificationTarget.id))
    ).all():
        try:
            masked = notifier.sender.masked(notifier.secrets.decrypt(target.apprise_url_encrypted))
            readable = True
        except SecretUnavailableError:
            masked, readable = "(saved by another install; remove and add again)", False
        views.append(TargetView(target.id, target.name, masked, target.enabled, readable))
    return views


async def settings_context(request: Request, db: AsyncSession) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    backups = sorted(settings.backup_dir.glob(f"{BACKUP_PREFIX}-*.db"))
    latest = datetime.fromtimestamp(backups[-1].stat().st_mtime, UTC) if backups else None
    last_sent, _ = parse_timestamped(await get_setting(db, LAST_SENT_KEY))
    last_error_at, last_error = parse_timestamped(await get_setting(db, LAST_ERROR_KEY))
    return {
        "error": None,
        "changed": False,
        "settings": settings,
        "browser_available": request.app.state.browser is not None,
        "backup_count": len(backups),
        "latest_backup": latest,
        "targets": await notification_targets(request, db),
        "notify_errors": {},
        "notify_values": {},
        "notify_status": request.query_params.get("notify"),
        "last_sent": last_sent,
        # Only show the last failure if nothing has been delivered since.
        "last_error": last_error
        if last_error_at and (not last_sent or last_error_at > last_sent)
        else None,
        "last_error_at": last_error_at,
    }


async def render_settings(
    request: Request,
    *,
    notify_errors: dict[str, str] | None = None,
    notify_values: dict[str, str] | None = None,
    error: str | None = None,
    changed: bool = False,
    status: int = 200,
) -> Response:
    factory = request.app.state.session_factory
    async with factory() as db:
        context = await settings_context(request, db)
    context.update(
        notify_errors=notify_errors or {},
        notify_values=notify_values or {},
        error=error,
        changed=changed,
    )
    return templates.TemplateResponse(request, "pages/settings.html", context, status_code=status)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, changed: bool = False) -> Response:
    return await render_settings(request, changed=changed)


def _backup_copy(db_path: Path) -> Path:
    # A fresh directory: VACUUM INTO refuses to overwrite an existing file.
    return vacuum_into(db_path, Path(tempfile.mkdtemp(prefix="jobmonitor-")) / "backup.db")


@router.get("/settings/backup")
async def download_backup(request: Request) -> Response:
    """Stream a consistent copy of the live database."""
    settings: Settings = request.app.state.settings
    path = await asyncio.to_thread(_backup_copy, settings.db_path)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    return FileResponse(
        path,
        media_type="application/vnd.sqlite3",
        filename=f"jobmonitor-backup-{stamp}.db",
        background=BackgroundTask(shutil.rmtree, path.parent, ignore_errors=True),
    )


@router.get("/sw.js", include_in_schema=False)
async def service_worker() -> Response:
    """Served from the root so the worker's scope covers the whole app."""
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/settings/password", response_class=HTMLResponse)
async def change_password(
    request: Request,
    db: DbSession,
    current: Annotated[str, Form(max_length=2048)],
    password: Annotated[str, Form(max_length=2048)],
    confirm: Annotated[str, Form(max_length=2048)],
) -> Response:
    ip = client_ip(request)

    async def form_error(message: str, status: int) -> Response:
        return await render_settings(request, error=message, status=status)

    if await is_locked_out(db, ip):
        return await form_error(LOCKED_MESSAGE, 429)
    password_hash = await get_setting(db, PASSWORD_HASH)
    if password_hash is None or not verify_password(password_hash, current):
        await record_attempt(db, ip, success=False)
        await failed_attempt_delay(request)
        return await form_error("Your current password is not right.", 403)
    try:
        validate_new_password(password, confirm)
    except PasswordPolicyError as exc:
        return await form_error(str(exc), 422)

    await set_setting(db, PASSWORD_HASH, hash_password(password))
    await record_attempt(db, ip, success=True)
    await delete_all_sessions(db)  # sign out everywhere else
    log.warning("Password changed from %s; all other sessions signed out", ip)
    return await start_session(request, db, "/settings?changed=true")
