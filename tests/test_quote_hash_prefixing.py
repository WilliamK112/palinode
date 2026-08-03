"""Algorithm-prefixed quote hashes, and backward compatibility with bare MD5.

Palinode <= 0.9.8 emitted a bare MD5 hex digest for ``quote_hash``. An
unprefixed digest carries no record of which algorithm produced it, so the
store could never move off MD5 without invalidating every existing anchor.

The fix prefixes the algorithm (``sha256:<hex>``) and makes every comparison
recompute under the *stored* hash's algorithm. The regression this guards
against is severe and silent: comparing a legacy bare-MD5 anchor against a
freshly computed SHA-256 digest reports ANCHOR_TAMPERED — i.e. every existing
citation in every existing store would suddenly claim it had been altered.
"""
from __future__ import annotations

import hashlib

import pytest

from palinode.core.claims import ClaimError, normalize_claims
from palinode.core.hashing import stable_md5_hexdigest
from palinode.core.quote_verify import (
    DEFAULT_QUOTE_HASH_ALGORITHM,
    QuoteStatus,
    UnsupportedHashAlgorithm,
    normalize_quote,
    parse_quote_hash,
    quote_hash,
    quote_hash_matches,
    verify_quote,
)

QUOTE = "list prices for insulin products rose 11% between 2024 and 2026"
SOURCE = f"The report found that {QUOTE}, outpacing general medical inflation."


def legacy_md5(quote: str) -> str:
    """Reproduce exactly what <= 0.9.8 wrote: a bare, unprefixed MD5 digest."""
    return stable_md5_hexdigest(normalize_quote(quote))


# --- emission ------------------------------------------------------------


def test_quote_hash_is_algorithm_prefixed():
    h = quote_hash(QUOTE)
    assert h.startswith("sha256:")
    algorithm, digest = h.split(":", 1)
    assert algorithm == DEFAULT_QUOTE_HASH_ALGORITHM
    assert digest == hashlib.sha256(normalize_quote(QUOTE).encode()).hexdigest()


def test_quote_hash_honours_explicit_algorithm():
    assert quote_hash(QUOTE, "md5") == f"md5:{legacy_md5(QUOTE)}"


def test_quote_hash_rejects_unknown_algorithm():
    with pytest.raises(UnsupportedHashAlgorithm):
        quote_hash(QUOTE, "crc32")


def test_quote_hash_is_normalization_stable():
    """Cosmetic differences must not change the digest."""
    assert quote_hash("the “price”  rose") == quote_hash('the "price" rose')


# --- parsing -------------------------------------------------------------


@pytest.mark.parametrize(
    "stored,expected_algorithm",
    [
        ("sha256:" + "a" * 64, "sha256"),
        ("md5:" + "b" * 32, "md5"),
        ("SHA256:" + "C" * 64, "sha256"),  # case-insensitive
    ],
)
def test_parse_quote_hash_prefixed(stored, expected_algorithm):
    algorithm, digest = parse_quote_hash(stored)
    assert algorithm == expected_algorithm
    assert digest == digest.lower()


def test_parse_quote_hash_bare_digest_is_md5():
    """The compatibility rule: unprefixed means MD5, because that is what we wrote."""
    algorithm, digest = parse_quote_hash(legacy_md5(QUOTE))
    assert algorithm == "md5"
    assert digest == legacy_md5(QUOTE)


@pytest.mark.parametrize("bad", ["crc32:deadbeef", "not-a-hash", "sha1:" + "a" * 40, ""])
def test_parse_quote_hash_rejects_unusable(bad):
    with pytest.raises(UnsupportedHashAlgorithm):
        parse_quote_hash(bad)


# --- matching ------------------------------------------------------------


def test_matches_legacy_bare_md5():
    """THE regression test. A pre-0.9.9 anchor must still match its quote."""
    assert quote_hash_matches(QUOTE, legacy_md5(QUOTE)) is True


def test_matches_new_prefixed_sha256():
    assert quote_hash_matches(QUOTE, quote_hash(QUOTE)) is True


def test_matches_prefixed_md5():
    assert quote_hash_matches(QUOTE, quote_hash(QUOTE, "md5")) is True


def test_does_not_match_wrong_quote():
    assert quote_hash_matches("a different quote entirely", quote_hash(QUOTE)) is False


def test_legacy_digest_is_not_naively_compared_to_default():
    """Guards the specific bug: the two forms differ as strings but both verify.

    If someone reverts to `quote_hash(q) == stored`, this test still passes
    trivially on the inequality but the verify_quote test below will fail —
    which is why both exist.
    """
    assert legacy_md5(QUOTE) != quote_hash(QUOTE)
    assert quote_hash_matches(QUOTE, legacy_md5(QUOTE))


# --- verification --------------------------------------------------------


def test_verify_legacy_anchor_reports_ok_not_tampered():
    """The silent-corruption case: an untouched legacy store must verify clean."""
    result = verify_quote(QUOTE, legacy_md5(QUOTE), SOURCE, ref="sources/x.md")
    assert result.status is QuoteStatus.OK, result.message
    assert result.ok


def test_verify_new_anchor_reports_ok():
    result = verify_quote(QUOTE, quote_hash(QUOTE), SOURCE, ref="sources/x.md")
    assert result.status is QuoteStatus.OK


def test_verify_still_detects_real_tampering_under_legacy_algorithm():
    """Backward compatibility must not become a hole that hides tampering."""
    result = verify_quote(QUOTE, legacy_md5("some other text"), SOURCE)
    assert result.status is QuoteStatus.ANCHOR_TAMPERED


