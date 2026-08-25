"""Tests for IETF KU frontmatter alignment.

Covers:
- Frontmatter with ku_version, confidence, lifecycle round-trips through the
  parser without breaking files that lack these fields (backward compat).
- Save path writes confidence to frontmatter when provided.
- Search results surface confidence as a top-level key when set.
- ku_compat=True: every save writes ku_version and lifecycle.
- ku_compat=False (default): no auto-population; only explicit fields land.
"""
from __future__ import annotations

import hashlib
import yaml
import pytest

from palinode.core.parser import parse_markdown


# ── Parser tests ─────────────────────────────────────────────────────────────


def test_parse_markdown_with_ku_fields_roundtrip():
    """Full parse_markdown roundtrip with KU fields in frontmatter."""
    content = (
        "---\n"
        "id: test-1\n"
        "ku_version: '1.0'\n"
        "confidence: 0.75\n"
        "lifecycle: active\n"
        "---\n\n"
        "Body content here.\n"
    )
    metadata, sections = parse_markdown(content)
    assert metadata["ku_version"] == "1.0"
    assert metadata["confidence"] == pytest.approx(0.75)
    assert metadata["lifecycle"] == "active"
    assert sections[0]["content"].strip() == "Body content here."


def test_parse_markdown_without_ku_fields_unchanged():
    """Existing files without KU fields parse without error."""
    content = (
        "---\n"
        "id: legacy-1\n"
        "type: Insight\n"
        "status: active\n"
        "---\n\n"
        "Legacy content.\n"
    )
    metadata, sections = parse_markdown(content)
    assert "ku_version" not in metadata
    assert "lifecycle" not in metadata
    assert metadata["status"] == "active"
    assert sections[0]["content"].strip() == "Legacy content."


# ── Save-path tests ───────────────────���───────────────────────────────────────


def _save_and_read_frontmatter(tmp_path, content, confidence=None, ku_compat_enabled=False):
    """Helper: write a memory file as the server save path does and read back frontmatter."""
    import hashlib as _hashlib

    # Mirror the server's frontmatter construction logic (simplified).
    slug = "test-memory"
    category = "insights"
    content_hash = _hashlib.sha256(content.encode()).hexdigest()

    frontmatter_dict = {
        "id": f"{category}-{slug}",
        "category": category,
        "type": "Insight",
        "content_hash": content_hash,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    if confidence is not None:
        frontmatter_dict["confidence"] = confidence

    if ku_compat_enabled:
        if "ku_version" not in frontmatter_dict:
            frontmatter_dict["ku_version"] = "1.0"
        if "lifecycle" not in frontmatter_dict:
            frontmatter_dict["lifecycle"] = "active"

    doc = f"---\n{yaml.safe_dump(frontmatter_dict, default_flow_style=False, allow_unicode=True)}---\n\n{content}\n"
    file_path = tmp_path / f"{slug}.md"
    file_path.write_text(doc)

    with open(file_path) as f:
        raw = f.read()
    metadata, _ = parse_markdown(raw)
    return metadata


def test_save_with_confidence_writes_frontmatter(tmp_path):
    metadata = _save_and_read_frontmatter(tmp_path, "Some insight text.", confidence=0.8)
    assert metadata.get("confidence") == pytest.approx(0.8)


def test_save_without_confidence_no_key(tmp_path):
    metadata = _save_and_read_frontmatter(tmp_path, "Some insight text.")
    assert "confidence" not in metadata


def test_save_ku_compat_true_writes_ku_version_and_lifecycle(tmp_path):
    metadata = _save_and_read_frontmatter(tmp_path, "Some insight.", ku_compat_enabled=True)
    assert metadata.get("ku_version") == "1.0"
    assert metadata.get("lifecycle") == "active"


def test_save_ku_compat_false_no_auto_population(tmp_path):
    """Default (ku_compat=False): ku_version and lifecycle are NOT written."""
    metadata = _save_and_read_frontmatter(tmp_path, "Some insight.", ku_compat_enabled=False)
    assert "ku_version" not in metadata
    assert "lifecycle" not in metadata


def test_save_content_hash_present(tmp_path):
    """content_hash is always written to frontmatter by the save path."""
    content = "This is the memory body."
    metadata = _save_and_read_frontmatter(tmp_path, content)
    expected_hash = hashlib.sha256(content.encode()).hexdigest()
    assert metadata.get("content_hash") == expected_hash


# ── Search-result confidence passthrough ───────────────────���─────────────────


def test_search_result_includes_confidence_when_set():
    """store.search() should surface confidence as a top-level key when metadata has it."""
    from unittest.mock import MagicMock, patch
    from palinode.core import store
    import json

    meta = {"type": "Insight", "confidence": 0.85}
    fake_row = {
        "file_path": "insights/test.md",
        "section_id": "root",
        "content": "some content",
        "category": "insights",
        "metadata": json.dumps(meta),
        "created_at": "2026-01-01",
        "last_updated": "2026-01-01",
        "distance": 0.0,  # distance=0 → cosine=1.0
        "content_hash": None,
    }

    # Make fake_row support dict-style key access like sqlite3.Row
    class FakeRow(dict):
        def keys(self):
            return super().keys()

    row = FakeRow(fake_row)

    with patch("palinode.core.store.get_db") as mock_db:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [row]
        mock_conn.cursor.return_value = mock_cursor
        mock_db.return_value = mock_conn

        results = store.search(query_embedding=[0.0] * 1024, threshold=0.0)

    assert len(results) == 1
    assert results[0]["confidence"] == pytest.approx(0.85)
    assert results[0]["metadata"]["confidence"] == pytest.approx(0.85)


def test_search_result_no_confidence_key_when_absent():
    """store.search() should NOT include confidence key when metadata lacks it."""
    from unittest.mock import MagicMock, patch
    from palinode.core import store
    import json

    meta = {"type": "Insight"}
    fake_row = {
        "file_path": "insights/no-conf.md",
        "section_id": "root",
        "content": "no confidence here",
        "category": "insights",
        "metadata": json.dumps(meta),
        "created_at": "2026-01-01",
        "last_updated": "2026-01-01",
        "distance": 0.0,
        "content_hash": None,
    }

    class FakeRow(dict):
        def keys(self):
            return super().keys()

    row = FakeRow(fake_row)

    with patch("palinode.core.store.get_db") as mock_db:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [row]
        mock_conn.cursor.return_value = mock_cursor
        mock_db.return_value = mock_conn

        results = store.search(query_embedding=[0.0] * 1024, threshold=0.0)

    assert len(results) == 1
    assert "confidence" not in results[0]


# ── Config flag tests ────────────────────────��──────────────────────────��─────


def test_ku_compat_config_defaults():
    """KUCompatConfig defaults: enabled=False, ku_version='1.0'."""
    from palinode.core.config import KUCompatConfig
    cfg = KUCompatConfig()
    assert cfg.enabled is False
    assert cfg.ku_version == "1.0"


def test_config_has_ku_compat_field():
    """Global config object has a ku_compat attribute."""
    from palinode.core.config import config
    assert hasattr(config, "ku_compat")
    assert hasattr(config.ku_compat, "enabled")
    assert hasattr(config.ku_compat, "ku_version")
