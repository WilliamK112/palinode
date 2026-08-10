"""the entity-canonicalization work step 2: the human-curated alias map, resolved at query time.

Detection (`core.lint.check_entity_aliases`) hands a human candidates. This is
what a human does with the answer: writes it down once, and lookups widen.

The property that matters throughout: **the files on disk are never touched.**
A short form and a longer form may be two different people, so a wrong mapping
has to be undoable by deleting one line — not by un-merging data.

All refs here are SYNTHETIC. Real refs in a live store are people's names, and
the content scrub does not catch collaborator surnames.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from palinode.core import aliases


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch):
    """Point the map at a temp dir and clear the cache around every test."""
    monkeypatch.setattr(aliases.config, "memory_dir", str(tmp_path), raising=False)
    aliases.reset_cache()
    yield
    aliases.reset_cache()


def _write(tmp_path: Path, body: str) -> None:
    (tmp_path / aliases.ALIAS_FILENAME).write_text(textwrap.dedent(body), encoding="utf-8")


# --- the default is exact match ---------------------------------------------


def test_no_file_means_exact_match(tmp_path: Path) -> None:
    """Absent map must be indistinguishable from the pre-alias behaviour."""
    assert aliases.resolve("person/alpha") == ["person/alpha"]
    assert aliases.load_alias_map() == {}


def test_empty_file_means_exact_match(tmp_path: Path) -> None:
    _write(tmp_path, "aliases: {}\n")
    assert aliases.resolve("person/alpha") == ["person/alpha"]


def test_a_ref_outside_every_group_is_untouched(tmp_path: Path) -> None:
    _write(tmp_path, """
        aliases:
          person/alpha:
            - person/alpha-bravo
        """)
    assert aliases.resolve("person/charlie") == ["person/charlie"]


# --- resolution is symmetric ------------------------------------------------


def test_any_member_resolves_to_the_whole_group(tmp_path: Path) -> None:
    """Which spelling a caller happens to hold must not change the answer."""
    _write(tmp_path, """
        aliases:
          person/alpha-bravo:
            - person/alpha
            - person/alphabravo
        """)
    expected = {"person/alpha", "person/alpha-bravo", "person/alphabravo"}
    for member in expected:
        assert set(aliases.resolve(member)) == expected
        assert aliases.resolve(member)[0] == member, "the requested ref leads"


def test_resolution_order_is_deterministic(tmp_path: Path) -> None:
    _write(tmp_path, """
        aliases:
          person/alpha:
            - person/alpha-zulu
            - person/alpha-bravo
        """)
    assert aliases.resolve("person/alpha") == [
        "person/alpha", "person/alpha-bravo", "person/alpha-zulu",
    ]


# --- a curated data file must never break lookup ----------------------------


def test_malformed_yaml_falls_back_to_exact_match(tmp_path: Path) -> None:
    _write(tmp_path, "aliases: [unclosed\n")
    assert aliases.resolve("person/alpha") == ["person/alpha"]


def test_wrong_shape_is_skipped_not_raised(tmp_path: Path) -> None:
    _write(tmp_path, """
        aliases:
          person/alpha: 42
          person/bravo:
            - person/bravo-charlie
        """)
    assert aliases.resolve("person/alpha") == ["person/alpha"]
    assert set(aliases.resolve("person/bravo")) == {"person/bravo", "person/bravo-charlie"}


def test_a_single_string_alias_is_accepted(tmp_path: Path) -> None:
    """Curation convenience: one alias needn't be written as a list."""
    _write(tmp_path, """
        aliases:
          person/alpha: person/alpha-bravo
        """)
    assert set(aliases.resolve("person/alpha")) == {"person/alpha", "person/alpha-bravo"}


def test_a_group_of_one_aliases_nothing(tmp_path: Path) -> None:
    _write(tmp_path, """
        aliases:
          person/alpha: []
        """)
    assert aliases.resolve("person/alpha") == ["person/alpha"]


def test_a_ref_in_two_groups_merges_rather_than_picking_one(tmp_path: Path) -> None:
    """A curation mistake with a real consequence: it would make resolution
    order-dependent. Merge and warn instead of silently choosing."""
    _write(tmp_path, """
        aliases:
          person/alpha:
            - person/shared
          person/bravo:
            - person/shared
        """)
    assert set(aliases.resolve("person/shared")) == {
        "person/alpha", "person/bravo", "person/shared",
    }


# --- reversibility ----------------------------------------------------------