def test_verify_still_detects_source_drift_with_legacy_anchor():
    """A legacy anchor that is internally consistent still catches source drift."""
    result = verify_quote(QUOTE, legacy_md5(QUOTE), "the source says something else")
    assert result.status is QuoteStatus.SOURCE_DRIFTED


def test_verify_reports_actual_hash_in_stored_algorithm():
    """Both halves of the result must be comparable — same algorithm."""
    result = verify_quote(QUOTE, legacy_md5("mismatch"), SOURCE)
    assert result.status is QuoteStatus.ANCHOR_TAMPERED
    assert result.actual_hash.startswith("md5:")


def test_verify_unusable_algorithm_is_tampered_not_ok():
    result = verify_quote(QUOTE, "crc32:deadbeef", SOURCE)
    assert result.status is QuoteStatus.ANCHOR_TAMPERED
    assert "crc32" in result.message


def test_verify_without_expected_hash_still_checks_source():
    """No stored hash means only the drift axis is checkable."""
    assert verify_quote(QUOTE, "", SOURCE).status is QuoteStatus.OK
    assert verify_quote(QUOTE, "", "unrelated").status is QuoteStatus.SOURCE_DRIFTED


# --- claims save path ----------------------------------------------------


def _claim(quote_hash_value=None):
    span = {"quote": QUOTE}
    if quote_hash_value is not None:
        span["quote_hash"] = quote_hash_value
    return [{"text": "prices rose", "source_id": "sources/kff.md", "span": span}]


def test_claims_accept_legacy_supplied_hash_and_upgrade_it():
    """A legacy anchor round-trips, and is stored in the canonical prefixed form."""
    out = normalize_claims(_claim(legacy_md5(QUOTE)), "research/x.md")
    assert out[0]["span"]["quote_hash"] == quote_hash(QUOTE)
    assert out[0]["span"]["quote_hash"].startswith("sha256:")


def test_claims_accept_new_supplied_hash():
    out = normalize_claims(_claim(quote_hash(QUOTE)), "research/x.md")
    assert out[0]["span"]["quote_hash"] == quote_hash(QUOTE)


def test_claims_compute_hash_when_absent():
    out = normalize_claims(_claim(), "research/x.md")
    assert out[0]["span"]["quote_hash"].startswith("sha256:")


def test_claims_still_reject_inconsistent_anchor():
    with pytest.raises(ClaimError, match="does not match its quote"):
        normalize_claims(_claim(legacy_md5("something else")), "research/x.md")


def test_claims_reject_unusable_algorithm():
    with pytest.raises(ClaimError, match="crc32"):
        normalize_claims(_claim("crc32:deadbeef"), "research/x.md")


# --- bare-digest resolution by length (0.10.0) -----------------------------


def test_bare_64_hex_resolves_as_sha256_not_md5():
    """A bare 64-hex digest CANNOT be MD5. Resolving it as MD5 guarantees a
    false anchor_tampered; resolution is by length (32=md5, 64=sha256)."""
    bare_sha = hashlib.sha256(normalize_quote(QUOTE).encode()).hexdigest()
    algorithm, digest = parse_quote_hash(bare_sha)
    assert algorithm == "sha256"
    assert digest == bare_sha
    # And it verifies clean end to end.
    assert quote_hash_matches(QUOTE, bare_sha) is True
    assert verify_quote(QUOTE, bare_sha, SOURCE).status is QuoteStatus.OK


@pytest.mark.parametrize("length", [8, 31, 33, 40, 63, 65, 128])
def test_bare_digest_of_implausible_length_is_rejected(length):
    """No supported algorithm produces these lengths; guessing is worse than
    erroring. 40 hex (SHA-1) is deliberately rejected, not guessed."""
    with pytest.raises(UnsupportedHashAlgorithm, match="matches no supported"):
        parse_quote_hash("a" * length)


def test_verify_reports_tampered_not_crash_on_implausible_bare_length():
    result = verify_quote(QUOTE, "deadbeef", SOURCE)
    assert result.status is QuoteStatus.ANCHOR_TAMPERED
    assert "matches no supported" in result.message


# --- partial flag (0.10.0) -------------------------------------------------


def test_hashless_ok_is_flagged_partial():
    """No stored hash: only drift was checkable. `ok` must say so."""
    result = verify_quote(QUOTE, "", SOURCE)
    assert result.status is QuoteStatus.OK
    assert result.partial is True


def test_hashless_drift_is_flagged_partial():
    result = verify_quote(QUOTE, "", "the source says something else")
    assert result.status is QuoteStatus.SOURCE_DRIFTED
    assert result.partial is True


def test_full_verification_is_not_partial():
    assert verify_quote(QUOTE, quote_hash(QUOTE), SOURCE).partial is False
    assert verify_quote(QUOTE, legacy_md5(QUOTE), SOURCE).partial is False


def test_resolve_memory_claims_surfaces_span_partial(tmp_path):
    """The claims read-surface must carry the partial flag through."""
    from palinode.core.claims import resolve_memory_claims

    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "kff.md").write_text(SOURCE, encoding="utf-8")
    mem = tmp_path / "research"
    mem.mkdir()
    (mem / "note.md").write_text(
        "---\n"
        "claims:\n"
        "  - text: prices rose\n"
        "    source_id: sources/kff.md\n"
        "    span:\n"
        f"      quote: \"{QUOTE}\"\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    resolved = resolve_memory_claims("research/note.md", str(tmp_path))
    assert len(resolved) == 1
    assert resolved[0]["span_status"] == "ok"
    assert resolved[0]["span_partial"] is True  # parsed claim had no stored hash
