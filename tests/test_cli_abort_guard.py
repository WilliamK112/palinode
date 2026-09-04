"""Guard against constructing and discarding ``click.Abort`` exceptions."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _is_bare_click_abort(node: ast.AST) -> bool:
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "Abort"
        and isinstance(func.value, ast.Name)
        and func.value.id == "click"
    )


def _bare_click_abort_lines(source: str, *, filename: str = "<unknown>") -> list[int]:
    tree = ast.parse(source, filename=filename)
    return [node.lineno for node in ast.walk(tree) if _is_bare_click_abort(node)]


def _tracked_python_sources() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "palinode"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return sorted(
        REPO_ROOT / rel
        for rel in result.stdout.split("\0")
        if rel.endswith(".py")
    )


def test_click_abort_is_never_a_bare_expression() -> None:
    offenders: list[str] = []
    for path in _tracked_python_sources():
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        offenders.extend(
            f"{rel}:{lineno}"
            for lineno in _bare_click_abort_lines(source, filename=rel)
        )

    assert not offenders, (
        "click.Abort() was constructed without being raised; use "
        "`raise click.Abort()`:\n  " + "\n  ".join(offenders)
    )


def test_click_abort_guard_distinguishes_raised_calls() -> None:
    source = """\
click.Abort()
raise click.Abort()
other.Abort()
error = click.Abort()
"""
    assert _bare_click_abort_lines(source) == [1]
