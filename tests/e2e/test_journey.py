"""The plan's Phase 5 journey in a real browser against the real app and a fake careers site."""

import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.conftest import PASSWORD, AppServer, JobsSite, axe_violations, horizontal_overflow

SCREENSHOTS = os.environ.get("E2E_SCREENSHOTS")  # set to a folder to keep screenshots

pytestmark = pytest.mark.e2e


def audit(page: Page, name: str, problems: list[str]) -> None:
    """Accessibility in light and dark; no sideways scrolling at phone and desktop widths."""
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        for width in (320, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            if (overflow := horizontal_overflow(page)) > 0:
                problems.append(f"{name} {scheme} {width}px: {overflow}px horizontal overflow")
            if SCREENSHOTS:
                Path(SCREENSHOTS).mkdir(parents=True, exist_ok=True)
                page.screenshot(path=f"{SCREENSHOTS}/{name}-{scheme}-{width}.png", full_page=True)
        problems.extend(f"{name} {scheme}: {v}" for v in axe_violations(page))
    page.emulate_media(color_scheme="light")
    page.set_viewport_size({"width": 1280, "height": 900})


def test_setup_monitor_check_and_match(
    app_server: AppServer, jobs_site: JobsSite, browser: Browser
) -> None:
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.set_default_timeout(20_000)
    problems: list[str] = []
    console_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

    # --- first-run setup -------------------------------------------------------------------
    page.goto(app_server.url)
    expect(page).to_have_url(re.compile(r"/setup$"))
    audit(page, "setup", problems)
    page.get_by_label("Setup token").fill(app_server.setup_token())
    page.get_by_label("New password").fill(PASSWORD)
    page.get_by_label("Confirm password").fill(PASSWORD)
    page.get_by_role("button", name="Save and sign in").click()
    expect(page.get_by_role("heading", name="Dashboard", level=1)).to_be_visible()
    expect(page.get_by_role("link", name="Add a monitor")).to_be_visible()

    # --- add a monitor, with detection and a live match preview ------------------------------
    page.get_by_role("link", name="Monitors", exact=True).click()
    page.get_by_role("link", name="Add a monitor").click()
    page.get_by_label("Address of the page that lists the jobs").fill(jobs_site.url)
    page.get_by_role("button", name="Find jobs").click()
    result = page.locator("#detect-result")
    expect(result).to_contain_text("Found 3 postings")
    expect(result).to_contain_text("links that look like job postings")

    page.get_by_label("Any of these words").fill("nurse")
    expect(page.locator("#match-preview")).to_contain_text("1 of the 3 postings match")
    expect(page.locator("#match-preview li.is-match")).to_contain_text("Registered Nurse - ICU")
    expect(page.locator("#match-preview li.is-match")).to_contain_text("title contains 'nurse'")
    audit(page, "monitor-new", problems)

    page.get_by_label("Name", exact=True).fill("Example Hospital")
    page.get_by_role("button", name="Save monitor").click()
    expect(page.get_by_role("heading", name="Example Hospital", level=1)).to_be_visible()

    # The baseline check runs straight away: all three listed, none alerted.
    for _ in range(20):
        if page.locator(".posting-list li").count() == 3:
            break
        page.wait_for_timeout(500)
        page.reload()
    expect(page.locator(".posting-list li")).to_have_count(3)
    expect(page.get_by_text("already there on the first check")).to_be_visible()
    audit(page, "monitor-detail", problems)

    # --- a new posting appears; "Check now" finds it ---------------------------------------
    jobs_site.jobs[104] = "Night Nurse - Emergency"
    page.get_by_role("button", name="Check now").click()
    expect(page.get_by_text("Checking now")).to_be_visible()
    # When the check finishes the status poll reloads the page.
    expect(page.locator(".posting-list li")).to_have_count(4, timeout=30_000)
    new_row = page.locator(".posting-list li", has_text="Night Nurse - Emergency")
    expect(new_row).to_contain_text("In your matches")

    # --- the match is on the dashboard; mark it seen ---------------------------------------
    page.get_by_role("link", name="Dashboard").click()
    card = page.locator(".match-card", has_text="Night Nurse - Emergency")
    expect(card).to_be_visible()
    expect(card).to_contain_text("title contains 'nurse'")
    expect(page.locator("#unseen-count")).to_have_text("1")
    audit(page, "dashboard", problems)
    card.get_by_role("button", name="Mark seen").click()
    expect(card).to_have_count(0)
    expect(page.locator("#unseen-count")).to_have_text("0")

    # --- the other pages -----------------------------------------------------------------------
    page.goto(f"{app_server.url}/matches?state=all")
    expect(page.locator(".match-card")).to_have_count(1)
    audit(page, "matches", problems)
    page.goto(f"{app_server.url}/monitors")
    expect(page.get_by_role("switch", name="Checking Example Hospital")).to_have_attribute(
        "aria-checked", "true"
    )
    audit(page, "monitors", problems)
    page.goto(f"{app_server.url}/settings")
    audit(page, "settings", problems)

    # Sign out; the login page is checked too.
    page.get_by_role("button", name="Sign out").click()
    expect(page).to_have_url(re.compile(r"/login$"))
    audit(page, "login", problems)

    assert not problems, "\n".join(problems)
    assert not console_errors, "\n".join(console_errors)
