"""Last resort: links that look like individual job postings."""

import re
from urllib.parse import parse_qs, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from app.scraping.adapters._util import document_base
from app.scraping.base import AdapterConfig, RawPosting, clean_text
from app.scraping.urls import canonical_url

NAME = "heuristic"
# A path segment that says "jobs live below here".
JOB_WORD = re.compile(
    r"^(jobs?|careers?|positions?|openings?|vacanc(y|ies)|requisitions?|postings?|roles?|"
    r"opportunit(y|ies)|job-?details?|jobdetail|job-.+)$",
    re.IGNORECASE,
)
# Words specific enough that a long slug after them is a job title even without an ID
# ("/jobs/senior-backend-engineer"). "careers" isn't one: sites put every page under it.
LISTING_WORD = re.compile(
    r"^(jobs?|positions?|openings?|vacanc(y|ies)|requisitions?|postings?)$", re.IGNORECASE
)
# Something in the rest of the path that identifies one posting.
ID_TOKEN = re.compile(
    r"\d{3,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
TITLE_SLUG = re.compile(r"^[^/]*[a-z][^/]*-[^/]+-[^/]+$", re.IGNORECASE)  # three or more words
ID_PARAMS = ("gh_jid", "jobid", "job_id", "jid", "reqid", "job", "req")
GENERIC_TEXT = frozenset(
    {
        "jobs", "careers", "career", "all jobs", "view all jobs", "see all jobs", "view jobs",
        "open positions", "open roles", "openings", "apply", "apply now", "learn more",
        "read more", "more", "search jobs", "job search", "careers home", "explore jobs",
        "back", "next", "previous", "home", "join us", "work with us", "see open roles",
        "view openings", "view all openings", "browse jobs", "job alerts", "find jobs",
        "search all jobs", "find a job", "current openings", "job openings",
    }
)  # fmt: skip
# Link texts that lead to a site's list of jobs, best first. Used to suggest a better address.
JOB_LIST_TEXT = (
    "view all jobs", "see all jobs", "search all jobs", "all jobs", "search jobs", "find jobs",
    "browse jobs", "explore jobs", "view jobs", "job search", "find a job", "jobs",
    "open positions", "open roles", "see open roles", "current openings", "job openings",
    "view openings", "view all openings", "openings",
)  # fmt: skip
SOCIAL_HOSTS = (
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "tiktok.com", "glassdoor.com",
)  # fmt: skip
# Site menus and footers link to "Careers", "How we hire", policies and so on, never to postings.
CHROME_TAGS = frozenset({"nav", "header", "footer"})
# Icon fonts write the icon's name as text ("work_outline"); screen readers are told to skip it.
HIDDEN = (
    '[aria-hidden="true"], .material-icons, .material-icons-outlined, .material-symbols-outlined'
)
TRIM = " .:>\u203a\u00bb\u2192"
# "Learn more about Software Engineer" -> "Software Engineer"
LABEL_PREFIX = re.compile(
    r"^(learn more|read more|more|apply now|apply|view|see|details)( about| for| on)?\s+",
    re.IGNORECASE,
)
MIN_TITLE = 4
MAX_TITLE = 200


def _external_id(url: str) -> str | None:
    return next(iter(_id_param_values(urlsplit(url).query)), None)


def _is_social(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower()
    return any(hostname == h or hostname.endswith("." + h) for h in SOCIAL_HOSTS)


def looks_like_posting(url: str) -> bool:
    """True if the URL looks like one job: a job word in the path followed by an ID or a title."""
    parts = urlsplit(url)
    if _id_param_values(parts.query):
        return True
    segments = [s for s in parts.path.split("/") if s]
    for i, segment in enumerate(segments):
        if not JOB_WORD.match(segment):
            continue
        rest = segments[i + 1 :]
        if segment.lower().startswith("job-") and ID_TOKEN.search(segment):
            return True  # "/job-12345-nurse"
        if any(s.isdigit() or ID_TOKEN.search(s) for s in rest):
            return True
        if LISTING_WORD.match(segment) and rest and TITLE_SLUG.match(rest[-1]):
            return True
    return False


def _id_param_values(query: str) -> list[str]:
    """Values of ID-style query parameters that hold an ID (not "?job=engineering")."""
    parsed = parse_qs(query)
    return [v for key in ID_PARAMS for v in parsed.get(key, []) if ID_TOKEN.fullmatch(v)]


def _same_site(url: str, base_url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    base = (urlsplit(base_url).hostname or "").lower().removeprefix("www.")
    return host == base or host.endswith("." + base)


def _in_site_chrome(node: LexborNode) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.tag in CHROME_TAGS:
            return True
        parent = parent.parent
    return False


def _link_text(anchor: LexborNode) -> str | None:
    text = clean_text(anchor.text(deep=True, separator=" "))
    if text is None:
        return None
    words = text.split(" ")
    half = len(words) // 2
    if len(words) % 2 == 0 and words[:half] == words[half:]:
        text = " ".join(words[:half])  # the same label twice, e.g. for mobile and desktop
    return text


def _key(text: str) -> str:
    """Link text as compared with GENERIC_TEXT: lower case, without trailing arrows."""
    return text.lower().strip(TRIM)


def _usable(title: str | None) -> str | None:
    if not title or not (MIN_TITLE <= len(title) <= MAX_TITLE) or _key(title) in GENERIC_TEXT:
        return None
    return title


def _posting_title(anchor: LexborNode) -> str | None:
    """The link text or, for "Learn more" style links, the aria-label or the card's heading."""
    if title := _usable(_link_text(anchor)):
        return title
    label = clean_text(anchor.attributes.get("aria-label"))
    if label and (title := _usable(LABEL_PREFIX.sub("", label))):
        return title
    card = anchor.parent
    for _ in range(6):  # the nearest enclosing element with exactly one heading is the card
        if card is None or card.tag in ("body", "html", "main", "ul", "ol", "table"):
            break
        headings = card.css("h1, h2, h3, h4, h5, h6")
        if len(headings) == 1:
            return _usable(clean_text(headings[0].text(deep=True, separator=" ")))
        if headings:
            break
        card = card.parent
    return None


def _parse(html: str) -> LexborHTMLParser:
    tree = LexborHTMLParser(html)
    for node in tree.css(HIDDEN):
        node.decompose()
    return tree


def find_job_links(html: str, base_url: str) -> list[RawPosting]:
    page = canonical_url(base_url)
    seen: set[str] = set()
    postings: list[RawPosting] = []
    tree = _parse(html)
    base_url = document_base(tree, base_url)
    for anchor in tree.css("a[href]"):
        href = (anchor.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href)
        if not url.startswith(("http://", "https://")) or _is_social(url):
            continue
        canonical = canonical_url(url)
        if canonical == page or canonical in seen or not looks_like_posting(url):
            continue
        if _in_site_chrome(anchor):
            continue
        title = _posting_title(anchor)
        if not title:
            continue
        seen.add(canonical)
        postings.append(RawPosting(title=title, url=url, external_id=_external_id(url)))
    return postings


def job_list_link(html: str, base_url: str) -> str | None:
    """The page's link to its list of jobs ("View all jobs"), to suggest when it lists none."""
    page = canonical_url(base_url)
    found: dict[str, str] = {}
    tree = _parse(html)
    link_base = document_base(tree, base_url)
    for anchor in tree.css("a[href]"):
        text = _key(_link_text(anchor) or "")
        href = (anchor.attributes.get("href") or "").strip()
        if text not in JOB_LIST_TEXT or text in found or not href or href.startswith("#"):
            continue
        url = urljoin(link_base, href)
        if not url.startswith(("http://", "https://")) or not _same_site(url, base_url):
            continue
        if canonical_url(url) == page:
            continue
        found[text] = url
    return next((found[text] for text in JOB_LIST_TEXT if text in found), None)


class HeuristicAdapter:
    name = NAME

    def detect(self, url: str, html: str | None) -> AdapterConfig | None:
        return AdapterConfig(NAME) if html and find_job_links(html, url) else None

    def parse(self, cfg: AdapterConfig, html: str, base_url: str) -> list[RawPosting]:
        return find_job_links(html, base_url)
