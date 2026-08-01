"""A reciprocal ``contradicts`` back-link must reach the index, not just disk.

``contradicts`` is symmetric: when A declares it contradicts B, B's frontmatter
gains A. That write lands on a file whose own save has already finished, so
nothing in the originating request reindexes it — the back-link sat in the file
and never in ``chunks.metadata``.

That is invisible in the worst way. The file on disk is correct, so an author
inspecting it sees the link; only recall and ``lint`` — the surfaces the
reciprocal write exists to serve — disagree. It converged wherever the file
watcher happened to be running and silently did not anywhere it was not.

The assertion is on the DB, deliberately. A test that read the file back would
have passed against the bug.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from palinode.core import store, typed_links
from palinode.core.config import config
from palinode.indexer import reconcile

_VEC = [0.03] * 1024
_PAD = "Filler sentence to clear the single-chunk threshold. " * 20


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    (tmp_path / "insights").mkdir()
    return tmp_path


def _doc(ident: str) -> str:
    return f"---\nid: {ident}\ncategory: insights\nstatus: active\n---\n\n# {ident}\n\nBody of {ident}.\n{_PAD}\n"


def _chunk_metadata(path: str) -> list[dict]:
    db = store.get_db()
    rows = db.execute(
        "SELECT metadata FROM chunks WHERE file_path = ?", (path,)
    ).fetchall()
    db.close()
    return [json.loads(r["metadata"]) for r in rows]


def test_back_link_reaches_chunks_metadata(mem):
    target = str(mem / "insights" / "beta.md")
    with open(target, "w") as f:
        f.write(_doc("beta"))

    with patch("palinode.core.embedder.embed", return_value=_VEC):
        reconcile.reconcile(target, _doc("beta"))
        assert all("contradicts" not in m for m in _chunk_metadata(target))

        modified = typed_links.add_reciprocal_contradicts(
            str(mem), "insights/alpha", ["insights/beta"], commit=False
        )

    assert modified == [target], modified
    metas = _chunk_metadata(target)
    assert metas, "target lost its chunks entirely"
    assert all("insights/alpha" in (m.get("contradicts") or []) for m in metas), (
        f"back-link never reached the index: {metas}"
    )


def test_back_link_does_not_re_embed(mem):
    """The body is byte-identical — only frontmatter moved — so this must take
    the metadata-only path. If it re-embeds, every back-link costs an embedder
    round trip on a file whose content did not change."""
    target = str(mem / "insights" / "beta.md")
    with open(target, "w") as f:
        f.write(_doc("beta"))

    with patch("palinode.core.embedder.embed", return_value=_VEC):
        reconcile.reconcile(target, _doc("beta"))

    with patch("palinode.core.embedder.embed", return_value=_VEC) as embed_spy:
        typed_links.add_reciprocal_contradicts(
            str(mem), "insights/alpha", ["insights/beta"], commit=False
        )
    embed_spy.assert_not_called()


def test_reindex_failure_never_fails_the_back_link(mem):
    """A back-link is a courtesy to the originating save and must not be able to
    break it — the whole function is best-effort by contract."""
    target = str(mem / "insights" / "beta.md")
    with open(target, "w") as f:
        f.write(_doc("beta"))

    with patch("palinode.core.embedder.embed", return_value=_VEC):
        reconcile.reconcile(target, _doc("beta"))

    with patch("palinode.indexer.index_file.index_file", side_effect=RuntimeError("boom")):
        modified = typed_links.add_reciprocal_contradicts(
            str(mem), "insights/alpha", ["insights/beta"], commit=False
        )

    # The disk write still happened and was still reported.
    assert modified == [target]
    assert "insights/alpha" in open(target).read()


def test_idempotent_second_call_is_a_noop(mem):
    target = str(mem / "insights" / "beta.md")
    with open(target, "w") as f:
        f.write(_doc("beta"))

    with patch("palinode.core.embedder.embed", return_value=_VEC):
        reconcile.reconcile(target, _doc("beta"))
        typed_links.add_reciprocal_contradicts(
            str(mem), "insights/alpha", ["insights/beta"], commit=False
        )
        again = typed_links.add_reciprocal_contradicts(
            str(mem), "insights/alpha", ["insights/beta"], commit=False
        )
    assert again == [], "second call rewrote an already-linked target"
