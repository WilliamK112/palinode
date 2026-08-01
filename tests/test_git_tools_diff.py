"""``git_tools.diff`` must not answer "nothing changed" when things changed.

``palinode_diff`` is the surface an operator reaches for to answer "did
anything change?". A wrong answer there is not a missing feature, it is a
confident false negative that gets acted on — and it did: a healthy
deterministic monitor was diagnosed as dead for eight days and escalated,
because two lookbacks (9 and 30 days) each returned a clean "No content
changes" over a store that was being written to daily.

Two independent defects produced that, and both are pinned here:

1. **The lookback window was a lie.** The base commit came from
   ``git log --after=<since> --reverse --format=%H -1``, which does *not*
   return the oldest commit in the window — ``-1`` limits the newest-first
   walk and ``--reverse`` then reverses a single-element result. The base was
   therefore always HEAD, so every ``diff(days=N)`` reported exactly the most
   recent commit and ``days`` changed nothing but the heading.

2. **The path filter was silent, and incomplete.** The default list omitted
   ``research/`` and ``inbox/``, two categories the save path really writes
   to — and ``inbox/`` is where deterministic-monitor writers land
   their incidents. Anything filtered out, by the default list or by the
   caller's own ``paths``, vanished without a word.

Real git repositories throughout — the defect lives in the argv handed to git,
so a mocked ``_run_git`` cannot see it.
"""
from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from palinode.core import git_tools
from palinode.core.config import config


def _git(repo: str, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=env
    )
    return result.stdout


@pytest.fixture()
def store(tmp_path, monkeypatch) -> str:
    """A real git-backed memory dir wired up as ``config.memory_dir``."""
    repo = str(tmp_path)
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    monkeypatch.setattr(config, "memory_dir", repo)
    return repo


def _commit(repo: str, rel: str, body: str, days_ago: int) -> None:
    """Write ``body`` to ``rel`` and commit it dated ``days_ago`` days back."""
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(body + "\n")
    stamp = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    env = dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"palinode: auto-save {rel}", env=env)


# ── 1. the lookback window ───────────────────────────────────────────────────


def test_window_reports_every_commit_in_range_not_just_the_last(store):
    """A 7-day lookback shows all seven days of changes, not only HEAD.

    The reproduction from the incident: daily reports land, one later commit
    lands on top, and the reports become invisible.
    """
    _commit(store, "projects/report-a.md", "55 services, all green", days_ago=5)
    _commit(store, "projects/report-b.md", "UPS down->up transition", days_ago=4)
    _commit(store, "projects/report-c.md", "24103 ticks", days_ago=3)
    _commit(store, "insights/unrelated.md", "something else entirely", days_ago=1)

    out = git_tools.diff(days=7)

    assert "report-a.md" in out
    assert "report-b.md" in out
    assert "report-c.md" in out
    assert "unrelated.md" in out


def test_days_argument_actually_changes_the_window(store):
    """``days`` must select different history, not just retitle the same output."""
    _commit(store, "projects/ancient.md", "long ago", days_ago=40)
    _commit(store, "projects/recent.md", "yesterday", days_ago=1)

    narrow = git_tools.diff(days=7)
    wide = git_tools.diff(days=90)

    assert "ancient.md" not in narrow
    assert "recent.md" in narrow
    assert "ancient.md" in wide
    assert "recent.md" in wide


def test_history_younger_than_the_window_is_reported_in_full(store):
    """When every commit is inside the window, the first commit still shows.

    The base is the newest commit *preceding* the cutoff; there isn't one here,
    so the diff must anchor on the empty tree rather than skip the root commit.
    """
    _commit(store, "projects/first.md", "the very first memory", days_ago=3)
    _commit(store, "projects/second.md", "the second", days_ago=2)

    out = git_tools.diff(days=30)

    assert "first.md" in out
    assert "second.md" in out


def test_window_with_no_commits_says_so(store):
    """An honest empty answer is fine — when it is actually true."""
    _commit(store, "projects/old.md", "well outside the window", days_ago=60)

    out = git_tools.diff(days=7)

    assert "No memory files changed in the last 7 days." == out


# ── 2. silent exclusions ─────────────────────────────────────────────────────


