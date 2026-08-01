"""``layer_hint: history`` must move the body to the history layer, not drop it.

``split_file`` writes the identity layer back over the *source* path, so a hint
that routes the body nowhere does not merely misfile it — it overwrites the
original file with an empty shell. Nothing else holds a copy: the file is not
committed by the split, and reindexing an emptied file prunes its chunks from
the store. These tests pin the body's survival at the only layer that can still
observe it, the files on disk.

Fixture sizing: each fixture carries four ``##`` sections whose headings
deliberately straddle the identity/status keyword lists, so that

* the hint is proven to be what routed the body (heuristic classification would
  have split these sections across two layers — asserted directly in
  ``test_history_hint_is_what_routes_the_body``), and
* a single-section document cannot make the assertions vacuous.
"""
from __future__ import annotations

import os

import pytest
import yaml

from palinode.consolidation import layer_split


# Four sections, split across identity keywords ("architecture", "key decisions")
# and status keywords ("current", "superseded" -> falls to the date heuristic).
BODY = """## Architecture

The retired ingest pipeline used a three-stage fan-out: collector, normalizer,
and writer. Each stage owned its own queue and back-pressure signal.

## Key Decisions

We chose at-least-once delivery over exactly-once because the writer was
idempotent on content hash, and the coordination cost of exactly-once dwarfed
the duplicate-write cost we measured.

## Current Status

Decommissioned 2026-02-14. Traffic moved to the v2 path.

## Superseded Facts

The claim that the normalizer was CPU-bound was wrong; profiling on 2026-01-30
showed 78% of wall time in the DNS resolver.
"""

# Substrings unique to each section — asserted individually so a partial loss
# (one section dropped) cannot pass on a total-length check.
MARKERS = [
    "three-stage fan-out",
    "at-least-once delivery",
    "Decommissioned 2026-02-14",
    "DNS resolver",
]


def _write_source(tmp_path, hint: str | None, body: str = BODY) -> str:
    """Write a core memory file, optionally carrying ``layer_hint``."""
    meta = "id: projects-legacy-ingest\ncategory: projects\ncore: true\n"
    if hint is not None:
        meta += f"layer_hint: {hint}\n"
    path = tmp_path / "legacy-ingest.md"
    path.write_text(f"---\n{meta}---\n\n{body}")
    return str(path)


def _body_of(path: str) -> str:
    """The markdown body of a memory file, frontmatter stripped."""
    content = open(path).read()
    if content.startswith("---"):
        return content.split("---", 2)[2].strip()
    return content.strip()


def _frontmatter(path: str) -> dict:
    content = open(path).read()
    assert content.startswith("---"), f"{path} has no frontmatter"
    return yaml.safe_load(content.split("---", 2)[1]) or {}


def _all_text_on_disk(directory) -> str:
    """Every byte of every markdown file in ``directory``, concatenated."""
    return "".join(
        open(os.path.join(directory, n)).read()
        for n in sorted(os.listdir(directory))
        if n.endswith(".md")
    )


def test_fixture_preconditions():
    """The fixture is big enough and varied enough for the assertions to bite."""
    assert BODY.count("\n## ") + BODY.startswith("## ") == 4, "expected 4 sections"
    assert len(BODY) > 500, "fixture must be more than a stub"
    for marker in MARKERS:
        assert BODY.count(marker) == 1, f"{marker!r} must be unique to one section"


def test_history_hint_preserves_the_body(tmp_path):
    """The regression: ``layer_hint: history`` must not discard the file body."""
    src = _write_source(tmp_path, "history")

    results = layer_split.split_file(src)

    assert "history" in results, "history layer file should have been written"
    history_body = _body_of(results["history"])
    for marker in MARKERS:
        assert marker in history_body, f"{marker!r} lost from the history layer"

    # And nothing escaped to a layer it does not belong in.
    assert _body_of(results["identity"]) == "", "hinted body must not stay in identity"
    assert "status" not in results


