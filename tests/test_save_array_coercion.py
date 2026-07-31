"""the array-param coercion fix: every array parameter on palinode_save tolerates a
double-encoded array.

Some MCP clients serialize array arguments as JSON strings. `_coerce_str_array`
was written against that observed behaviour but wired to only ONE of save's five
array params, so the same client input made `entities` work and the other four
fail — in three different error shapes, from three hand-rolled code paths.

Partial tolerance is the worst of both worlds: the caller sees some arrays land
and concludes the transport is fine.
"""

from __future__ import annotations

import json

from palinode.mcp import _coerce_str_array

# The five array parameters on palinode_save.
SAVE_ARRAY_PARAMS = ("entities", "sources", "contradicts", "backed_by", "claims")


def test_decodes_a_double_encoded_string_array() -> None:
    assert _coerce_str_array('["project/alpha","person/bravo"]') == [
        "project/alpha",
        "person/bravo",
    ]


def test_decodes_arrays_of_objects_too() -> None:
    """`sources` and `claims` carry dicts, not strings.

    The helper's name says "str_array" but it never inspects element types — it
    returns any decoded JSON list. This is what makes uniform application safe.
    """
    payload = [{"id": "s1", "quote": "x"}, {"id": "s2", "quote": "y"}]
    assert _coerce_str_array(json.dumps(payload)) == payload


def test_native_lists_pass_through_untouched() -> None:
    native = ["project/alpha"]
    assert _coerce_str_array(native) is native
    objs = [{"id": "s1"}]
    assert _coerce_str_array(objs) is objs


def test_non_array_input_is_returned_unchanged() -> None:
    """A plain string stays a string — the API still validates and 400s on it."""
    assert _coerce_str_array("not json at all") == "not json at all"
    assert _coerce_str_array('{"a": 1}') == '{"a": 1}', "a JSON object is not an array"
    assert _coerce_str_array('"scalar"') == '"scalar"'
    assert _coerce_str_array(None) is None
    assert _coerce_str_array(7) == 7


def test_every_save_array_param_is_guarded_in_the_handler() -> None:
    """The regression this issue is about: guarding one param and not the rest.

    Reads the handler source so a newly added array parameter that forwards
    `arguments[...]` verbatim fails here instead of shipping a sixth
    inconsistency.
    """
    import inspect

    import palinode.mcp as mcp

    src = inspect.getsource(mcp)
    for param in SAVE_ARRAY_PARAMS:
        guarded = f'body["{param}"] = _coerce_str_array(arguments["{param}"])'
        assert guarded in src, f"{param!r} forwards verbatim — same defect as #691"
