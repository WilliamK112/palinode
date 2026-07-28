"""Guard: `## Unreleased` keeps exactly one of each Keep-a-Changelog heading.

`docs/CHANGELOG.md` is marked ``merge=union`` in ``.gitattributes`` so N parallel
branches can each append a bullet without hand-resolving markers. The cost of
that driver is that it resolves **silently**: when two sides both carry bullets
under their own copy of a heading, union keeps BOTH copies, and the result has
no conflict marker, no rejected hunk, and nothing in `git status` to look at.

Until now the only thing standing between that and a malformed changelog was a
paragraph in CLAUDE.md telling a human to check afterwards. That instruction was
correct and it was followed — and it still depended on somebody remembering, on
every CHANGELOG-touching merge, forever.

It fired for real: merging main into a release branch produced two `### Fixed`
headings under `## Unreleased`. Caught by hand, one merge before the release
that would have shipped it. This test is that check, made mechanical, so the
next one is caught by CI instead of by luck.

Also guards the seeding itself. All five headings are permanently present under
`## Unreleased` precisely so a branch adds its bullet under an existing heading
rather than creating a sixth — a branch that introduces its own `### Fixed`
defeats the union driver's whole purpose. Deleting an empty heading when cutting
a release breaks that, so absence fails here too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CHANGELOG = Path(__file__).resolve().parent.parent / "docs" / "CHANGELOG.md"

# Keep a Changelog 1.1.0. Order is not enforced — only presence and uniqueness.
REQUIRED_HEADINGS = ("Added", "Changed", "Fixed", "Removed", "Security")

_RELEASE_HEADING = re.compile(r"^## ", re.M)


def _unreleased_section() -> str:
    """The text of `## Unreleased`, up to the first released-version heading."""
    text = CHANGELOG.read_text(encoding="utf-8")
    start = text.find("## Unreleased")
    assert start != -1, "docs/CHANGELOG.md has no `## Unreleased` section"
    rest = text[start + len("## Unreleased"):]
    nxt = _RELEASE_HEADING.search(rest)
    return rest[: nxt.start()] if nxt else rest


def _subheadings(section: str) -> list[str]:
    return re.findall(r"^### (.+?)\s*$", section, re.M)


@pytest.mark.parametrize("heading", REQUIRED_HEADINGS)
def test_unreleased_has_exactly_one_of_each_heading(heading: str) -> None:
    """Duplicate => a union merge kept both sides. Missing => the seeding was lost."""
    found = _subheadings(_unreleased_section())
    count = found.count(heading)

    assert count != 0, (
        f"`### {heading}` is missing from `## Unreleased`. All five headings stay "
        f"permanently seeded so branches append under an existing one — see "
        f".gitattributes. Leave them in place when cutting a release."
    )
    assert count == 1, (
        f"`### {heading}` appears {count}x under `## Unreleased`. A `merge=union` "
        f"merge kept both sides and left no conflict marker. Collapse them into one "
        f"heading, keeping every bullet:\n"
        f"    git fetch origin && git merge origin/main   # union auto-resolves\n"
        f"    # then check this section by eye before pushing\n"
        f"Found: {found}"
    )


def test_unreleased_has_no_unexpected_headings() -> None:
    """A branch inventing its own heading is what defeats the seeding."""
    extra = sorted(set(_subheadings(_unreleased_section())) - set(REQUIRED_HEADINGS))
    assert not extra, (
        f"Unexpected heading(s) under `## Unreleased`: {extra}. Add bullets under one "
        f"of the five seeded headings {list(REQUIRED_HEADINGS)} instead of creating a "
        f"new one — union keeps both sides, so a new heading becomes a duplicate."
    )


def test_every_unreleased_bullet_sits_under_a_heading() -> None:
    """A bullet above the first `###` would be silently dropped from release notes."""
    section = _unreleased_section()
    first = section.find("### ")
    preamble = section if first == -1 else section[:first]
    orphans = [ln for ln in preamble.splitlines() if ln.lstrip().startswith("- ")]
    assert not orphans, (
        "Bullet(s) under `## Unreleased` but above the first `###` heading — they "
        "belong to no bucket and would be lost when the section becomes release "
        f"notes:\n  " + "\n  ".join(orphans)
    )
