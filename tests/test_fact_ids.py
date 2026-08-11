import os
import tempfile

from palinode.consolidation.fact_ids import generate_fact_id, add_fact_ids_to_file
from palinode.core.config import config

def test_generate_fact_id_deterministic():
    id1 = generate_fact_id("/some/path/my-file.md", "- A fact about something")
    id2 = generate_fact_id("/other/my-file.md", "- A fact about something")
    assert id1 == id2
    assert id1.startswith("my-file-")

def test_add_fact_ids_to_file(tmp_path, monkeypatch):
    # write_memory_file validates its target resolves inside config.memory_dir.
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", False)

    content = """---
title: simple test
---

- List item 1
* List item 2
- List item 3 <!-- fact:existing-123 -->
  - Nested item 1
  * Nested item 2
"""
    fd, path = tempfile.mkstemp(suffix=".md", dir=str(tmp_path))
    with os.fdopen(fd, 'w') as f:
        f.write(content)

    try:
        count = add_fact_ids_to_file(path)
        assert count == 4 # Should tag item 1, item 2, nested 1, nested 2
        
        with open(path) as f:
            new_content = f.read()
            
        assert "List item 1 <!-- fact:" in new_content
        assert "List item 2 <!-- fact:" in new_content
        assert "List item 3 <!-- fact:existing-123 -->" in new_content
        assert "  - Nested item 1 <!-- fact:" in new_content
        assert "  * Nested item 2 <!-- fact:" in new_content
        
        # Test idempotence (skip existing)
        count2 = add_fact_ids_to_file(path)
        assert count2 == 0
    finally:
        os.remove(path)
