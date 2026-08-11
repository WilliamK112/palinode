"""The save surface and the save capability must reject the same input the same way.

Since the capability was extracted to ``core/save.py`` there are two public
entry points into a save: ``save_memory()`` for in-process callers (CLI, MCP
and the plugin could all use it) and ``POST /save`` for HTTP. They validate the
same values, so they must produce the same verdict — otherwise "is this input
valid?" has two answers depending on how you asked.

They did not. ``sources`` was annotated ``list[dict[str, Any]] | None`` while
its three siblings (``contradicts``, ``backed_by``, ``claims``) were ``Any``, so
a non-list ``sources`` was rejected by Pydantic with a 422 blob before
``_normalize_sources`` could return its "sources must be a list" message. The
identical call through ``save_memory()`` raised ``SaveValidationError`` with
that clean message. One input, two answers.

The resolution direction matters and is deliberate: these collapse toward the
**actionable 400**, not toward Pydantic's 422. That is the design intent the
model already documented on three of the four fields — a bad value "must reach
the handler so it returns a 400 with an actionable message, not Pydantic's
422" — and the OpenAPI document keeps the real shape via ``json_schema_extra``,
the same trick ``epistemic`` uses to stay loosely typed while advertising its
enum.
"""

from __future__ import annotations

import pytest

from palinode.core.config import config
from palinode.core.save import SaveValidationError, save_memory


@pytest.fixture
def mock_memory_dir(tmp_path, monkeypatch):
    """Real SQLite under tmp_path — no DB mocking, per the save-test convention."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    yield tmp_path

#: (field, value) pairs that every entry point must reject. Each is a *shape*
#: error — the kind Pydantic would otherwise claim for itself.
BAD_SHAPES = [
    ("sources", "notalist"),
    ("claims", "notalist"),
    ("contradicts", [{"not": "a ref"}]),
    ("backed_by", [{"not": "a ref"}]),
]


def _http_save(client, field, value, slug):
    return client.post(
        "/save",
        json={"content": "probe body", "type": "Insight", "slug": slug, field: value},
    )


@pytest.mark.parametrize("field,value", BAD_SHAPES)
def test_http_rejects_with_the_capabilitys_own_message(mock_memory_dir, field, value):
    """HTTP returns 400 carrying exactly what the capability would have said."""
    from fastapi.testclient import TestClient

    from palinode.api.server import app

    with pytest.raises(SaveValidationError) as exc:
        save_memory(
            content="probe body",
            type="Insight",
            slug=f"direct-{field}",
            **{field: value},
        )
    capability_message = str(exc.value)

    res = _http_save(TestClient(app), field, value, f"http-{field}")

    assert res.status_code == 400, (
        f"{field} returned {res.status_code}, not the capability's 400 — a shape "
        f"error is being claimed by Pydantic before the normalizer sees it: {res.text}"
    )
    assert res.json()["detail"] == capability_message


@pytest.mark.parametrize("field", ["sources", "claims", "contradicts", "backed_by"])
def test_openapi_still_documents_the_real_shape(field):
    """Loose typing must not cost the OpenAPI document its shape information.

    The annotations are deliberately permissive so the normalizer owns the
    error message; ``json_schema_extra`` is what keeps ``/docs`` honest. Without
    this test, "typed as Any for a better 400" quietly degrades into an
    undocumented parameter.
    """
    from palinode.api.server import app

    schema = app.openapi()["components"]["schemas"]["SaveRequest"]["properties"][field]
    assert schema.keys() & {"type", "oneOf"}, (
        f"{field} advertises no shape in the OpenAPI document — add "
        "json_schema_extra alongside the permissive annotation."
    )


def test_a_bare_ref_string_is_still_accepted_as_sugar(mock_memory_dir):
    """Loosening must not have quietly removed the one-ref shorthand.

    ``normalize_link_refs`` coerces a bare string to a one-element list, and the
    schema advertises both forms. A stricter annotation would have turned this
    documented shorthand into a 422 — which is the concrete cost of tightening
    that this direction avoids.
    """
    res = save_memory(
        content="probe body",
        type="Insight",
        slug="bare-ref-sugar",
        contradicts="decisions/some-call",
    )
    assert res["id"] == "insights-bare-ref-sugar"
