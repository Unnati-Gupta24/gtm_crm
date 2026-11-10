"""The five orchestrator jobs.

    discover -> invite -> check_accepts -> send_dms -> check_replies

Two deliberate departures from a naive implementation, both for speed:

1. check_accepts scans "recently added connections" once per run instead of
   re-reading every invited person's profile. One page load replaces N.
2. discover trusts the search result card (name, headline, location, degree)
   instead of opening each profile. Set OUTREACH_ENRICH_PROFILES=true if you
   want the slower, richer version.
"""

from __future__ import annotations

import asyncio
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from outreach.campaigns import Campaign, template_values
from outreach.config import OutreachSettings
from outreach.mcp_client import LinkedInMCP, MCPToolError, execution_outcome
from outreach.state import ActionType, Status, Store


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(slots=True)
class Runner:
    mcp: LinkedInMCP
    store: Store
    settings: OutreachSettings
    campaigns: list[Campaign]
    dry_run: bool
    counters: Counter[str] = field(default_factory=Counter)

    def log(self, message: str) -> None:
        print(f"  {message}")

    def event(self, job: str, event: str, **kwargs: Any) -> None:
        self.store.record_event(job=job, event=event, **kwargs)

    async def throttle(self, base_seconds: float, *, remaining: int) -> None:
        """Pause between account-changing actions.

        This is the main lever on account safety: LinkedIn tolerates a human
        pace, and a burst of writes seconds apart is what looks automated.
        Jitter keeps the gaps uneven. Skipped entirely when nothing is left to
        send, so a run never idles for no reason.
        """

        if self.dry_run or remaining <= 0:
            return
        delay = base_seconds + random.uniform(0, self.settings.action_jitter_seconds)
        if delay <= 0:
            return
        self.log(f"waiting {delay:.0f}s before the next send ({remaining} left this run)")
        await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# 1. discover
# ---------------------------------------------------------------------------


async def discover(runner: Runner) -> None:
    print("\n[discover] searching for new leads")
    for campaign in runner.campaigns:
        budget = campaign.max_discover
        if budget is None:
            budget = runner.settings.max_discover_per_run
        if budget <= 0:
            continue

        known = runner.store.known_slugs(campaign.id)
        added = 0
        cursor: str | None = None
        # Diagnostics, so a zero-result run explains itself instead of going quiet.
        seen = 0
        already_known = 0
        hidden = 0
        pages = 0

        while added < budget:
            try:
                result = await runner.mcp.people_search(
                    query=campaign.query,
                    title_keywords=campaign.title_keywords,
                    filters=campaign.people_filters() or None,
                    page_size=min(budget - added, 100),
                    cursor=cursor,
                )
            except MCPToolError as error:
                runner.log(f"{campaign.id}: search failed - {error.detail}")
                runner.event(
                    "discover", "search_failed", campaign_id=campaign.id, detail=error.detail
                )
                break

            people = result.get("people") or []
            coverage = result.get("coverage") or {}
            pages += 1
            seen += len(people)
            # LinkedIn withholds identity for many out-of-network members.
            hidden += int(coverage.get("unidentifiable_result_count") or 0)

            if not people:
                break

            for person in people:
                slug = person.get("profile_slug")
                if not slug:
                    continue
                if slug in known:
                    already_known += 1
                    # Still note that the search matched them again, so the
                    # sheet shows when each contact was last seen.
                    runner.store.mark_seen(slug, campaign.id)
                    continue

                degree = person.get("connection_degree")
                is_new = runner.store.upsert_discovered(
                    profile_slug=slug,
                    campaign_id=campaign.id,
                    name=person.get("name"),
                    headline=person.get("headline"),
                    location=person.get("location"),
                    profile_url=person.get("profile_url"),
                    connection_degree=degree,
                )
                if not is_new:
                    continue

                known.add(slug)
                added += 1
                runner.store.mark_seen(slug, campaign.id)

                # Already a 1st-degree connection: no invite needed, DM-eligible.
                if degree == "first":
                    runner.store.set_status(
                        slug, campaign.id, Status.CONNECTED, connected_at=_now()
                    )
                    runner.event(
                        "discover",
                        "already_connected",
                        campaign_id=campaign.id,
                        profile_slug=slug,
                    )

                if added >= budget:
                    break

            pagination = result.get("pagination") or {}
            cursor = pagination.get("next_cursor")
            if not pagination.get("has_more") or not cursor:
                break

        runner.counters["discovered"] += added
        runner.store.record_search(
            campaign_id=campaign.id,
            query=campaign.query,
            filters=str(campaign.people_filters() or {}),
            matched=seen,
            new_leads=added,
            already_known=already_known,
            hidden=hidden,
            pages=pages,
        )
        detail = (
            f"{added} new | {seen} matched, {already_known} already known, "
            f"{hidden} hidden by LinkedIn, {pages} page(s)"
        )
        runner.log(f"{campaign.id}: {detail}")

        if added == 0:
            runner.log(f"  -> {_zero_result_hint(campaign, seen, already_known, hidden)}")

        runner.event("discover", "completed", campaign_id=campaign.id, detail=detail)

    if runner.settings.enrich_profiles:
        await _enrich(runner)


