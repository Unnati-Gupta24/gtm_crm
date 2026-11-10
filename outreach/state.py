"""Crash-safe lead state and exactly-once action records.

SQLite is the source of truth. Excel is a rendering of it. If a run dies
between "LinkedIn accepted the action" and "the spreadsheet was written", the
action record is already committed here, so the next run will not repeat it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class Status(StrEnum):
    DISCOVERED = "DISCOVERED"
    INVITED = "INVITED"
    CONNECTED = "CONNECTED"
    CONTACTED = "CONTACTED"
    REPLIED = "REPLIED"
    # Terminal: LinkedIn offers no way to invite this person (Follow-only
    # profile, Connect hidden, or out of network). Never retried, never
    # consumes invite budget again.
    SKIPPED = "SKIPPED"


class ActionType(StrEnum):
    INVITE = "invite"
    DM = "dm"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    profile_slug        TEXT NOT NULL,
    campaign_id         TEXT NOT NULL,
    status              TEXT NOT NULL,
    name                TEXT,
    headline            TEXT,
    location            TEXT,
    profile_url         TEXT,
    connection_degree   TEXT,
    company             TEXT,
    conversation_id     TEXT,
    baseline_refs       TEXT,
    discovered_at       TEXT,
    last_seen_at        TEXT,
    invited_at          TEXT,
    connected_at        TEXT,
    contacted_at        TEXT,
    replied_at          TEXT,
    reply_text          TEXT,
    last_error          TEXT,
    PRIMARY KEY (profile_slug, campaign_id)
);

-- One row per search execution, so you can see when each query last ran and
-- what it yielded without inferring it from lead timestamps.
CREATE TABLE IF NOT EXISTS searches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    searched_at  TEXT NOT NULL,
    campaign_id  TEXT NOT NULL,
    query        TEXT,
    filters      TEXT,
    matched      INTEGER NOT NULL DEFAULT 0,
    new_leads    INTEGER NOT NULL DEFAULT 0,
    already_known INTEGER NOT NULL DEFAULT 0,
    hidden       INTEGER NOT NULL DEFAULT 0,
    pages        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads (status, campaign_id);

-- The exactly-once guard. UNIQUE key means a replayed run cannot double-send.
CREATE TABLE IF NOT EXISTS actions (
    idempotency_key TEXT PRIMARY KEY,
    profile_slug    TEXT NOT NULL,
    campaign_id     TEXT NOT NULL,
    action_type     TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    performed       INTEGER,
    detail          TEXT,
    created_at      TEXT NOT NULL,
    created_day     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_actions_day ON actions (created_day, action_type, outcome);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    job          TEXT NOT NULL,
    campaign_id  TEXT,
    profile_slug TEXT,
    event        TEXT NOT NULL,
    detail       TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    mode        TEXT NOT NULL,
    summary     TEXT
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


class Store:
    """All persistence for the orchestrator."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        CREATE TABLE IF NOT EXISTS silently does nothing on an existing table,
        so new columns have to be added explicitly or older databases break.
        """

        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(leads)")}
        for column in ("last_seen_at",):
            if column not in existing:
                self._conn.execute(f"ALTER TABLE leads ADD COLUMN {column} TEXT")

    def is_fresh(self) -> bool:
        """True when this database has never held a lead.

        Used to decide whether a brand-new workbook should be started.
        """

        row = self._conn.execute("SELECT 1 FROM leads LIMIT 1").fetchone()
        return row is None

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------
    # Leads
    # ------------------------------------------------------------------

    def upsert_discovered(
        self,
        *,
        profile_slug: str,
        campaign_id: str,
        name: str | None,
        headline: str | None,
        location: str | None,
        profile_url: str | None,
        connection_degree: str | None,
    ) -> bool:
        """Insert a newly found lead. Returns True only if it was new."""

        with self._tx() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO leads (
                    profile_slug, campaign_id, status, name, headline, location,
                    profile_url, connection_degree, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_slug,
                    campaign_id,
                    Status.DISCOVERED,
                    name,
                    headline,
                    location,
                    profile_url,
                    connection_degree,
                    _now(),
                ),
            )
            return cursor.rowcount > 0

    def mark_seen(self, profile_slug: str, campaign_id: str) -> None:
        """Record that a search matched this person again, without changing state."""

        with self._tx() as conn:
            conn.execute(
                "UPDATE leads SET last_seen_at = ? WHERE profile_slug = ? AND campaign_id = ?",
                (_now(), profile_slug, campaign_id),
            )

    def record_search(
        self,
        *,
        campaign_id: str,
        query: str | None,
        filters: str,
        matched: int,
        new_leads: int,
        already_known: int,
        hidden: int,
        pages: int,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO searches (
                    searched_at, campaign_id, query, filters,
                    matched, new_leads, already_known, hidden, pages
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now(),
                    campaign_id,
                    query,
                    filters[:500],
                    matched,
                    new_leads,
                    already_known,
                    hidden,
                    pages,
                ),
            )

    def searches(self, limit: int = 5000) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM searches ORDER BY id ASC LIMIT ?", (limit,)))

    def update_lead(self, profile_slug: str, campaign_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._tx() as conn:
            conn.execute(
                f"UPDATE leads SET {assignments} WHERE profile_slug = ? AND campaign_id = ?",
                (*fields.values(), profile_slug, campaign_id),
            )

    def set_status(
        self,
        profile_slug: str,
        campaign_id: str,
        status: Status,
        **fields: Any,
    ) -> None:
        self.update_lead(profile_slug, campaign_id, status=str(status), **fields)

    def leads_by_status(
        self,
        status: Status,
        *,
        campaign_id: str | None = None,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM leads WHERE status = ?"
        params: list[Any] = [str(status)]
        if campaign_id:
            sql += " AND campaign_id = ?"
            params.append(campaign_id)
        sql += " ORDER BY discovered_at ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return list(self._conn.execute(sql, params))

    def all_leads(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute("SELECT * FROM leads ORDER BY campaign_id ASC, discovered_at ASC")
        )

    def known_slugs(self, campaign_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT profile_slug FROM leads WHERE campaign_id = ?", (campaign_id,)
        )
        return {row["profile_slug"] for row in rows}

    def set_baseline_refs(self, profile_slug: str, campaign_id: str, refs: Sequence[str]) -> None:
        self.update_lead(profile_slug, campaign_id, baseline_refs=json.dumps(list(refs)))

    @staticmethod
    def baseline_refs(row: sqlite3.Row) -> set[str]:
        raw = row["baseline_refs"]
        if not raw:
            return set()
        try:
            return set(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            return set()

    # ------------------------------------------------------------------
    # Exactly-once action records
    # ------------------------------------------------------------------

    @staticmethod
    def idempotency_key(action: ActionType, profile_slug: str, campaign_id: str) -> str:
        """Stable across runs, unique per (action, lead, campaign).

        Must satisfy the MCP Identifier pattern: ^[A-Za-z0-9][A-Za-z0-9._:-]*$
        """

        safe_slug = "".join(c if (c.isalnum() or c in "._-") else "-" for c in profile_slug)
        safe_campaign = "".join(c if (c.isalnum() or c in "._-") else "-" for c in campaign_id)
        return f"{action}:{safe_campaign}:{safe_slug}"[:200]

    def already_done(self, idempotency_key: str) -> bool:
        """True if this exact action already reached a verified outcome."""

        row = self._conn.execute(
            "SELECT 1 FROM actions WHERE idempotency_key = ? AND outcome = 'verified'",
            (idempotency_key,),
        ).fetchone()
        return row is not None

    def attempted(self, idempotency_key: str) -> bool:
        """True if we ever attempted this action, whatever the outcome."""

        row = self._conn.execute(
            "SELECT 1 FROM actions WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return row is not None

    def blocked(self, idempotency_key: str) -> bool:
        """True if this action must not be attempted again.

        Only two outcomes block:

        * `verified`  - it definitely happened; repeating it would duplicate.
        * `uncertain` - LinkedIn may or may not have acted and the result page
          could not be read. Retrying risks a duplicate, so a human decides.

        A `failed` outcome means the action provably did not happen (the tool
        raised before anything was sent), so it is safe to try again on a later
        run. Transient page states and momentarily missing controls land here.
        """

        row = self._conn.execute(
            """
            SELECT 1 FROM actions
            WHERE idempotency_key = ?
              AND outcome IN ('verified', 'uncertain', 'skipped')
            """,
            (idempotency_key,),
        ).fetchone()
        return row is not None

    def record_action(
        self,
        *,
        idempotency_key: str,
        profile_slug: str,
        campaign_id: str,
        action_type: ActionType,
        outcome: str,
        performed: bool | None,
        detail: str,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO actions (
                    idempotency_key, profile_slug, campaign_id, action_type,
                    outcome, performed, detail, created_at, created_day
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    profile_slug,
                    campaign_id,
                    str(action_type),
                    outcome,
                    None if performed is None else int(performed),
                    detail[:1000],
                    _now(),
                    _today(),
                ),
            )

    def actions_today(self, action_type: ActionType) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS n FROM actions
            WHERE created_day = ? AND action_type = ? AND outcome IN ('verified', 'uncertain')
            """,
            (_today(), str(action_type)),
        ).fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------

    def record_event(
        self,
        *,
        job: str,
        event: str,
        campaign_id: str | None = None,
        profile_slug: str | None = None,
        detail: str = "",
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO events (at, job, campaign_id, profile_slug, event, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (_now(), job, campaign_id, profile_slug, event, detail[:1000]),
            )

    def recent_events(self, limit: int = 2000) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)))

    def events_ascending(self, limit: int = 20_000) -> list[sqlite3.Row]:
        """Oldest first, which is the order an append-only sheet needs."""

        return list(self._conn.execute("SELECT * FROM events ORDER BY id ASC LIMIT ?", (limit,)))

    def start_run(self, mode: str) -> int:
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO runs (started_at, mode) VALUES (?, ?)", (_now(), mode)
            )
            return int(cursor.lastrowid or 0)

    def finish_run(self, run_id: int, summary: dict[str, Any]) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE runs SET finished_at = ?, summary = ? WHERE id = ?",
                (_now(), json.dumps(summary), run_id),
            )

    def recent_runs(self, limit: int = 200) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)))
