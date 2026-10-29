"""URL canonicalization and posting fingerprints."""

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = frozenset(
    {
        "gh_src",
        "source",
        "src",
        "lever-source",
        "lever-origin",
        "lever-via",
        "ref",
        "referrer",
        "trk",
        "fbclid",
        "gclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
    }
)
DEFAULT_PORTS = {"http": 80, "https": 443}


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("utm_") or lowered in TRACKING_PARAMS


def canonical_url(url: str) -> str:
    """Lower-case scheme/host; drop default port, fragment, tracking params and trailing slash."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if parts.port and parts.port != DEFAULT_PORTS.get(scheme):
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)
        )
    )
    return urlunsplit((scheme, host, path, query, ""))


def fingerprint(adapter: str, external_id: str | None, url: str) -> str:
    identity = external_id or canonical_url(url)
    return hashlib.sha256(f"{adapter}:{identity}".encode()).hexdigest()
