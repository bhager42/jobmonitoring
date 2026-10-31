"""Monitor pages: list, create/edit with detection and live match preview, detail, actions."""

import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.engine import get_session
from app.db.models import CheckRun, Match, Monitor, Posting, utcnow
from app.matching.criteria import Criteria
from app.matching.matcher import MatchResult, PostingView, matches
from app.scraping.base import AdapterConfig, FetchError, RawPosting
from app.scraping.detect import detect
from app.services.checks import is_backing_off, reapply_criteria, view_of
from app.services.fetching import make_context
from app.services.scheduler import Scheduler
from app.web.forms import MonitorForm
from app.web.preview import Preview, PreviewCache
from app.web.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/monitors")

DbSession = Annotated[AsyncSession, Depends(get_session)]
PREVIEW_LIMIT = 20
POSTINGS_LIMIT = 300
RUNS_LIMIT = 15


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def scheduler_of(request: Request) -> Scheduler:
    return request.app.state.scheduler


def previews_of(request: Request) -> PreviewCache:
    return request.app.state.previews


async def get_monitor(db: AsyncSession, monitor_id: int) -> Monitor:
    monitor = await db.get(Monitor, monitor_id)
    if monitor is None:
        raise HTTPException(404, "No such monitor.")
    return monitor


@dataclass(frozen=True)
class PreviewRow:
    title: str
    url: str
    location: str | None
    result: MatchResult | None


def preview_rows(preview: Preview, criteria: Criteria | None) -> list[PreviewRow]:
    now = utcnow()
    return [
        PreviewRow(
            p.title,
            p.url,
            p.location,
            matches(PostingView.of(p), criteria, now=now) if criteria is not None else None,
        )
        for p in preview.postings[:PREVIEW_LIMIT]
    ]


def form_page(
    request: Request,
    form: MonitorForm,
    *,
    monitor: Monitor | None = None,
    detect_error: str | None = None,
    status_code: int = 200,
) -> Response:
    preview = previews_of(request).get(form.preview_token)
    criteria = form.criteria or form.validate_criteria()
    return templates.TemplateResponse(
        request,
        "pages/monitor_form.html",
        {
            "form": form,
            "monitor": monitor,
            "preview": preview,
            "rows": preview_rows(preview, criteria) if preview else [],
            "detect_error": detect_error,
            "browser_available": request.app.state.browser is not None,
        },
        status_code=status_code,
    )


# --- list ------------------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def list_monitors(request: Request, db: DbSession) -> Response:
    monitors = (await db.scalars(select(Monitor).order_by(Monitor.name, Monitor.id))).all()
    active_rows = await db.execute(
        select(Posting.monitor_id, func.count())
        .where(Posting.is_active.is_(True))
        .group_by(Posting.monitor_id)
    )
    # Not dict(result): SQLAlchemy results have .keys(), so dict() would treat them as mappings.
    active = {monitor_id: count for monitor_id, count in active_rows}
    match_counts = {
        row[0]: (row[1], row[2] or 0)
        for row in (
            await db.execute(
                select(
                    Match.monitor_id,
                    func.count(),
                    func.sum(
                        case((Match.seen.is_(False) & Match.dismissed.is_(False), 1), else_=0)
                    ),
                ).group_by(Match.monitor_id)
            )
        )
    }
    scheduler = scheduler_of(request)
    return templates.TemplateResponse(
        request,
        "pages/monitors.html",
        {
            "monitors": monitors,
            "active": active,
            "match_counts": match_counts,
            "busy": {m.id for m in monitors if scheduler.is_busy(m.id)},
            "backing_off": {m.id for m in monitors if is_backing_off(m)},
        },
    )


# --- create / edit ---------------------------------------------------------------------------


