"""
Tests for palinode/consolidation/write_time.py (tier 2a, ADR-004).

Most of these tests mock the LLM call path entirely — they verify queue
mechanics, marker files, feature flag, and error handling without touching a
real embedder or consolidation runner. The "Sync path" tests below are the
exception: they run the real `_run_check_and_apply` end to end (with a fake
`llm_fn` injected at the propose seam and the embedder/vector-search calls it
makes faked at the infra boundary) — see that section's comment for why.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from unittest.mock import patch

import pytest

from palinode.consolidation import write_time


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_palinode_dir(monkeypatch):
    """Point config at a temp directory and reset module state between tests."""
    with tempfile.TemporaryDirectory() as tmp:
        # Point config at the temp dir
        from palinode.core.config import config

        monkeypatch.setattr(config, "memory_dir", tmp)
        monkeypatch.setattr(config, "db_path", os.path.join(tmp, ".palinode.db"))
        # Enable the feature flag for tests that need it
        monkeypatch.setattr(config.consolidation.write_time, "enabled", True)
        monkeypatch.setattr(config.consolidation.write_time, "queue_max_size", 10)
        # Reset module-level queue state between tests
        monkeypatch.setattr(write_time, "_queue", None)
        yield tmp


@pytest.fixture
def tmp_memory_file(tmp_palinode_dir):
    """Create a memory file in the temp palinode dir."""
    path = os.path.join(tmp_palinode_dir, "decisions", "test-decision.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("---\nid: decision-test\ncategory: decision\n---\n\n# Test Decision\n")
    return path


@pytest.fixture
def sample_item():
    """A sample item dict matching what save_api passes through."""
    return {
        "content": "We chose Postgres over MySQL",
        "category": "decisions",
        "type": "Decision",
        "entities": ["project/test"],
        "id": "decision-test",
    }


# ── Feature flag tests ─────────────────────────────────────────────────────


def test_feature_flag_disabled_returns_none(tmp_palinode_dir, tmp_memory_file, sample_item):
    """When config.consolidation.write_time.enabled is False, schedule is a no-op."""
    from palinode.core.config import config
    config.consolidation.write_time.enabled = False

    result = write_time.schedule_contradiction_check(
        tmp_memory_file, sample_item, sync=False
    )
    assert result is None

    result = write_time.schedule_contradiction_check(
        tmp_memory_file, sample_item, sync=True
    )
    assert result is None


# ── Disk marker tests ──────────────────────────────────────────────────────


def test_write_marker_atomic(tmp_palinode_dir, tmp_memory_file, sample_item):
    """Marker files are written atomically — no .tmp files left behind on success."""
    marker = write_time._write_marker(tmp_memory_file, sample_item)

    assert os.path.exists(marker)
    assert marker.endswith(".json")
    assert not marker.endswith(".tmp")

    # Load it and verify structure
    with open(marker) as f:
        job = json.load(f)
    assert job["file_path"] == tmp_memory_file
    assert job["item"] == sample_item
    assert "enqueued_at" in job


def test_write_marker_creates_pending_dir(tmp_palinode_dir, tmp_memory_file, sample_item):
    """_write_marker creates the pending directory if it doesn't exist."""
    pending_dir = write_time._pending_dir()
    # Directory should not exist yet
    assert not os.path.exists(pending_dir)

    write_time._write_marker(tmp_memory_file, sample_item)
    assert os.path.isdir(pending_dir)


def test_mark_failed_renames_to_failed_json(tmp_palinode_dir, tmp_memory_file, sample_item):
    """_mark_failed renames a marker to .failed.json for operator review."""
    marker = write_time._write_marker(tmp_memory_file, sample_item)
    assert os.path.exists(marker)

    write_time._mark_failed(marker)

    assert not os.path.exists(marker)
    failed_path = marker.replace(".json", ".failed.json")
    assert os.path.exists(failed_path)


# ── Sweeper tests ──────────────────────────────────────────────────────────


def test_sweep_empty_pending_dir_returns_zero(tmp_palinode_dir):
    """Sweeping a non-existent pending dir is a no-op."""
    recovered = write_time.sweep_pending_markers()
    assert recovered == 0


def test_sweep_recovers_markers_to_queue(tmp_palinode_dir, tmp_memory_file, sample_item):
    """Sweeper finds marker files and re-enqueues them."""

    async def run():
        # Pre-create a marker on disk (simulates a CLI save from before API startup)
        marker = write_time._write_marker(tmp_memory_file, sample_item)
        assert os.path.exists(marker)

        recovered = write_time.sweep_pending_markers()
        assert recovered == 1

        # Marker should be deleted after successful enqueue
        assert not os.path.exists(marker)

        # Queue should have the job
        queue = write_time._get_queue()
        assert queue.qsize() == 1
        job = queue.get_nowait()
        assert job["file_path"] == tmp_memory_file
        assert job["item"] == sample_item

    asyncio.run(run())


