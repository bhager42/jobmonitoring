"""CSRF defences: a per-session synchronizer token plus a Fetch Metadata / Origin check."""

import hashlib
import hmac
from collections.abc import Mapping

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
HEADER_NAME = "x-csrf-token"
FORM_FIELD = "csrf_token"


def csrf_token(secret_key: str, session_id: str) -> str:
    """Derive the session's CSRF token; it changes whenever the session does."""
    return hmac.new(secret_key.encode(), f"csrf:{session_id}".encode(), hashlib.sha256).hexdigest()


def verify_csrf_token(secret_key: str, session_id: str, candidate: str | None) -> bool:
    if not candidate:
        return False
    return hmac.compare_digest(csrf_token(secret_key, session_id), candidate)


def is_cross_origin(headers: Mapping[str, str], own_origin: str) -> bool:
    """True when a browser tells us the request came from another site.

    Uses Sec-Fetch-Site when present (all current browsers send it), else Origin. Requests with
    neither header are not from a browser, so CSRF does not apply to them.
    """
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site not in ("same-origin", "none")
    origin = headers.get("origin")
    if origin is None:
        return False
    return origin.lower() != own_origin.lower()
