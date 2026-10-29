"""Parse and validate the monitor form (create and edit share it)."""

from dataclasses import dataclass, field
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import ValidationError
from starlette.datastructures import FormData

from app.db.models import Monitor
from app.matching.criteria import Criteria

CSS_FIELDS = ("item_selector", "title_selector", "link_selector", "location_selector")
LIST_FIELDS = (
    "keywords_any",
    "keywords_all",
    "keywords_none",
    "locations_any",
    "departments_any",
    "employment_types_any",
)
RENDER_MODES = ("auto", "static", "browser")
MAX_SELECTOR = 500


def _text(form: FormData, key: str) -> str:
    value = form.get(key)
    return value.strip() if isinstance(value, str) else ""


@dataclass
class MonitorForm:
    """Raw form values (for re-display) plus parsed, validated values and per-field errors."""

    name: str = ""
    url: str = ""
    interval_hours: str = "12"
    enabled: bool = True
    render_mode: str = "auto"
    respect_robots: bool = True
    alert_existing: bool = False
    notify: bool = True
    css: dict[str, str] = field(default_factory=dict[str, str])
    wait_selector: str = ""
    criteria_values: dict[str, Any] = field(default_factory=dict[str, Any])
    preview_token: str = ""
    errors: dict[str, str] = field(default_factory=dict[str, str])
    criteria: Criteria | None = None

    # --- construction -------------------------------------------------------------------------

    @classmethod
    def blank(cls, default_interval: int) -> "MonitorForm":
        return cls(interval_hours=str(default_interval), criteria_values={"title_only": True})

    @classmethod
    def from_form(cls, form: FormData) -> "MonitorForm":
        criteria: dict[str, Any] = {key: _text(form, key) for key in LIST_FIELDS}
        criteria["title_only"] = "title_only" in form
        criteria["remote_only"] = "remote_only" in form
        criteria["title_regex"] = _text(form, "title_regex") or None
        criteria["posted_within_days"] = _text(form, "posted_within_days") or None
        return cls(
            name=_text(form, "name"),
            url=_text(form, "url"),
            interval_hours=_text(form, "interval_hours"),
            enabled="enabled" in form,
            render_mode=_text(form, "render_mode") or "auto",
            respect_robots="respect_robots" in form,
            alert_existing="alert_existing" in form,
            notify="notify" in form,
            css={key: v for key in CSS_FIELDS if (v := _text(form, key))},
            wait_selector=_text(form, "wait_selector"),
            criteria_values=criteria,
            preview_token=_text(form, "preview_token"),
        )

    @classmethod
    def from_monitor(cls, monitor: Monitor) -> "MonitorForm":
        try:
            saved = Criteria.from_json(monitor.criteria).to_json()
        except ValidationError:  # show what was saved so the user can fix it
            saved = {**Criteria().to_json(), **(monitor.criteria or {})}
        values = {
            key: ", ".join(map(str, saved[key])) if isinstance(saved[key], list) else saved[key]
            for key in saved
        }
        config = monitor.adapter_config or {}
        return cls(
            name=monitor.name,
            url=monitor.url,
            interval_hours=str(monitor.interval_hours),
            enabled=monitor.enabled,
            render_mode=monitor.render_mode,
            respect_robots=monitor.respect_robots,
            alert_existing=monitor.alert_existing,
            notify=monitor.notify,
            css={k: str(config[k]) for k in CSS_FIELDS if config.get(k)}
            if monitor.adapter == "css_selector"
            else {},
            wait_selector=str(config.get("wait_selector") or ""),
            criteria_values=values,
        )

    # --- validation ---------------------------------------------------------------------------

    def validate_url(self) -> bool:
        parts = urlsplit(self.url)
        if not self.url:
            self.errors["url"] = "Enter the address of the careers page."
        elif parts.scheme not in ("http", "https") or not parts.hostname:
            self.errors["url"] = "Enter a full web address starting with https://"
        elif len(self.url) > 2048:
            self.errors["url"] = "That address is too long."
        if self.render_mode not in RENDER_MODES:
            self.errors["render_mode"] = "Choose one of the listed options."
        for key, value in [*self.css.items(), ("wait_selector", self.wait_selector)]:
            if len(value) > MAX_SELECTOR:
                self.errors[key] = f"Use at most {MAX_SELECTOR} characters."
        return "url" not in self.errors

    def validate_criteria(self) -> Criteria | None:
        try:
            self.criteria = Criteria.model_validate(self.criteria_values)
        except ValidationError as exc:
            for error in exc.errors():
                key = str(error["loc"][0]) if error["loc"] else "criteria"
                self.errors.setdefault(key, str(error["msg"]).removeprefix("Value error, "))
            self.criteria = None
        return self.criteria

    def validate(self) -> bool:
        self.validate_url()
        self.validate_criteria()
        if not self.name and "url" not in self.errors:
            self.name = (urlsplit(self.url).hostname or "").removeprefix("www.")
        if len(self.name) > 200:
            self.errors["name"] = "Use at most 200 characters."
        try:
            hours = int(self.interval_hours)
        except ValueError:
            self.errors["interval_hours"] = "Enter a whole number of hours."
        else:
            if not 1 <= hours <= 720:
                self.errors["interval_hours"] = "Check every 1 to 720 hours (30 days)."
        return not self.errors

    # --- results ------------------------------------------------------------------------------

    @property
    def interval(self) -> int:
        return int(self.interval_hours)

    @property
    def render(self) -> Literal["auto", "static", "browser"]:
        return cast(Literal["auto", "static", "browser"], self.render_mode)

    def adapter_extras(self) -> dict[str, str]:
        return {"wait_selector": self.wait_selector} if self.wait_selector else {}