def test_sweep_handles_corrupt_marker(tmp_palinode_dir):
    """Corrupt JSON in a marker file → renamed to .failed.json, not a crash."""

    async def run():
        pending_dir = write_time._pending_dir()
        os.makedirs(pending_dir, exist_ok=True)
        bad_marker = os.path.join(pending_dir, "20260410T000000-deadbeef.json")
        with open(bad_marker, "w") as f:
            f.write("{not valid json")

        recovered = write_time.sweep_pending_markers()
        assert recovered == 0
        assert not os.path.exists(bad_marker)
        assert os.path.exists(bad_marker.replace(".json", ".failed.json"))

    asyncio.run(run())


def test_sweep_handles_marker_missing_fields(tmp_palinode_dir):
    """Marker with missing file_path or item → renamed to .failed.json."""

    async def run():
        pending_dir = write_time._pending_dir()
        os.makedirs(pending_dir, exist_ok=True)
        bad_marker = os.path.join(pending_dir, "20260410T000000-cafebabe.json")
        with open(bad_marker, "w") as f:
            json.dump({"enqueued_at": "2026-04-10"}, f)

        recovered = write_time.sweep_pending_markers()
        assert recovered == 0
        assert os.path.exists(bad_marker.replace(".json", ".failed.json"))

    asyncio.run(run())


def test_sweep_processes_markers_in_timestamp_order(tmp_palinode_dir, tmp_memory_file, sample_item):
    """Markers are sorted by timestamp so older jobs run first."""

    async def run():
        pending_dir = write_time._pending_dir()
        os.makedirs(pending_dir, exist_ok=True)
        # Write three markers with different timestamps (sorted lexically = sorted by time)
        ts_order = ["20260410T100000", "20260410T100005", "20260410T100010"]
        for ts in ts_order:
            path = os.path.join(pending_dir, f"{ts}-abcd1234.json")
            with open(path, "w") as f:
                json.dump({"file_path": tmp_memory_file, "item": sample_item}, f)

        recovered = write_time.sweep_pending_markers()
        assert recovered == 3

        queue = write_time._get_queue()
        assert queue.qsize() == 3

    asyncio.run(run())


# ── Queue tests ────────────────────────────────────────────────────────────


def test_enqueue_when_no_event_loop_falls_to_marker(
    tmp_palinode_dir, tmp_memory_file, sample_item
):
    """Calling schedule from sync context (no event loop) uses disk markers."""
    result = write_time.schedule_contradiction_check(
        tmp_memory_file, sample_item, sync=False
    )
    assert result is None

    pending_dir = write_time._pending_dir()
    markers = [f for f in os.listdir(pending_dir) if f.endswith(".json")]
    assert len(markers) == 1


def test_queue_full_falls_to_marker(tmp_palinode_dir, tmp_memory_file, sample_item):
    """When the asyncio queue is full, new jobs land on disk instead of blocking."""

    async def run():
        # Fill the queue
        queue = write_time._get_queue()
        for _ in range(queue.maxsize):
            queue.put_nowait({"file_path": tmp_memory_file, "item": sample_item})

        assert queue.full()

        # Now schedule one more — should fall through to a marker
        result = write_time.schedule_contradiction_check(
            tmp_memory_file, sample_item, sync=False
        )
        assert result is None

        pending_dir = write_time._pending_dir()
        markers = [f for f in os.listdir(pending_dir) if f.endswith(".json")]
        assert len(markers) == 1, f"Expected 1 marker, found {markers}"

    asyncio.run(run())


# ── Sync path tests ────────────────────────────────────────────────────────
#
# These run the REAL `_run_check_and_apply` — the one function joining
# `_translate_ops` to `apply_operations` — rather than patching it out
# wholesale. Prior versions of this test patched `write_time._run_check_and_apply`
# itself, which meant the join was never exercised anywhere in the suite;
# see tests/test_proposer_seam.py's docstring for why wholesale-mocking the
# propose seam is inadequate. Only the LLM call (`llm_fn`) and the
# infra-boundary calls it makes (embedder, vector search) are faked — the real
# translate -> apply_operations -> file mutation pipeline runs end to end.


