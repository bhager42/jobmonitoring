import unicodedata
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from pydantic import ValidationError

from app.matching.criteria import MAX_TERMS, Criteria
from app.matching.matcher import MatchResult, PostingView, contains, matches, normalize

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def check(posting: PostingView, **criteria: object) -> MatchResult:
    return matches(posting, Criteria.model_validate(criteria), now=NOW)


ENGINEER = PostingView(
    title="Senior Backend Engineer (Python/Go)",
    location="Denver, CO",
    department="Platform Engineering",
    employment_type="Full-time",
    description_text="You will build APIs in Python and run Kubernetes.",
)


# --- criteria validation ---------------------------------------------------------------------


def test_criteria_defaults_and_cleanup() -> None:
    c = Criteria.model_validate(
        {
            "keywords_any": ["  python ", "", "PYTHON", "machine   learning"],
            "locations_any": "denver, remote",
        }
    )
    assert c.keywords_any == ["python", "machine learning"]
    assert c.locations_any == ["denver", "remote"]
    assert c.title_only is True
    assert Criteria().is_empty()
    assert not c.is_empty()
    assert Criteria.from_json(c.to_json()) == c


@pytest.mark.parametrize(
    "data",
    [
        {"keywords_any": ["x" * 101]},
        {"keywords_any": [f"term{i}" for i in range(MAX_TERMS + 1)]},
        {"keywords_any": [1, 2]},
        {"title_regex": "([unclosed"},
        {"posted_within_days": 0},
        {"unknown_field": True},
    ],
)
def test_criteria_rejects_bad_input(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Criteria.model_validate(data)


# --- examples ----------------------------------------------------------------------------------


def test_empty_criteria_match_everything() -> None:
    result = check(ENGINEER)
    assert result == MatchResult(True, ["no criteria set: every posting matches"])


def test_keywords_any_is_or() -> None:
    assert check(ENGINEER, keywords_any=["rust", "python"]).reasons == ["title contains 'python'"]
    assert not check(ENGINEER, keywords_any=["rust", "java"]).matched


def test_keywords_all_is_and() -> None:
    assert check(ENGINEER, keywords_all=["senior", "backend"]).matched
    assert not check(ENGINEER, keywords_all=["senior", "frontend"]).matched


def test_keywords_none_excludes() -> None:
    result = check(ENGINEER, keywords_any=["python"], keywords_none=["senior"])
    assert result == MatchResult(False, ["title contains excluded 'senior'"])


def test_title_only_vs_description() -> None:
    assert not check(ENGINEER, keywords_any=["kubernetes"]).matched
    result = check(ENGINEER, keywords_any=["kubernetes"], title_only=False)
    assert result.reasons == ["posting contains 'kubernetes'"]


@pytest.mark.parametrize(
    ("title", "term", "expected"),
    [
        ("JavaScript Developer", "java", False),
        ("Java Developer", "java", True),
        ("Intern - Data", "intern", True),
        ("International Sales", "intern", False),
        ("C++ Engineer", "c++", True),
        ("C Engineer", "c++", False),
        (".NET Developer", ".net", True),
        ("Senior Staff Engineer", "senior staff", True),
        ("Senior  Staff\nEngineer", "senior staff", True),
        ("Staff Senior Engineer", "senior staff", False),
        (
            "\uff30\uff39\uff34\uff28\uff2f\uff2e Developer",
            "python",
            True,
        ),  # full-width letters (NFKC)
        ("Straße Planner", "STRASSE", True),  # casefold
        ("Café Manager", "café", True),
        ("Café Manager", "café", True),  # combining accent (NFKC composes it)
    ],
)
def test_whole_word_matching(title: str, term: str, expected: bool) -> None:
    assert contains(title, term) is expected


def test_title_regex() -> None:
    assert check(ENGINEER, title_regex=r"engineer\b.*\(python").matched
    assert not check(ENGINEER, title_regex=r"^junior").matched


def test_catastrophic_regex_times_out() -> None:
    slow = PostingView(title="a" * 60 + "b")
    result = check(slow, title_regex=r"(a|aa)+$")
    assert result == MatchResult(False, ["title pattern took too long"])


def test_locations() -> None:
    assert check(ENGINEER, locations_any=["denver", "boulder"]).reasons == [
        "location matches 'denver'"
    ]
    assert not check(ENGINEER, locations_any=["boulder"]).matched
    remote = PostingView(title="Analyst", location=None, remote=True)
    assert check(remote, locations_any=["Remote", "denver"]).reasons == [
        "location matches 'Remote'"
    ]
    assert not check(remote, locations_any=["denver"]).matched


def test_remote_only() -> None:
    assert not check(ENGINEER, remote_only=True).matched
    by_flag = PostingView(title="Analyst", remote=True)
    by_text = PostingView(title="Analyst", location="Anywhere in the US")
    assert check(by_flag, remote_only=True).reasons == ["remote"]
    assert check(by_text, remote_only=True).matched


def test_department_and_employment_type() -> None:
    assert check(
        ENGINEER, departments_any=["engineering"], employment_types_any=["full-time"]
    ).matched
    assert not check(ENGINEER, departments_any=["sales"]).matched
    assert not check(ENGINEER, employment_types_any=["contract"]).matched
    assert not check(PostingView(title="x"), departments_any=["sales"]).matched


def test_posted_within_days() -> None:
    fresh = PostingView(title="x", posted_at=NOW - timedelta(days=2))
    stale = PostingView(title="x", posted_at=NOW - timedelta(days=40))
    undated = PostingView(title="x")
    assert check(fresh, posted_within_days=7).reasons == ["posted within 7 days"]
    assert not check(stale, posted_within_days=7).matched
    assert check(undated, posted_within_days=7).matched  # unknown dates pass


def test_all_conditions_combine() -> None:
    result = check(
        ENGINEER,
        keywords_any=["python"],
        keywords_all=["backend"],
        locations_any=["denver"],
        departments_any=["platform"],
    )
    assert result.matched
    assert len(result.reasons) == 4


# --- properties --------------------------------------------------------------------------------

WORD = st.text(alphabet=st.characters(categories=["Ll", "Lu", "Nd"]), min_size=1, max_size=12)
FILLER = st.text(alphabet=st.characters(categories=["Ll", "Lu", "Nd", "Zs", "Po"]), max_size=30)


@given(term=WORD, before=FILLER, after=FILLER)
def test_term_found_between_separators(term: str, before: str, after: str) -> None:
    assume(normalize(term))
    assert contains(f"{before} {term} {after}", term)


@given(term=WORD)
def test_equal_after_normalization_means_match(term: str) -> None:
    # Not simply term.upper(): Unicode case mapping isn't reversible for every letter. Turkish
    # dotless i (U+0131) uppercases to 'I', which casefolds to 'i'. Hypothesis found that one.
    variants = {term.upper(), term.lower(), term.title(), unicodedata.normalize("NFKD", term)}
    for variant in variants:
        if normalize(variant) == normalize(term) and normalize(term):
            assert contains(variant, term)


ASCII_WORD = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=12)


