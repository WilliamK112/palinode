"""Freshness annotation on search results.

Every fixture supplies the hash through the ``content_hash`` **column**, which
is the only valid comparand. These tests originally passed it through
``metadata`` instead — the frontmatter fallback — with a hand-computed
per-section hash. That combination does not occur in production: the
frontmatter field holds a hash of the whole submitted request body, not of one
section, so the fallback compared two different domains and reported ``stale``
unconditionally for every row with a NULL column. The tests hid it by feeding
the fallback data it would never really receive.

"""
import hashlib
from palinode.core.store import check_freshness
from palinode.core import parser as _parser
from palinode.core.config import config

def test_fresh_result_marked_valid(tmp_path, monkeypatch):
    """File unchanged since indexing → freshness: valid"""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    content = "---\nid: test\n---\nHello"
    file_path = "test_valid.md"
    full_path = tmp_path / file_path
    full_path.write_text(content)

    # Hash the body only (below frontmatter), matching what check_freshness does.
    # Truncated to 16 chars deliberately: the comparison still supports legacy
    # short hashes, and that path deserves to stay covered.
    body_hash = hashlib.sha256("Hello".encode()).hexdigest()[:16]
    results = [{"file_path": file_path, "content_hash": body_hash}]

    checked = check_freshness(results)
    assert checked[0]["freshness"] == "valid"

def test_modified_file_marked_stale(tmp_path, monkeypatch):
    """File changed after indexing → freshness: stale"""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    content = "---\n---\nHello"
    file_path = "test_stale.md"
    full_path = tmp_path / file_path
    full_path.write_text(content)
    
    db_hash = "wrong1234567890a"
    results = [{"file_path": file_path, "content_hash": db_hash}]
    
    checked = check_freshness(results)
    assert checked[0]["freshness"] == "stale"

def test_missing_hash_marked_unknown(tmp_path, monkeypatch):
    """Old memories without content_hash → freshness: unknown"""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    content = "---\n---\nHello"
    file_path = "test_unknown.md"
    full_path = tmp_path / file_path
    full_path.write_text(content)
    
    results = [{"file_path": file_path, "metadata": {}}] # No content_hash
    checked = check_freshness(results)
    assert checked[0]["freshness"] == "unknown"

def test_deleted_file_marked_stale(tmp_path, monkeypatch):
    """Source file deleted → freshness: stale"""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    file_path = "test_deleted.md"
    # Do not create file
    results = [{"file_path": file_path, "content_hash": "somehash"}]
    checked = check_freshness(results)
    assert checked[0]["freshness"] == "stale"

def test_freshness_parses_each_file_once_regardless_of_result_count(tmp_path, monkeypatch):
    """Work is proportional to distinct *files*, not to result count.

    This replaces a ``duration < 0.05`` wall-clock assertion. That threshold was a
    proxy for an algorithmic regression — something turning 100 checks into 100 x N
    work — but it also measured how busy the machine was, so it passed in isolation
    and on CI and failed under full-suite load. The cost of that was never the flake
    itself; it was the adjudication, since a red timing assertion cannot tell a
    contributor whether they caused it.

    Worse, the old fixture could not detect the regression it was guarding: 100
    results across 100 *distinct* files parse 100 times with or without
    ``check_freshness``'s ``_sections_cache``. The cache only pays off when several
    chunks share a file, which is the normal case for a multi-section memory — so
    that is the shape asserted here, by counting parses instead of timing them.
    """
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))

    FILE_COUNT = 10
    RESULT_COUNT = 100
    content = _make_multisection_content()

    # Hash each section exactly as the indexer does, by parsing the real file —
    # a wrong hash here would report `stale` and skip nothing, quietly making the
    # parse count meaningless.
    chunks = []
    for i in range(FILE_COUNT):
        file_path = f"test_perf_{i}.md"
        (tmp_path / file_path).write_text(content)
        _, sections = _parser.parse_markdown(content)
        for section in sections:
            chunks.append({
                "file_path": file_path,
                "section_id": section["section_id"],
                "content_hash": hashlib.sha256(section["content"].encode()).hexdigest(),
            })

    assert len(chunks) > FILE_COUNT, "fixture must put several chunks in each file"

    # Cycle the chunks up to RESULT_COUNT so results repeatedly revisit the same
    # files — without repeats the cache is never exercised.
    results = [dict(chunks[i % len(chunks)]) for i in range(RESULT_COUNT)]

    parse_calls = []
    real_parse = _parser.parse_markdown

    def counting_parse(raw):
        parse_calls.append(raw)
        return real_parse(raw)

    monkeypatch.setattr(_parser, "parse_markdown", counting_parse)

    checked = check_freshness(results)

    assert len(checked) == RESULT_COUNT
    assert all(r["freshness"] == "valid" for r in checked), (
        "every chunk must verify, or the parse count below proves nothing"
    )
    # The invariant: one parse per distinct file. Dropping the cache makes this
    # RESULT_COUNT; any per-result re-read makes it worse.
    assert len(parse_calls) == FILE_COUNT, (
        f"expected {FILE_COUNT} parses (one per file), got {len(parse_calls)} "
        f"for {RESULT_COUNT} results — the per-file section cache is not holding"
    )


