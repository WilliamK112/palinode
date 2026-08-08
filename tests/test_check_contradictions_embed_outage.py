"""Regression: consolidation's per-item dedup check must degrade, not abort a
whole project's run, when the embedder is unavailable.

``runner._check_contradictions`` used to rely on the embedder's old silent
contract (a falsy ``[]`` return meant "skip dedup, just ADD"). Now that
``embed()`` raises ``EmbeddingUnavailable`` instead, the same degrade must be
reached via an explicit catch — otherwise one backend hiccup would abort the
whole per-item loop and, by extension, the project's consolidation for that
run (caught only at ``run_consolidation``'s per-project try/except, which
skips the *entire* project rather than just the one item that failed to
embed).

Real SQLite is not needed here — a failed embed must never reach
``store.search_internal`` at all, which the spy on that call asserts.
"""
from __future__ import annotations

from unittest.mock import patch

from palinode.consolidation import runner
from palinode.core.config import config
from palinode.core.embedder import EmbeddingUnavailable


def _seed_update_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    prompts_dir = tmp_path / "specs" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "update.md").write_text("Return the operation as JSON.\n")


def _boom(text, backend="local"):
    raise EmbeddingUnavailable(
        backend="local", model="bge-m3", text_len=len(text),
        cause="connection refused",
    )


def test_check_contradictions_degrades_to_add_on_embed_outage(tmp_path, monkeypatch):
    _seed_update_prompt(tmp_path, monkeypatch)
    item = {"content": "a new fact", "category": "insights"}

    with patch("palinode.consolidation.runner.embedder.embed", side_effect=_boom), \
            patch("palinode.consolidation.runner.store.search_internal") as search_spy:
        ops = runner._check_contradictions([item], "proj-x")

    assert ops == [{"operation": "ADD", "item": item}]
    search_spy.assert_not_called()


def test_check_contradictions_continues_past_a_failed_item(tmp_path, monkeypatch):
    """One item's embed outage must not stop the loop from evaluating the rest."""
    _seed_update_prompt(tmp_path, monkeypatch)
    items = [
        {"content": "fails to embed", "category": "insights"},
        {"content": "embeds fine", "category": "insights"},
    ]

    def _selective(text, backend="local"):
        if text == "fails to embed":
            raise EmbeddingUnavailable(
                backend="local", model="bge-m3", text_len=len(text),
                cause="connection refused",
            )
        return [0.1] * 8

    with patch("palinode.consolidation.runner.embedder.embed", side_effect=_selective), \
            patch("palinode.consolidation.runner.store.search_internal", return_value=[]) as search_spy:
        ops = runner._check_contradictions(items, "proj-x")

    assert ops == [
        {"operation": "ADD", "item": items[0]},
        {"operation": "ADD", "item": items[1]},
    ]
    # Only the second item's vector reached search_internal.
    search_spy.assert_called_once()
