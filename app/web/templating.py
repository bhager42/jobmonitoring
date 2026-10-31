"""Jinja2 templates with auth context, versioned static URLs, and date/time filters."""

from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import SOURCE_URL, __version__
from app.db.models import utcnow
from app.web.assets import static_url

TEMPLATES_DIR = Path(__file__).parent / "templates"

STATUS_LABELS = {
    "ok": ("OK", "ok"),
    "warning": ("Warning", "warning"),
    "error": ("Error", "error"),
    "blocked": ("Blocked", "error"),
    "skipped": ("Skipped", "muted"),
    "running": ("Running", "muted"),
    None: ("Not checked yet", "muted"),
}


ADAPTER_LABELS = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
    "smartrecruiters": "SmartRecruiters",
    "workday": "Workday",
    "jsonld": "job data embedded in the page",
    "css_selector": "your CSS selectors",
    "heuristic": "links that look like job postings",
}


ADAPTER_SHORT = {
    "jsonld": "Page data",
    "css_selector": "CSS selectors",
    "heuristic": "Job links",
}


def adapter_badge(adapter: str | None) -> str:
    if not adapter:
        return ""
    return ADAPTER_SHORT.get(adapter) or ADAPTER_LABELS.get(adapter, adapter)


def _auth_context(request: Request) -> dict[str, Any]:
    auth = getattr(request.state, "auth", None)
    return {
        "authenticated": auth is not None,
        "csrf_token": auth.csrf_token if auth is not None else "",
        "app_version": __version__,
        "source_url": SOURCE_URL,
        "current_path": request.url.path,
    }


def relative(value: datetime | None, now: datetime | None = None) -> str:
    """'just now', '5 min ago', 'in 3 h', '2 days ago'."""
    if value is None:
        return "never"
    seconds = int((value - (now or utcnow())).total_seconds())
    future = seconds > 0
    seconds = abs(seconds)
    if seconds < 60:
        return "in under a minute" if future else "just now"
    for unit_seconds, unit in ((86400, "day"), (3600, "h"), (60, "min")):
        if seconds >= unit_seconds:
            count = seconds // unit_seconds
            label = f"{count} {unit}" + ("s" if unit == "day" and count != 1 else "")
            return f"in {label}" if future else f"{label} ago"
    return ""  # pragma: no cover - unreachable


def local(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Absolute time in the server's time zone (the container's TZ)."""
    return value.astimezone().strftime(fmt) if value is not None else ""


def iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def host(url: str | None) -> str:
    return (urlsplit(url).hostname or url or "") if url else ""


def status_label(status: str | None) -> str:
    return STATUS_LABELS.get(status, (status or "", "muted"))[0]


def status_tone(status: str | None) -> str:
    return STATUS_LABELS.get(status, ("", "muted"))[1]


templates = Jinja2Templates(directory=TEMPLATES_DIR, context_processors=[_auth_context])
_env: Any = templates.env  # Jinja's globals/filters dicts are untyped
_env.globals["static"] = static_url
_env.globals["ADAPTER_LABELS"] = ADAPTER_LABELS
_env.filters.update(
    relative=relative,
    local=local,
    iso=iso,
    host=host,
    status_label=status_label,
    status_tone=status_tone,
    adapter_badge=adapter_badge,
)
