"""Static files with content-hashed URLs: `/static/css/app.css?v=<hash>` is cached for a year."""

import hashlib
import mimetypes
from functools import lru_cache
from pathlib import Path

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

STATIC_DIR = Path(__file__).parent / "static"
IMMUTABLE = "public, max-age=31536000, immutable"

mimetypes.add_type("application/manifest+json", ".webmanifest")


@lru_cache(maxsize=1)
def _versions() -> dict[str, str]:
    return {
        path.relative_to(STATIC_DIR).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        for path in STATIC_DIR.rglob("*")
        if path.is_file()
    }


def static_url(path: str) -> str:
    version = _versions().get(path)
    if version is None:
        raise ValueError(f"no static file named {path!r}")
    return f"/static/{path}?v={version}"


class VersionedStaticFiles(StaticFiles):
    """Cache for a year when the URL has the file's current version; revalidate otherwise."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        query = scope.get("query_string", b"").decode()
        current = _versions().get(path)
        if response.status_code == 200 and current and f"v={current}" in query.split("&"):
            response.headers["Cache-Control"] = IMMUTABLE
        return response
