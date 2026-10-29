"""Match actions and the full list of matches."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import ColumnElement, Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.auth.middleware import safe_next_url
from app.db.engine import get_session
from app.db.models import Match, Monitor
from app.web.templating import templates

router = APIRouter()

DbSession = Annotated[AsyncSession, Depends(get_session)]
State = Literal["unseen", "seen", "dismissed", "all"]
PAGE_SIZE = 25
MATCHES_CHANGED = "matches-changed"


def with_details(query: Select[Match]) -> Select[Match]:
    return query.options(joinedload(Match.posting), joinedload(Match.monitor))


async def unseen_count(db: AsyncSession) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(Match)
            .where(Match.seen.is_(False), Match.dismissed.is_(False))
        )
    ).scalar_one()


@router.get("/matches", response_class=HTMLResponse)
async def list_matches(
    request: Request,
    db: DbSession,
    state: State = "unseen",
    monitor: int | None = None,
    page: int = 1,
) -> Response:
    page = max(page, 1)
    conditions: list[ColumnElement[bool]] = []
    if state == "unseen":
        conditions = [Match.seen.is_(False), Match.dismissed.is_(False)]
    elif state == "seen":
        conditions = [Match.seen.is_(True), Match.dismissed.is_(False)]
    elif state == "dismissed":
        conditions = [Match.dismissed.is_(True)]
    if monitor is not None:
        conditions.append(Match.monitor_id == monitor)
    total = (
        await db.execute(select(func.count()).select_from(Match).where(*conditions))
    ).scalar_one()
    rows = (
        await db.scalars(
            with_details(select(Match))
            .where(*conditions)
            .order_by(Match.matched_at.desc(), Match.id.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    ).all()
    monitors = (await db.scalars(select(Monitor).order_by(Monitor.name))).all()
    return templates.TemplateResponse(
        request,
        "pages/matches.html",
        {
            "matches": rows,
            "monitors": monitors,
            "state": state,
            "monitor_id": monitor,
            "page": page,
            "pages": max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
            "total": total,
        },
    )


@router.get("/matches/count", response_class=HTMLResponse)
async def match_count(request: Request, db: DbSession) -> Response:
    return templates.TemplateResponse(
        request, "partials/unseen_count.html", {"unseen": await unseen_count(db)}
    )


async def _update_match(
    request: Request,
    db: DbSession,
    match_id: int,
    values: dict[str, bool],
    context: str,
    next_url: str,
) -> Response:
    match = await db.scalar(with_details(select(Match)).where(Match.id == match_id))
    if match is None:
        raise HTTPException(404, "No such match.")
    for key, value in values.items():
        setattr(match, key, value)
    await db.commit()
    if request.headers.get("hx-request") != "true":
        return RedirectResponse(safe_next_url(next_url), 303)
    if context == "dashboard":
        response: Response = HTMLResponse("")  # the card leaves the dashboard
    else:
        response = templates.TemplateResponse(
            request, "partials/match_card.html", {"match": match, "context": context}
        )
    response.headers["HX-Trigger"] = MATCHES_CHANGED
    return response


ContextField = Annotated[str, Form(max_length=20)]
NextField = Annotated[str, Form(max_length=2048)]


@router.post("/matches/{match_id}/seen", response_class=HTMLResponse)
async def mark_seen(
    request: Request,
    db: DbSession,
    match_id: int,
    context: ContextField = "",
    next: NextField = "/",
) -> Response:
    return await _update_match(request, db, match_id, {"seen": True}, context, next)


@router.post("/matches/{match_id}/unseen", response_class=HTMLResponse)
async def mark_unseen(
    request: Request,
    db: DbSession,
    match_id: int,
    context: ContextField = "",
    next: NextField = "/",
) -> Response:
    return await _update_match(request, db, match_id, {"seen": False}, context, next)


@router.post("/matches/{match_id}/dismiss", response_class=HTMLResponse)
async def dismiss(
    request: Request,
    db: DbSession,
    match_id: int,
    context: ContextField = "",
    next: NextField = "/",
) -> Response:
    return await _update_match(
        request, db, match_id, {"dismissed": True, "seen": True}, context, next
    )


@router.post("/matches/{match_id}/restore", response_class=HTMLResponse)
async def restore(
    request: Request,
    db: DbSession,
    match_id: int,
    context: ContextField = "",
    next: NextField = "/",
) -> Response:
    return await _update_match(request, db, match_id, {"dismissed": False}, context, next)


@router.post("/matches/seen-all")
async def mark_all_seen(db: DbSession) -> Response:
    await db.execute(
        update(Match).where(Match.seen.is_(False), Match.dismissed.is_(False)).values(seen=True)
    )
    await db.commit()
    return RedirectResponse("/", 303)
