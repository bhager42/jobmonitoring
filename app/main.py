"""FastAPI application factory."""

from fastapi import FastAPI

from app import __version__
from app.web.routes import health


def create_app() -> FastAPI:
    app = FastAPI(
        title="Job Monitor",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(health.router)
    return app
