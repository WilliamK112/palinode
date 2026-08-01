"""the entity-canonicalization work: detect entity refs that look like aliases of one another.

Detection only. The check must never merge, and these tests pin that boundary as
hard as they pin the detection itself — a wrong join is unrecoverable from the
merged data, whereas a split is merely invisible until someone looks.

NOTE: every ref here is SYNTHETIC. Real entity refs in the live store are actual
people's names, and the content scrub does not catch collaborator surnames
(verified 2026-07-21), so fixtures must never be drawn from real data.
"""

from __future__ import annotations

import pytest

from palinode.core.lint import (
    _alias_key,
    _is_stray_short_form,
    _is_token_prefix,
    check_entity_aliases,
)


def _kinds(clusters):
    return {c["kind"] for c in clusters}


def _refs(clusters, kind):
    for c in clusters:
        if c["kind"] == kind:
            return [r["ref"] for r in c["refs"]]
    return []


# --- the primitives ---------------------------------------------------------


def test_alias_key_ignores_case_and_separators() -> None:
    assert _alias_key("alpha-bravo") == _alias_key("Alpha_Bravo") == "alphabravo"
    assert _alias_key("alpha") != _alias_key("alphabravo")


def test_token_prefix_requires_a_boundary() -> None:
    """`alpha` vs `alpha-bravo` is a short/full split; `alpha` vs `alpine` is not."""
    assert _is_token_prefix("alpha", "alpha-bravo") is True
    assert _is_token_prefix("alpha", "alpha_bravo") is True
    assert _is_token_prefix("alpha", "alphabravo") is False, "mid-token: a different name"
    assert _is_token_prefix("alpha", "alpine") is False
    assert _is_token_prefix("alpha", "alpha") is False, "identical is not a prefix pair"


# --- the observed shapes ----------------------------------------------------


def test_detects_the_three_way_split_shape() -> None:
    """The shape the entity-canonicalization work measured: short form, full form, and a concatenation.

    A near-even split, not a long tail — looking up either form returns a
    plausible, non-empty, incomplete answer.
    """
    clusters = check_entity_aliases({
        "person/alpha": 176,
        "person/alpha-bravo": 129,
        "person/alphabravo": 1,
    })
    assert _kinds(clusters) == {"separator", "prefix"}
    # separator: the two that differ only by a hyphen
    assert set(_refs(clusters, "separator")) == {"person/alpha-bravo", "person/alphabravo"}
    # prefix: all three belong to the same candidate cluster
    assert len(_refs(clusters, "prefix")) == 3


def test_refs_are_ordered_by_file_count() -> None:
    """The operator needs the shape of the split, so the biggest ref leads."""
    clusters = check_entity_aliases({"person/a-b": 2, "person/ab": 90})
    assert [r["files"] for r in clusters[0]["refs"]] == [90, 2]


# --- what it must NOT flag --------------------------------------------------


def test_different_categories_are_not_aliases() -> None:
    """`person/alpha` and `project/alpha` are different namespaces."""
    assert check_entity_aliases({"person/alpha": 5, "project/alpha": 5}) == []


def test_similar_looking_but_distinct_names_are_not_flagged() -> None:
    """Shared letters are not evidence. This is why edit distance isn't used."""
    assert check_entity_aliases({"person/alpha": 5, "person/alpine": 5}) == []
    assert check_entity_aliases({"person/charlie": 5, "person/charlotte": 5}) == []


def test_unsplittable_refs_are_ignored() -> None:
    """A bare ref with no `category/name` shape has nothing to compare within."""
    assert check_entity_aliases({"alpha": 5, "bravo": 5}) == []


def test_a_clean_store_reports_nothing() -> None:
    assert check_entity_aliases({}) == []
    assert check_entity_aliases({"person/alpha": 5, "person/bravo": 5}) == []


# --- the boundary that matters ----------------------------------------------


