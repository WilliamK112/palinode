"""Envelope-markup guard on the save path (the save-side half of the tool-envelope validation).

`session_end` got this guard in the session-end envelope rejection. Save is the other
write path the same corruption enters through, but the strongest of the three detection
signals does NOT transfer, and these tests pin that asymmetry so a later
"simplification" can't quietly reintroduce it.

Session-end rejects on *co-occurrence*: envelope markup present AND the
`decisions`/`blockers` arrays absent. That is near-zero-false-positive there
because a real /wrap summary essentially always carries those arrays, so their
absence is anomalous. Every one of save's arrays (`entities`, `sources`,
`claims`, `contradicts`, `backed_by`) is optional and absent on most honest
calls — so on save, "no arrays arrived" is the *norm*, and treating it as a
signature would collapse the guard into the blanket vocabulary ban the tool-envelope validation
explicitly forbids. Save therefore relies on the unmatched-tag and
trailing-fragment signals only.

The non-rejection tests below are the load-bearing half: each one is a save that
would 400 under a naive lift of session-end's semantics.
"""
from __future__ import annotations

import glob
import os

import pytest
from click.testing import CliRunner
from fastapi import HTTPException
from unittest.mock import patch

from palinode.api.server import SaveRequest, save_api
from palinode.cli.save import save as save_cmd
from palinode.core.config import config
from palinode.core.envelope import envelope_complaint

import importlib

cli_save_mod = importlib.import_module("palinode.cli.save")


@pytest.fixture
def mock_memory_dir(tmp_path):
    old_memory_dir = config.memory_dir
    old_auto_commit = config.git.auto_commit
    config.memory_dir = str(tmp_path)
    config.git.auto_commit = False
    try:
        yield str(tmp_path)
    finally:
        config.memory_dir = old_memory_dir
        config.git.auto_commit = old_auto_commit


def _save(content: str, **kwargs):
    """Call the save route with the store's content scan stubbed out."""
    with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
        return save_api(SaveRequest(content=content, type="Insight", **kwargs))


def _written_files(memory_dir: str) -> list[str]:
    return glob.glob(os.path.join(memory_dir, "**", "*.md"), recursive=True)


# ── Rejections: the two signals save actually has ────────────────────────────


def test_unmatched_closing_tag_is_rejected(mock_memory_dir):
    """Signal 2. A closing tag with no opener is structurally not prose."""
    with pytest.raises(HTTPException) as exc:
        _save("Shipped the parser rewrite</parameter>\n</invoke>")

    assert exc.value.status_code == 400
    detail = exc.value.detail
    assert "content" in detail, detail
    assert "</parameter>" in detail or "</invoke>" in detail, detail


def test_opening_tag_at_the_tail_is_rejected(mock_memory_dir):
    """Signal 3. Absorption lands the envelope at the very end of the value —
    here a well-formed opener, so signal 2 cannot be what catches it."""
    with pytest.raises(HTTPException) as exc:
        _save('Notes on the retrieval bug\n<invoke name="palinode_save">')

    assert exc.value.status_code == 400
    assert "<invoke" in exc.value.detail, exc.value.detail


def test_harness_markup_at_the_tail_is_rejected(mock_memory_dir):
    """The live-store case from the tool-envelope validation (`Topic: <command-message>…`), which
    arrives through save the same way it arrived through session-end. The tags
    are matched, so only the tail signal fires."""
    with pytest.raises(HTTPException) as exc:
        _save("Auto-captured session. Topic: "
              "<command-message>palinode-session</command-message>")

    assert exc.value.status_code == 400
    assert "command-message" in exc.value.detail, exc.value.detail


def test_rejection_writes_nothing(mock_memory_dir):
    """Fail loud *before* the write, not after — a rejected save must not leave
    a partial file or a git commit behind."""
    with pytest.raises(HTTPException):
        _save("Shipped the rewrite</decisions>\n</invoke>")

    assert _written_files(mock_memory_dir) == []


