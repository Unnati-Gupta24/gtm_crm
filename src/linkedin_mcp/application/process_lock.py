"""Singleton lock for the one local LinkedIn account runtime.

POSIX uses ``fcntl.flock`` on the ownership file (advisory). Windows uses a
companion ``.fd`` lock file with ``LockFileEx`` because Windows locks are
mandatory and would otherwise block reading the owner JSON while the runtime is
alive. Owner metadata is stored in a separate ``.owner`` file on Windows.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, cast
from uuid import uuid4

from linkedin_mcp.errors import ConfigurationError


def _owner_file_path(path: Path) -> Path:
    """Readable ownership JSON. Never byte-range-locked on Windows."""

    if sys.platform == "win32":
        return path.with_name(path.name + ".owner")
    return path


def _lock_file_path(path: Path) -> Path:
    """File that carries the exclusive process lock."""

    if sys.platform == "win32":
        return path.with_name(path.name + ".fd")
    return path


if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _ERROR_LOCK_VIOLATION = 33
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    def _lock_exclusive_nb(fileno: int) -> None:
        handle = msvcrt.get_osfhandle(fileno)
        overlapped = _OVERLAPPED()
        ctypes.set_last_error(0)
        ok = _kernel32.LockFileEx(
            handle,
            _LOCKFILE_EXCLUSIVE_LOCK | _LOCKFILE_FAIL_IMMEDIATELY,
            0,
            0xFFFFFFFF,
            0xFFFFFFFF,
            ctypes.byref(overlapped),
        )
        if not ok:
            error = ctypes.get_last_error()
            if error in {_ERROR_LOCK_VIOLATION, 0}:
                raise BlockingIOError(error, "Resource temporarily unavailable")
            raise OSError(error, f"LockFileEx failed with Win32 error {error}")

    def _unlock(fileno: int) -> None:
        handle = msvcrt.get_osfhandle(fileno)
        overlapped = _OVERLAPPED()
        _kernel32.UnlockFileEx(handle, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(overlapped))

else:
    import fcntl

    def _lock_exclusive_nb(fileno: int) -> None:
        fcntl.flock(fileno, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fileno: int) -> None:
        fcntl.flock(fileno, fcntl.LOCK_UN)


def _secure_file_mode(fileno: int, mode: int = 0o600) -> None:
    if hasattr(os, "fchmod"):
        with suppress(OSError, NotImplementedError):
            os.fchmod(fileno, mode)


@dataclass(frozen=True, slots=True)
class AccountRuntimeOwner:
    """Non-secret identity of the process holding one account lock."""

    pid: int
    instance_id: str | None = None
    account_id: str | None = None
    command: str | None = None
    transport: str | None = None
    started_at: str | None = None
    endpoint: str | None = None
    version: str | None = None
    configuration_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class AccountRuntimeStatus:
    """Current ownership state for one configured runtime lock."""

    running: bool
    owner: AccountRuntimeOwner | None = None


class AccountProcessLock:
    """Elect one shared browser-runtime owner for a configured LinkedIn account."""

    def __init__(
        self,
        path: Path,
        *,
        account_id: str | None = None,
        command: str | None = None,
        transport: str | None = None,
        version: str | None = None,
        configuration_fingerprint: str | None = None,
    ) -> None:
        self._path = path
        self._owner_path = _owner_file_path(path)
        self._lock_path = _lock_file_path(path)
        self._account_id = account_id
        self._command = command
        self._transport = transport
        self._version = version
        self._configuration_fingerprint = configuration_fingerprint
        self._instance_id = str(uuid4())
        self._handle: TextIO | None = None
        self._owner: AccountRuntimeOwner | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        if self.acquired:
            return
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(OSError):
            self._path.parent.chmod(0o700)
        handle = self._lock_path.open("a+", encoding="utf-8")
        try:
            _secure_file_mode(handle.fileno(), 0o600)
            _lock_exclusive_nb(handle.fileno())
        except BlockingIOError as error:
            handle.close()
            status = inspect_account_runtime(self._path)
            owner_suffix = ""
            if status.owner is not None:
                owner_suffix = f" (PID {status.owner.pid})"
            raise ConfigurationError(
                "Another local LinkedIn runtime or profile-maintenance command already owns "
                "this account"
                f"{owner_suffix}. Run `linkedin-mcp status`, then `linkedin-mcp stop` "
                "to release it."
            ) from error
        except Exception:
            handle.close()
            raise
        owner = AccountRuntimeOwner(
            pid=os.getpid(),
            instance_id=self._instance_id,
            account_id=self._account_id,
            command=self._command,
            transport=self._transport,
            started_at=datetime.now(UTC).isoformat(),
            version=self._version,
            configuration_fingerprint=self._configuration_fingerprint,
        )
        self._handle = handle
        self._owner = owner
        self._write_owner(owner)

    def publish_endpoint(self, endpoint: str) -> AccountRuntimeOwner:
        """Publish the healthy loopback endpoint while retaining lock ownership."""

        if not endpoint.startswith(("http://127.0.0.1:", "http://[::1]:", "http://localhost:")):
            raise ValueError("The shared runtime endpoint must use an explicit loopback host.")
        owner = self._owner
        if self._handle is None or owner is None:
            raise RuntimeError("The account runtime lock is not owned by this process.")
        owner = replace(owner, endpoint=endpoint)
        self._owner = owner
        self._write_owner(owner)
        return owner

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        self._owner = None
        if handle is None:
            return
        try:
            with suppress(OSError):
                self._owner_path.write_text("", encoding="utf-8")
            _unlock(handle.fileno())
        finally:
            handle.close()

    def _write_owner(self, owner: AccountRuntimeOwner) -> None:
        if self._handle is None:
            raise RuntimeError("The account runtime lock is not owned by this process.")
        payload = json.dumps(asdict(owner), sort_keys=True) + "\n"
        if sys.platform == "win32":
            self._owner_path.write_text(payload, encoding="utf-8")
            return
        handle = self._handle
        handle.seek(0)
        handle.truncate()
        handle.write(payload)
        handle.flush()


def inspect_account_runtime(path: Path) -> AccountRuntimeStatus:
    """Inspect a runtime lock without acquiring ownership or starting Chromium."""

    lock_path = _lock_file_path(path)
    try:
        handle = lock_path.open("a+", encoding="utf-8")
    except FileNotFoundError:
        return AccountRuntimeStatus(running=False)
    except OSError as error:
        raise ConfigurationError("The LinkedIn runtime lock could not be inspected.") from error

    with handle:
        try:
            _lock_exclusive_nb(handle.fileno())
        except BlockingIOError:
            return AccountRuntimeStatus(
                running=True,
                owner=_read_owner_file(_owner_file_path(path)),
            )
        except OSError as error:
            raise ConfigurationError("The LinkedIn runtime lock could not be inspected.") from error
        else:
            _unlock(handle.fileno())
            return AccountRuntimeStatus(running=False)


def stop_account_runtime(
    path: Path,
    *,
    timeout_seconds: float = 30.0,
) -> AccountRuntimeStatus:
    """Ask the exact lock-owning process to terminate and wait for release."""

    if not 0.1 <= timeout_seconds <= 300:
        raise ValueError("Runtime stop timeout must be between 0.1 and 300 seconds.")
    initial = inspect_account_runtime(path)
    if not initial.running:
        return initial
    owner = initial.owner
    if owner is None:
        raise ConfigurationError(
            "The active LinkedIn runtime did not expose a valid owner PID; it was not stopped."
        )
    if owner.pid == os.getpid():
        raise ConfigurationError("The LinkedIn runtime cannot stop itself through this command.")

    confirmed = inspect_account_runtime(path)
    if not confirmed.running or confirmed.owner != owner:
        raise ConfigurationError(
            "LinkedIn runtime ownership changed while stop was being prepared; retry status first."
        )
    try:
        os.kill(owner.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError as error:
        raise ConfigurationError(
            "The LinkedIn runtime belongs to a process this user cannot stop."
        ) from error
    except OSError as error:
        raise ConfigurationError("The LinkedIn runtime could not be stopped safely.") from error

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = inspect_account_runtime(path)
        if not status.running:
            return AccountRuntimeStatus(running=False, owner=owner)
        if status.owner != owner:
            raise ConfigurationError(
                "A different LinkedIn runtime acquired the profile while stop was waiting."
            )
        time.sleep(0.1)
    raise ConfigurationError(
        "The LinkedIn runtime is still stopping. No force-kill was attempted; retry status shortly."
    )


def _read_owner_file(path: Path) -> AccountRuntimeOwner | None:
    try:
        return _parse_owner(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _parse_owner(value: str) -> AccountRuntimeOwner | None:
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        pid = int(text)
        return AccountRuntimeOwner(pid=pid) if pid > 1 else None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    payload = cast(dict[str, object], payload)
    pid = payload.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return None
    return AccountRuntimeOwner(
        pid=pid,
        instance_id=_optional_string(payload.get("instance_id")),
        account_id=_optional_string(payload.get("account_id")),
        command=_optional_string(payload.get("command")),
        transport=_optional_string(payload.get("transport")),
        started_at=_optional_string(payload.get("started_at")),
        endpoint=_optional_string(payload.get("endpoint")),
        version=_optional_string(payload.get("version")),
        configuration_fingerprint=_optional_string(payload.get("configuration_fingerprint")),
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