def test_output_is_advisory_only_and_never_prescribes_a_merge() -> None:
    """The report hands the operator a question, not an instruction.

    Guards the design constraint: detect -> propose -> human confirms. If a
    future change starts emitting a canonical/winner field, this fails loudly.
    """
    clusters = check_entity_aliases({"person/alpha": 176, "person/alpha-bravo": 129})
    assert clusters
    for c in clusters:
        assert set(c) == {"kind", "confidence", "category", "refs", "detail"}
        # `confidence` ranks the question; it must never become a verdict.
        assert c["confidence"] in {"high", "low"}
        for forbidden in ("canonical", "merge_into", "winner", "replace_with", "action"):
            assert forbidden not in c, f"{forbidden!r} would turn a question into an order"


def test_check_is_pure_and_does_not_touch_the_store() -> None:
    """Takes a ref->count mapping and returns findings. No DB, no filesystem."""
    refs = {"person/alpha": 1, "person/alpha-bravo": 1}
    before = dict(refs)
    check_entity_aliases(refs)
    assert refs == before, "input must not be mutated"


# --- confidence tiers: the store's own usage as evidence ---------------------


def _conf(clusters, refs_subset):
    """Confidence of the cluster containing all of `refs_subset`."""
    for c in clusters:
        got = {r["ref"] for r in c["refs"]}
        if set(refs_subset) <= got:
            return c["confidence"]
    return None


def test_a_longer_ref_used_across_categories_is_demoted() -> None:
    """Two established entries, not one split.

    A name the store references under SEVERAL categories has an identity of its
    own. Flagging it as an alias candidate at full confidence invites exactly the
    merge that must never happen — fusing a project with its own repo.
    """
    clusters = check_entity_aliases({
        "project/alpha": 466,
        "project/alpha-dev": 19,
        "insight/alpha-dev": 3,        # <- established elsewhere
        "delta/alpha-dev": 21,
    })
    assert _conf(clusters, ["project/alpha", "project/alpha-dev"]) == "low"


def test_a_persons_short_and_full_form_stay_high() -> None:
    """The founding shape: a given name beside the full name, both in real use.

    A person's longer form is nearly always the same person written out, so in a
    category that names people a prefix match is evidence on its own. Nowhere
    else is that true — see the repo-family cases below.
    """
    clusters = check_entity_aliases({"person/bravo": 81, "person/bravo-charlie": 8})
    assert _conf(clusters, ["person/bravo", "person/bravo-charlie"]) == "high"


def test_a_stray_short_form_survives_the_demotion() -> None:
    """The demotion asks about the LONGER form; it must not hide a stray SHORT one.

    Observed on real data: a 1-file `x` beside an established `x-mcp` was demoted
    purely because `x-mcp` is established — hiding a real one-line merge.
    """
    clusters = check_entity_aliases({
        "project/echo": 1,             # <- stray short form, one category
        "project/echo-mcp": 17,
        "service/echo-mcp": 1,         # <- longer form IS established
    })
    assert _conf(clusters, ["project/echo", "project/echo-mcp"]) == "high"


def test_separator_matches_are_always_high_confidence() -> None:
    """Pure spelling variance — no judgement needed, whatever else exists."""
    clusters = check_entity_aliases({
        "project/foxtrot-golf": 36,
        "project/foxtrotgolf": 9,
        "insight/foxtrot-golf": 4,     # established elsewhere; irrelevant here
    })
    assert _conf(clusters, ["project/foxtrot-golf", "project/foxtrotgolf"]) == "high"


def test_high_confidence_clusters_sort_first() -> None:
    """The operator meets the near-certain merges before the probably-distinct ones."""
    clusters = check_entity_aliases({
        "project/alpha": 466, "project/alpha-dev": 19, "insight/alpha-dev": 3,
        "person/bravo": 81, "person/bravo-charlie": 8,
    })
    confidences = [c["confidence"] for c in clusters]
    assert confidences == sorted(confidences, key=lambda x: x != "high")


