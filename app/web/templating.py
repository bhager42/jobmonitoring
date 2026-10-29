"""Jinja2 templates with auth context available to every page."""

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import __version__

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _auth_context(request: Request) -> dict[str, Any]:
    auth = getattr(request.state, "auth", None)
    return {
        "authenticated": auth is not None,
        "csrf_token": auth.csrf_token if auth is not None else "",
        "app_version": __version__,
    }


templates = Jinja2Templates(directory=TEMPLATES_DIR, context_processors=[_auth_context])
