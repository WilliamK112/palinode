"""Tests for the ``git_commit_ready`` doctor check.

Real ``tmp_path`` git repositories, real git — the check is a read-only probe
of exactly the precondition the save path's auto-commit needs, so the three
states mirror ``tests/test_save_git_committed.py``: not a repo, a repo with no
identity, a healthy repo.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from palinode.core.config import Config
from palinode.diagnostics.checks.git_identity import git_commit_ready
from palinode.diagnostics.registry import all_checks
from palinode.diagnostics.types import DoctorContext


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False,
    )


@pytest.fixture(autouse=True)
def hermetic_git(monkeypatch):
    for var in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL", "EMAIL", "GIT_DIR", "GIT_WORK_TREE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _ctx(memory_dir: Path, auto_commit: bool = True) -> DoctorContext:
    cfg = Config(memory_dir=str(memory_dir), db_path=str(memory_dir / ".palinode.db"))
    cfg.git.auto_commit = auto_commit
    return DoctorContext(config=cfg)


def _init_repo(path: Path, identity: bool) -> None:
    assert _git(path, "init", "-q").returncode == 0
    _git(path, "config", "user.useConfigOnly", "true")
    if identity:
        _git(path, "config", "user.name", "Palinode Tests")
        _git(path, "config", "user.email", "tests@example.com")


def test_registered_as_fast_check() -> None:
    names = {fn.__name__: tags for fn, tags in all_checks()}
    assert "git_commit_ready" in names
    assert "fast" in names["git_commit_ready"]


def test_not_a_repo_warns(tmp_path: Path) -> None:
    res = git_commit_ready(_ctx(tmp_path))
    assert res.name == "git_commit_ready"
    assert res.passed is False
    assert res.severity == "warn"
    assert "not a git repository" in res.message
    assert " init" in (res.remediation or "")


def test_repo_without_identity_warns(tmp_path: Path) -> None:
    _init_repo(tmp_path, identity=False)
    res = git_commit_ready(_ctx(tmp_path))
    assert res.passed is False
    assert res.severity == "warn"
    assert "identity" in res.message.lower()
    assert "user.email" in (res.remediation or "")


def test_healthy_repo_passes(tmp_path: Path) -> None:
    _init_repo(tmp_path, identity=True)
    res = git_commit_ready(_ctx(tmp_path))
    assert res.passed is True
    assert "commit identity" in res.message


def test_auto_commit_disabled_is_info_pass(tmp_path: Path) -> None:
    res = git_commit_ready(_ctx(tmp_path, auto_commit=False))
    assert res.passed is True
    assert res.severity == "info"
    assert not (tmp_path / ".git").exists()