def test_every_cluster_carries_a_confidence() -> None:
    clusters = check_entity_aliases({"person/hotel": 5, "person/hotel-india": 2})
    assert clusters and all(c["confidence"] in {"high", "low"} for c in clusters)


# --- the labelled set -------------------------------------------------------
#
# Hand-labelled from a curation pass over a live 700-ref store, where a prefix
# match on its own had promoted whole repo families to high confidence. Every
# ref below is SYNTHETIC (see the module docstring); what is real is the SHAPE —
# the file counts and the cross-category presence are those measured.
#
# It holds both directions on purpose. A change that demoted everything would
# clear the wrong-highs and break the genuine splits, and only the second half
# of the set can tell the two apart.

_LABELLED_STORE: dict[str, int] = {
    # (A) Repo/model/milestone families. A sibling adds a qualifier token and
    # lives under exactly one category, so the cross-category demotion has
    # nothing to catch and a bare prefix match used to promote all of these.
    "project/alpha": 528,
    "project/alpha-os": 19,             # sibling repo, well established
    "project/alpha-assistant": 2,       # sibling repo, thin
    "project/bravo": 97,
    "project/bravo-research": 1,        # sibling repo, referenced once
    "project/charlie": 495,
    "project/charlie-public-sync": 6,   # a distinct concern, not a spelling
    "project/charlie-test": 1,
    "project/delta": 72,
    "project/delta-trial": 1,
    "milestone/m9": 1,
    "milestone/m9.1": 1,                # a later milestone, not a spelling
    "model/echo-3": 1,
    "model/echo-3.1-32b": 1,            # a model variant
    "model/foxtrot-coder": 1,
    "model/foxtrot-coder-next": 1,

    # (B) What the cross-category demotion was built for. These were already
    # correct and must not move: the longer form has an identity of its own.
    "project/golf": 495,
    "project/golf-dev": 19,
    "insight/golf-dev": 1,
    "project/hotel": 93,
    "project/hotel-core": 83,
    "insight/hotel-core": 1,
    "project/india-core": 107,
    "project/india": 58,
    "decision/india-core": 1,

    # (C) Genuine splits. Losing these is the way a fix for (A) fails.
    "person/juliett": 160,              # given name and full name, both in use
    "person/juliett-kilo": 95,
    "person/juliettkilo": 1,
    "person/lima": 82,
    "person/lima-mike": 9,
    "project/november": 1,              # stray short form of an anchor
    "project/november-mcp": 17,
    "service/november-mcp": 1,
    "project/oscar": 1,
    "project/oscar-council": 23,
}

# (A) — ranked high before, and every one of them a wrong join.
_WRONG_HIGHS = [
    ["project/alpha", "project/alpha-os"],
    ["project/alpha", "project/alpha-assistant"],
    ["project/bravo", "project/bravo-research"],
    ["project/charlie", "project/charlie-public-sync"],
    ["project/charlie", "project/charlie-test"],
    ["project/delta", "project/delta-trial"],
    ["milestone/m9", "milestone/m9.1"],
    ["model/echo-3", "model/echo-3.1-32b"],
    ["model/foxtrot-coder", "model/foxtrot-coder-next"],
]

# (B) — correctly low before, and still low.
_CORRECT_LOWS = [
    ["project/golf", "project/golf-dev"],
    ["project/hotel", "project/hotel-core"],
    ["project/india", "project/india-core"],
]

# (C) — correctly high before, and still high.
_CORRECT_HIGHS = [
    ["person/juliett", "person/juliett-kilo"],
    ["person/lima", "person/lima-mike"],
    ["project/november", "project/november-mcp"],
    ["project/oscar", "project/oscar-council"],
]


@pytest.mark.parametrize("pair", _WRONG_HIGHS, ids=lambda p: p[1])
def test_family_siblings_are_not_high_confidence_merges(pair) -> None:
    """A qualifier token added to a name is how a FAMILY branches.

    `<repo>` and `<repo>-os` are two repos; `m9` and `m9.1` are two milestones.
    Nothing in the store says so — a sibling lives under one category, so the
    cross-category demotion sees nothing — and a prefix match alone must not
    stand in for the evidence that is missing. `confidence` is what an operator
    sorts and acts on, so a hedge in `detail` does not pay for a wrong high.
    """
    assert _conf(check_entity_aliases(_LABELLED_STORE), pair) == "low"


