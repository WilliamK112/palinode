"""Regression coverage for the memory injection scanner's precision."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from palinode.core.store import scan_memory_content


_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "injection_scanner_cases.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("content", _CORPUS["safe"])
def test_benign_technical_content_is_allowed(content: str) -> None:
    assert scan_memory_content(content) == (True, "ok")


@pytest.mark.parametrize("content", _CORPUS["blocked"])
def test_injection_and_executable_script_content_is_blocked(content: str) -> None:
    is_safe, reason = scan_memory_content(content)

    assert is_safe is False
    assert reason.startswith("Content matches injection pattern:")
