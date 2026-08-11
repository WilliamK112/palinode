"""Regression coverage for the ``palinode read`` presenter."""
from __future__ import annotations

from click.testing import CliRunner

from palinode.cli.read import api_client, read


def test_read_meta_keeps_body_when_frontmatter_value_contains_fence(monkeypatch):
    content = (
        "---\n"
        'title: "alpha---beta"\n'
        "category: insights\n"
        "---\n"
        "The body remains intact."
    )
    payload = {
        "file": "insights/fenced-title.md",
        "content": content,
        "frontmatter": {"title": "alpha---beta", "category": "insights"},
    }
    monkeypatch.setattr(api_client, "read", lambda file_path, meta: payload)

    result = CliRunner().invoke(
        read, ["insights/fenced-title.md", "--meta", "--format", "text"]
    )

    assert result.exit_code == 0, result.output
    content_section = result.output.split("── Content ──\n", 1)[1]
    assert content_section == "The body remains intact.\n"
