"""Excel workbook: an append-only log of contacts and searches.

Copyright 2026 Yash Yadav
Licensed under the Apache License, Version 2.0.

Rows are never deleted and never reordered. A contact is appended once, the
first time a search finds them, and afterwards only its managed cells are
refreshed in place. Anything you add to the sheet yourself -- notes, extra
columns, colour -- survives every subsequent run.

A brand-new workbook is created when the file is missing, or when the database
is fresh (no leads yet) and a stale workbook from a previous account is sitting
there.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from outreach.state import Status, Store

_HEADER_FILL = PatternFill("solid", start_color="1F3864")
_HEADER_FONT = Font(bold=True, color="FFFFFF")

_STATUS_FILL = {
    Status.DISCOVERED: PatternFill("solid", start_color="EDEDED"),
    Status.INVITED: PatternFill("solid", start_color="DDEBF7"),
    Status.CONNECTED: PatternFill("solid", start_color="FFF2CC"),
    Status.CONTACTED: PatternFill("solid", start_color="FCE4D6"),
    Status.REPLIED: PatternFill("solid", start_color="C6EFCE"),
    Status.SKIPPED: PatternFill("solid", start_color="D9D2E9"),
}

# (header, database column, width). Column 1 is the stable key used to match an
# existing row, so it must never move.
_LEAD_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("Profile slug", "profile_slug", 30),
    ("Campaign", "campaign_id", 20),
    ("Status", "status", 12),
    ("Name", "name", 24),
    ("Headline", "headline", 44),
    ("Company", "company", 20),
    ("Location", "location", 20),
    ("Degree", "connection_degree", 10),
    ("Profile URL", "profile_url", 38),
    ("First found (UTC)", "discovered_at", 20),
    ("Last seen in search (UTC)", "last_seen_at", 24),
    ("Invited (UTC)", "invited_at", 20),
    ("Connected (UTC)", "connected_at", 20),
    ("DM sent (UTC)", "contacted_at", 20),
    ("Replied (UTC)", "replied_at", 20),
    ("Reply", "reply_text", 46),
    ("Conversation", "conversation_id", 20),
    ("Last error", "last_error", 34),
)
_KEY_COLUMN = 1
_STATUS_COLUMN = 3
_URL_COLUMN = 9

_SEARCH_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("#", "id", 6),
    ("Searched at (UTC)", "searched_at", 22),
    ("Campaign", "campaign_id", 22),
    ("Query", "query", 46),
    ("Filters", "filters", 40),
    ("Matched", "matched", 10),
    ("New", "new_leads", 8),
    ("Already known", "already_known", 15),
    ("Hidden by LinkedIn", "hidden", 19),
    ("Pages", "pages", 8),
)

_EVENT_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("#", "id", 6),
    ("At (UTC)", "at", 20),
    ("Job", "job", 14),
    ("Campaign", "campaign_id", 20),
    ("Profile", "profile_slug", 26),
    ("Event", "event", 20),
    ("Detail", "detail", 56),
)


def _style_header(sheet: Worksheet, widths: list[int]) -> None:
    for index, width in enumerate(widths, start=1):
        cell = sheet.cell(row=1, column=index)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(vertical="center")
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"


def _lead_key(row: Any) -> str:
    return f"{row['profile_slug']}\x1f{row['campaign_id']}"


# ---------------------------------------------------------------------------
# Leads: append new, refresh existing in place
# ---------------------------------------------------------------------------


def _sync_leads(sheet: Worksheet, leads: list[sqlite3.Row]) -> tuple[int, int]:
    """Append unseen contacts, refresh managed cells on the rest."""

    existing: dict[str, int] = {}
    for excel_row in range(2, sheet.max_row + 1):
        slug = sheet.cell(row=excel_row, column=_KEY_COLUMN).value
        campaign = sheet.cell(row=excel_row, column=2).value
        if slug:
            existing[f"{slug}\x1f{campaign}"] = excel_row

    appended = updated = 0
    for lead in leads:
        key = _lead_key(lead)
        target = existing.get(key)

        if target is None:
            sheet.append([lead[column] for _, column, _ in _LEAD_COLUMNS])
            target = sheet.max_row
            appended += 1
            url = lead["profile_url"]
            if url:
                cell = sheet.cell(row=target, column=_URL_COLUMN)
                cell.hyperlink = url
                cell.font = Font(color="0563C1", underline="single")
        else:
            # Only rewrite cells whose value actually changed, so untouched
            # columns keep any formatting or notes the operator applied.
            for index, (_, column, _) in enumerate(_LEAD_COLUMNS, start=1):
                cell = sheet.cell(row=target, column=index)
                if cell.value != lead[column]:
                    cell.value = lead[column]
                    updated += 1

        fill = _STATUS_FILL.get(lead["status"])
        if fill is not None:
            sheet.cell(row=target, column=_STATUS_COLUMN).fill = fill

    sheet.auto_filter.ref = f"A1:{get_column_letter(len(_LEAD_COLUMNS))}{max(sheet.max_row, 1)}"
    return appended, updated


# ---------------------------------------------------------------------------
# Append-only sheets keyed on a monotonic id
# ---------------------------------------------------------------------------


def _append_by_id(
    sheet: Worksheet,
    rows: list[sqlite3.Row],
    columns: tuple[tuple[str, str, int], ...],
) -> int:
    """Append rows whose id is greater than the highest already present."""

    highest = 0
    for excel_row in range(2, sheet.max_row + 1):
        value = sheet.cell(row=excel_row, column=1).value
        if isinstance(value, int):
            highest = max(highest, value)

    appended = 0
    for row in rows:
        if int(row["id"]) <= highest:
            continue
        sheet.append([row[column] for _, column, _ in columns])
        appended += 1
    return appended


def _rebuild_summary(sheet: Worksheet, leads: list[sqlite3.Row]) -> None:
    """The summary is derived counts only, so it is safe to regenerate."""

    sheet.delete_rows(1, sheet.max_row)
    statuses = [status.value for status in Status]
    sheet.append(["Campaign", *statuses, "Total"])

    per_campaign: dict[str, Counter[str]] = {}
    for lead in leads:
        per_campaign.setdefault(lead["campaign_id"], Counter())[lead["status"]] += 1

    for campaign, counts in sorted(per_campaign.items()):
        sheet.append([campaign, *(counts.get(s, 0) for s in statuses), sum(counts.values())])

    if per_campaign:
        totals: Counter[str] = Counter()
        for counts in per_campaign.values():
            totals.update(counts)
        sheet.append(["ALL", *(totals.get(s, 0) for s in statuses), sum(totals.values())])
        for cell in sheet[sheet.max_row]:
            cell.font = Font(bold=True)

    _style_header(sheet, [22, *([13] * len(statuses)), 10])
    sheet.cell(
        row=sheet.max_row + 2, column=1
    ).value = f"Updated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC"


def _rebuild_runs(sheet: Worksheet, runs: list[sqlite3.Row]) -> None:
    sheet.delete_rows(1, sheet.max_row)
    sheet.append(["Started (UTC)", "Finished (UTC)", "Mode", "Summary"])
    for run in runs:
        summary = run["summary"]
        if summary:
            try:
                parsed = json.loads(summary)
                summary = ", ".join(f"{k}={v}" for k, v in parsed.items())
            except json.JSONDecodeError:
                pass
        sheet.append([run["started_at"], run["finished_at"], run["mode"], summary])
    _style_header(sheet, [20, 20, 12, 78])


# ---------------------------------------------------------------------------
# Workbook assembly
# ---------------------------------------------------------------------------


def _new_workbook() -> Workbook:
    workbook = Workbook()
    summary = workbook.active
    assert summary is not None
    summary.title = "Summary"

    for title, columns in (
        ("Leads", _LEAD_COLUMNS),
        ("Searches", _SEARCH_COLUMNS),
        ("Events", _EVENT_COLUMNS),
    ):
        sheet = workbook.create_sheet(title)
        sheet.append([header for header, _, _ in columns])
        _style_header(sheet, [width for _, _, width in columns])

    workbook.create_sheet("Runs")
    return workbook


def _open_or_create(path: Path, *, fresh: bool) -> tuple[Workbook, bool]:
    """Return the workbook to write into, and whether it was created new."""

    if fresh or not path.exists():
        return _new_workbook(), True
    try:
        workbook = load_workbook(path)
    except Exception:
        # Corrupt or unreadable: start clean rather than lose the run.
        return _new_workbook(), True

    # A workbook predating the Searches sheet, or hand-mangled, gets rebuilt.
    if not {"Summary", "Leads", "Searches", "Events", "Runs"} <= set(workbook.sheetnames):
        return _new_workbook(), True
    return workbook, False


def write_workbook(store: Store, path: Path) -> Path:
    """Append new contacts and searches. Returns the path actually written."""

    leads = store.all_leads()
    # A fresh database with a leftover workbook means a new operator or a reset;
    # archive nothing, just start a clean sheet.
    workbook, created = _open_or_create(path, fresh=store.is_fresh() and not leads)

    appended, _ = _sync_leads(workbook["Leads"], leads)
    new_searches = _append_by_id(workbook["Searches"], store.searches(), _SEARCH_COLUMNS)
    _append_by_id(workbook["Events"], store.events_ascending(), _EVENT_COLUMNS)
    _rebuild_summary(workbook["Summary"], leads)
    _rebuild_runs(workbook["Runs"], store.recent_runs())

    written = _save(workbook, path)
    if created:
        print(f"  created a fresh workbook at {written.name}")
    elif appended or new_searches:
        print(f"  appended {appended} contact(s), {new_searches} search record(s)")
    return written


def _save(workbook: Workbook, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(suffix=".xlsx", dir=str(path.parent))
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        workbook.save(temp_path)
        os.replace(temp_path, path)
        return path
    except PermissionError:
        fallback = path.with_name(
            f"{path.stem}.{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}{path.suffix}"
        )
        os.replace(temp_path, fallback)
        print(
            f"warning: {path.name} is locked (open in Excel?). Wrote {fallback.name} instead.",
            file=sys.stderr,
        )
        return fallback
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def summarise(store: Store) -> dict[str, Any]:
    counts = Counter(row["status"] for row in store.all_leads())
    return {status.value: counts.get(status.value, 0) for status in Status}
