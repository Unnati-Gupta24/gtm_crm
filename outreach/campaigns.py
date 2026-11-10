"""Campaign definitions: who to target, what to say, how fast to go.

Field names map onto the MCP server's PeopleSearchFilters exactly. Those models
forbid unknown keys, so anything not listed here would be rejected at call time.
"""

from __future__ import annotations

import string
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_NOTE_CHARS: Final = 200
MAX_DM_CHARS: Final = 8_000
MAX_FACETS: Final = 10

VALID_DEGREES: Final = frozenset({"first", "second", "third_or_more"})


class _SafeDict(dict[str, str]):
    """Leaves unknown placeholders visibly intact rather than raising."""

    def __missing__(self, key: str) -> str:
        return ""


def render(template: str, values: dict[str, str]) -> str:
    """Fill {first_name}-style placeholders, tolerating missing ones."""

    return string.Formatter().vformat(template, (), _SafeDict(values)).strip()


class Campaign(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    enabled: bool = True

    # --- Targeting ---
    query: str | None = Field(default=None, max_length=500)
    title_keywords: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=300)
    company: str | None = Field(default=None, max_length=300)
    school: str | None = Field(default=None, max_length=300)
    actively_hiring: bool = False
    connection_degrees: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    current_companies: list[str] = Field(default_factory=list)
    past_companies: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    schools: list[str] = Field(default_factory=list)

    # --- Messaging ---
    invite_note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS * 2)
    dm_body: str = Field(min_length=1, max_length=MAX_DM_CHARS)

    # --- Per-campaign overrides (fall back to global settings when unset) ---
    max_discover: int | None = Field(default=None, ge=0, le=200)
    max_invites: int | None = Field(default=None, ge=0, le=50)
    max_dms: int | None = Field(default=None, ge=0, le=50)

    @field_validator("connection_degrees")
    @classmethod
    def check_degrees(cls, values: list[str]) -> list[str]:
        invalid = set(values) - VALID_DEGREES
        if invalid:
            raise ValueError(
                f"invalid connection_degrees {sorted(invalid)}; allowed: {sorted(VALID_DEGREES)}"
            )
        return values

    @field_validator("locations", "current_companies", "past_companies", "industries", "schools")
    @classmethod
    def cap_facets(cls, values: list[str]) -> list[str]:
        if len(values) > MAX_FACETS:
            raise ValueError(f"at most {MAX_FACETS} values allowed (LinkedIn's own limit)")
        return values

    @model_validator(mode="after")
    def require_a_search_criterion(self) -> Campaign:
        if not any(
            (
                self.query,
                self.title_keywords,
                self.title,
                self.company,
                self.school,
                self.actively_hiring,
                self.connection_degrees,
                self.locations,
                self.current_companies,
                self.past_companies,
                self.industries,
                self.schools,
            )
        ):
            raise ValueError(
                f"campaign '{self.id}' needs at least one targeting field "
                "(query, title, locations, current_companies, ...)"
            )
        return self

    # ------------------------------------------------------------------

    def people_filters(self) -> dict[str, Any]:
        """Build the PeopleSearchFilters payload, omitting empty values."""

        filters: dict[str, Any] = {}
        if self.connection_degrees:
            filters["connection_degrees"] = self.connection_degrees
        if self.locations:
            filters["location_names"] = self.locations
        if self.current_companies:
            filters["current_company_names"] = self.current_companies
        if self.past_companies:
            filters["past_company_names"] = self.past_companies
        if self.industries:
            filters["industry_names"] = self.industries
        if self.schools:
            filters["school_names"] = self.schools
        if self.title:
            filters["title"] = self.title
        if self.company:
            filters["company"] = self.company
        if self.school:
            filters["school"] = self.school
        if self.actively_hiring:
            filters["actively_hiring"] = True
        return filters

    def note_for(self, values: dict[str, str]) -> str | None:
        """Rendered invite note, hard-truncated to LinkedIn's 200-char limit."""

        if not self.invite_note:
            return None
        text = render(self.invite_note, values)
        if not text:
            return None
        if len(text) > MAX_NOTE_CHARS:
            text = text[: MAX_NOTE_CHARS - 1].rstrip() + "…"
        return text

    def dm_for(self, values: dict[str, str]) -> str:
        text = render(self.dm_body, values)
        return text[:MAX_DM_CHARS]


def load_campaigns(path: Path) -> list[Campaign]:
    if not path.exists():
        raise FileNotFoundError(
            f"No campaign file at {path}. Copy campaigns.example.yaml to {path.name} and edit it."
        )

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("campaigns") if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path} must contain a non-empty 'campaigns:' list")

    campaigns = [Campaign.model_validate(entry) for entry in entries]

    seen: set[str] = set()
    for campaign in campaigns:
        if campaign.id in seen:
            raise ValueError(f"duplicate campaign id '{campaign.id}' in {path}")
        seen.add(campaign.id)
    return campaigns


def template_values(
    *,
    name: str | None,
    headline: str | None,
    company: str | None,
    location: str | None,
) -> dict[str, str]:
    full_name = (name or "").strip()
    first_name = full_name.split(" ")[0] if full_name else "there"
    return {
        "name": full_name,
        "first_name": first_name,
        "headline": (headline or "").strip(),
        "company": (company or "").strip(),
        "location": (location or "").strip(),
    }
