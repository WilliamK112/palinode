"""Frontmatter/body splitting must be canonical, not reimplemented per caller.

``core.parser.split_frontmatter`` is the single lossless source of truth
(``frontmatter_block + body == content`` always). Two other call sites used to
diverge from it:

- ``cli/read.py`` did a naive ``content.split("---", 2)`` — splitting on the
  first two ``---`` occurrences *anywhere* in the string, not on fence lines.
  A ``---`` inside a frontmatter *value* (a title, a quote anchor, an
  em-dash-heavy description) landed on the wrong split point and truncated
  the body.
- ``api/ui/router.py`` used the ``frontmatter`` library directly instead of
  the canonical helper.

Both now delegate to ``split_frontmatter``. This file locks that in.
"""
from __future__ import annotations

from palinode.core.parser import split_frontmatter

# A `---` INSIDE a frontmatter value (mid-line, not its own fence line) is the
# failure trigger: `str.split("---", 2)` treats it as a delimiter regardless
# of position, while a fence-aware split does not, since it is never preceded
# by a newline and followed only by whitespace-then-newline.
_CONTENT_WITH_DASHES_IN_VALUE = (
    '---\n'
    'title: "Launch plan --- v2"\n'
    'category: decisions\n'
    '---\n'
    'Body intro.\n'
    '\n'
    '---\n'
    '\n'
    'Body after a thematic break.\n'
)

_EXPECTED_BODY = (
    'Body intro.\n'
    '\n'
    '---\n'
    '\n'
    'Body after a thematic break.\n'
)


def test_split_frontmatter_is_lossless_with_dashes_in_a_value():
    fm, body = split_frontmatter(_CONTENT_WITH_DASHES_IN_VALUE)
    assert fm + body == _CONTENT_WITH_DASHES_IN_VALUE
    assert body == _EXPECTED_BODY
    # The frontmatter block captured the mid-value dashes as part of the
    # frontmatter, not as a stray delimiter.
    assert 'title: "Launch plan --- v2"' in fm
    # The body's own thematic-break `---` line survived untouched.
    assert body.count("---") == 1


def test_cli_read_strip_frontmatter_does_not_truncate_on_dashes_in_a_value():
    from palinode.cli.read import _strip_frontmatter

    body = _strip_frontmatter(_CONTENT_WITH_DASHES_IN_VALUE)
    assert body == _EXPECTED_BODY
    assert "category: decisions" not in body  # frontmatter leaked into body = bug
    assert "Body after a thematic break." in body


def test_ui_router_strip_frontmatter_does_not_truncate_on_dashes_in_a_value():
    from palinode.api.ui.router import _strip_frontmatter

    body = _strip_frontmatter(_CONTENT_WITH_DASHES_IN_VALUE)
    assert body == _EXPECTED_BODY
    assert "category: decisions" not in body
    assert "Body after a thematic break." in body


def test_cli_read_and_ui_router_agree_with_canonical_split():
    """Both call sites' output equals the canonical body (module docstring's
    contract), not merely "something that looks plausible"."""
    from palinode.api.ui.router import _strip_frontmatter as ui_strip
    from palinode.cli.read import _strip_frontmatter as cli_strip

    _, canonical_body = split_frontmatter(_CONTENT_WITH_DASHES_IN_VALUE)
    assert cli_strip(_CONTENT_WITH_DASHES_IN_VALUE) == canonical_body.lstrip("\n")
    assert ui_strip(_CONTENT_WITH_DASHES_IN_VALUE) == canonical_body


def test_content_with_no_frontmatter_passes_through_unchanged():
    plain = "Just a body.\n\n---\n\nWith a thematic break.\n"
    from palinode.api.ui.router import _strip_frontmatter as ui_strip
    from palinode.cli.read import _strip_frontmatter as cli_strip

    assert split_frontmatter(plain) == ("", plain)
    assert cli_strip(plain) == plain
    assert ui_strip(plain) == plain
