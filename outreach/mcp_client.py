"""Thin async wrapper over the local LinkedIn MCP server (stdio transport).

One session is opened per run and reused by all five jobs. That matters for
speed: the MCP server boots Chromium and restores the LinkedIn session on first
use, which costs several seconds. Paying that once per run instead of once per
job is the single largest win available here.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import timedelta
from types import TracebackType
from typing import Any, Self

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPToolError(RuntimeError):
    """A tool call the LinkedIn MCP server rejected or could not complete."""

    def __init__(self, tool: str, detail: str) -> None:
        super().__init__(f"{tool}: {detail}")
        self.tool = tool
        self.detail = detail


def _identifier() -> str:
    """An MCP Identifier: ^[A-Za-z0-9][A-Za-z0-9._:-]*$, <= 200 chars."""

    return uuid.uuid4().hex


@dataclass(slots=True)
class LinkedInMCP:
    """Typed-enough helpers for the subset of tools this CRM uses."""

    timeout_seconds: float = 300.0
    env_overrides: dict[str, str] | None = None
    _session: ClientSession | None = None
    _stack: Any = None
    _context_id: str = ""

    async def __aenter__(self) -> Self:
        from contextlib import AsyncExitStack

        self._context_id = _identifier()
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        env = dict(os.environ)
        env.update(self.env_overrides or {})
        # Keep the child's stdout clean: stdio transport is the protocol channel.
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "linkedin_mcp", "serve", "--transport", "stdio"],
            env=env,
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(
            ClientSession(
                read,
                write,
                read_timeout_seconds=timedelta(seconds=self.timeout_seconds),
            )
        )
        await session.initialize()
        self._session = session
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(exc_type, exc, tb)
        self._session = None
        self._stack = None

    # ------------------------------------------------------------------
    # Core call plumbing
    # ------------------------------------------------------------------

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("LinkedInMCP must be used as an async context manager.")

        payload: dict[str, Any] = {
            "context_id": self._context_id,
            "request_id": _identifier(),
            **arguments,
        }
        result = await self._session.call_tool(tool, payload)

        if getattr(result, "isError", False):
            raise MCPToolError(tool, _text_of(result) or "unknown tool error")

        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            # FastMCP wraps non-object returns under "result".
            return structured.get("result", structured) if len(structured) == 1 else structured

        text = _text_of(result)
        if not text:
            raise MCPToolError(tool, "empty response")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            raise MCPToolError(tool, f"non-JSON response: {text[:200]}") from error
        if not isinstance(parsed, dict):
            raise MCPToolError(tool, "response was not a JSON object")
        return parsed

    # ------------------------------------------------------------------
    # Read capabilities
    # ------------------------------------------------------------------

    async def session_status(self) -> dict[str, Any]:
        return await self.call("linkedin.session.status", {})

    async def people_search(
        self,
        *,
        query: str | None,
        title_keywords: str | None = None,
        filters: dict[str, Any] | None = None,
        page_size: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"page_size": min(page_size, 100)}
        if query:
            args["query"] = query
        if title_keywords:
            args["title_keywords"] = title_keywords
        if filters:
            args["filters"] = filters
        if cursor:
            args["cursor"] = cursor
        return await self.call("linkedin.people.search", args)

    async def people_get(
        self,
        *,
        profile_slug: str,
        sections: tuple[str, ...] = ("overview",),
    ) -> dict[str, Any]:
        return await self.call(
            "linkedin.people.get",
            {"profile_slug": profile_slug, "sections": list(sections)},
        )

    async def connections_list(
        self,
        *,
        page_size: int = 100,
        cursor: str | None = None,
        sort_by: str = "recently_added",
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"page_size": min(page_size, 100), "sort_by": sort_by}
        if cursor:
            args["cursor"] = cursor
        return await self.call("linkedin.connections.list", args)

    async def conversation_get(
        self,
        *,
        profile_slug: str | None = None,
        conversation_id: str | None = None,
        max_messages: int = 25,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"max_messages": min(max_messages, 100)}
        if profile_slug:
            args["profile_slug"] = profile_slug
        if conversation_id:
            args["conversation_id"] = conversation_id
        return await self.call("linkedin.messaging.conversation.get", args)

    # ------------------------------------------------------------------
    # Write capabilities (prepare -> execute)
    # ------------------------------------------------------------------

    async def invite(
        self,
        *,
        profile_slug: str,
        note: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        prepare_args: dict[str, Any] = {"profile_slug": profile_slug}
        if note:
            prepare_args["note"] = note
        prepared = await self.call("linkedin.invitations.send.prepare", prepare_args)
        return await self._execute("linkedin.invitations.send.execute", prepared, idempotency_key)

    async def send_message(
        self,
        *,
        profile_slug: str,
        message: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        prepared = await self.call(
            "linkedin.messaging.message.prepare",
            {"profile_slug": profile_slug, "message": message},
        )
        return await self._execute("linkedin.messaging.message.execute", prepared, idempotency_key)

    async def _execute(
        self,
        tool: str,
        prepared: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Hand the prepare output back verbatim; the server verifies every field."""

        draft = prepared.get("draft") or {}
        preview = prepared.get("approval_preview")
        action_id = draft.get("action_id")
        payload_hash = draft.get("payload_hash")
        if not (action_id and payload_hash and preview):
            raise MCPToolError(tool, "prepare did not return a usable draft")

        executed = await self.call(
            tool,
            {
                "action_id": action_id,
                "payload_hash": payload_hash,
                "approval_preview": preview,
                "idempotency_key": idempotency_key,
            },
        )
        # Carry the resolved target through so callers can log who was contacted.
        executed["_target"] = (preview or {}).get("target", {})
        return executed


def _text_of(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts).strip()


def execution_outcome(executed: dict[str, Any]) -> tuple[str, bool | None, str]:
    """Return (outcome, performed, detail) from an execute result.

    outcome is one of: verified | failed | uncertain.
    """

    result = executed.get("result") or {}
    return (
        str(result.get("outcome", "uncertain")),
        result.get("performed"),
        str(result.get("detail", "")),
    )
