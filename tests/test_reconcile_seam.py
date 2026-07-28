"""The write-path reconcile seam — derive / plan / apply (#717, #698, #699).

Real SQLite on tmp_path (project rule: no DB mocks); the embedder is the only
patched dependency, since these tests are about reconciliation, not embedding.
The autouse ``_warm_embed_gate`` fixture (conftest) forces embedding mode, so a
patched ``embedder.embed`` supplies vectors.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from palinode.core import store
from palinode.core.config import config
from palinode.indexer import reconcile

_VEC = [0.03] * 1024


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    return tmp_path


def _doc(entities: list[str], body: str = "A fact.", status: str = "active") -> str:
    ent = "".join(f"- {e}\n" for e in entities)
    return (
        "---\n"
        "id: proj-x\n"
        "category: projects\n"
        f"status: {status}\n"
        "entities:\n" + ent +
        "---\n\n"
        f"# X\n\n{body}\n"
    )


def _entity_rows(file_path: str) -> set[str]:
    db = store.get_db()
    rows = db.execute(
        "SELECT entity_ref FROM entities WHERE file_path = ?", (file_path,)
    ).fetchall()
    db.close()
    return {r["entity_ref"] for r in rows}


def _reconcile(path: str, content: str):
    with patch("palinode.core.embedder.embed", return_value=_VEC):
        return reconcile.reconcile(path, content)


# ── stage 1: derive is pure ───────────────────────────────────────────────────


def test_derive_is_pure_and_deterministic():
    content = _doc(["person/alice"])
    a = reconcile.derive("/m/x.md", content)
    b = reconcile.derive("/m/x.md", content)
    assert a == b


def test_derive_reads_entities_verbatim_no_canonicalization():
    # A body wikilink and a raw frontmatter ref must NOT be merged/canonicalized
    # by the write path — canonicalization is deliberately out of scope here.
    content = _doc(["person/alice"], body="Mentions [[Bob Jones]] in the body.")
    state = reconcile.derive("/m/x.md", content)
    assert state.entities == ("person/alice",)


# ── stage 2: plan diffs against the DB ─────────────────────────────────────────


def test_plan_is_noop_on_an_unchanged_file(tmp_store):
    path = str(tmp_store / "projects" / "x.md")
    (tmp_store / "projects").mkdir()
    content = _doc(["person/alice"])
    _reconcile(path, content)

    p = reconcile.plan(reconcile.derive(path, content))
    assert p.is_noop


def test_plan_frontmatter_only_edit_is_meta_only_not_reembed(tmp_store):
    """#698: a frontmatter-only change is seen, and does not re-embed."""
    (tmp_store / "projects").mkdir()
    path = str(tmp_store / "projects" / "x.md")
    _reconcile(path, _doc(["person/alice"], status="active"))

    # Flip a frontmatter field; the body is byte-identical.
    p = reconcile.plan(reconcile.derive(path, _doc(["person/alice"], status="archived")))
    assert p.meta_only and not p.to_index
    assert not p.is_noop


def test_plan_flags_entity_change(tmp_store):
    (tmp_store / "projects").mkdir()
    path = str(tmp_store / "projects" / "x.md")
    _reconcile(path, _doc(["person/alice"]))

    p = reconcile.plan(reconcile.derive(path, _doc(["person/alice-smith"])))
    assert p.entities_changed


# ── stage 3: apply reconciles, deletion included ──────────────────────────────


def test_changed_entity_ref_replaces_rather_than_orphans(tmp_store):
    """#699: correcting a ref must not leave the old row behind."""
    (tmp_store / "projects").mkdir()
    path = str(tmp_store / "projects" / "x.md")
    _reconcile(path, _doc(["person/alice"]))
    assert _entity_rows(path) == {"person/alice"}

    _reconcile(path, _doc(["person/alice-smith"]))
    assert _entity_rows(path) == {"person/alice-smith"}, (
        "the old ref must be gone, not accumulated"
    )


def test_removing_all_entities_clears_the_rows(tmp_store):
    """#699 corollary: the old upsert early-returned on [], deleting nothing."""
    (tmp_store / "projects").mkdir()
    path = str(tmp_store / "projects" / "x.md")
    _reconcile(path, _doc(["person/alice"]))
    assert _entity_rows(path) == {"person/alice"}

    _reconcile(path, _doc([]))
    assert _entity_rows(path) == set()


def test_frontmatter_only_edit_refreshes_cached_metadata_without_reembed(tmp_store):
    """#698 end to end: the status flip reaches chunks.metadata, no embed call."""
    (tmp_store / "projects").mkdir()
    path = str(tmp_store / "projects" / "x.md")
    _reconcile(path, _doc(["person/alice"], status="active"))

    with patch("palinode.core.embedder.embed") as embed_spy:
        diff = reconcile.reconcile(path, _doc(["person/alice"], status="archived"))
    embed_spy.assert_not_called()
    assert diff.meta_updated >= 1 and diff.written == 0

    db = store.get_db()
    import json
    metas = [
        json.loads(r["metadata"])
        for r in db.execute(
            "SELECT metadata FROM chunks WHERE file_path = ?", (path,)
        ).fetchall()
    ]
    db.close()
    assert all(m.get("status") == "archived" for m in metas)


def test_reconcile_is_idempotent(tmp_store):
    (tmp_store / "projects").mkdir()
    path = str(tmp_store / "projects" / "x.md")
    content = _doc(["person/alice"])
    _reconcile(path, content)

    second = _reconcile(path, content)
    assert second.committed
    assert second.written == 0 and second.reembedded == 0
    assert second.deleted == 0 and second.meta_updated == 0
    assert not second.entities_replaced


def test_stale_section_chunk_is_pruned(tmp_store):
    """A section that disappears from the file leaves no chunk behind."""
    (tmp_store / "projects").mkdir()
    path = str(tmp_store / "projects" / "x.md")
    long = "word " * 500  # over the single-chunk threshold, forces h2 split
    two = (
        "---\nid: proj-x\ncategory: projects\nentities:\n- person/alice\n---\n\n"
        f"## Alpha\n\n{long}\n\n## Beta\n\n{long}\n"
    )
    _reconcile(path, two)
    db = store.get_db()
    n_before = db.execute(
        "SELECT count(*) FROM chunks WHERE file_path = ?", (path,)
    ).fetchone()[0]
    db.close()
    assert n_before == 2

    one = (
        "---\nid: proj-x\ncategory: projects\nentities:\n- person/alice\n---\n\n"
        f"## Alpha\n\n{long}\n"
    )
    diff = _reconcile(path, one)
    assert diff.deleted == 1
    db = store.get_db()
    n_after = db.execute(
        "SELECT count(*) FROM chunks WHERE file_path = ?", (path,)
    ).fetchone()[0]
    db.close()
    assert n_after == 1