# ---------------------------------------------------------------------------
# Issue multi-section files must compare per-section hashes
# ---------------------------------------------------------------------------

def _make_multisection_content() -> str:
    """Return a >2000-char markdown body with two named sections so the parser
    splits it into multiple chunks instead of keeping it as a single root."""
    # Use >2000 chars total body to force section splitting.
    filler = "x" * 800
    return (
        "---\n"
        "id: multi-section-test\n"
        "category: insights\n"
        "type: Insight\n"
        "---\n\n"
        f"Preamble text that is long enough to form its own root chunk. {filler}\n\n"
        f"## Section Alpha\n\nContent of alpha section. {filler}\n\n"
        f"## Section Beta\n\nContent of beta section. {filler}\n"
    )


def test_multisection_fresh_file_marked_valid(tmp_path, monkeypatch):
    """Multi-section file just indexed → every chunk must report freshness: valid.

    Pre-fix behaviour: check_freshness hashed the whole body and compared
    it to the per-section content_hash → mismatch → all chunks stale.
    """
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    content = _make_multisection_content()
    file_path = "multi_fresh.md"
    full_path = tmp_path / file_path
    full_path.write_text(content)

    # Parse the file the same way the indexer does so we get the real section
    # content strings and can compute the expected per-section hashes.
    _, sections = _parser.parse_markdown(content)
    assert len(sections) > 1, "test file did not split into multiple sections"

    results = [
        {
            "file_path": file_path,
            "section_id": sec["section_id"],
            "content_hash": hashlib.sha256(sec["content"].encode()).hexdigest(),
        }
        for sec in sections
    ]

    checked = check_freshness(results)
    for r in checked:
        assert r["freshness"] == "valid", (
            f"section {r['section_id']!r} reported {r['freshness']!r} but should be valid (#203)"
        )


def test_multisection_modified_section_marked_stale(tmp_path, monkeypatch):
    """Modifying one section of a multi-section file → that chunk reports stale,
    unmodified chunks remain valid."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    content = _make_multisection_content()
    file_path = "multi_modify.md"
    full_path = tmp_path / file_path
    full_path.write_text(content)

    _, sections = _parser.parse_markdown(content)
    assert len(sections) > 1

    # Compute hashes from the *original* content (simulates what the indexer stored).
    original_hashes = {
        sec["section_id"]: hashlib.sha256(sec["content"].encode()).hexdigest()
        for sec in sections
    }

    # Modify the file on disk — change Section Alpha's content.
    modified_content = content.replace(
        "Content of alpha section.", "Content of alpha section MODIFIED."
    )
    full_path.write_text(modified_content)

    results = [
        {
            "file_path": file_path,
            "section_id": sec_id,
            "content_hash": orig_hash,
        }
        for sec_id, orig_hash in original_hashes.items()
    ]

    checked = check_freshness(results)
    freshness_by_id = {r["section_id"]: r["freshness"] for r in checked}

    # The alpha section was changed — it must be stale.
    alpha_slug = next(
        sec["section_id"] for sec in sections if "alpha" in sec["section_id"].lower()
    )
    assert freshness_by_id[alpha_slug] == "stale", (
        f"Modified section {alpha_slug!r} should be stale but got {freshness_by_id[alpha_slug]!r}"
    )

    # Unmodified sections should still be valid.
    for sec in sections:
        sid = sec["section_id"]
        if sid != alpha_slug:
            assert freshness_by_id[sid] == "valid", (
                f"Unmodified section {sid!r} should be valid but got {freshness_by_id[sid]!r}"
            )
