"""Session cookie helpers."""

from datetime import datetime

from starlette.responses import Response

from app.auth.sessions import cookie_name
from app.db.models import utcnow


def set_session_cookie(
    response: Response, token: str, *, secure: bool, absolute_expiry: datetime
) -> None:
    response.set_cookie(
        cookie_name(secure),
        token,
        max_age=max(int((absolute_expiry - utcnow()).total_seconds()), 0),
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(
        cookie_name(secure), path="/", secure=secure, httponly=True, samesite="strict"
    )
