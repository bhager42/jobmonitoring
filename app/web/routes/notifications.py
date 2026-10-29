"""Notification targets: add, test, pause, remove. URLs are stored encrypted, shown masked."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models import NotificationTarget
from app.security.secrets import SecretUnavailableError
from app.services.notify import Notifier
from app.web.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/settings/notifications")

DbSession = Annotated[AsyncSession, Depends(get_session)]
MAX_NAME = 100
MAX_URL = 2000
BACK = "/settings#notifications-title"


def notifier_of(request: Request) -> Notifier:
    return request.app.state.notifier


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


async def get_target(db: AsyncSession, target_id: int) -> NotificationTarget:
    target = await db.get(NotificationTarget, target_id)
    if target is None:
        raise HTTPException(404, "No such notification target.")
    return target


@router.post("", response_class=HTMLResponse)
async def add_target(
    request: Request,
    db: DbSession,
    name: Annotated[str, Form(max_length=MAX_NAME * 2)] = "",
    url: Annotated[str, Form(max_length=MAX_URL * 2)] = "",
) -> Response:
    from app.web.routes.pages import render_settings  # avoid an import cycle

    name, url = name.strip(), url.strip()
    errors: dict[str, str] = {}
    if not name:
        errors["notify_name"] = "Give it a name, e.g. Phone or Email."
    elif len(name) > MAX_NAME:
        errors["notify_name"] = f"Use at most {MAX_NAME} characters."
    if not url:
        errors["notify_url"] = "Enter a notification URL."
    elif len(url) > MAX_URL or not notifier_of(request).sender.validate(url):
        errors["notify_url"] = (
            "That isn't a notification URL Apprise understands. "
            "Check the format for your service in the Apprise documentation."
        )
    if errors:
        return await render_settings(
            request, notify_errors=errors, notify_values={"name": name, "url": url}, status=422
        )
    db.add(
        NotificationTarget(
            name=name, apprise_url_encrypted=notifier_of(request).secrets.encrypt(url)
        )
    )
    await db.commit()
    log.info("Added notification target %r", name)
    return RedirectResponse("/settings?notify=added#notifications-title", 303)


@router.post("/{target_id}/test", response_class=HTMLResponse)
async def test_target(request: Request, db: DbSession, target_id: int) -> Response:
    target = await get_target(db, target_id)
    notifier = notifier_of(request)
    try:
        delivery = await notifier.send_test(notifier.secrets.decrypt(target.apprise_url_encrypted))
        ok = delivery.ok
        message = "Test sent. Check that it arrived." if ok else delivery.error
    except SecretUnavailableError:
        ok = False
        message = (
            "This target was saved by another install and can't be read. Remove and re-add it."
        )
    if not is_htmx(request):
        return RedirectResponse(
            f"/settings?notify={'test-ok' if ok else 'test-failed'}#notifications-title", 303
        )
    return templates.TemplateResponse(
        request,
        "partials/notify_test_result.html",
        {"target": target, "ok": ok, "message": message},
    )


@router.post("/{target_id}/toggle")
async def toggle_target(db: DbSession, target_id: int) -> Response:
    target = await get_target(db, target_id)
    target.enabled = not target.enabled
    await db.commit()
    return RedirectResponse(BACK, 303)


@router.post("/{target_id}/delete")
async def delete_target(db: DbSession, target_id: int) -> Response:
    target = await get_target(db, target_id)
    await db.delete(target)
    await db.commit()
    return RedirectResponse("/settings?notify=removed#notifications-title", 303)
