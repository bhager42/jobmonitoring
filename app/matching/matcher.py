"""Decide whether a posting matches a monitor's criteria. Pure functions, no I/O."""

import logging
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Protocol

import regex

from app.matching.criteria import Criteria

log = logging.getLogger(__name__)

REGEX_TIMEOUT_SECONDS = 0.1
REMOTE_TERMS = frozenset({"remote", "anywhere", "work from home", "wfh", "telecommute"})


@dataclass(frozen=True)
class PostingView:
    """The fields the matcher looks at. Built from a RawPosting or a stored Posting."""

    title: str
    location: str | None = None
    department: str | None = None
    employment_type: str | None = None
    remote: bool | None = None
    description_text: str | None = None
    posted_at: datetime | None = None

    @classmethod
    def of(cls, posting: "PostingLike") -> "PostingView":
        return cls(
            title=posting.title,
            location=posting.location,
            department=posting.department,
            employment_type=posting.employment_type,
            remote=posting.remote,
            description_text=posting.description_text,
            posted_at=posting.posted_at,
        )


class PostingLike(Protocol):
    """Anything shaped like a posting: a scraped RawPosting or a stored Posting row."""

    @property
    def title(self) -> str: ...
    @property
    def location(self) -> str | None: ...
    @property
    def department(self) -> str | None: ...
    @property
    def employment_type(self) -> str | None: ...
    @property
    def remote(self) -> bool | None: ...
    @property
    def description_text(self) -> str | None: ...
    @property
    def posted_at(self) -> datetime | None: ...


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    reasons: list[str] = field(default_factory=list[str])


def normalize(text: str) -> str:
    """NFKC, casefold, collapse whitespace: full-width "PYTHON" and "python" compare equal."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


@lru_cache(maxsize=4096)
def _term_pattern(term: str) -> "regex.Pattern[str]":
    # Lookarounds instead of \b, so terms that start or end with symbols ("c++", ".net") work.
    return regex.compile(r"(?<!\w)" + regex.escape(normalize(term)) + r"(?!\w)")


def contains(text: str | None, term: str) -> bool:
    """Whole-word / whole-phrase, case- and Unicode-insensitive containment."""
    if not text:
        return False
    return _term_pattern(term).search(normalize(text)) is not None


def _first_contained(text: str | None, terms: list[str]) -> str | None:
    return next((t for t in terms if contains(text, t)), None)


def _is_remote(posting: PostingView) -> bool:
    return posting.remote is True or any(contains(posting.location, t) for t in REMOTE_TERMS)


def matches(posting: PostingView, criteria: Criteria, *, now: datetime) -> MatchResult:
    """Every non-empty condition must hold (AND); within an `_any` list one term is enough (OR)."""
    reasons: list[str] = []
    searched = (
        posting.title
        if criteria.title_only
        else f"{posting.title}\n{posting.description_text or ''}"
    )
    where = "title" if criteria.title_only else "posting"

    excluded = _first_contained(searched, criteria.keywords_none)
    if excluded is not None:
        return MatchResult(False, [f"{where} contains excluded '{excluded}'"])

    if criteria.keywords_any:
        term = _first_contained(searched, criteria.keywords_any)
        if term is None:
            return MatchResult(False)
        reasons.append(f"{where} contains '{term}'")

    if criteria.keywords_all:
        missing = [t for t in criteria.keywords_all if not contains(searched, t)]
        if missing:
            return MatchResult(False)
        reasons.append(
            f"{where} contains all of " + ", ".join(f"'{t}'" for t in criteria.keywords_all)
        )

    if criteria.title_regex:
        try:
            found = regex.search(
                criteria.title_regex,
                posting.title,
                flags=regex.IGNORECASE,
                timeout=REGEX_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            log.warning("Title pattern %r timed out; treating as no match", criteria.title_regex)
            return MatchResult(False, ["title pattern took too long"])
        if found is None:
            return MatchResult(False)
        reasons.append(f"title matches pattern '{criteria.title_regex}'")

    if criteria.locations_any:
        term = _first_contained(posting.location, criteria.locations_any)
        if term is None and _is_remote(posting):
            term = next((t for t in criteria.locations_any if normalize(t) in REMOTE_TERMS), None)
        if term is None:
            return MatchResult(False)
        reasons.append(f"location matches '{term}'")

    if criteria.remote_only:
        if not _is_remote(posting):
            return MatchResult(False)
        reasons.append("remote")

    if criteria.departments_any:
        term = _first_contained(posting.department, criteria.departments_any)
        if term is None:
            return MatchResult(False)
        reasons.append(f"department matches '{term}'")

    if criteria.employment_types_any:
        term = _first_contained(posting.employment_type, criteria.employment_types_any)
        if term is None:
            return MatchResult(False)
        reasons.append(f"employment type matches '{term}'")

    if criteria.posted_within_days is not None and posting.posted_at is not None:
        # Postings without a date pass: most sites don't publish one, and the posting is new to us.
        if posting.posted_at < now - timedelta(days=criteria.posted_within_days):
            return MatchResult(False)
        reasons.append(f"posted within {criteria.posted_within_days} days")

    if not reasons:
        reasons.append("no criteria set: every posting matches")
    return MatchResult(True, reasons)
