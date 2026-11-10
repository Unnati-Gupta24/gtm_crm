# Local patches to the vendored MCP server

`src/linkedin_mcp/` is third-party code (upstream:
[linkedin-mcp-server](https://github.com/prakharagarwal-dev/linkedin-mcp-server),
Apache-2.0). We have modified it. **Re-downloading or upgrading that folder will
silently revert these changes and break the DM stage**, so re-apply them after
any upgrade.

---

## 1. Messaging refused genuine first-degree connections

**File:** `src/linkedin_mcp/browser/pages/messaging.py`, in `_open()`
**Applied:** 2026-08-09
**Symptom:**

```
linkedin.messaging.message.prepare: invalid_target:
Standard messaging is limited to a visible first-degree connection.
```

raised for a member who *is* a first-degree connection, making `send-dms` fail
100% of the time.

### Root cause — two compounding faults

The guard required the string `1st` in the first 30 lines of the profile's
introduction card, checked **exactly once, immediately after navigation**:

```python
introduction, expected_name = await self._profile_introduction(main)
top_text = "\n".join(_lines(await _visible_text(introduction))[:30])
if not re.search(r"\b1st\b", top_text):
    raise InvalidTargetError(...)
```

1. **Race.** The degree badge hydrates asynchronously, so a single immediate
   check is a coin flip. This is what made the failure *intermittent* — the same
   profile that accepted a DM refused a conversation read minutes later. The
   invitations page already solves this (`_wait_for_connect_control` retries
   `_PROFILE_ACTION_SETTLE_ATTEMPTS` times); messaging had no equivalent.
2. **Wrong element.** `_profile_introduction()` locates the card as *"first
   `<section>` under `main` containing a heading whose accessible name exactly
   equals the member name"*. On some renders that resolves to a nested element
   which excludes the badge.

The badge is demonstrably present — `linkedin.people.get` on the same profile
returns `connection_degree: "first"`, and line 3 of `main`'s visible text is
`· 1st`.

### Fix

A settle loop that polls both scopes, mirroring what the invitations page does:

```python
if not await self._visible_first_degree(page, main, introduction):
    raise InvalidTargetError(...)
```

`_visible_first_degree()` retries `_DEGREE_SETTLE_ATTEMPTS` (8) times with a
`_DEGREE_SETTLE_DELAY_MS` (400 ms) gap, checking the introduction card and then
the top of `<main>` on each pass.

This **widens where and how long we look, not what we accept** — a visible
first-degree badge is still required, so the safety property (never message
non-connections, never fall through to paid InMail) is preserved.

### Verified

- `send-dms --live` → `outcome=verified`, `performed=true`, detail *"The exact
  confirmed message content newly appeared in visible outgoing history"*, and
  the message is visible in the conversation thread.
- `check-replies` run 3× consecutively → 3/3 polled successfully, where the
  pre-fix code failed intermittently on the same profile.

---

## Gotcha: the shared runtime caches old code

The first client starts a **long-lived shared runtime** on `127.0.0.1:8000`.
Every later client attaches to it. Editing anything under `src/linkedin_mcp/`
has **no effect** until that process restarts — the patch above appeared not to
work for two runs for exactly this reason.

`linkedin-mcp stop` reported `"status": "not_running"` while the process was
very much alive, so do not trust it. Verify and kill directly:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  Select-Object OwningProcess
Stop-Process -Id <pid> -Force
```

Then re-run. Always do this after touching `src/linkedin_mcp/`.
