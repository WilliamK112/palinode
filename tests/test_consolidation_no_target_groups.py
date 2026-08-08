"""A group with no project document is a reported skip, not a silent failure.

Consolidation groups notes by the ``project/`` refs they carry and compacts each
group into that project's status document. When no such document exists, the
target read raised, the per-project ``except`` logged it, and the run summary —
which hardcodes ``"status": "success"`` and has no field for failures — said
nothing at all.

Measured on the dogfood store when the corpus widened to include ``insights/``:
80 notes formed 11 groups, and **9 of them had no target file**. The summary read
``success · processed_notes: 80 · projects_compacted: 2``, with roughly half the
collected notes producing nothing and no way to tell from the result.

The widening did not cause that. It revealed it: with ``daily/`` alone the same
store formed 2 groups and both had targets, so the failure mode had nowhere to
show.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from palinode.consolidation import runner
from palinode.core.config import config


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _no_ops(_system: str, _user: str) -> tuple[str, str]:
    return "[]", "stub"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Two subjects in the notes; only one has a project document."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))

    (tmp_path / "specs" / "prompts").mkdir(parents=True)
    (tmp_path / "specs" / "prompts" / "compaction.md").write_text("prompt\n")
    (tmp_path / "specs" / "prompts" / "nightly-consolidation.md").write_text("prompt\n")

    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "alpha.md").write_text(
        "---\nid: projects-alpha\n---\n\n- A fact. <!-- fact:alpha-1 -->\n"
    )

    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / f"{_today()}.md").write_text(
        "---\nid: d\n---\n\nWork on project/alpha and also project/ghost today.\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# The target helper
# ---------------------------------------------------------------------------

def test_target_prefers_the_status_layer(store):
    (store / "projects" / "alpha-status.md").write_text("---\nid: s\n---\n\nstatus\n")

    assert runner._target_file_for("alpha").endswith("alpha-status.md")


def test_target_falls_back_to_the_project_file(store):
    assert runner._target_file_for("alpha").endswith("alpha.md")


def test_target_is_none_when_no_document_exists(store):
    assert runner._target_file_for("ghost") is None


# ---------------------------------------------------------------------------
# Filtering at grouping
# ---------------------------------------------------------------------------

def test_partition_splits_on_target_existence(store):
    grouped = {"alpha": [{"content": "x"}], "ghost": [{"content": "y"}]}

    keep, skipped = runner._partition_by_target(grouped)

    assert list(keep) == ["alpha"]
    assert skipped == ["ghost"]


def test_partition_sorts_skips_for_stable_reporting(store):
    """Names chosen to have no project document, so all three are skips."""
    grouped = {k: [] for k in ("zeta-none", "alpha-none", "mid-none")}

    _, skipped = runner._partition_by_target(grouped)

    assert skipped == ["alpha-none", "mid-none", "zeta-none"], (
        "unstable order makes diffs noisy"
    )


def test_real_run_survives_a_pass_that_proposes_nothing(store, monkeypatch):
    """Regression: `model_used` was read at commit time but only assigned inside
    the loop, after the empty-operations `continue`. A quiet week — every fact a
    KEEP, so no project yields operations — raised UnboundLocalError at the
    commit step. `run_nightly` already guarded this; `run_consolidation` did not.
    """
    monkeypatch.setattr(config.git, "auto_commit", False)

    result = runner.run_consolidation(llm_fn=_no_ops)

    assert result["status"] == "success"
    assert result["projects_compacted"] == 0


# ---------------------------------------------------------------------------
# The result is honest — the point of the change
# ---------------------------------------------------------------------------

def test_dry_run_reports_the_skip(store):
    result = runner.run_consolidation(dry_run=True, llm_fn=_no_ops)

    assert result["groups_skipped_no_target"] == 1
    assert result["skipped_no_target_projects"] == ["ghost"]


def test_real_run_reports_the_skip(store, monkeypatch):
    monkeypatch.setattr(config.git, "auto_commit", False)

    result = runner.run_consolidation(llm_fn=_no_ops)

    assert result["groups_skipped_no_target"] == 1
    assert result["skipped_no_target_projects"] == ["ghost"]


def test_nightly_reports_the_skip_too(store):
    """`run_nightly` had the identical shape and the identical gap."""
    result = runner.run_nightly(dry_run=True, llm_fn=_no_ops)

    assert result["groups_skipped_no_target"] == 1
    assert result["skipped_no_target_projects"] == ["ghost"]


def test_no_skips_means_no_key(store):
    """Absent rather than zero, matching how `yaml_parse_errors` behaves."""
    (store / "projects" / "ghost.md").write_text(
        "---\nid: g\n---\n\n- A fact. <!-- fact:ghost-1 -->\n"
    )

    result = runner.run_consolidation(dry_run=True, llm_fn=_no_ops)

    assert "groups_skipped_no_target" not in result
    assert "skipped_no_target_projects" not in result


# ---------------------------------------------------------------------------
# Skipping is not swallowing
# ---------------------------------------------------------------------------

def test_the_group_with_a_target_still_compacts(store):
    """Filtering must remove only the untargetable groups."""
    result = runner.run_consolidation(dry_run=True, llm_fn=_no_ops)

    assert result["processed_notes"] == 1, "the note itself is still collected"
    assert result["status"] == "success"
    assert "alpha" not in result.get("skipped_no_target_projects", [])


def test_no_target_never_reaches_the_llm(store):
    """The skip happens before any proposal, so it costs no inference."""
    seen: list[str] = []

    def _tracking(_system: str, user: str) -> tuple[str, str]:
        seen.append(user)
        return "[]", "stub"

    runner.run_consolidation(dry_run=True, llm_fn=_tracking)

    assert len(seen) == 1, "one LLM call, for the one group with a target"


def test_direct_call_on_a_missing_target_does_not_raise(store):
    """`_consolidate_project` is defensive for callers that skip the filter."""
    ops, model = runner._consolidate_project("ghost", [{"date": _today(), "content": "n"}])

    assert ops == []
    assert model == "primary"
