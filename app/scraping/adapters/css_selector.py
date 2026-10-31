"""User-supplied CSS selectors, for pages no other adapter understands."""

from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser, LexborNode, SelectolaxError

from app.scraping.adapters._util import document_base
from app.scraping.base import AdapterConfig, FetchError, RawPosting, clean_text, looks_remote

NAME = "css_selector"
PARAMS = ("item_selector", "title_selector", "link_selector", "location_selector")


def _text(node: LexborNode | None) -> str | None:
    return clean_text(node.text(deep=True, separator=" ")) if node is not None else None


def _first(node: LexborNode, selector: str | None) -> LexborNode | None:
    if not selector:
        return None
    try:
        return node.css_first(selector)
    except SelectolaxError as exc:
        raise FetchError(f"invalid CSS selector {selector!r}: {exc}") from exc


class CssSelectorAdapter:
    name = NAME

    def detect(self, url: str, html: str | None) -> AdapterConfig | None:
        return None  # only used when the user supplies selectors

    def parse(self, cfg: AdapterConfig, html: str, base_url: str) -> list[RawPosting]:
        item_selector = cfg.params.get("item_selector")
        if not item_selector:
            raise FetchError("CSS selector monitors need an item selector")
        tree = LexborHTMLParser(html)
        link_base = document_base(tree, base_url)
        try:
            items = tree.css(item_selector)
        except SelectolaxError as exc:
            raise FetchError(f"invalid CSS selector {item_selector!r}: {exc}") from exc

        postings: list[RawPosting] = []
        for item in items:
            title_node = _first(item, cfg.params.get("title_selector")) or item
            link_node = _first(item, cfg.params.get("link_selector"))
            if link_node is None:
                link_node = item if item.tag == "a" else item.css_first("a[href]")
            href = link_node.attributes.get("href") if link_node is not None else None
            title = _text(title_node)
            if not title or not href:
                continue
            location = _text(_first(item, cfg.params.get("location_selector")))
            postings.append(
                RawPosting(
                    title=title,
                    url=urljoin(link_base, href.strip()),
                    location=location,
                    remote=looks_remote(location),
                )
            )
        return postings
