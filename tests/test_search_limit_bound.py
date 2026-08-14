"""Tests for the /search `limit` bound and the sqlite-vec KNN clamp.

Two separate defenses, deliberately not the same mechanism:

* ``SearchRequest.limit`` carries an edge bound so an absurd value is a 422
  naming the constraint rather than a silent clamp.
* ``store.search`` clamps ``k`` at ``VEC_KNN_MAX_K`` so the KNN query stays
  legal no matter how many multipliers the callers stacked on the way down.

The clamp is the half that actually prevents the HTTP 500, because the request
limit is not the number that reaches sqlite-vec — see ``VEC_KNN_MAX_K``.
"""
import pytest
from pydantic import ValidationError

from palinode.core import store
from palinode.core.config import config
from palinode.api.server import SearchRequest


# ---- the edge bound ------------------------------------------------------


def test_default_limit_is_accepted():
    assert SearchRequest(query="x").limit == config.search.default_limit


def test_limit_at_the_bound_is_accepted():
    assert SearchRequest(query="x", limit=config.search.max_limit).limit == (
        config.search.max_limit
    )


def test_limit_past_the_bound_is_rejected():
    with pytest.raises(ValidationError) as exc:
        SearchRequest(query="x", limit=config.search.max_limit + 1)
    # The message must name the bound — a 422 that does not say what the
    # ceiling is just moves the guessing game.
    assert str(config.search.max_limit) in str(exc.value)


def test_limit_999_is_accepted():
    """The exact value from the original bug report.

    999 used to reach sqlite-vec as k=5994 and raise OperationalError -> 500.
    It is comfortably inside the bound, so the fix must NOT be "reject it" —
    it has to still work, via the clamp.
    """
    assert SearchRequest(query="x", limit=999).limit == 999


@pytest.mark.parametrize("bad", [0, -1])
def test_nonpositive_limit_is_rejected(bad):
    with pytest.raises(ValidationError):
        SearchRequest(query="x", limit=bad)


def test_limit_none_still_allowed():
    """None means "caller did not set it" and must survive the bound."""
    assert SearchRequest(query="x", limit=None).limit is None


# ---- the KNN clamp -------------------------------------------------------


@pytest.fixture
def tmp_store_db(tmp_path, monkeypatch):
    db_path = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(store, "_db_checked", False)
    store.init_db()
    yield


def _dim() -> int:
    return config.embeddings.primary.dimensions


def test_huge_top_k_does_not_raise(tmp_store_db):
    """The reported crash, against a real sqlite-vec.

    Before the clamp this raised sqlite3.OperationalError
    ("k value in knn query too large") which the router turned into a 500.
    top_k=1998 is what a limit=999 request actually produces by the time
    search_hybrid has doubled it: 1998 * 3 = 5994, past the 4096 ceiling.
    """
    store.search([0.0] * _dim(), top_k=1998)


def test_top_k_far_past_the_ceiling_does_not_raise(tmp_store_db):
    """Worst case: filters and the visibility widen stack to ~150x."""
    store.search([0.0] * _dim(), top_k=50_000)


def test_ordinary_search_still_works(tmp_store_db):
    """The clamp must not perturb searches nowhere near the ceiling."""
    store.search([0.0] * _dim(), top_k=10)


def test_ceiling_matches_sqlite_vec_documented_limit():
    """Pinned so a bump is a deliberate edit, not a drift.

    sqlite-vec raises "k value in knn query too large ... the limit is 4096".
    """
    assert store.VEC_KNN_MAX_K == 4096