@pytest.mark.parametrize("pair", _CORRECT_LOWS, ids=lambda p: p[1])
def test_the_cross_category_demotion_still_fires(pair) -> None:
    """The rule this change completes must keep working unchanged."""
    assert _conf(check_entity_aliases(_LABELLED_STORE), pair) == "low"


@pytest.mark.parametrize("pair", _CORRECT_HIGHS, ids=lambda p: p[1])
def test_genuine_splits_stay_high(pair) -> None:
    """Demoting everything would pass the wrong-high tests and gut the check."""
    assert _conf(check_entity_aliases(_LABELLED_STORE), pair) == "high"


def test_the_labelled_set_promotes_nothing_else() -> None:
    """Precision over the whole set, not just the labelled pairs.

    Pins the count as well as the members: a future rule that keeps the four
    genuine splits high while promoting a dozen bystanders is not an
    improvement, and per-pair assertions alone would not notice.
    """
    high = {
        frozenset(r["ref"] for r in c["refs"])
        for c in check_entity_aliases(_LABELLED_STORE)
        if c["confidence"] == "high"
    }
    expected = {frozenset(p) for p in _CORRECT_HIGHS}
    # ...plus the one separator cluster in the set, which needs no judgement.
    expected.add(frozenset({"person/juliett-kilo", "person/juliettkilo"}))
    # The three-ref prefix cluster carries the concatenation too.
    expected.discard(frozenset({"person/juliett", "person/juliett-kilo"}))
    expected.add(frozenset({
        "person/juliett", "person/juliett-kilo", "person/juliettkilo",
    }))
    assert high == expected


# --- the signal that separates them -----------------------------------------


def test_stray_short_form_reads_the_asymmetry_in_one_direction_only() -> None:
    """Which side is thin is the whole signal.

    A rare SHORT form beside an anchor is somebody abbreviating a name the store
    already owns. A rare LONG form beside an anchor is the opposite — adding a
    qualifier is how a new artifact enters the store. Reading the asymmetry
    without its direction promotes the second as eagerly as the first.
    """
    assert _is_stray_short_form(1, 17) is True
    assert _is_stray_short_form(17, 1) is False, "direction is the signal"
    assert _is_stray_short_form(2, 6) is True
    assert _is_stray_short_form(3, 90) is False, "three files is a subject"
    assert _is_stray_short_form(1, 1) is False, "two thin refs, no anchor"
    assert _is_stray_short_form(1, 2) is False, "no anchor to have strayed from"


def test_the_same_shape_is_high_for_a_person_and_low_for_a_repo() -> None:
    """The two populations name themselves by opposite conventions.

    Identical counts, identical structure, opposite verdicts — the category is
    doing the work, and that is the claim this change rests on.
    """
    people = check_entity_aliases({"person/papa": 60, "person/papa-quebec": 12})
    repos = check_entity_aliases({"project/papa": 60, "project/papa-quebec": 12})
    assert _conf(people, ["person/papa", "person/papa-quebec"]) == "high"
    assert _conf(repos, ["project/papa", "project/papa-quebec"]) == "low"


def test_a_thin_short_form_with_its_own_identity_is_not_a_stray() -> None:
    """Thin is not the same as stray.

    A ref used once but referenced under other categories is a subject nobody
    has written up yet, not a slip of the pen — the same evidence the demotion
    reads on the longer form, read on the shorter one.
    """
    clusters = check_entity_aliases({
        "project/romeo": 1,
        "project/romeo-space": 9,
        "insight/romeo": 3,            # <- the short form is its own thing
        "tool/romeo": 1,
    })
    assert _conf(clusters, ["project/romeo", "project/romeo-space"]) == "low"
