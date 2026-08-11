"""Tests for the path-traversal hardening of the shared memory-path guard.

Tied to the marketplace security review (Tier B finding #2) and, later, the
guard-unification change described below. Until that change landed, this
suite only exercised ``api.path_safety._resolve_memory_path`` — the hardened
implementation — even though ``core/git_tools.py`` and
``consolidation/archive.py`` resolved paths through a materially weaker,
separate implementation (``os.path.realpath`` + ``str.startswith``, no
absolute-path rejection, no symlink-loop handling, and a ``ValueError`` whose
message echoed the caller's raw input). The docstring here used to describe
that legacy implementation's flaws in the past tense, as though it had
already been replaced everywhere; it hadn't — only the HTTP read routes went
through the hardened path, while ``rollback`` (a write operation),
``blame``, ``history``, and the on-demand archive/retract ops still used the
weaker one.

The guard-unification change promoted the hardened implementation to
:func:`palinode.core.path_guard.resolve_memory_path` — the single
implementation now — and pointed every caller at it. This suite is
parametrized over all of them so a regression in any one caller's adapter
fails here, not just on the HTTP surface:

- ``api.path_safety._resolve_memory_path`` — the FastAPI adapter, raises
  ``HTTPException`` (400 for malformed input, 403 for a path that resolves
  outside ``memory_dir``).
- ``core.git_tools._resolve_memory_path`` — used by ``blame``, ``history``,
  ``first_commit``, ``last_commit``, and ``rollback``.
- ``consolidation.archive.resolve_memory_ref`` — the single path guard for
  ``archive_memory`` and, via it, ``retract_mentions``.

Both core-level callers raise :class:`~palinode.core.path_guard.PathTraversalError`
directly (a ``ValueError`` subclass) rather than an ``HTTPException`` — they
must not import FastAPI — and carry the same malformed/escape distinction on
``.malformed``.

This test suite asserts, for every caller:
- ``../`` traversal is rejected
- absolute paths are rejected
- null-byte paths are rejected
- symlinks pointing outside ``memory_dir`` are rejected
- a normal nested path is accepted
- the rejection never echoes the offending input back to the caller
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from fastapi import HTTPException

from palinode.core.path_guard import PathTraversalError


def _make_memory_dir(tmp_path: Path) -> Path:
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "decisions").mkdir()
    (mem / "decisions" / "ok.md").write_text("# ok\n")
    return mem


def _patch_memory_dir(monkeypatch: pytest.MonkeyPatch, mem: Path) -> None:
    """Point every caller at the same memory_dir.

    All callers below resolve through the same
    :func:`palinode.core.path_guard.resolve_memory_path`, which reads
    ``config.memory_dir`` — one monkeypatch covers all of them.
    """
    from palinode.core.config import config

    monkeypatch.setattr(config, "memory_dir", str(mem), raising=False)


def _call_path_safety(file_path: str):
    from palinode.api.path_safety import _resolve_memory_path

    return _resolve_memory_path(file_path)


def _call_git_tools(file_path: str):
    from palinode.core.git_tools import _resolve_memory_path

    return _resolve_memory_path(file_path)


def _call_archive(file_path: str):
    from palinode.consolidation.archive import resolve_memory_ref

    return resolve_memory_ref(file_path)


#: Every real caller of the shared guard. A new caller of
#: ``core.path_guard.resolve_memory_path`` should be added here too — that
#: is the point of parametrizing this suite instead of covering one caller
#: and hoping the others behave the same.
CALLERS = {
    "api.path_safety": _call_path_safety,
    "core.git_tools": _call_git_tools,
    "consolidation.archive": _call_archive,
}


def _assert_rejected(fn, file_path: str, *, malformed: bool) -> None:
    """Assert ``fn(file_path)`` was rejected the way its transport represents that.

    ``api.path_safety`` is the FastAPI adapter and raises ``HTTPException`` —
    400 for input that's malformed on its face (a null byte), 403 for a
    syntactically valid path that resolves outside ``memory_dir``. The two
    core-level callers raise ``PathTraversalError`` directly and carry the
    same distinction on ``.malformed``. Either way, the offending input must
    never appear in the message exposed to a caller.
    """
    with pytest.raises((HTTPException, PathTraversalError)) as exc_info:
        fn(file_path)
    exc = exc_info.value
    if isinstance(exc, HTTPException):
        assert exc.status_code == (400 if malformed else 403)
        assert exc.detail == "Invalid path"
    else:
        assert exc.malformed is malformed
        assert str(exc) == "Invalid path"
    assert file_path not in str(exc)


# ---------------------------------------------------------------------------
# Traversal rejection — parametrized over every caller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caller_name", CALLERS)
def test_dotdot_traversal_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller_name: str
) -> None:
    mem = _make_memory_dir(tmp_path)
    _patch_memory_dir(monkeypatch, mem)

    # Two levels up from `decisions/` lands outside memory_dir.
    _assert_rejected(CALLERS[caller_name], "decisions/../../etc/passwd", malformed=False)


@pytest.mark.parametrize("caller_name", CALLERS)
def test_absolute_path_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller_name: str
) -> None:
    mem = _make_memory_dir(tmp_path)
    _patch_memory_dir(monkeypatch, mem)

    _assert_rejected(CALLERS[caller_name], "/etc/passwd", malformed=False)


@pytest.mark.parametrize("caller_name", CALLERS)
def test_null_byte_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller_name: str
) -> None:
    mem = _make_memory_dir(tmp_path)
    _patch_memory_dir(monkeypatch, mem)

    _assert_rejected(CALLERS[caller_name], "decisions/foo\x00bar.md", malformed=True)


@pytest.mark.parametrize("caller_name", CALLERS)
def test_symlink_outside_memory_dir_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller_name: str
) -> None:
    """A symlink whose target sits outside memory_dir must be rejected."""
    mem = _make_memory_dir(tmp_path)
    _patch_memory_dir(monkeypatch, mem)

    # Create a target file outside memory_dir
    outside = tmp_path / "outside-secrets.md"
    outside.write_text("secret\n")

    # Create a symlink inside memory_dir pointing to it
    link = mem / "decisions" / "link-out.md"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover (Windows w/o privilege)
        pytest.skip(f"Cannot create symlink in this env: {exc}")

    _assert_rejected(CALLERS[caller_name], "decisions/link-out.md", malformed=False)


# ---------------------------------------------------------------------------
# Happy path — parametrized over every caller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caller_name", CALLERS)
def test_legitimate_nested_path_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller_name: str
) -> None:
    mem = _make_memory_dir(tmp_path)
    _patch_memory_dir(monkeypatch, mem)

    # Every caller's return shape differs (a (base, resolved) tuple, a bare
    # relative path, or a (rel, abs) tuple — see the shape-specific tests
    # below) but none of them may raise for a path legitimately inside
    # memory_dir.
    CALLERS[caller_name]("decisions/ok.md")


@pytest.mark.parametrize("caller_name", CALLERS)
def test_path_with_dot_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller_name: str
) -> None:
    mem = _make_memory_dir(tmp_path)
    _patch_memory_dir(monkeypatch, mem)

    # "decisions/./ok.md" is identical to "decisions/ok.md" after resolve()
    # — must not raise for any caller.
    CALLERS[caller_name]("decisions/./ok.md")


@pytest.mark.parametrize("caller_name", CALLERS)
def test_nonexistent_path_does_not_leak_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller_name: str
) -> None:
    """A nonexistent path under memory_dir should resolve cleanly (callers
    decide whether to 404). It must NOT raise — strict=False is intentional."""
    mem = _make_memory_dir(tmp_path)
    _patch_memory_dir(monkeypatch, mem)

    CALLERS[caller_name]("decisions/does-not-exist.md")


def test_path_safety_returns_resolved_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API adapter's specific return shape: ``(base_dir, resolved_path)``."""
    mem = _make_memory_dir(tmp_path)
    _patch_memory_dir(monkeypatch, mem)

    base, resolved = _call_path_safety("decisions/ok.md")
    assert base == str(mem.resolve())
    assert resolved == str((mem / "decisions" / "ok.md").resolve())


