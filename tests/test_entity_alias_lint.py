"""the entity-canonicalization work: detect entity refs that look like aliases of one another.

Detection only. The check must never merge, and these tests pin that boundary as
hard as they pin the detection itself — a wrong join is unrecoverable from the
merged data, whereas a split is merely invisible until someone looks.

NOTE: every ref here is SYNTHETIC. Real entity refs in the live store are actual
people's names, and the content scrub does not catch collaborator surnames
(verified 2026-07-21), so fixtures must never be drawn from real data.
"""

from __future__ import annotations

from palinode.core.lint import _alias_key, _is_token_prefix, check_entity_aliases


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


def test_a_longer_ref_seen_nowhere_else_stays_high() -> None:
    """A straggler spelling: the shape of a genuine short/full split."""
    clusters = check_entity_aliases({"person/bravo": 81, "person/bravo-charlie": 8})
    assert _conf(clusters, ["person/bravo", "person/bravo-charlie"]) == "high"


def test_a_lone_straggler_survives_the_demotion() -> None:
    """The demotion asks about the LONGER form; it must not hide a stray SHORT one.

    Observed on real data: a 1-file `x` beside an established `x-mcp` was demoted
    purely because `x-mcp` is established — hiding a real one-line merge.
    """
    clusters = check_entity_aliases({
        "project/echo": 1,             # <- lone straggler, one category
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
