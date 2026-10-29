"""Adapter registry, in detection order (most reliable first)."""

from app.scraping.adapters.ashby import AshbyAdapter
from app.scraping.adapters.css_selector import CssSelectorAdapter
from app.scraping.adapters.greenhouse import GreenhouseAdapter
from app.scraping.adapters.heuristic import HeuristicAdapter
from app.scraping.adapters.jsonld import JsonLdAdapter
from app.scraping.adapters.lever import LeverAdapter
from app.scraping.adapters.smartrecruiters import SmartRecruitersAdapter
from app.scraping.adapters.workday import WorkdayAdapter
from app.scraping.base import ApiAdapter, PageAdapter

API_ADAPTERS: tuple[ApiAdapter, ...] = (
    GreenhouseAdapter(),
    LeverAdapter(),
    AshbyAdapter(),
    SmartRecruitersAdapter(),
    WorkdayAdapter(),
)
PAGE_ADAPTERS: tuple[PageAdapter, ...] = (
    JsonLdAdapter(),
    CssSelectorAdapter(),
    HeuristicAdapter(),
)
ADAPTERS: dict[str, ApiAdapter | PageAdapter] = {a.name: a for a in (*API_ADAPTERS, *PAGE_ADAPTERS)}
