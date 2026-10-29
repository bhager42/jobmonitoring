"""What a user is looking for. Stored as JSON in `monitors.criteria`."""

from typing import Annotated, Any

import regex
from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_TERMS = 50
MAX_TERM_LENGTH = 100
MAX_REGEX_LENGTH = 500


class Criteria(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    keywords_any: list[str] = Field(default_factory=list[str])
    keywords_all: list[str] = Field(default_factory=list[str])
    keywords_none: list[str] = Field(default_factory=list[str])
    title_only: bool = True
    title_regex: str | None = None
    locations_any: list[str] = Field(default_factory=list[str])
    remote_only: bool = False
    departments_any: list[str] = Field(default_factory=list[str])
    employment_types_any: list[str] = Field(default_factory=list[str])
    posted_within_days: Annotated[int, Field(ge=1, le=365)] | None = None

    @field_validator(
        "keywords_any",
        "keywords_all",
        "keywords_none",
        "locations_any",
        "departments_any",
        "employment_types_any",
        mode="before",
    )
    @classmethod
    def _clean_terms(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = value.split(",")  # convenient for form input: "python, go"
        if not isinstance(value, list):
            raise ValueError("must be a list of words or phrases")
        terms: list[str] = []
        for item in value:  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(item, str):
                raise ValueError("each entry must be text")
            term = " ".join(item.split())
            if not term:
                continue
            if len(term) > MAX_TERM_LENGTH:
                raise ValueError(f"'{term[:20]}…' is longer than {MAX_TERM_LENGTH} characters")
            if term.casefold() not in (t.casefold() for t in terms):
                terms.append(term)
        if len(terms) > MAX_TERMS:
            raise ValueError(f"use at most {MAX_TERMS} entries")
        return terms

    @field_validator("title_regex", mode="before")
    @classmethod
    def _check_regex(cls, value: Any) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str):
            raise ValueError("must be text")
        if len(value) > MAX_REGEX_LENGTH:
            raise ValueError(f"use at most {MAX_REGEX_LENGTH} characters")
        try:
            regex.compile(value)
        except regex.error as exc:
            raise ValueError(f"not a valid regular expression: {exc}") from exc
        return value

    def is_empty(self) -> bool:
        return not any(
            (
                self.keywords_any,
                self.keywords_all,
                self.keywords_none,
                self.title_regex,
                self.locations_any,
                self.remote_only,
                self.departments_any,
                self.employment_types_any,
                self.posted_within_days,
            )
        )

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> "Criteria":
        return cls.model_validate(data or {})

    def to_json(self) -> dict[str, Any]:
        return self.model_dump()
