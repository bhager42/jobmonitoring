"""Unauthenticated liveness endpoint used by the container healthcheck."""

from fastapi import APIRouter

from app import __version__

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
