"""JSON API. Uses the same session cookie and CSRF rules as the web pages."""

from fastapi import APIRouter, Request

from app.auth.middleware import AuthContext

router = APIRouter(prefix="/api/v1")


@router.get("/session")
async def session_info(request: Request) -> dict[str, object]:
    context: AuthContext = request.state.auth
    return {"authenticated": True, "csrf_token": context.csrf_token}