def _seed_contradiction_check_files(
    tmp_dir: str,
    *,
    fact_id: str = "f-old",
    fact_text: str = "Old fact needing update.",
    target_rel: str = os.path.join("decisions", "target.md"),
) -> str:
    """Seed the update.md prompt (required for _check_contradictions to reach
    its LLM call instead of short-circuiting to ADD) plus a target memory file
    with one tagged fact the executor can match against. Returns the target's
    absolute path."""
    prompts_dir = os.path.join(tmp_dir, "specs", "prompts")
    os.makedirs(prompts_dir, exist_ok=True)
    with open(os.path.join(prompts_dir, "update.md"), "w") as f:
        f.write("Return the operation as JSON.\n")

    target = os.path.join(tmp_dir, target_rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w") as f:
        f.write(
            "---\nid: decision-test\ncategory: decision\n---\n\n"
            f"- [2026-06-01] {fact_text} <!-- fact:{fact_id} -->\n"
        )
    return target


def _fake_llm(op_json: str):
    """A fake propose seam returning fixed op-JSON, regardless of the prompts."""
    def _fn(system_prompt: str, user_prompt: str) -> tuple[str, str]:
        return op_json, "fake-model"
    return _fn


def test_sync_path_applies_real_write_time_delete_end_to_end(
    tmp_palinode_dir, sample_item
):
    """A write-time DELETE, driven through the real pipeline with a fake
    llm_fn, produces a visible supersession in the target file. `embedder.embed`
    and `store.search_internal` are faked at the infra boundary (network
    embedder, vector DB) — everything downstream of the LLM call (parse,
    translate, apply_operations, file write) is real."""
    target = _seed_contradiction_check_files(tmp_palinode_dir)
    ops_json = json.dumps(
        {
            "operation": "DELETE",
            "target_id": "f-old",
            "reason": "contradicted by new save",
        }
    )

    with patch("palinode.consolidation.runner.embedder.embed", return_value=[0.1] * 8), \
            patch(
                "palinode.consolidation.runner.store.search_internal",
                return_value=[{"id": "f-old", "content": "Old fact needing update."}],
            ):
        result = write_time.schedule_contradiction_check(
            target, sample_item, sync=True, llm_fn=_fake_llm(ops_json)
        )

    assert result is not None
    assert result["applied_stats"]["superseded"] == 1
    body = open(target).read()
    # The old fact is tombstoned (struck through), not silently dropped —
    # the visible supersession the executor's SUPERSEDE guard used to
    # discard for lack of `new_text`.
    assert "~~" in body
    assert "[superseded" in body
    # The replacement text comes from the item that superseded it (the
    # `_check_contradictions` per-item loop overwrites `operation["item"]`
    # with the item actually being checked — see runner._check_contradictions).
    assert sample_item["content"] in body


def test_sync_path_swallows_check_errors(tmp_palinode_dir, sample_item):
    """Errors in the real pipeline never propagate to the save caller — the
    ADR-004 save-never-fails invariant — exercised via a genuine failure at
    the embedder boundary rather than a mock of _run_check_and_apply itself."""
    target = _seed_contradiction_check_files(tmp_palinode_dir)

    def _boom(text, backend="local"):
        raise RuntimeError("LLM exploded")

    with patch("palinode.consolidation.runner.embedder.embed", side_effect=_boom):
        # Must not raise
        result = write_time.schedule_contradiction_check(
            target, sample_item, sync=True
        )
        assert result is None  # error path returns None, save continues


# ── Op translation tests ───────────────────────────────────────────────────


def test_translate_ops_filters_ops_without_target_id():
    """Ops without a target_id can't be applied deterministically → filtered out."""
    ops = [
        {"operation": "UPDATE", "item": {"content": "new"}},  # no target_id
        {"operation": "UPDATE", "item": {"content": "new"}, "target_id": "f1", "new_text": "x"},
    ]
    translated = write_time._translate_ops(ops, "/tmp/fake.md")
    assert len(translated) == 1
    assert translated[0]["op"] == "UPDATE"
    assert translated[0]["id"] == "f1"


def test_translate_ops_delete_becomes_supersede():
    """DELETE from _check_contradictions maps to SUPERSEDE (we never delete).

    The executor's SUPERSEDE guard (executor.py) requires ``new_text`` to be
    truthy or the op is dropped with no mutation, no stats increment, and no
    log line — assert the translator actually produces it, not just the
    id/superseded_by shape the prior version of this test pinned.
    """
    ops = [
        {
            "operation": "DELETE",
            "item": {"id": "decision-new", "content": "The new content that superseded it."},
            "target_id": "f-old",
            "reason": "contradicted by new",
        }
    ]
    translated = write_time._translate_ops(ops, "/tmp/fake.md")
    assert len(translated) == 1
    op = translated[0]
    assert op["op"] == "SUPERSEDE"
    assert op["id"] == "f-old"
    assert op["superseded_by"] == "decision-new"
    # Falls back to the superseding item's content when the op carries no
    # explicit new_text of its own.
    assert op["new_text"] == "The new content that superseded it."


def test_translate_ops_delete_prefers_explicit_new_text_over_item_content():
    """An explicit ``new_text`` on the op wins over the item's content."""
    ops = [
        {
            "operation": "DELETE",
            "item": {"id": "decision-new", "content": "item content, should be ignored"},
            "new_text": "explicit replacement text",
            "target_id": "f-old",
        }
    ]
    translated = write_time._translate_ops(ops, "/tmp/fake.md")
    assert translated[0]["new_text"] == "explicit replacement text"
