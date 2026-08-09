"""Regression coverage for doctor filesystem isolation in the test suite."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from palinode.core.config import config
from palinode.diagnostics.checks import phantom_db
from palinode.diagnostics.types import DoctorContext


def test_default_doctor_walk_stays_inside_tmp_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The suite default must not let doctor walk a developer's real home."""
    visited_roots: list[Path] = []
    real_walk = os.walk

    def recording_walk(root, *args, **kwargs):
        visited_roots.append(Path(root).resolve())
        yield from real_walk(root, *args, **kwargs)

    monkeypatch.setattr(phantom_db.os, "walk", recording_walk)

    phantom_db.phantom_db_files(DoctorContext(config=config))

    assert visited_roots == [tmp_path.resolve()]


@pytest.mark.doctor_real_search_roots
def test_real_search_roots_marker_opts_out(tmp_path: Path) -> None:
    """The explicit marker leaves production root discovery unmodified."""
    assert str(tmp_path) not in config.doctor.search_roots
