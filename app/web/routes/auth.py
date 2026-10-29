"""First-run setup, login, and logout."""

import asyncio
import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.bootstrap import AuthState
from app.auth.middleware import AuthContext, safe_next_url
from app.auth.passwords import (
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    validate_new_password,
    verify_password,
)
from app.auth.ratelimit import is_locked_out, record_attempt
from app.auth.sessions import cookie_name, create_session, delete_session, validate_session
from app.config import Settings
from app.db.engine import get_session
from app.services.app_settings import PASSWORD_HASH, get_setting, set_setting
from app.web.cookies import clear_session_cookie, set_session_cookie
from app.web.templating import templates

log = logging.getLogger(__name__)
router = APIRouter()

DbSession = Annotated[AsyncSession, Depends(get_session)]
LOCKED_MESSAGE = "Too many failed attempts. Wait 15 minutes, then try again."


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def failed_attempt_delay(request: Request) -> None:
    await asyncio.sleep(request.app.state.failed_login_delay)


async def start_session(request: Request, db: AsyncSession, redirect_to: str) -> Response:
    settings: Settings = request.app.state.settings
    new = await create_session(
        db, user_agent=request.headers.get("user-agent"), ip=client_ip(request)
    )
    response = RedirectResponse(redirect_to, 303)
    set_session_cookie(
        response, new.token, secure=settings.secure_cookies, absolute_expiry=new.absolute_expiry
    )
    return response


# --- setup -------------------------------------------------------------------------------------


@router.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request) -> Response:
    auth: AuthState = request.app.state.auth
    if auth.password_set:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "pages/setup.html", {"error": None, "token": ""})


@router.post("/setup", response_class=HTMLResponse)
async def setup_submit(
    request: Request,
    db: DbSession,
    token: Annotated[str, Form(max_length=200)],
    password: Annotated[str, Form(max_length=2048)],
    confirm: Annotated[str, Form(max_length=2048)],
) -> Response:
    auth: AuthState = request.app.state.auth
    if auth.password_set or auth.setup_token is None:
        raise HTTPException(404)
    ip = client_ip(request)

    def form_error(message: str, status: int) -> Response:
        return templates.TemplateResponse(
            request,
            "pages/setup.html",
            {"error": message, "token": token.strip()},
            status_code=status,
        )

    if await is_locked_out(db, ip):
        return form_error(LOCKED_MESSAGE, 429)
    if not secrets.compare_digest(token.strip(), auth.setup_token):
        await record_attempt(db, ip, success=False)
        await failed_attempt_delay(request)
        return form_error("That setup token is not right. Copy it from the container log.", 403)
    try:
        validate_new_password(password, confirm)
    except PasswordPolicyError as exc:
        return form_error(str(exc), 422)

    await set_setting(db, PASSWORD_HASH, hash_password(password))
    await record_attempt(db, ip, success=True)
    auth.consume_setup_token()
    log.warning("Password set via /setup from %s", ip)
    return await start_session(request, db, "/")


# --- login / logout ----------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, db: DbSession, next: str = "/") -> Response:
    settings: Settings = request.app.state.settings
    if await validate_session(db, request.cookies.get(cookie_name(settings.secure_cookies))):
        return RedirectResponse(safe_next_url(next), 303)
    return templates.TemplateResponse(
        request, "pages/login.html", {"error": None, "next": safe_next_url(next)}
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    db: DbSession,
    password: Annotated[str, Form(max_length=2048)],
    next: Annotated[str, Form(max_length=2048)] = "/",
) -> Response:
    ip = client_ip(request)
    target = safe_next_url(next)

    def form_error(message: str, status: int) -> Response:
        return templates.TemplateResponse(
            request, "pages/login.html", {"error": message, "next": target}, status_code=status
        )

    if await is_locked_out(db, ip):
        log.warning("Login refused for locked-out IP %s", ip)
        return form_error(LOCKED_MESSAGE, 429)

    password_hash = await get_setting(db, PASSWORD_HASH)
    if password_hash is None or not verify_password(password_hash, password):
        await record_attempt(db, ip, success=False)
        log.warning("Failed login from %s", ip)
        await failed_attempt_delay(request)
        return form_error("Incorrect password.", 401)

    if needs_rehash(password_hash):
        await set_setting(db, PASSWORD_HASH, hash_password(password))
    await record_attempt(db, ip, success=True)
    return await start_session(request, db, target)


@router.post("/logout")
async def logout(request: Request, db: DbSession) -> Response:
    settings: Settings = request.app.state.settings
    context: AuthContext = request.state.auth
    await delete_session(db, context.session_id)
    response = RedirectResponse("/login", 303)
    clear_session_cookie(response, secure=settings.secure_cookies)
    return response
