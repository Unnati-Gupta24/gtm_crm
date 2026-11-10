"""Runtime configuration.

Everything is read from `.env` (or real environment variables, which win).
That single file also configures the MCP server, because the two use different
prefixes: LINKEDIN_MCP_* and OUTREACH_*.

Nothing here is secret: the LinkedIn session lives in the MCP server's own
Chromium profile, so this module never handles passwords, cookies, or tokens.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILENAME: Final = ".env"
EXAMPLE_FILENAME: Final = ".env.example"


def project_root() -> Path:
    """Directory the operator runs from; all relative paths hang off this."""

    return Path.cwd()


def env_file_path() -> Path:
    return project_root() / ENV_FILENAME


def _split_list(value: str) -> list[str]:
    """Parse a comma-separated env value into a clean list."""

    return [item.strip() for item in value.split(",") if item.strip()]


def _unescape(value: str) -> str:
    r"""Turn literal \n and \t in an env value into real whitespace.

    .env files are single-line, so multi-line message bodies are written with
    escape sequences.
    """

    return value.replace("\\n", "\n").replace("\\t", "\t").strip()


class OutreachSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OUTREACH_",
        env_file=ENV_FILENAME,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Where state lives -------------------------------------------------
    excel_path: Path = Path("outreach_log.xlsx")
    db_path: Path = Path("outreach_state.db")

    # --- Campaign definition (single-campaign mode) ------------------------
    campaign_id: str = Field(
        default="default",
        min_length=1,
        max_length=60,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    search_query: str = ""
    search_title: str = ""
    search_title_keywords: str = ""
    search_company: str = ""
    search_school: str = ""
    search_actively_hiring: bool = False
    connection_degrees: str = "second,third_or_more"
    search_locations: str = ""
    search_industries: str = ""
    search_current_companies: str = ""
    search_past_companies: str = ""
    search_schools: str = ""
    invite_note: str = ""
    dm_body: str = ""

    # --- Multi-campaign mode ----------------------------------------------
    campaigns_file: str = Field(
        default="",
        description="Path to a campaigns YAML file. When set and present, it replaces "
        "the single campaign defined above.",
    )

    # --- Per-run caps ------------------------------------------------------
    max_discover_per_run: int = Field(default=25, ge=0, le=200)
    max_invites_per_run: int = Field(default=5, ge=0, le=50)
    max_dms_per_run: int = Field(default=5, ge=0, le=50)

    # --- Daily caps across all runs ---------------------------------------
    daily_invite_cap: int = Field(default=20, ge=0, le=200)
    daily_dm_cap: int = Field(default=20, ge=0, le=200)

    # --- Polling breadth ---------------------------------------------------
    accept_scan_pages: int = Field(default=3, ge=1, le=20)
    accept_verify_per_run: int = Field(default=5, ge=0, le=50)
    max_reply_checks_per_run: int = Field(default=25, ge=0, le=200)

    # --- Throttling between account-changing actions -----------------------
    # These are the main lever on account safety. LinkedIn tolerates a human
    # pace; a burst of writes seconds apart is what looks automated.
    invite_delay_seconds: float = Field(
        default=45.0,
        ge=0,
        le=3_600,
        description="Pause after each connection request.",
    )
    dm_delay_seconds: float = Field(
        default=60.0,
        ge=0,
        le=3_600,
        description="Pause after each message.",
    )
    action_jitter_seconds: float = Field(
        default=20.0,
        ge=0,
        le=600,
        description="Random extra 0-N seconds on each pause, so sends are not evenly spaced.",
    )

    # --- Loop timings (used by start.ps1 and `run-loop`) -------------------
    loop_minutes: int = Field(
        default=30,
        ge=1,
        le=1_440,
        description="Minutes to wait between automatic runs.",
    )
    loop_jitter_minutes: int = Field(
        default=5,
        ge=0,
        le=120,
        description="Random extra delay added to each wait, so runs are not clockwork.",
    )
    max_loops: int = Field(
        default=0,
        ge=0,
        le=10_000,
        description="Stop after this many iterations. 0 means run forever.",
    )

    # --- Behaviour ---------------------------------------------------------
    dry_run: bool = True
    enrich_profiles: bool = False
    mcp_timeout_seconds: float = Field(default=300.0, ge=30, le=3_600)
    browser_headless: bool = True

    @field_validator("excel_path", "db_path")
    @classmethod
    def resolve_against_cwd(cls, value: Path) -> Path:
        return value if value.is_absolute() else (project_root() / value).resolve()

    # ------------------------------------------------------------------

    def mcp_env_overrides(self) -> dict[str, str]:
        """LINKEDIN_MCP_* values this orchestrator dictates to the server."""

        return {
            "LINKEDIN_MCP_BROWSER_HEADLESS": "true" if self.browser_headless else "false",
            "LINKEDIN_MCP_TRANSPORT": "stdio",
        }

    def campaigns_file_path(self) -> Path | None:
        if not self.campaigns_file.strip():
            return None
        path = Path(self.campaigns_file.strip())
        return path if path.is_absolute() else project_root() / path

    def campaign_from_env(self) -> dict[str, Any]:
        """Build a single campaign definition out of the environment."""

        definition: dict[str, Any] = {
            "id": self.campaign_id,
            "enabled": True,
            "dm_body": _unescape(self.dm_body),
            "max_discover": self.max_discover_per_run,
            "max_invites": self.max_invites_per_run,
            "max_dms": self.max_dms_per_run,
        }

        scalars = (
            ("query", self.search_query),
            ("title", self.search_title),
            ("title_keywords", self.search_title_keywords),
            ("company", self.search_company),
            ("school", self.search_school),
            ("invite_note", _unescape(self.invite_note)),
        )
        for key, value in scalars:
            if value.strip():
                definition[key] = value.strip()

        lists = (
            ("connection_degrees", self.connection_degrees),
            ("locations", self.search_locations),
            ("industries", self.search_industries),
            ("current_companies", self.search_current_companies),
            ("past_companies", self.search_past_companies),
            ("schools", self.search_schools),
        )
        for key, value in lists:
            parsed = _split_list(value)
            if parsed:
                definition[key] = parsed

        if self.search_actively_hiring:
            definition["actively_hiring"] = True

        return definition


class ConfigError(RuntimeError):
    """Configuration is missing or unusable."""


def load_settings() -> OutreachSettings:
    """Load settings from .env plus real environment variables."""

    if not env_file_path().exists() and not _any_outreach_env_set():
        raise ConfigError(
            f"No {ENV_FILENAME} found in {project_root()} and no OUTREACH_* variables set.\n"
            f"Copy {EXAMPLE_FILENAME} to {ENV_FILENAME} and edit it:\n"
            f"    copy {EXAMPLE_FILENAME} {ENV_FILENAME}"
        )
    return OutreachSettings()


def _any_outreach_env_set() -> bool:
    return any(name.upper().startswith("OUTREACH_") for name in os.environ)


def create_env_from_example(*, force: bool = False) -> Path:
    """Copy .env.example to .env so the operator has something to edit."""

    target = env_file_path()
    example = project_root() / EXAMPLE_FILENAME

    if target.exists() and not force:
        print(f"{ENV_FILENAME} already exists; leaving it alone.")
        return target
    if not example.exists():
        raise ConfigError(f"{EXAMPLE_FILENAME} is missing from {project_root()}.")

    target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"Created {target}")
    print("Edit it before running live - the search terms and message bodies live there.")
    return target


def describe(settings: OutreachSettings) -> str:
    """A short, non-secret summary for `doctor` and startup banners."""

    campaigns_file = settings.campaigns_file_path()
    mode = f"campaigns file ({campaigns_file})" if campaigns_file else "single campaign from .env"
    return "\n".join(
        (
            f"  mode          : {mode}",
            f"  campaign      : {settings.campaign_id}",
            f"  query         : {settings.search_query or '(none)'}",
            f"  degrees       : {settings.connection_degrees}",
            f"  dry_run       : {settings.dry_run}",
            f"  headless      : {settings.browser_headless}",
            f"  caps/run      : discover={settings.max_discover_per_run} "
            f"invite={settings.max_invites_per_run} dm={settings.max_dms_per_run}",
            f"  caps/day      : invite={settings.daily_invite_cap} dm={settings.daily_dm_cap}",
            f"  throttle      : invite={settings.invite_delay_seconds:.0f}s "
            f"dm={settings.dm_delay_seconds:.0f}s "
            f"(+0-{settings.action_jitter_seconds:.0f}s jitter)",
            f"  loop          : every {settings.loop_minutes}m "
            f"(+0-{settings.loop_jitter_minutes}m jitter)"
            + (f", max {settings.max_loops} runs" if settings.max_loops else ", forever"),
            f"  excel         : {settings.excel_path}",
            f"  database      : {settings.db_path}",
        )
    )