def test_removing_the_file_restores_exact_match(tmp_path: Path) -> None:
    """The whole safety argument: a bad mapping is undone by an edit, not a migration."""
    _write(tmp_path, """
        aliases:
          person/alpha:
            - person/alpha-bravo
        """)
    assert len(aliases.resolve("person/alpha")) == 2
    (tmp_path / aliases.ALIAS_FILENAME).unlink()
    aliases.reset_cache()
    assert aliases.resolve("person/alpha") == ["person/alpha"]


def test_an_edit_is_picked_up_without_a_restart(tmp_path: Path) -> None:
    _write(tmp_path, "aliases:\n  person/alpha:\n    - person/alpha-bravo\n")
    assert len(aliases.resolve("person/alpha")) == 2
    import os
    import time
    _write(tmp_path, "aliases: {}\n")
    # nudge mtime so the staleness check fires on fast filesystems
    fp = tmp_path / aliases.ALIAS_FILENAME
    os.utime(fp, (time.time() + 1, time.time() + 1))
    assert aliases.resolve("person/alpha") == ["person/alpha"]


def test_empty_ref_resolves_to_nothing(tmp_path: Path) -> None:
    assert aliases.resolve("") == []


# --- the point of the whole exercise: a real lookup returns the union --------


@pytest.fixture
def store_db(tmp_path, monkeypatch):
    """Real SQLite in tmp_path — no mocking (repo convention)."""
    from palinode.core import store
    from palinode.core.config import config as cfg

    monkeypatch.setattr(cfg, "memory_dir", str(tmp_path))
    monkeypatch.setattr(cfg, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(store, "_db_checked", False)
    store.init_db()
    aliases.reset_cache()
    yield store


def test_a_split_subject_answers_as_one_after_mapping(store_db, tmp_path: Path) -> None:
    """The under-recall this issue is about, closed.

    Three spellings across three files. Before the mapping, asking for any one of
    them returns a plausible, non-empty, INCOMPLETE answer — which is exactly why
    the problem never announces itself.
    """
    store_db.upsert_entities("a.md", {"entities": ["person/alpha"], "category": "people"})
    store_db.upsert_entities("b.md", {"entities": ["person/alpha-bravo"], "category": "people"})
    store_db.upsert_entities("c.md", {"entities": ["person/alphabravo"], "category": "people"})

    # Before: each spelling answers for itself only.
    assert len(store_db.get_entity_files("person/alpha")) == 1

    _write(tmp_path, """
        aliases:
          person/alpha-bravo:
            - person/alpha
            - person/alphabravo
        """)
    aliases.reset_cache()

    # After: any spelling answers for the subject.
    for member in ("person/alpha", "person/alpha-bravo", "person/alphabravo"):
        paths = {f["file_path"] for f in store_db.get_entity_files(member)}
        assert paths == {"a.md", "b.md", "c.md"}, f"{member} under-recalled"


def test_a_file_carrying_two_spellings_is_returned_once(store_db, tmp_path: Path) -> None:
    """Unioning the group must not double-count a file that uses both."""
    store_db.upsert_entities(
        "both.md", {"entities": ["person/alpha", "person/alpha-bravo"], "category": "people"}
    )
    _write(tmp_path, """
        aliases:
          person/alpha:
            - person/alpha-bravo
        """)
    aliases.reset_cache()
    files = store_db.get_entity_files("person/alpha")
    assert [f["file_path"] for f in files] == ["both.md"]


def test_the_files_on_disk_are_never_rewritten(store_db, tmp_path: Path) -> None:
    """The safety property. Aliasing widens lookup; it does not touch data.

    If this ever fails, the reversibility argument for query-time resolution is
    gone and a bad mapping becomes unrecoverable.
    """
    store_db.upsert_entities("a.md", {"entities": ["person/alpha"], "category": "people"})
    _write(tmp_path, """
        aliases:
          person/alpha-bravo:
            - person/alpha
        """)
    aliases.reset_cache()
    store_db.get_entity_files("person/alpha-bravo")

    db = store_db.get_db()
    try:
        rows = [r[0] for r in db.execute(
            "SELECT entity_ref FROM entities WHERE file_path = ?", ("a.md",)
        ).fetchall()]
    finally:
        db.close()
    assert rows == ["person/alpha"], "the stored ref must be exactly what the file declared"


def test_other_spellings_are_not_reported_as_neighbours(store_db, tmp_path: Path) -> None:
    """Once aliased they are the same node, not co-occurring entities."""
    store_db.upsert_entities(
        "a.md",
        {"entities": ["person/alpha", "person/alpha-bravo", "project/delta"],
         "category": "people"},
    )
    _write(tmp_path, """
        aliases:
          person/alpha:
            - person/alpha-bravo
        """)
    aliases.reset_cache()
    graph = store_db.get_entity_graph("person/alpha")
    assert graph["person/alpha"] == ["project/delta"]