def test_rejection_does_not_give_session_end_advice(mock_memory_dir):
    """The remediation sentence is per-surface. Telling a save caller to
    're-send with decisions/blockers as real JSON arrays' would be nonsense —
    save has no such parameters."""
    with pytest.raises(HTTPException) as exc:
        _save("Shipped the rewrite</invoke>")

    detail = exc.value.detail
    assert "decisions" not in detail and "blockers" not in detail, detail
    assert "`content`" in detail, detail
    assert "fenced code block" in detail, detail


# ── Non-rejections: what a naive lift of session-end's gate would break ──────


def test_details_summary_block_is_saveable(mock_memory_dir):
    """`<summary>` is in the vocabulary because it is a session-end parameter
    name — but it is also a standard HTML element that appears in ordinary
    markdown notes. Matched and mid-content, it is content."""
    res = _save(
        "Findings from the sweep.\n\n"
        "<details><summary>Full output</summary>\n\n"
        "the run took 4m12s\n\n</details>\n\n"
        "Conclusion: the index was stale."
    )
    assert res["file_path"]


def test_note_about_tool_call_syntax_is_saveable(mock_memory_dir):
    """the tool-envelope validation's own constraint: the investigation that produced this guard has to
    stay saveable. Fenced code is the escape hatch."""
    res = _save(
        "The absorption bug leaves the envelope tail in the string param:\n\n"
        "```\n"
        '<parameter name="summary">real text</parameter>\n'
        "</invoke>\n"
        "```\n\n"
        "Detection keys on the unmatched closing tag."
    )
    assert res["file_path"]


def test_absent_arrays_are_not_treated_as_an_absorption_signature(mock_memory_dir):
    """The core of the save-side envelope validation. This save has no `entities`, no `sources`, no
    `claims` — the ordinary shape — and matched envelope vocabulary mid-content.
    Under session-end's co-occurrence gate that combination is a hard reject;
    on save it must pass, because absent arrays mean nothing here."""
    res = _save(
        "Reviewed the <summary>section</summary> handling in the parser "
        "and it round-trips cleanly."
    )
    assert res["file_path"]


def test_arrays_present_does_not_change_the_verdict(mock_memory_dir):
    """Corollary: since save never consults its arrays, supplying them cannot
    make a passing save fail or a failing save pass."""
    content = 'Notes on the retrieval bug\n<invoke name="palinode_save">'
    with pytest.raises(HTTPException):
        _save(content, entities=["project/palinode"])


# ── The asymmetry, at the guard itself ───────────────────────────────────────


def test_same_text_rejects_for_session_end_and_passes_for_save():
    """One string, two verdicts, and the difference is exactly the signal the
    caller is entitled to claim."""
    text = "Reviewed the <summary>section</summary> handling in the parser."

    assert envelope_complaint(text, "content") is None
    assert envelope_complaint(
        text, "summary", missing_params=("decisions", "blockers")
    ) is not None


# ── CLI exit codes: `click.Abort()` was constructed, never raised ────────────


def test_cli_save_exits_nonzero_when_the_save_fails(monkeypatch):
    """`palinode save … && echo ok` must not print ok for a save that never
    happened. The failure handler built a `click.Abort()` and dropped it."""
    def fake_save(*args, **kwargs):
        raise RuntimeError("API refused: envelope markup in `content`")

    monkeypatch.setattr(cli_save_mod.api_client, "save", fake_save)
    result = CliRunner().invoke(save_cmd, ["Body", "--type", "Insight"])

    assert result.exit_code != 0, result.output
    assert "Error saving memory" in result.output


def test_cli_save_exits_nonzero_on_missing_type(monkeypatch):
    """Same defect, argument-validation half: six of the seven abort sites in
    cli/save.py returned instead of raising."""
    def fake_save(*args, **kwargs):
        raise AssertionError("save should not be called")

    monkeypatch.setattr(cli_save_mod.api_client, "save", fake_save)
    result = CliRunner().invoke(save_cmd, ["Body"])

    assert result.exit_code != 0, result.output


def test_cli_save_exits_nonzero_on_bad_metadata_json(monkeypatch):
    def fake_save(*args, **kwargs):
        raise AssertionError("save should not be called")

    monkeypatch.setattr(cli_save_mod.api_client, "save", fake_save)
    result = CliRunner().invoke(
        save_cmd, ["Body", "--type", "Insight", "--metadata-json", "not json"]
    )

    assert result.exit_code != 0, result.output