def classify_invite_failure(detail: str) -> tuple[str, Status | None]:
    """Map an invite-prepare failure onto the right lead state.

    The MCP's _connect_control() reports four states and only one is a real
    error. `pending_sent` and `already_connected` mean LinkedIn is already
    further along than we are, so they are progress, not failure.

    Returns (event_name, new_status). A None status means "genuine failure,
    leave the lead where it is and let a later run retry".
    """

    lowered = detail.lower()

    if "pending_sent" in lowered:
        # An invite already exists - probably sent outside this tool.
        return "already_pending", Status.INVITED

    if "already_connected" in lowered:
        # Already 1st degree; skip straight to the DM stage.
        return "already_connected", Status.CONNECTED

    if "connect_unavailable" in lowered or "no unique visible connect control" in lowered:
        # Follow-only profile, Connect buried behind "More", or out of network.
        # LinkedIn offers no invite path, so retrying wastes budget forever.
        return "no_connect_control", Status.SKIPPED

    return "failed", None


def _zero_result_hint(campaign: Campaign, seen: int, already_known: int, hidden: int) -> str:
    """Explain a zero-result search instead of leaving the operator guessing."""

    if seen and seen == already_known:
        return "every match is already in the database; broaden the query or raise max_discover"
    if hidden and not seen:
        return (
            f"LinkedIn hid all {hidden} result(s) behind 'LinkedIn Member'. "
            "Widen connection_degrees or use broader keywords."
        )
    narrowing = [
        label
        for label, value in (
            ("locations", campaign.locations),
            ("industries", campaign.industries),
            ("current_companies", campaign.current_companies),
            ("schools", campaign.schools),
            ("title", campaign.title),
        )
        if value
    ]
    if narrowing:
        return (
            f"no matches. Try removing {', '.join(narrowing)} first "
            "(location and industry facets are the most common cause of 0 results)."
        )
    return "no matches for this query; try broader keywords."


async def _enrich(runner: Runner) -> None:
    """Optional: open each un-enriched profile for company/location detail."""

    pending = [row for row in runner.store.all_leads() if not row["company"]]
    for row in pending[: runner.settings.max_discover_per_run]:
        try:
            result = await runner.mcp.people_get(profile_slug=row["profile_slug"])
        except MCPToolError as error:
            runner.log(f"enrich {row['profile_slug']}: {error.detail}")
            continue
        person = result.get("person") or {}
        runner.store.update_lead(
            row["profile_slug"],
            row["campaign_id"],
            company=person.get("current_company_text"),
            headline=person.get("headline") or row["headline"],
            location=person.get("location") or row["location"],
            connection_degree=person.get("connection_degree"),
        )


# ---------------------------------------------------------------------------
# 2. invite
# ---------------------------------------------------------------------------


