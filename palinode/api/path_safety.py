"""Memory-path resolution and traversal/symlink guards.

Extracted from the former ``routers/_shared.py`` junk drawer. The seam every
file-touching handler crosses before reading a caller-supplied memory path:
resolve it inside ``memory_dir`` (rejecting traversal and absolute paths) and
open it without following symlinks. Client-facing error messages are
intentionally generic so filesystem layout never leaks to an unauthenticated
caller.

The resolution guard itself now lives in :mod:`palinode.core.path_guard` —
it has no FastAPI dependency, so ``core/git_tools.py`` and
``consolidation/archive.py`` can use the same implementation without
importing a web framework. ``_resolve_memory_path`` here is the HTTP
adapter: it calls the core guard and maps
:class:`~palinode.core.path_guard.PathTraversalError` onto an
``HTTPException`` — 400 for input that's malformed on its face (a null
byte), 403 for a syntactically valid path that resolves outside
``memory_dir`` — the same split this module used before the guard moved.
"""

from __future__ import annotations

import logging
import os

from fastapi import HTTPException

from palinode.core import path_guard

logger = logging.getLogger("palinode.api")


def _memory_base_dir() -> str:
    """Return the canonical memory root."""
    return path_guard.memory_base_dir()


def _resolve_memory_path(file_path: str) -> tuple[str, str]:
    """Resolve a relative memory path without allowing traversal outside memory_dir.

    Thin HTTP adapter over :func:`palinode.core.path_guard.resolve_memory_path`
    — see that function for the guard itself (pathlib-based canonicalization,
    absolute-path rejection, null-byte rejection, symlink-loop handling).
    Converts the core :class:`~palinode.core.path_guard.PathTraversalError`
    into an ``HTTPException`` with a status code that never leaks filesystem
    layout: 400 for malformed input (null byte), 403 for a path that
    resolves outside ``memory_dir`` (absolute, ``../`` traversal, symlink
    escape). The core guard already logs the rejection (and the raw input)
    at INFO, so this adapter does not log again.
    """
    try:
        return path_guard.resolve_memory_path(file_path)
    except path_guard.PathTraversalError as exc:
        status_code = 400 if exc.malformed else 403
        raise HTTPException(status_code=status_code, detail="Invalid path") from exc


def _open_memory_file_text(resolved_path: str) -> str:
    """Open a resolved memory path for reading, rejecting symlinks on POSIX.

    L5 hardening: closes the TOCTOU window where a symlink swap between
    `os.path.exists()` and `open()` could redirect a memory read to a
    sensitive file outside memory_dir. Uses ``os.O_NOFOLLOW`` where
    available (POSIX) so opening a symlink raises OSError. On platforms
    without ``O_NOFOLLOW`` (Windows), falls back to a plain open which
    is the previous behaviour (memory_dir already restricts the path).

    Raises ``FileNotFoundError`` if the file does not exist (caller maps
    that to a 404), and ``OSError`` for any other I/O failure.
    """
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is not None:
        flags |= nofollow
    # os.fdopen takes ownership of the fd; the `with` closes it on exit.
    with os.fdopen(os.open(resolved_path, flags), "r", encoding="utf-8") as f:
        return f.read()
