"""Authentication and CSRF middleware. Every route requires a login unless listed as public."""

import logging
from dataclasses import dataclass
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth.bootstrap import AuthState
from app.auth.csrf import FORM_FIELD, HEADER_NAME, SAFE_METHODS, csrf_token, is_cross_origin
from app.auth.csrf import verify_csrf_token as _verify
from app.auth.sessions import cookie_name, validate_session
from app.services.app_settings import PASSWORD_HASH, get_setting

log = logging.getLogger(__name__)

# Paths reachable without a session. Everything else needs one ("default deny").
PUBLIC_PATHS = frozenset({"/healthz", "/login", "/setup"})
PUBLIC_PREFIXES = ("/static/",)
# While no password exists, only these are reachable; everything else redirects to /setup.
SETUP_PATHS = frozenset({"/healthz", "/setup"})

MAX_FORM_BYTES = 1024 * 1024
FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


@dataclass(frozen=True)
class AuthContext:
    session_id: str
    csrf_token: str


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def safe_next_url(target: str | None) -> str:
    """Only allow same-site relative paths, to prevent open redirects after login."""
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/"
    if any(ch in target for ch in "\r\n\t"):
        return "/"
    return target


class _BodyTooLargeError(Exception):
    pass


async def _buffer_body(receive: Receive, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunk: bytes = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            raise _BodyTooLargeError
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replay(body: bytes, receive: Receive) -> Receive:
    sent = False

    async def replay_receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay_receive


class AuthMiddleware:
    def __init__(self, app: ASGIApp, *, secure_cookies: bool) -> None:
        self.app = app
        self.cookie = cookie_name(secure_cookies)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        path: str = scope["path"]
        unsafe = request.method not in SAFE_METHODS
        auth: AuthState = request.app.state.auth

        if unsafe and is_cross_origin(request.headers, _own_origin(request)):
            log.warning("Blocked cross-origin %s %s", request.method, path)
            await PlainTextResponse("Cross-origin request blocked.", 403)(scope, receive, send)
            return

        if not auth.password_set:
            factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
            async with factory() as db:
                if await get_setting(db, PASSWORD_HASH) is not None:
                    auth.consume_setup_token()  # set meanwhile, e.g. by `jobmonitor reset-password`
        if not auth.password_set:
            if path in SETUP_PATHS or path.startswith(PUBLIC_PREFIXES):
                await self.app(scope, receive, send)
            else:
                await self._redirect_or_401(request, "/setup", "Setup required")(
                    scope, receive, send
                )
            return

        if is_public(path):
            await self.app(scope, receive, send)
            return

        factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
        async with factory() as db:
            session = await validate_session(db, request.cookies.get(self.cookie))
        if session is None:
            target = "/login"
            if request.method == "GET" and path != "/":
                target += "?next=" + quote(_path_with_query(request), safe="")
            await self._redirect_or_401(request, target, "Not authenticated")(scope, receive, send)
            return

        context = AuthContext(session.id, csrf_token(auth.secret_key, session.id))
        scope.setdefault("state", {})["auth"] = context

        if unsafe:
            candidate = request.headers.get(HEADER_NAME)
            if candidate is None and request.headers.get("content-type", "").startswith(FORM_TYPES):
                try:
                    body = await _buffer_body(receive, MAX_FORM_BYTES)
                except _BodyTooLargeError:
                    await PlainTextResponse("Request too large.", 413)(scope, receive, send)
                    return
                form = await Request(scope, _replay(body, receive)).form()
                receive = _replay(body, receive)  # let the route read the body too
                value = form.get(FORM_FIELD)
                candidate = value if isinstance(value, str) else None
            if not _verify(auth.secret_key, session.id, candidate):
                log.warning("Rejected %s %s: missing or invalid CSRF token", request.method, path)
                await PlainTextResponse("Invalid or missing CSRF token.", 403)(scope, receive, send)
                return

        await self.app(scope, receive, send)

    @staticmethod
    def _redirect_or_401(request: Request, target: str, detail: str) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": detail}, 401)
        if request.headers.get("hx-request") == "true":
            return Response(status_code=401, headers={"HX-Redirect": target})
        return RedirectResponse(target, 303)


def _own_origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def _path_with_query(request: Request) -> str:
    query = request.url.query
    return request.url.path + (f"?{query}" if query else "")
