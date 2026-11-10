"""Shared Playwright Chromium/Chrome launch helpers."""

from __future__ import annotations

from typing import Any

from playwright.async_api import BrowserContext, Playwright

from linkedin_mcp.config import Settings


def persistent_context_kwargs(
    settings: Settings,
    *,
    user_data_dir: str,
    headless: bool,
) -> dict[str, Any]:
    """Build kwargs for ``chromium.launch_persistent_context``.

    Prefer the installed Google Chrome channel so Windows does not fall back to
    an old system browser when Playwright's bundled Chromium is unavailable.
    """

    kwargs: dict[str, Any] = {
        "user_data_dir": user_data_dir,
        "headless": headless,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-default-browser-check",
            "--disable-features=TranslateUI",
        ],
    }
    channel = (settings.browser_channel or "").strip()
    if channel:
        kwargs["channel"] = channel
    return kwargs


async def launch_persistent_context(
    playwright: Playwright,
    settings: Settings,
    *,
    user_data_dir: str,
    headless: bool,
) -> BrowserContext:
    """Launch a persistent Chrome/Chromium context for the LinkedIn profile."""

    return await playwright.chromium.launch_persistent_context(
        **persistent_context_kwargs(
            settings,
            user_data_dir=user_data_dir,
            headless=headless,
        )
    )


def channel_label(settings: Settings) -> str:
    channel = (settings.browser_channel or "").strip()
    return channel or "chromium"
