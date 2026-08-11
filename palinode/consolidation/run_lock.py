"""Cross-process ownership for consolidation runs.

The lock belongs to the memory store, not to a Palinode installation. Every
public consolidation runner acquires the same file before reading or mutating
the store, so API, CLI/MCP, and cron entry points cannot race one another.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from palinode.core.config import config

logger = logging.getLogger("palinode.consolidation")

# ``.palinode/`` is the store's existing git-ignored operational directory;
# watcher/import paths also skip it, so the lock never becomes memory content.
LOCK_RELATIVE_PATH = Path(".palinode") / "consolidation.lock"
LOCK_TTL_SECONDS = 6 * 60 * 60
_MAX_ACQUIRE_ATTEMPTS = 4


class ConsolidationAlreadyRunning(RuntimeError):
    """Raised when another process owns the memory store's run lock."""


@dataclass(frozen=True)
class _LockOwner:
    path: Path
    token: str


@dataclass(frozen=True)
class _ObservedLock:
    metadata: dict[str, object]
    inode: int
    mtime_ns: int


def consolidation_lock_path(memory_dir: str | os.PathLike[str] | None = None) -> Path:
    """Return the operational lock path for one configured memory store."""
    base = memory_dir or getattr(config, "memory_dir", None) or config.palinode_dir
    return Path(base).expanduser() / LOCK_RELATIVE_PATH


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # ``os.kill(pid, 0)`` is not a harmless existence probe on Windows:
        # non-console signals call TerminateProcess. Query a process handle
        # instead so stale-lock recovery can never kill the alleged owner.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED still means it exists.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        return error.errno != errno.ESRCH
    return True


def _read_lock(path: Path) -> _ObservedLock | None:
    try:
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            raw = handle.read()
    except FileNotFoundError:
        return None
    except OSError:
        # Preserve the stat identity when possible. An unreadable recent lock
        # must fail closed rather than be treated as stale without evidence.
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        return _ObservedLock({}, stat.st_ino, stat.st_mtime_ns)

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        parsed = {}
    metadata = parsed if isinstance(parsed, dict) else {}
    return _ObservedLock(metadata, stat.st_ino, stat.st_mtime_ns)


def _lock_age_seconds(observed: _ObservedLock, now: float) -> float:
    created_at = observed.metadata.get("created_at")
    try:
        created = float(created_at)
    except (TypeError, ValueError):
        created = observed.mtime_ns / 1_000_000_000
    return max(0.0, now - created)


def _stale_reason(observed: _ObservedLock, now: float) -> str | None:
    age = _lock_age_seconds(observed, now)
    owner_host = observed.metadata.get("hostname")
    owner_pid = observed.metadata.get("pid")

    if owner_host == socket.gethostname():
        try:
            pid = int(owner_pid)
        except (TypeError, ValueError):
            pid = 0
        if pid and not _pid_is_alive(pid):
            return f"owner pid {pid} is no longer running"
        if pid:
            # A live local owner wins over age. Consolidation can legitimately
            # exceed the fallback TTL on a slow model; reclaiming it would
            # create the mutation race this lock exists to prevent.
            return None

    if age > LOCK_TTL_SECONDS:
        return f"lock age {age:.0f}s exceeds TTL {LOCK_TTL_SECONDS}s"
    return None


def _busy_message(observed: _ObservedLock) -> str:
    metadata = observed.metadata
    owner_text = (
        f"pid={metadata['pid']}"
        if metadata.get("pid") is not None
        else "owner metadata unavailable"
    )
    return (
        f"Consolidation is already running ({owner_text}; lock={LOCK_RELATIVE_PATH}). "
        "Wait for it to finish, or reclaim the lock after its owner exits."
    )


def _create_lock(path: Path, now: float) -> _LockOwner:
    token = uuid.uuid4().hex
    payload = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "created_at": now,
        "token": token,
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        offset = 0
        while offset < len(encoded):
            written = os.write(fd, encoded[offset:])
            if written == 0:
                raise OSError("short write while creating consolidation lock")
            offset += written
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            path.unlink()
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    return _LockOwner(path=path, token=token)


def _remove_if_unchanged(path: Path, observed: _ObservedLock) -> bool:
    current = _read_lock(path)
    if current is None:
        return False
    if (current.inode, current.mtime_ns) != (observed.inode, observed.mtime_ns):
        return False
    if current.metadata.get("token") != observed.metadata.get("token"):
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _acquire(memory_dir: str | os.PathLike[str] | None = None) -> _LockOwner:
    path = consolidation_lock_path(memory_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    for _ in range(_MAX_ACQUIRE_ATTEMPTS):
        now = time.time()
        try:
            return _create_lock(path, now)
        except FileExistsError:
            observed = _read_lock(path)
            if observed is None:
                continue
            reason = _stale_reason(observed, now)
            if reason is None:
                raise ConsolidationAlreadyRunning(_busy_message(observed))
            if not _remove_if_unchanged(path, observed):
                continue
            logger.warning(
                "Reclaimed stale consolidation lock path=%s reason=%s",
                path,
                reason,
            )

    observed = _read_lock(path)
    if observed is not None:
        raise ConsolidationAlreadyRunning(_busy_message(observed))
    raise ConsolidationAlreadyRunning(
        f"Could not acquire consolidation lock after concurrent recovery (lock={path})."
    )


def _release(owner: _LockOwner) -> None:
    observed = _read_lock(owner.path)
    if observed is None:
        return
    if observed.metadata.get("token") != owner.token:
        logger.warning(
            "Did not release consolidation lock owned by another run path=%s",
            owner.path,
        )
        return
    try:
        owner.path.unlink()
    except FileNotFoundError:
        pass


@contextmanager
def consolidation_run_lock(
    memory_dir: str | os.PathLike[str] | None = None,
) -> Iterator[Path]:
    """Acquire the store-local run lock and always release this ownership."""
    owner = _acquire(memory_dir)
    try:
        yield owner.path
    finally:
        _release(owner)