async def invite(runner: Runner) -> None:
    print("\n[invite] sending connection requests")

    sent_today = runner.store.actions_today(ActionType.INVITE)
    daily_remaining = max(0, runner.settings.daily_invite_cap - sent_today)
    if daily_remaining == 0:
        runner.log(f"daily invite cap reached ({runner.settings.daily_invite_cap})")
        return

    run_budget = min(runner.settings.max_invites_per_run, daily_remaining)

    for campaign in runner.campaigns:
        if run_budget <= 0:
            break
        campaign_budget = campaign.max_invites
        limit = run_budget if campaign_budget is None else min(run_budget, campaign_budget)

        leads = runner.store.leads_by_status(
            Status.DISCOVERED, campaign_id=campaign.id, limit=limit
        )
        for lead in leads:
            if run_budget <= 0:
                break

            slug = lead["profile_slug"]
            key = Store.idempotency_key(ActionType.INVITE, slug, campaign.id)

            if runner.store.blocked(key):
                # Verified -> advance. Uncertain -> leave it for a human.
                if runner.store.already_done(key):
                    runner.store.set_status(slug, campaign.id, Status.INVITED, invited_at=_now())
                continue

            note = campaign.note_for(
                template_values(
                    name=lead["name"],
                    headline=lead["headline"],
                    company=lead["company"],
                    location=lead["location"],
                )
            )

            if runner.dry_run:
                runner.log(f"DRY-RUN invite {slug}" + (f" note={note!r}" if note else ""))
                runner.counters["invites_planned"] += 1
                run_budget -= 1
                continue

            try:
                executed = await runner.mcp.invite(
                    profile_slug=slug, note=note, idempotency_key=key
                )
            except MCPToolError as error:
                event, new_status = classify_invite_failure(error.detail)

                runner.store.record_action(
                    idempotency_key=key,
                    profile_slug=slug,
                    campaign_id=campaign.id,
                    action_type=ActionType.INVITE,
                    # 'skipped' blocks any future retry; a plain failure does not.
                    outcome="skipped" if new_status is Status.SKIPPED else "failed",
                    performed=False,
                    detail=error.detail,
                )

                if new_status is Status.SKIPPED:
                    runner.store.set_status(
                        slug,
                        campaign.id,
                        Status.SKIPPED,
                        last_error="No Connect option (Follow-only or out of network)",
                    )
                    runner.counters["skipped_no_connect"] += 1
                    runner.log(f"skip {slug}: no Connect option (Follow-only profile)")
                elif new_status is Status.INVITED:
                    runner.store.set_status(
                        slug, campaign.id, Status.INVITED, invited_at=_now(), last_error=None
                    )
                    runner.counters["already_pending"] += 1
                    runner.log(f"{slug}: invite already pending, advanced to INVITED")
                elif new_status is Status.CONNECTED:
                    runner.store.set_status(
                        slug, campaign.id, Status.CONNECTED, connected_at=_now(), last_error=None
                    )
                    runner.counters["already_connected"] += 1
                    runner.log(f"{slug}: already connected, advanced to CONNECTED")
                else:
                    runner.store.update_lead(slug, campaign.id, last_error=error.detail[:400])
                    runner.counters["invites_failed"] += 1
                    runner.log(f"invite {slug} failed: {error.detail}")

                runner.event(
                    "invite",
                    event,
                    campaign_id=campaign.id,
                    profile_slug=slug,
                    detail=error.detail,
                )
                # Only a real send consumes budget; state corrections are free.
                if new_status is None:
                    run_budget -= 1
                continue

            outcome, performed, detail = execution_outcome(executed)
            runner.store.record_action(
                idempotency_key=key,
                profile_slug=slug,
                campaign_id=campaign.id,
                action_type=ActionType.INVITE,
                outcome=outcome,
                performed=performed,
                detail=detail,
            )

            if outcome == "verified":
                runner.store.set_status(
                    slug, campaign.id, Status.INVITED, invited_at=_now(), last_error=None
                )
                runner.counters["invited"] += 1
                runner.log(f"invited {slug}")
            else:
                runner.store.update_lead(slug, campaign.id, last_error=f"{outcome}: {detail}"[:400])
                runner.counters[f"invites_{outcome}"] += 1
                runner.log(f"invite {slug} -> {outcome}: {detail}")

            runner.event(
                "invite", outcome, campaign_id=campaign.id, profile_slug=slug, detail=detail
            )
            run_budget -= 1
            await runner.throttle(runner.settings.invite_delay_seconds, remaining=run_budget)


# ---------------------------------------------------------------------------
# 3. check_accepts
# ---------------------------------------------------------------------------


