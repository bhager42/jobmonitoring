"""Authenticated pages."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import (
    PasswordPolicyError,
    hash_password,
    validate_new_password,
    verify_password,
)
from app.auth.ratelimit import is_locked_out, record_attempt
from app.auth.sessions import delete_all_sessions
from app.db.engine import get_session
from app.services.app_settings import PASSWORD_HASH, get_setting, set_setting
from app.web.routes.auth import LOCKED_MESSAGE, client_ip, failed_attempt_delay, start_session
from app.web.templating import templates

log = logging.getLogger(__name__)
router = APIRouter()

DbSession = Annotated[AsyncSession, Depends(get_session)]


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> Response:
    return templates.TemplateResponse(request, "pages/dashboard.html")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, changed: bool = False) -> Response:
    return templates.TemplateResponse(
        request, "pages/settings.html", {"error": None, "changed": changed}
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

    def form_error(message: str, status: int) -> Response:
        return templates.TemplateResponse(
            request,
            "pages/settings.html",
            {"error": message, "changed": False},
            status_code=status,
        )

    if await is_locked_out(db, ip):
        return form_error(LOCKED_MESSAGE, 429)
    password_hash = await get_setting(db, PASSWORD_HASH)
    if password_hash is None or not verify_password(password_hash, current):
        await record_attempt(db, ip, success=False)
        await failed_attempt_delay(request)
        return form_error("Your current password is not right.", 403)
    try:
        validate_new_password(password, confirm)
    except PasswordPolicyError as exc:
        return form_error(str(exc), 422)

    await set_setting(db, PASSWORD_HASH, hash_password(password))
    await record_attempt(db, ip, success=True)
    await delete_all_sessions(db)  # sign out everywhere else
    log.warning("Password changed from %s; all other sessions signed out", ip)
    return await start_session(request, db, "/settings?changed=true")
