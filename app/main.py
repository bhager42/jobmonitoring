"""FastAPI application factory."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.auth.bootstrap import BootstrapError, bootstrap_auth
from app.auth.middleware import AuthMiddleware
from app.config import Settings, get_settings
from app.db.engine import create_engine, create_session_factory
from app.security.headers import SecurityHeadersMiddleware
from app.web.api import v1
from app.web.routes import auth, health, pages

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "web" / "static"
FAILED_LOGIN_DELAY_SECONDS = 0.3


def create_app(
    settings: Settings | None = None, *, failed_login_delay: float = FAILED_LOGIN_DELAY_SECONDS
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
        try:
            yield
        finally:
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

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(v1.router)
    return app
