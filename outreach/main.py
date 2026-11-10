"""Command-line entrypoint for the LinkedIn outreach CRM.

linkedin-outreach init        # create .env from .env.example
linkedin-outreach doctor      # verify config + LinkedIn session
linkedin-outreach run-once    # all five jobs, once
linkedin-outreach run-loop    # all five jobs, forever, on a timer
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from outreach import __version__
from outreach.campaigns import Campaign, load_campaigns
from outreach.config import (
    ConfigError,
    OutreachSettings,
    create_env_from_example,
    describe,
    load_settings,
)
from outreach.excel_log import summarise, write_workbook
from outreach.jobs import JOBS, Runner
from outreach.mcp_client import LinkedInMCP, MCPToolError
from outreach.state import Store

_ORDER = ("discover", "invite", "check-accepts", "send-dms", "check-replies")

# Two halves of the funnel, so they can be run at different rhythms.
# Filling the pipeline is cheap and rarely needed; working it is where the
# account risk lives, so most people want `dm` running often and `search`
# occasionally.
STAGES: dict[str, tuple[str, ...]] = {
    "search": ("discover", "invite"),
    "dm": ("check-accepts", "send-dms", "check-replies"),
    "all": _ORDER,
}


def _parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="linkedin-outreach",
        description="Autonomous LinkedIn outreach CRM (search -> invite -> DM -> reply).",
    )
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = root.add_subparsers(dest="command", required=True)

    setup = commands.add_parser("init", help="Create .env from .env.example")
    setup.add_argument("--force", action="store_true", help="Overwrite an existing .env")

    commands.add_parser("doctor", help="Check config, campaigns, and LinkedIn session")
    commands.add_parser("status", help="Print lead counts without touching LinkedIn")
    commands.add_parser("report", help="Rebuild the Excel workbook from local state")

    for name in (*_ORDER, "run-once"):
        _add_run_flags(commands.add_parser(name, help=f"Run the {name} job"))

    loop = commands.add_parser("run-loop", help="Run everything on a timer until stopped")
    _add_run_flags(loop)
    loop.add_argument("--every", type=int, help="Minutes between runs (overrides .env)")
    loop.add_argument("--max-loops", type=int, help="Stop after N runs (0 = forever)")

    return root


def _add_run_flags(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="Actually send (default is dry-run).")
    mode.add_argument("--dry-run", action="store_true", help="Log intended actions, send nothing.")
    parser.add_argument("--campaign", help="Limit to a single campaign id")
    parser.add_argument("--excel", type=Path, help="Override the Excel output path")
    parser.add_argument(
        "--stage",
        choices=sorted(STAGES),
        default="all",
        help=(
            "Which half of the funnel to run. "
            "search = discover + invite; dm = check-accepts + send-dms + check-replies; "
            "all = everything (default)."
        ),
    )


def _resolve_dry_run(arguments: argparse.Namespace, settings: OutreachSettings) -> bool:
    if getattr(arguments, "live", False):
        return False
    if getattr(arguments, "dry_run", False):
        return True
    return settings.dry_run


def resolve_campaigns(settings: OutreachSettings, only: str | None = None) -> list[Campaign]:
    """Campaigns come from a YAML file when configured, otherwise from .env."""

    path = settings.campaigns_file_path()
    if path is not None:
        if not path.exists():
            raise ConfigError(
                f"OUTREACH_CAMPAIGNS_FILE points at {path}, which does not exist.\n"
                "Create it, or blank that setting to use the single campaign from .env."
            )
        campaigns = load_campaigns(path)
    else:
        try:
            campaigns = [Campaign.model_validate(settings.campaign_from_env())]
        except ValidationError as error:
            raise ConfigError(
                f"The campaign defined in .env is not usable:\n{error}\n\n"
                "Check OUTREACH_SEARCH_QUERY and OUTREACH_DM_BODY are both set."
            ) from error

    active = [campaign for campaign in campaigns if campaign.enabled]
    if only:
        active = [campaign for campaign in active if campaign.id == only]
        if not active:
            raise ConfigError(f"No enabled campaign with id '{only}'.")
    if not active:
        raise ConfigError("No enabled campaigns.")
    return active


async def _run_jobs(
    job_names: Sequence[str],
    *,
    settings: OutreachSettings,
    campaigns: list[Campaign],
    dry_run: bool,
    excel_path: Path,
    quiet_banner: bool = False,
) -> int:
    store = Store(settings.db_path)
    mode = "dry-run" if dry_run else "LIVE"
    run_id = store.start_run(mode)

    if not quiet_banner:
        banner = f"LinkedIn outreach - {mode} - {len(campaigns)} campaign(s)"
        print(banner)
        print("=" * len(banner))
        if not dry_run:
            print("Sending for real. Ctrl+C now if that was not intended.")

    exit_code = 0
    summary: dict[str, object] = {}
    try:
        async with LinkedInMCP(
            timeout_seconds=settings.mcp_timeout_seconds,
            env_overrides=settings.mcp_env_overrides(),
        ) as mcp:
            status = await mcp.session_status()
            if status.get("paused"):
                print(
                    f"LinkedIn session is paused: {status.get('pause_reason')}. "
                    "Run `linkedin-mcp login` and retry.",
                    file=sys.stderr,
                )
                return 2

            runner = Runner(
                mcp=mcp,
                store=store,
                settings=settings,
                campaigns=campaigns,
                dry_run=dry_run,
            )
            for name in job_names:
                await JOBS[name](runner)

            summary = dict(runner.counters)
    except MCPToolError as error:
        print(f"error: {error}", file=sys.stderr)
        summary = {"error": error.detail[:200]}
        exit_code = 1
    except KeyboardInterrupt:
        print("\ninterrupted; state is saved", file=sys.stderr)
        summary = {"interrupted": 1}
        exit_code = 130
    finally:
        totals = summarise(store)
        store.finish_run(run_id, {**summary, **totals})
        written = write_workbook(store, excel_path)
        print("\nLeads: " + ", ".join(f"{key}={value}" for key, value in totals.items()))
        print(f"Excel: {written}")
        store.close()

    return exit_code


async def _run_loop(
    *,
    settings: OutreachSettings,
    campaigns: list[Campaign],
    dry_run: bool,
    excel_path: Path,
    every_minutes: int,
    max_loops: int,
    stage: str = "all",
) -> int:
    mode = "dry-run" if dry_run else "LIVE"
    jobs = STAGES[stage]
    print(f"LinkedIn outreach - {mode} - looping every {every_minutes}m")
    print(f"  stage     : {stage} ({', '.join(jobs)})")
    print(f"  campaigns : {', '.join(c.id for c in campaigns)}")
    print("  stop with : Ctrl+C" + (f"   (max {max_loops} runs)" if max_loops else ""))
    print()

    iteration = 0
    while True:
        iteration += 1
        stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        header = f"--- run {iteration} at {stamp} UTC ---"
        print(header)

        try:
            await _run_jobs(
                jobs,
                settings=settings,
                campaigns=campaigns,
                dry_run=dry_run,
                excel_path=excel_path,
                quiet_banner=True,
            )
        except KeyboardInterrupt:
            print("\nstopped", file=sys.stderr)
            return 130
        except Exception as error:
            # One bad run must never kill an unattended loop.
            print(f"run {iteration} failed: {error}", file=sys.stderr)

        if max_loops and iteration >= max_loops:
            print(f"\nreached max_loops={max_loops}; stopping.")
            return 0

        jitter = random.uniform(0, settings.loop_jitter_minutes)
        wait_minutes = every_minutes + jitter
        resume = datetime.now(UTC) + timedelta(minutes=wait_minutes)
        print(f"\nnext run in {wait_minutes:.1f}m (around {resume.strftime('%H:%M:%S')} UTC)\n")
        try:
            await asyncio.sleep(wait_minutes * 60)
        except KeyboardInterrupt:
            print("\nstopped", file=sys.stderr)
            return 130


def _cmd_status(settings: OutreachSettings) -> int:
    store = Store(settings.db_path)
    try:
        print(f"State: {settings.db_path}")
        for status, count in summarise(store).items():
            print(f"  {status:<12} {count}")
    finally:
        store.close()
    return 0


def _cmd_report(settings: OutreachSettings, excel_path: Path) -> int:
    store = Store(settings.db_path)
    try:
        print(f"Wrote {write_workbook(store, excel_path)}")
    finally:
        store.close()
    return 0


async def _cmd_doctor(settings: OutreachSettings) -> int:
    print("Configuration")
    print(describe(settings))

    print("\nCampaigns")
    try:
        campaigns = resolve_campaigns(settings)
    except ConfigError as error:
        print(f"  ERROR - {error}", file=sys.stderr)
        return 1
    for campaign in campaigns:
        note = campaign.invite_note or "(none)"
        print(f"  {campaign.id}: note={len(note)} chars, dm={len(campaign.dm_body)} chars")

    print("\nLinkedIn session (first run installs Chromium, please wait)")
    try:
        async with LinkedInMCP(
            timeout_seconds=settings.mcp_timeout_seconds,
            env_overrides=settings.mcp_env_overrides(),
        ) as mcp:
            status = await mcp.session_status()
    except Exception as error:
        print(f"  ERROR - {error}", file=sys.stderr)
        return 1

    for key in ("profile_present", "authentication_state", "paused", "pause_reason"):
        print(f"  {key:<20}: {status.get(key)}")

    if status.get("paused") or not status.get("profile_present"):
        print("\nRun `linkedin-mcp login` to sign in once, then re-run doctor.")
        return 1

    print("\nReady. Start with:  linkedin-outreach run-once")
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)

    try:
        if arguments.command == "init":
            create_env_from_example(force=arguments.force)
            print("\nNext:")
            print("  1. edit .env  (search terms and message bodies)")
            print("  2. linkedin-mcp login")
            print("  3. linkedin-outreach doctor")
            raise SystemExit(0)

        settings = load_settings()

        if arguments.command == "status":
            raise SystemExit(_cmd_status(settings))
        if arguments.command == "report":
            raise SystemExit(_cmd_report(settings, settings.excel_path))
        if arguments.command == "doctor":
            raise SystemExit(asyncio.run(_cmd_doctor(settings)))

        campaigns = resolve_campaigns(settings, arguments.campaign)
        excel_path = arguments.excel or settings.excel_path
        dry_run = _resolve_dry_run(arguments, settings)

        if arguments.command == "run-loop":
            every = arguments.every or settings.loop_minutes
            max_loops = (
                arguments.max_loops if arguments.max_loops is not None else settings.max_loops
            )
            raise SystemExit(
                asyncio.run(
                    _run_loop(
                        settings=settings,
                        campaigns=campaigns,
                        dry_run=dry_run,
                        excel_path=excel_path,
                        every_minutes=every,
                        max_loops=max_loops,
                        stage=arguments.stage,
                    )
                )
            )

        job_names = (
            STAGES[arguments.stage] if arguments.command == "run-once" else (arguments.command,)
        )
        raise SystemExit(
            asyncio.run(
                _run_jobs(
                    job_names,
                    settings=settings,
                    campaigns=campaigns,
                    dry_run=dry_run,
                    excel_path=excel_path,
                )
            )
        )

    except ConfigError as error:
        print(f"\nconfiguration error:\n{error}\n", file=sys.stderr)
        raise SystemExit(1) from error
    except FileNotFoundError as error:
        print(f"\nerror: {error}\n", file=sys.stderr)
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
