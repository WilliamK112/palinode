"""Deterministic migration for flat, date-prefixed bullet-list exports."""
from __future__ import annotations

import re
from typing import Any

from palinode.migration.openclaw import (
    _detect_type,
    _slugify,
    _validate_source_path,
    run_sections_migration,
)

_BULLET = re.compile(r"^\s*[-*]\s+(?:(\d{4}-\d{2}-\d{2})\s+)?(.+?)\s*$")
_UNDATED_HEADING = "Undated memories"


def _section(heading: str, items: list[str]) -> dict[str, Any]:
    body = "\n".join(f"- {item}" for item in items)
    return {
        "heading": heading,
        "body": body,
        "type": _detect_type(heading, body),
        "slug": _slugify(heading),
    }


def _parse_raw(raw: str) -> list[dict[str, Any]]:
    """Group bullets into a leading undated section and date sections."""
    undated_items: list[str] = []
    dated_items: dict[str, list[str]] = {}
    current_date: str | None = None

    for line in raw.splitlines():
        match = _BULLET.match(line)
        if match is None:
            continue
        date, text = match.groups()
        if date is not None:
            current_date = date
        if current_date is None:
            undated_items.append(text)
        else:
            dated_items.setdefault(current_date, []).append(text)

    sections: list[dict[str, Any]] = []
    if undated_items:
        sections.append(_section(_UNDATED_HEADING, undated_items))
    sections.extend(_section(date, items) for date, items in dated_items.items())
    return sections


def parse_bullet_list(source_path: str) -> list[dict[str, Any]]:
    """Parse a bullet-list export into the canonical migration section shape."""
    validated = _validate_source_path(source_path)
    with open(validated, "r", encoding="utf-8") as source:
        return _parse_raw(source.read())


def run_migration(source_path: str, dry_run: bool = False) -> dict[str, Any]:
    """Import a flat bullet-list export through the shared migration writer."""
    validated = _validate_source_path(source_path)
    sections = parse_bullet_list(validated)
    return run_sections_migration(
        validated,
        sections,
        source_name="bullets",
        display_name="Bullet-list",
        dry_run=dry_run,
    )