@router.get("/new", response_class=HTMLResponse)
async def new_monitor(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    return form_page(request, MonitorForm.blank(settings.default_interval_hours))


@router.post("/detect", response_class=HTMLResponse)
async def detect_postings(request: Request) -> Response:
    """Find postings at the form's URL. htmx gets the preview fragment; others the whole form."""
    form = MonitorForm.from_form(await request.form())
    form.preview_token = ""
    error: str | None = None
    if form.validate_url():
        ctx = make_context(
            request.app.state.http_client,
            request.app.state.browser,
            respect_robots=form.respect_robots,
            render_mode=form.render_mode,
            wait_selector=form.wait_selector or None,
        )
        try:
            outcome = await detect(form.url, ctx, css=form.css or None)
        except FetchError as exc:
            error = str(exc)
        except Exception:
            log.exception("Detection failed for %s", form.url)
            error = "Something went wrong while reading that page. The log has details."
        else:
            form.preview_token = previews_of(request).put(
                Preview(
                    form.url, dict(form.css), outcome.config, outcome.postings, outcome.rendered
                )
            )
    else:
        error = form.errors["url"]

    if not is_htmx(request):
        return form_page(request, form, detect_error=error, status_code=200 if not error else 422)
    preview = previews_of(request).get(form.preview_token)
    criteria = form.validate_criteria()
    return templates.TemplateResponse(
        request,
        "partials/detect_result.html",
        {
            "form": form,
            "preview": preview,
            "rows": preview_rows(preview, criteria) if preview else [],
            "detect_error": error,
        },
    )


@router.post("/preview", response_class=HTMLResponse)
async def preview_matches(request: Request) -> Response:
    """Live match preview while the criteria are edited."""
    form = MonitorForm.from_form(await request.form())
    preview = previews_of(request).get(form.preview_token)
    criteria = form.validate_criteria()
    return templates.TemplateResponse(
        request,
        "partials/match_preview.html",
        {
            "form": form,
            "preview": preview,
            "rows": preview_rows(preview, criteria) if preview else [],
        },
    )


def _apply_form(
    monitor: Monitor, form: MonitorForm, preview: Preview | None, *, is_new: bool
) -> None:
    """Copy validated form values onto a monitor, resetting detection if the source changed."""
    if form.criteria is None:
        raise ValueError("validate the form before applying it")
    source_changed = (
        is_new or monitor.url != form.url or MonitorForm.from_monitor(monitor).css != form.css
    )
    monitor.name = form.name
    monitor.url = form.url
    monitor.interval_hours = form.interval
    monitor.enabled = form.enabled
    monitor.render_mode = form.render
    monitor.respect_robots = form.respect_robots
    monitor.alert_existing = form.alert_existing
    monitor.notify = form.notify
    monitor.criteria = form.criteria.to_json()

    if source_changed:
        if preview is not None and preview.same_source(form.url, form.css):
            config = preview.config
        elif form.css:
            config = AdapterConfig("css_selector", dict(form.css))
        else:
            config = None  # detected on the first check
        monitor.adapter = config.adapter if config else None
        monitor.adapter_config = {**(config.params if config else {}), **form.adapter_extras()}
        # Postings are fingerprinted per adapter: start a fresh baseline so nothing old alerts.
        monitor.baseline_done = False
        monitor.next_check_at = None
    else:
        params = {k: v for k, v in (monitor.adapter_config or {}).items() if k != "wait_selector"}
        monitor.adapter_config = {**params, **form.adapter_extras()}


@router.post("", response_class=HTMLResponse)
async def create_monitor(request: Request, db: DbSession) -> Response:
    form = MonitorForm.from_form(await request.form())
    if not form.validate():
        return form_page(request, form, status_code=422)
    monitor = Monitor(name=form.name, url=form.url)
    _apply_form(monitor, form, previews_of(request).get(form.preview_token), is_new=True)
    db.add(monitor)
    await db.commit()
    scheduler_of(request).wake()  # due now: next_check_at is None
    log.info("Created monitor %d (%s)", monitor.id, monitor.url)
    return RedirectResponse(f"/monitors/{monitor.id}?created=true", 303)


@router.get("/{monitor_id}/edit", response_class=HTMLResponse)
async def edit_monitor(request: Request, db: DbSession, monitor_id: int) -> Response:
    monitor = await get_monitor(db, monitor_id)
    form = MonitorForm.from_monitor(monitor)
    form.preview_token = await _stored_preview(request, db, monitor, form) or ""
    return form_page(request, form, monitor=monitor)


async def _stored_preview(
    request: Request, db: AsyncSession, monitor: Monitor, form: MonitorForm
) -> str | None:
    """Preview the criteria against the postings the last check found, so edits show live."""
    if monitor.adapter is None:
        return None
    rows = (
        await db.scalars(
            select(Posting)
            .where(Posting.monitor_id == monitor.id, Posting.is_active.is_(True))
            .order_by(Posting.first_seen_at.desc(), Posting.id.desc())
        )
    ).all()
    if not rows:
        return None
    postings = [
        RawPosting(
            title=row.title,
            url=row.url,
            external_id=row.external_id,
            location=row.location,
            department=row.department,
            employment_type=row.employment_type,
            remote=row.remote,
            posted_at=row.posted_at,
            description_text=row.description_text,
        )
        for row in rows
    ]
    config = AdapterConfig(monitor.adapter, dict(monitor.adapter_config or {}))
    preview = Preview(form.url, form.css, config, postings, rendered=False, from_last_check=True)
    return previews_of(request).put(preview)


@router.post("/{monitor_id}", response_class=HTMLResponse)
async def update_monitor(request: Request, db: DbSession, monitor_id: int) -> Response:
    monitor = await get_monitor(db, monitor_id)
    form = MonitorForm.from_form(await request.form())
    if not form.validate():
        return form_page(request, form, monitor=monitor, status_code=422)
    old_interval = monitor.interval_hours
    old_criteria = monitor.criteria
    _apply_form(monitor, form, previews_of(request).get(form.preview_token), is_new=False)
    query = "saved=true"
    # A changed source resets the baseline instead; the next check judges everything afresh.
    if monitor.criteria != old_criteria and monitor.baseline_done:
        changes = await reapply_criteria(db, monitor, utcnow())
        query += f"&added={changes.added}&removed={changes.removed}"
    if (
        monitor.next_check_at is not None
        and monitor.last_check_at is not None
        and monitor.interval_hours != old_interval
    ):
        monitor.next_check_at = monitor.last_check_at + (
            monitor.next_check_at - monitor.last_check_at
        ) * (monitor.interval_hours / old_interval)
    await db.commit()
    scheduler_of(request).wake()
    return RedirectResponse(f"/monitors/{monitor.id}?{query}", 303)


# --- detail ----------------------------------------------------------------------------------


@router.get("/{monitor_id}", response_class=HTMLResponse)
async def monitor_detail(
    request: Request,
    db: DbSession,
    monitor_id: int,
    matching: bool = False,
    created: bool = False,
    saved: bool = False,
    added: int | None = None,
    removed: int | None = None,
) -> Response:
    monitor = await get_monitor(db, monitor_id)
    runs = (
        await db.scalars(
            select(CheckRun)
            .where(CheckRun.monitor_id == monitor_id)
            .order_by(CheckRun.started_at.desc(), CheckRun.id.desc())
            .limit(RUNS_LIMIT)
        )
    ).all()
    postings = (
        await db.scalars(
            select(Posting)
            .where(Posting.monitor_id == monitor_id, Posting.is_active.is_(True))
            .order_by(Posting.first_seen_at.desc(), Posting.title)
            .limit(POSTINGS_LIMIT)
        )
    ).all()
    alerted = set(
        (await db.scalars(select(Match.posting_id).where(Match.monitor_id == monitor_id))).all()
    )
    inactive = (
        await db.execute(
            select(func.count())
            .select_from(Posting)
            .where(Posting.monitor_id == monitor_id, Posting.is_active.is_(False))
        )
    ).scalar_one()

    try:
        criteria: Criteria | None = Criteria.from_json(monitor.criteria)
    except ValueError:
        criteria = None
    now = utcnow()
    results = {
        p.id: matches(view_of(p), criteria, now=now) if criteria else MatchResult(False)
        for p in postings
    }
    matching_ids = {pid for pid, r in results.items() if r.matched}
    unalerted_matching = len(matching_ids - alerted)
    shown = [p for p in postings if p.id in matching_ids] if matching else list(postings)

    return templates.TemplateResponse(
        request,
        "pages/monitor_detail.html",
        {
            "monitor": monitor,
            "criteria": criteria,
            "runs": runs,
            "postings": shown,
            "total_active": len(postings),
            "inactive": inactive,
            "results": results,
            "alerted": alerted,
            "matching_count": len(matching_ids),
            "unalerted_matching": unalerted_matching,
            "only_matching": matching,
            "busy": scheduler_of(request).is_busy(monitor_id),
            "backing_off": is_backing_off(monitor),
            "created": created,
            "saved": saved,
            "added": max(added or 0, 0),
            "removed": max(removed or 0, 0),
            "reapplied": added is not None or removed is not None,
        },
    )


# --- actions ---------------------------------------------------------------------------------


def status_fragment(request: Request, monitor: Monitor, *, busy: bool) -> Response:
    return templates.TemplateResponse(
        request,
        "partials/monitor_status.html",
        {"monitor": monitor, "busy": busy, "backing_off": is_backing_off(monitor)},
    )


@router.post("/{monitor_id}/check", response_class=HTMLResponse)
async def check_now(request: Request, db: DbSession, monitor_id: int) -> Response:
    monitor = await get_monitor(db, monitor_id)
    scheduler_of(request).request_check(monitor_id)  # False just means one is already running
    if is_htmx(request):
        return status_fragment(request, monitor, busy=True)
    return RedirectResponse(f"/monitors/{monitor_id}", 303)


@router.get("/{monitor_id}/status", response_class=HTMLResponse)
async def check_status(
    request: Request, db: DbSession, monitor_id: int, was_busy: bool = False
) -> Response:
    monitor = await get_monitor(db, monitor_id)
    busy = scheduler_of(request).is_busy(monitor_id)
    response = status_fragment(request, monitor, busy=busy)
    if was_busy and not busy:
        response.headers["HX-Refresh"] = "true"  # reload to show new postings and matches
    return response


@router.post("/{monitor_id}/toggle", response_class=HTMLResponse)
async def toggle_monitor(request: Request, db: DbSession, monitor_id: int) -> Response:
    monitor = await get_monitor(db, monitor_id)
    monitor.enabled = not monitor.enabled
    await db.commit()
    if monitor.enabled:
        scheduler_of(request).wake()
    if is_htmx(request):
        return templates.TemplateResponse(
            request, "partials/monitor_toggle.html", {"monitor": monitor}
        )
    return RedirectResponse("/monitors", 303)


@router.get("/{monitor_id}/delete", response_class=HTMLResponse)
async def confirm_delete(request: Request, db: DbSession, monitor_id: int) -> Response:
    """No-JavaScript fallback for the delete dialog."""
    monitor = await get_monitor(db, monitor_id)
    return templates.TemplateResponse(request, "pages/monitor_delete.html", {"monitor": monitor})


@router.post("/{monitor_id}/delete")
async def delete_monitor(db: DbSession, monitor_id: int) -> Response:
    monitor = await get_monitor(db, monitor_id)
    await db.delete(monitor)
    await db.commit()
    log.info("Deleted monitor %d", monitor_id)
    return RedirectResponse("/monitors?deleted=true", 303)
