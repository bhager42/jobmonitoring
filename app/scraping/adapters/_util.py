"""Helpers shared by adapters."""

import html as html_lib
import re
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

SLUG = r"[A-Za-z0-9][A-Za-z0-9_.%-]*"


def html_to_text(value: object) -> str | None:
    """Convert an HTML fragment (possibly entity-escaped, as Greenhouse sends) to plain text."""
    if not isinstance(value, str) or not value.strip():
        return None
    markup = html_lib.unescape(value) if "&lt;" in value else value
    text = LexborHTMLParser(markup).text(separator=" ")
    return re.sub(r"\s+", " ", text).strip() or None


def parse_datetime(value: object) -> datetime | None:
    """Parse ISO-8601 strings/dates or epoch milliseconds into an aware UTC datetime."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 10**11 else value
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def path_segments(url: str) -> list[str]:
    return [s for s in urlsplit(url).path.split("/") if s]


def host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


JSON = dict[str, Any]


def as_dict(value: object) -> JSON | None:
    return cast(JSON, value) if isinstance(value, dict) else None


def as_list(value: object) -> list[Any]:
    if value is None:
        return []
    return cast(list[Any], value) if isinstance(value, list) else [value]


def dicts(value: object) -> list[JSON]:
    return [d for item in as_list(value) if (d := as_dict(item)) is not None]


def get_str(obj: object, *keys: str) -> str | None:
    """Walk nested dicts; return the value if it is a non-empty string."""
    current: object = obj
    for key in keys:
        node = as_dict(current)
        if node is None:
            return None
        current = node.get(key)
    return current if isinstance(current, str) and current.strip() else None


def get_id(obj: JSON, key: str = "id") -> str | None:
    value = obj.get(key)
    if isinstance(value, bool) or value is None:
        return None
    return str(value) if isinstance(value, str | int) else None


def humanize(value: str | None) -> str | None:
    """'FullTime' -> 'Full time', 'FULL_TIME' -> 'Full time'."""
    if not value:
        return None
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value).replace("_", " ").replace("-", " ")
    return spaced.strip().lower().capitalize()


def document_base(tree: LexborHTMLParser, page_url: str) -> str:
    """Relative links resolve against <base href> when the page has one."""
    node = tree.css_first("base[href]")
    href = (node.attributes.get("href") or "").strip() if node else ""
    base = urljoin(page_url, href) if href else page_url
    return base if base.startswith(("http://", "https://")) else page_url
