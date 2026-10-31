"""What `jobmonitor serve` runs instead of the app when it can't start (e.g. /data not writable).

Exiting would make Docker restart the container in a loop, which TrueNAS shows as an app cycling
between Deploying and Stopped that can't be stopped. Instead the container stays up: every page
explains the problem, and /healthz answers 503 so the app shows as unhealthy.
"""

import html
import json

from starlette.types import ASGIApp, Receive, Scope, Send

from app import __version__
from app.security.headers import SecurityHeadersMiddleware

PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Monitor can't start</title></head>
<body>
<main>
<h1>Job Monitor can't start</h1>
<p>{message}</p>
<p>Fix this, then restart the app. The same message is in the app's log.</p>
<p><small>Job Monitor {version}</small></p>
</main>
</body>
</html>
"""


def startup_error_app(message: str) -> ASGIApp:
    page = PAGE.format(message=html.escape(message), version=__version__).encode()
    health = json.dumps({"status": "error", "error": message}).encode()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while (await receive())["type"] != "lifespan.shutdown":
                await send({"type": "lifespan.startup.complete"})
            await send({"type": "lifespan.shutdown.complete"})
            return
        if scope["type"] != "http":
            return
        is_health = scope["path"] == "/healthz"
        body = health if is_health else page
        content_type = b"application/json" if is_health else b"text/html; charset=utf-8"
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", content_type),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return SecurityHeadersMiddleware(app, hsts=False)