async def check_accepts(runner: Runner) -> None:
    print("\n[check-accepts] looking for newly accepted invitations")

    invited = runner.store.leads_by_status(Status.INVITED)
    if not invited:
        runner.log("nothing awaiting acceptance")
        return

    pending: dict[str, list[Any]] = {}
    for lead in invited:
        pending.setdefault(lead["profile_slug"], []).append(lead)

    # One scan of recently-added connections beats N profile reads.
    connected_slugs: set[str] = set()
    cursor: str | None = None
    for _ in range(runner.settings.accept_scan_pages):
        try:
            result = await runner.mcp.connections_list(page_size=100, cursor=cursor)
        except MCPToolError as error:
            runner.log(f"connections scan failed - {error.detail}")
            runner.event("check_accepts", "scan_failed", detail=error.detail)
            return

        for connection in result.get("connections") or []:
            slug = connection.get("profile_slug")
            if slug:
                connected_slugs.add(slug)

        pagination = result.get("pagination") or {}
        cursor = pagination.get("next_cursor")
        if not pagination.get("has_more") or not cursor:
            break

    newly = set(pending.keys() & connected_slugs)

    # The connections page does not always enumerate reliably -- on some
    # accounts it yields almost nothing. Without a fallback an accept would
    # never be detected and the whole pipeline would stall at INVITED, so
    # re-check a bounded number of pending invites individually.
    unconfirmed = [slug for slug in pending if slug not in newly]
    verified_individually = 0
    for slug in unconfirmed[: runner.settings.accept_verify_per_run]:
        try:
            result = await runner.mcp.people_get(profile_slug=slug)
        except MCPToolError as error:
            runner.log(f"verify {slug}: {error.detail}")
            continue
        person = result.get("person") or {}
        if person.get("connection_degree") == "first":
            newly.add(slug)
            verified_individually += 1

    for slug in newly:
        for lead in pending[slug]:
            runner.store.set_status(
                slug, lead["campaign_id"], Status.CONNECTED, connected_at=_now()
            )
            runner.counters["connected"] += 1
            runner.event(
                "check_accepts",
                "accepted",
                campaign_id=lead["campaign_id"],
                profile_slug=slug,
            )

    runner.log(
        f"scanned {len(connected_slugs)} connection(s), "
        f"re-checked {min(len(unconfirmed), runner.settings.accept_verify_per_run)} profile(s); "
        f"{len(newly)} of {len(pending)} pending invite(s) accepted"
        + (f" ({verified_individually} via profile check)" if verified_individually else "")
    )
    if len(connected_slugs) <= 1 and len(pending) > 1:
        runner.log(
            "  -> connections.list returned almost nothing; "
            "profile re-checks are carrying accept detection"
        )


# ---------------------------------------------------------------------------
# 4. send_dms
# ---------------------------------------------------------------------------


async def send_dms(runner: Runner) -> None:
    print("\n[send-dms] messaging new connections")

    sent_today = runner.store.actions_today(ActionType.DM)
    daily_remaining = max(0, runner.settings.daily_dm_cap - sent_today)
    if daily_remaining == 0:
        runner.log(f"daily DM cap reached ({runner.settings.daily_dm_cap})")
        return

    run_budget = min(runner.settings.max_dms_per_run, daily_remaining)

    for campaign in runner.campaigns:
        if run_budget <= 0:
            break
        campaign_budget = campaign.max_dms
        limit = run_budget if campaign_budget is None else min(run_budget, campaign_budget)

        leads = runner.store.leads_by_status(Status.CONNECTED, campaign_id=campaign.id, limit=limit)
        for lead in leads:
            if run_budget <= 0:
                break

            slug = lead["profile_slug"]
            key = Store.idempotency_key(ActionType.DM, slug, campaign.id)

            if runner.store.blocked(key):
                if runner.store.already_done(key):
                    runner.store.set_status(
                        slug, campaign.id, Status.CONTACTED, contacted_at=_now()
                    )
                continue

            body = campaign.dm_for(
                template_values(
                    name=lead["name"],
                    headline=lead["headline"],
                    company=lead["company"],
                    location=lead["location"],
                )
            )

            if runner.dry_run:
                preview = body.replace("\n", " ")[:80]
                runner.log(f"DRY-RUN dm {slug}: {preview}...")
                runner.counters["dms_planned"] += 1
                run_budget -= 1
                continue

            try:
                executed = await runner.mcp.send_message(
                    profile_slug=slug, message=body, idempotency_key=key
                )
            except MCPToolError as error:
                runner.store.record_action(
                    idempotency_key=key,
                    profile_slug=slug,
                    campaign_id=campaign.id,
                    action_type=ActionType.DM,
                    outcome="failed",
                    performed=False,
                    detail=error.detail,
                )
                runner.store.update_lead(slug, campaign.id, last_error=error.detail[:400])
                runner.log(f"dm {slug} failed: {error.detail}")
                runner.counters["dms_failed"] += 1
                run_budget -= 1
                continue

            outcome, performed, detail = execution_outcome(executed)
            runner.store.record_action(
                idempotency_key=key,
                profile_slug=slug,
                campaign_id=campaign.id,
                action_type=ActionType.DM,
                outcome=outcome,
                performed=performed,
                detail=detail,
            )

            if outcome == "verified":
                conversation_id = (executed.get("_target") or {}).get("conversation_id")
                runner.store.set_status(
                    slug,
                    campaign.id,
                    Status.CONTACTED,
                    contacted_at=_now(),
                    conversation_id=conversation_id,
                    last_error=None,
                )
                await _snapshot_baseline(runner, slug, campaign.id, conversation_id)
                runner.counters["dmed"] += 1
                runner.log(f"messaged {slug}")
            else:
                runner.store.update_lead(slug, campaign.id, last_error=f"{outcome}: {detail}"[:400])
                runner.counters[f"dms_{outcome}"] += 1
                runner.log(f"dm {slug} -> {outcome}: {detail}")

            runner.event(
                "send_dms", outcome, campaign_id=campaign.id, profile_slug=slug, detail=detail
            )
            run_budget -= 1
            await runner.throttle(runner.settings.dm_delay_seconds, remaining=run_budget)


