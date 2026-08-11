"""Forcing function: every top-level section shipped in
``palinode.config.yaml.example`` must be read somewhere in ``palinode/``.

A documented config knob that changes nothing is a defect — a user sets it,
believes it took effect, and it silently didn't. For a safety-relevant knob
(an ops allowlist, a scrub-pattern path) that is the worst kind: it reads as
protection and provides none. Two whole subtrees (``recall:``, ``security:``)
and one misnamed one (``compaction.allowed_ops``) shipped in exactly that
shape and were caught only by manual review — this test is what should have
caught them mechanically.

Scope, deliberately narrowed: this checks TOP-LEVEL sections only, not every
leaf key. A per-leaf "is this exact dotted path read" check needs either a
real static-analysis pass (attribute access on a `Config` instance is not
statically distinguishable from any other attribute access without one) or an
AST walk expensive enough to not belong in a fast unit test — and either
would still miss a leaf field inside an otherwise-live nested dataclass
(exactly the `compaction.aggressiveness` shape: `compaction.layer_split` was
read, so `config.compaction` is not dead, but one field inside it was). A
naive substring/grep at the leaf level produces false positives on comments
and false negatives on aliasing (`cfg = config.decay; cfg.enabled`), so it
would be a flaky, unreliable gate, not a stronger one. Top-level section
coverage is a real, mechanical improvement over having no check at all: it
is exactly the shape of the two bugs this test was added for.
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "palinode.config.yaml.example"
PACKAGE_ROOT = REPO_ROOT / "palinode"

# core/config.py is the *definition* site — every key trivially appears
# there. Excluding it is what turns "is this key declared" (a schema check,
# which TypeAdapter already enforces at load time) into "is this key ever
# read by code that isn't the config module itself" (a usage check).
_CONFIG_MODULE = PACKAGE_ROOT / "core" / "config.py"


def _source_files() -> list[Path]:
    return [
        p
        for p in sorted(PACKAGE_ROOT.rglob("*.py"))
        if p != _CONFIG_MODULE and "__pycache__" not in p.parts
    ]


def _has_reader(section: str) -> bool:
    """True if ``config.<section>`` appears as real code anywhere under
    ``palinode/`` (excluding the config module itself and comment-only
    lines — a dedicated-comment-line filter, not a full tokenizer, since a
    trailing inline comment referencing the same section as a real access
    on the same line is not a false positive worth guarding against here).
    """
    needle = f"config.{section}"
    for path in _source_files():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if needle in stripped:
                return True
    return False


def test_every_top_level_example_config_section_has_a_reader() -> None:
    """Every top-level key documented in the shipped example config must be
    read by production code, not just declared in the ``Config`` dataclass.
    """
    raw = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8")) or {}
    assert raw, (
        f"{EXAMPLE_CONFIG} parsed to an empty mapping — the fixture path or "
        "its contents are stale."
    )

    unread = sorted(key for key in raw if not _has_reader(key))
    assert not unread, (
        "Top-level key(s) in palinode.config.yaml.example have no reader "
        f"anywhere in palinode/ (outside core/config.py): {unread}. "
        "A shipped, documented config section that nothing reads is a "
        "knob that silently does nothing — either wire it up or delete it "
        "from both the example and the Config dataclass."
    )