def test_default_paths_cover_every_save_category():
    """No category the save path writes to may be invisible to diff.

    Pinned against the save-path map rather than restated, so adding a category
    without adding it here fails loudly instead of silently hiding a whole class
    of memory from the "what changed?" surface.
    """
    from palinode.api.memory_write import _TYPE_TO_CATEGORY

    covered = {p.rstrip("/") for p in git_tools.DEFAULT_DIFF_PATHS}
    missing = set(_TYPE_TO_CATEGORY.values()) - covered

    assert not missing, (
        f"save() writes to {sorted(missing)} but diff's default filter omits them — "
        "changes there are invisible to the 'what changed?' surface"
    )


def test_caller_path_filter_reports_what_it_hid(store):
    """Narrowing with ``paths`` must account for the changes it dropped."""
    _commit(store, "projects/report.md", "the daily report", days_ago=2)
    _commit(store, "inbox/monitor-incident.md", "status: resolved", days_ago=1)

    out = git_tools.diff(days=7, paths=["projects/"])

    assert "report.md" in out
    assert "Not shown" in out
    assert "inbox/" in out


def test_all_changes_filtered_away_is_never_reported_as_no_changes(store):
    """The false negative itself: everything hidden must not read as nothing changed.

    This is the exact shape of the escalated incident — a filter removed every
    change in the window and the output asserted there had been none.
    """
    _commit(store, "inbox/monitor-incident.md", "status: resolved", days_ago=2)
    _commit(store, "insights/lesson.md", "what we learned", days_ago=1)

    out = git_tools.diff(days=7, paths=["decisions/"])

    assert "Not shown" in out
    assert "2 changed file(s)" in out
    assert "No content changes in the specified paths." not in out


def test_default_filter_reports_what_it_hid(store):
    """The default filter is a filter too — its exclusions are reported as well."""
    _commit(store, "projects/report.md", "the daily report", days_ago=2)
    _commit(store, "archive/retired-note.md", "archived", days_ago=1)

    out = git_tools.diff(days=7)

    assert "Not shown" in out
    assert "archive/" in out
    assert "the default path filter" in out


def test_nothing_hidden_means_no_suppression_notice(store):
    """The notice is a report of fact, not decoration — absent when nothing was cut."""
    _commit(store, "projects/report.md", "the daily report", days_ago=2)

    out = git_tools.diff(days=7)

    assert "report.md" in out
    assert "Not shown" not in out


# ── 3. telemetry is not a diff-level predicate ───────────────────────────────


def test_telemetry_kind_is_visible_in_diff(store):
    """``metadata.kind: telemetry`` is a *recall* exclusion, not a provenance one.

    The default policy keeps machine writes out of semantic recall so ranking
    stays clean. Provenance answers a different question — "what changed?" — and
    must not inherit that exclusion: a monitor's own writes are precisely what an
    operator is looking for when they diagnose the monitor.
    """
    _commit(
        store,
        "inbox/power-incident.md",
        "---\ntype: ActionItem\nmetadata:\n  kind: telemetry\n---\nUPS transition",
        days_ago=2,
    )

    out = git_tools.diff(days=7)

    assert "power-incident.md" in out
    assert "kind: telemetry" in out


# ── 4. the notice has to survive the trip to the caller ──────────────────────


def test_suppression_notice_reaches_the_api_surface(store, monkeypatch):
    """MCP, CLI and the plugin all read the API's ``diff`` string verbatim.

    All four surfaces render whatever this one endpoint returns, so pinning it
    here pins the whole set: an operator on any surface sees the exclusion.
    """
    import importlib

    from fastapi.testclient import TestClient

    _commit(store, "projects/report.md", "the daily report", days_ago=2)
    _commit(store, "archive/retired-note.md", "archived", days_ago=1)

    monkeypatch.setattr(config.services.api, "host", "127.0.0.1")
    for var in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE", "PALINODE_API_HOST"):
        monkeypatch.delenv(var, raising=False)
    # The store is git-seeded, not save()-seeded, so it has memory files and no
    # database — which the startup guard correctly reads as a misconfiguration.
    monkeypatch.setenv("PALINODE_ALLOW_FRESH_DB", "1")
    import palinode.api.server as srv

    srv = importlib.reload(srv)
    with TestClient(srv.app, raise_server_exceptions=True) as client:
        body = client.get("/diff", params={"days": 7}).json()["diff"]

    assert "report.md" in body
    assert "Not shown" in body
    assert "archive/" in body
