"""Duplicate heading slugs must not collide on chunk_id.

A section_id becomes a chunk id via ``stable_md5_hexdigest(f"{path}#{id}")``,
so two headings that slugify identically used to map to one row. The second
overwrote the first under ``ON CONFLICT(id) DO UPDATE``, which had two
consequences: one section was neither vector- nor keyword-searchable, and the
file could never reach a no-op, because ``plan`` compared the first section's
hash against a row holding the second's and rewrote every pass.

Measured at 3% of a 400-file live store, all of them a repeated ``## See also``
— a template pattern, not an authoring mistake.

Fixtures must exceed ~2000 characters. Below that ``parse_markdown`` collapses
the whole document to a single ``root`` section (``core/parser.py``), so a
short fixture silently exercises none of this and passes for the wrong reason.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from palinode.core import parser, store
from palinode.core.config import config
from palinode.indexer import reconcile

_VEC = [0.03] * 1024
_PAD = "Filler sentence to clear the single-chunk threshold. " * 20


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    (tmp_path / "projects").mkdir()
    return tmp_path


def _doc() -> str:
    return (
        "---\nid: d\ncategory: projects\n---\n\n"
        f"## See also\n\nFIRST_MARKER body.\n{_PAD}\n\n"
        f"## Intro\n\nMiddle content.\n{_PAD}\n\n"
        f"## See also\n\nSECOND_MARKER body.\n{_PAD}\n"
    )


def _reconcile(path: str, content: str):
    with patch("palinode.core.embedder.embed", return_value=_VEC):
        return reconcile.reconcile(path, content)


def test_repeated_heading_gets_a_distinct_section_id():
    _, sections = parser.parse_markdown(_doc())
    ids = [s["section_id"] for s in sections]

    assert len(ids) == 3, ids
    assert len(set(ids)) == 3, f"section ids collide: {ids}"
    # The first occurrence keeps its bare slug — a file with no duplicates must
    # produce byte-identical ids and need no reindex.
    assert ids[0] == "see-also", ids
    assert ids[2] == "see-also-2", ids


def test_a_file_without_duplicates_is_unchanged():
    """The disambiguator must be inert on ordinary files, or it churns every
    chunk id in the store for no benefit."""
    content = (
        "---\nid: d\ncategory: projects\n---\n\n"
        f"## Alpha\n\nA.\n{_PAD}\n\n## Beta\n\nB.\n{_PAD}\n"
    )
    _, sections = parser.parse_markdown(content)
    assert [s["section_id"] for s in sections] == ["alpha", "beta"]


def test_both_duplicate_sections_are_indexed(tmp_store):
    """The symptom that matters: content in the second section was invisible to
    recall while the file on disk looked perfectly fine."""
    path = str(tmp_store / "projects" / "dupe.md")
    _reconcile(path, _doc())

    db = store.get_db()
    rows = db.execute(
        "SELECT section_id, content FROM chunks WHERE file_path = ?", (path,)
    ).fetchall()
    db.close()

    assert len(rows) == 3, [r["section_id"] for r in rows]
    bodies = " ".join(r["content"] for r in rows)
    assert "FIRST_MARKER" in bodies
    assert "SECOND_MARKER" in bodies


def test_a_file_with_duplicate_headings_converges(tmp_store):
    """Non-convergence: every pass rewrote, because the stored row for
    `see-also` held the *second* section's hash while `plan` compared it
    against the first's."""
    path = str(tmp_store / "projects" / "dupe.md")
    _reconcile(path, _doc())

    plan = reconcile.plan(reconcile.derive(path, _doc()))
    assert plan.is_noop, (
        f"second pass still writes: to_index={len(plan.to_index)} "
        f"meta_only={len(plan.meta_only)} delete={len(plan.delete_ids)}"
    )


def test_three_way_collision_keeps_going(tmp_store):
    """Two is the case in the wild; the numbering must not stop there."""
    content = (
        "---\nid: d\ncategory: projects\n---\n\n"
        f"## Notes\n\nONE.\n{_PAD}\n\n## Notes\n\nTWO.\n{_PAD}\n\n## Notes\n\nTHREE.\n{_PAD}\n"
    )
    _, sections = parser.parse_markdown(content)
    assert [s["section_id"] for s in sections] == ["notes", "notes-2", "notes-3"]
