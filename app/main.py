"""FastAPI application factory."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.auth.bootstrap import BootstrapError, bootstrap_auth
from app.auth.middleware import AuthMiddleware
from app.config import Settings, get_settings
from app.db.engine import create_engine, create_session_factory
from app.db.models import utcnow
from app.security.headers import SecurityHeadersMiddleware
from app.security.secrets import SecretBox
from app.services.checks import CheckRunner, mark_interrupted_runs
from app.services.fetching import context_factory, create_browser, create_http_client
from app.services.maintenance import run_maintenance_if_due
from app.services.notify import AppriseSender, Notifier
from app.services.scheduler import Scheduler
from app.web.api import v1
from app.web.assets import STATIC_DIR, VersionedStaticFiles
from app.web.preview import PreviewCache
from app.web.routes import auth, health, matches, monitors, notifications, pages
from app.web.templating import templates

log = logging.getLogger(__name__)

FAILED_LOGIN_DELAY_SECONDS = 0.3


def create_app(
    settings: Settings | None = None,
    *,
    failed_login_delay: float = FAILED_LOGIN_DELAY_SECONDS,
    start_scheduler: bool = True,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        engine = create_engine(settings.db_path)
        app.state.engine = engine
        app.state.session_factory = factory = create_session_factory(engine)
        try:
            async with factory() as db:
                app.state.auth = await bootstrap_auth(db, settings)
        except BootstrapError as exc:
            log.error("%s", exc)
            await engine.dispose()
            raise

        async with factory() as db:
            if interrupted := await mark_interrupted_runs(db, utcnow()):
                log.warning("Marked %d check(s) interrupted by the last shutdown", interrupted)
        client = create_http_client(settings)
        browser = create_browser(settings, client)
        notifier = Notifier(factory, SecretBox(settings.secrets_key_path), AppriseSender())
        runner = CheckRunner(
            factory, context_factory(client, browser), on_new_matches=notifier.notify_matches
        )
        scheduler = Scheduler(
            factory,
            runner,
            max_concurrent=settings.max_concurrent_checks,
            maintenance=partial(run_maintenance_if_due, settings, factory),
        )
        app.state.http_client, app.state.browser = client, browser
        app.state.runner, app.state.scheduler = runner, scheduler
        app.state.notifier = notifier
        if start_scheduler:
            scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            if browser is not None:
                await browser.close()
            await client.aclose()
            await engine.dispose()

    app = FastAPI(
        title="Job Monitor",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.failed_login_delay = failed_login_delay

    # Added innermost first: security headers wrap everything, including auth rejections.
    app.add_middleware(AuthMiddleware, secure_cookies=settings.secure_cookies)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.secure_cookies)

    app.state.previews = PreviewCache()

    @app.exception_handler(StarletteHTTPException)
    async def html_errors(request: Request, exc: StarletteHTTPException) -> Response:
        """HTML pages get a friendly error page; the JSON API keeps JSON errors."""
        if request.url.path.startswith("/api/") or exc.status_code < 400:
            return await http_exception_handler(request, exc)
        title = {404: "Not found", 405: "Not allowed"}.get(exc.status_code, "Something went wrong")
        message = (
            "There's nothing at this address. It may have been deleted."
            if exc.status_code == 404
            else str(exc.detail)
        )
        return templates.TemplateResponse(
            request,
            "pages/error.html",
            {"title": title, "message": message},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    app.mount("/static", VersionedStaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(monitors.router)
    app.include_router(matches.router)
    app.include_router(notifications.router)
    app.include_router(v1.router)
    return app