async def _snapshot_baseline(
    runner: Runner, slug: str, campaign_id: str, conversation_id: str | None
) -> None:
    """Record which incoming messages already existed when we sent the DM.

    Reply detection compares against this set. It cannot use timestamps: the
    MCP server returns LinkedIn's visible relative text ("2h ago"), not a
    machine-readable time.
    """

    try:
        result = await runner.mcp.conversation_get(
            profile_slug=slug, conversation_id=conversation_id, max_messages=25
        )
    except MCPToolError:
        # Non-fatal: an empty baseline just means the first incoming message
        # counts as a reply, which is the correct behaviour for a new thread.
        runner.store.set_baseline_refs(slug, campaign_id, [])
        return

    conversation = result.get("conversation") or {}
    refs = [
        message.get("message_ref")
        for message in conversation.get("messages") or []
        if message.get("direction") == "incoming" and message.get("message_ref")
    ]
    runner.store.set_baseline_refs(slug, campaign_id, [ref for ref in refs if ref])
    if conversation.get("conversation_id"):
        runner.store.update_lead(slug, campaign_id, conversation_id=conversation["conversation_id"])


# ---------------------------------------------------------------------------
# 5. check_replies
# ---------------------------------------------------------------------------


async def check_replies(runner: Runner) -> None:
    print("\n[check-replies] polling conversations for inbound messages")

    contacted = runner.store.leads_by_status(Status.CONTACTED)
    if not contacted:
        runner.log("no contacted leads to poll")
        return

    budget = runner.settings.max_reply_checks_per_run
    checked = 0

    for lead in contacted[:budget]:
        slug = lead["profile_slug"]
        campaign_id = lead["campaign_id"]

        try:
            result = await runner.mcp.conversation_get(
                profile_slug=slug,
                conversation_id=lead["conversation_id"],
                max_messages=25,
            )
        except MCPToolError as error:
            runner.log(f"conversation {slug}: {error.detail}")
            continue

        checked += 1
        conversation = result.get("conversation") or {}
        baseline = Store.baseline_refs(lead)

        new_incoming = [
            message
            for message in conversation.get("messages") or []
            if message.get("direction") == "incoming"
            and message.get("message_ref")
            and message["message_ref"] not in baseline
        ]

        if not new_incoming:
            continue

        latest = new_incoming[-1]
        runner.store.set_status(
            slug,
            campaign_id,
            Status.REPLIED,
            replied_at=_now(),
            reply_text=(latest.get("text") or "")[:1000],
        )
        runner.counters["replied"] += 1
        runner.log(f"reply from {slug}")
        runner.event(
            "check_replies",
            "replied",
            campaign_id=campaign_id,
            profile_slug=slug,
            detail=(latest.get("text") or "")[:300],
        )

    runner.log(f"polled {checked} conversation(s)")


JOBS = {
    "discover": discover,
    "invite": invite,
    "check-accepts": check_accepts,
    "send-dms": send_dms,
    "check-replies": check_replies,
}