def test_history_hint_loses_nothing_anywhere_on_disk(tmp_path):
    """Whole-directory sweep: no section text vanishes from the tree."""
    src = _write_source(tmp_path, "history")

    layer_split.split_file(src)

    on_disk = _all_text_on_disk(tmp_path)
    for marker in MARKERS:
        assert marker in on_disk, f"{marker!r} vanished from every file on disk"


def test_history_hint_is_what_routes_the_body(tmp_path):
    """Without the hint, the heuristics split this body across two layers.

    Guards the test itself: proves the fixture is not one the classifier would
    have dumped into the history layer anyway, so the passing assertions above
    are attributable to the hint.
    """
    src = _write_source(tmp_path, None)

    results = layer_split.split_file(src)

    assert "status" in results, "unhinted fixture should produce a status layer"
    assert _body_of(results["identity"]), "unhinted fixture should produce identity"
    # The seeded history layer is the placeholder, holding none of the content.
    for marker in MARKERS:
        assert marker not in _body_of(results["history"])


def test_history_layer_frontmatter_is_well_formed(tmp_path):
    """The written history layer is a valid, correctly-typed memory file."""
    src = _write_source(tmp_path, "history")

    results = layer_split.split_file(src)

    fm = _frontmatter(results["history"])
    assert fm["layer"] == "history"
    assert fm["core"] is False, "history must stay out of core injection"
    assert fm["parent"] == "projects-legacy-ingest"
    assert fm["id"] == "projects-legacy-ingest-history"
    assert fm["category"] == "projects"


def test_history_hint_appends_and_never_clobbers(tmp_path):
    """A pre-existing history file keeps its content when new material arrives.

    A history file accumulates archived material. Overwriting it would be the
    same silent loss in a different place, so this pins append semantics.
    """
    src = _write_source(tmp_path, "history")
    history_path = str(tmp_path / "legacy-ingest-history.md")

    # Seed a history file the way a prior split or an executor ARCHIVE would.
    open(history_path, "w").write(
        "---\ncategory: projects\ncore: false\nlayer: history\n"
        "status: archived\n---\n\n# legacy-ingest — History\n\n"
        "- [2026-01-05 09:00] earlier archived fact <!-- fact:abc123 -->\n"
    )

    results = layer_split.split_file(src)

    history_text = open(results["history"]).read()
    assert "earlier archived fact" in history_text, "pre-existing history was clobbered"
    assert "fact:abc123" in history_text, "pre-existing audit marker was clobbered"
    for marker in MARKERS:
        assert marker in history_text, f"{marker!r} not appended to history"
    # Frontmatter written by another producer survives untouched.
    assert _frontmatter(results["history"])["status"] == "archived"


def test_re_splitting_a_history_hinted_file_is_idempotent(tmp_path):
    """Running the split twice must not duplicate the body into history."""
    src = _write_source(tmp_path, "history")

    first = layer_split.split_file(src)
    after_first = open(first["history"]).read()

    # The hint is sticky frontmatter, so the second pass takes the same branch
    # over a now-empty body.
    assert _frontmatter(src)["layer_hint"] == "history"
    second = layer_split.split_file(src)

    assert "history" not in second, "empty body should not rewrite the history file"
    assert open(str(tmp_path / "legacy-ingest-history.md")).read() == after_first
    for marker in MARKERS:
        assert after_first.count(marker) == 1, f"{marker!r} duplicated on re-split"


@pytest.mark.parametrize("hint", ["identity", "status"])
def test_other_hints_still_preserve_the_body(tmp_path, hint):
    """The two hints that already worked keep working."""
    src = _write_source(tmp_path, hint)

    results = layer_split.split_file(src)

    assert hint in results
    body = _body_of(results[hint])
    for marker in MARKERS:
        assert marker in body, f"{marker!r} lost from the {hint} layer"