@given(term=ASCII_WORD)
def test_full_width_and_case_insensitive(term: str) -> None:
    full_width_upper = "".join(chr(ord(c.upper()) + 0xFEE0) for c in term)
    assert contains(f"Hiring: {full_width_upper} now", term)
    assert contains(term, full_width_upper)


@given(term=WORD, suffix=WORD)
def test_never_matches_inside_a_longer_word(term: str, suffix: str) -> None:
    # Only for terms that are still letters/digits after NFKC: U+013F becomes 'l' + a middle dot,
    # and a term ending in punctuation has different word boundaries (Hypothesis found that too).
    assume(normalize(term).isalnum() and normalize(suffix).isalnum())
    longer = term + suffix
    # Only a real whole-word occurrence may match: the term must not appear on its own.
    assume(not any(normalize(part) == normalize(term) for part in normalize(longer).split()))
    assert not contains(longer, term)


@given(title=FILLER, term=WORD)
def test_excluded_term_always_wins(title: str, term: str) -> None:
    assume(normalize(term))
    posting = PostingView(title=f"{title} {term}")
    assert not check(posting, keywords_any=[term], keywords_none=[term]).matched


@given(title=FILLER)
def test_empty_criteria_always_match(title: str) -> None:
    assert check(PostingView(title=title)).matched


@given(text=FILLER)
def test_normalize_is_idempotent(text: str) -> None:
    assert normalize(normalize(text)) == normalize(text)