def test_git_tools_returns_original_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git_tools's specific return shape: the original relative path,
    unchanged — callers pass it straight to ``git`` with ``cwd=memory_dir``."""
    mem = _make_memory_dir(tmp_path)
    _patch_memory_dir(monkeypatch, mem)

    assert _call_git_tools("decisions/ok.md") == "decisions/ok.md"


def test_archive_returns_rel_and_unresolved_abs_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """archive's specific return shape: ``(rel, memory_dir-joined abs)`` — the
    *un*-realpath'd form the indexer stores in ``chunks.file_path``."""
    mem = _make_memory_dir(tmp_path)
    _patch_memory_dir(monkeypatch, mem)

    rel, abs_path = _call_archive("decisions/ok.md")
    assert rel == "decisions/ok.md"
    assert abs_path == os.path.join(str(mem), rel)


# ---------------------------------------------------------------------------
# Error-message safety: the legacy implementation embedded the resolved path
# in the exception text, and one HTTP route (`git_history.py`, before the
# guards were unified) forwarded that text straight to the client via
# `str(exc)`. Every caller
# must now only ever expose "Invalid path".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caller_name", CALLERS)
@pytest.mark.parametrize(
    ("inp", "malformed"),
    [
        ("../../../etc/shadow", False),
        ("/var/log/auth.log", False),
        # Null byte is checked first regardless of what else is in the
        # string, so this is deterministically "malformed", not "escape".
        ("decisions/foo\x00../../etc/passwd", True),
    ],
)
def test_no_filesystem_info_leak_in_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller_name: str,
    inp: str,
    malformed: bool,
) -> None:
    mem = _make_memory_dir(tmp_path)
    _patch_memory_dir(monkeypatch, mem)

    _assert_rejected(CALLERS[caller_name], inp, malformed=malformed)
