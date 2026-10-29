"""schema.org JobPosting objects embedded as JSON-LD."""

import json
from collections.abc import Iterator
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from app.scraping.adapters._util import (
    JSON,
    as_dict,
    as_list,
    dicts,
    get_str,
    html_to_text,
    parse_datetime,
)
from app.scraping.base import AdapterConfig, RawPosting, looks_remote

NAME = "jsonld"
CONTAINER_KEYS = ("@graph", "itemListElement", "item", "mainEntity", "hasPart")
MAX_DEPTH = 8


def _is_job_posting(node: JSON) -> bool:
    return "JobPosting" in [t for t in as_list(node.get("@type")) if isinstance(t, str)]


def _walk(node: Any, depth: int = 0) -> Iterator[JSON]:
    if depth > MAX_DEPTH:
        return
    for item in as_list(node):
        obj = as_dict(item)
        if obj is None:
            continue
        if _is_job_posting(obj):
            yield obj
            continue
        for key in CONTAINER_KEYS:
            if key in obj:
                yield from _walk(obj[key], depth + 1)


def job_postings(html: str) -> list[JSON]:
    found: list[JSON] = []
    for script in LexborHTMLParser(html).css('script[type="application/ld+json"]'):
        raw = script.text(deep=True)
        try:
            data = json.loads(raw, strict=False)
        except ValueError:
            continue
        found.extend(_walk(data))
    return found


def _location(posting: JSON) -> str | None:
    places: list[str] = []
    for place in as_list(posting.get("jobLocation")):
        if isinstance(place, str):
            places.append(place)
            continue
        place_obj = as_dict(place) or {}
        raw_address = place_obj.get("address")
        if isinstance(raw_address, str) and raw_address.strip():
            places.append(raw_address.strip())
            continue
        address = as_dict(raw_address)
        if address is None:
            if name := get_str(place_obj, "name"):
                places.append(name)
            continue
        parts = [
            get_str(address, "addressLocality"),
            get_str(address, "addressRegion"),
            get_str(address, "addressCountry") or get_str(address, "addressCountry", "name"),
        ]
        text = ", ".join(p for p in parts if p)
        if text:
            places.append(text)
    return "; ".join(dict.fromkeys(places)) or None


def _identifier(posting: JSON) -> str | None:
    value = posting.get("identifier")
    if isinstance(value, str | int) and not isinstance(value, bool):
        return str(value)
    for ident in dicts(value):
        found = ident.get("value")
        if isinstance(found, str | int) and not isinstance(found, bool):
            return str(found)
    return None


class JsonLdAdapter:
    name = NAME

    def detect(self, url: str, html: str | None) -> AdapterConfig | None:
        return AdapterConfig(NAME) if html and job_postings(html) else None

    def parse(self, cfg: AdapterConfig, html: str, base_url: str) -> list[RawPosting]:
        postings: list[RawPosting] = []
        objects = job_postings(html)
        for obj in objects:
            title = get_str(obj, "title") or get_str(obj, "name")
            url = get_str(obj, "url") or get_str(obj, "sameAs")
            if not url and len(objects) == 1:
                url = base_url  # a single posting page describes itself
            if not title or not url:
                continue
            location = _location(obj)
            telecommute = (get_str(obj, "jobLocationType") or "").upper() == "TELECOMMUTE"
            employment = [e for e in as_list(obj.get("employmentType")) if isinstance(e, str)]
            postings.append(
                RawPosting(
                    title=title,
                    url=url,
                    external_id=_identifier(obj),
                    location=location or ("Remote" if telecommute else None),
                    department=get_str(obj, "occupationalCategory")
                    if isinstance(obj.get("occupationalCategory"), str)
                    else None,
                    employment_type=", ".join(e.replace("_", " ").capitalize() for e in employment)
                    or None,
                    remote=True if telecommute else looks_remote(location),
                    posted_at=parse_datetime(obj.get("datePosted")),
                    description_text=html_to_text(obj.get("description")),
                )
            )
        return postings
