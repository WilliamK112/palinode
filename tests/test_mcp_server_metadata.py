"""The running server's display metadata must match what the registry publishes.

``server.json`` is what the MCP Registry listing renders; the ``initialize``
response is what a client connecting directly sees. They describe the same
server, so a reader should not be able to get two different answers depending on
where they looked. Nothing enforced that before — the fields simply were not set
on the server at all, so the two could not disagree by being absent from one side.

They are duplicated rather than shared because ``server.json`` is a repo-root
registry manifest, not a packaged file: reading it at runtime would work in a
checkout and raise in an installed wheel. Duplication plus a drift test is the
trade, and this is the test.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from palinode import mcp as palinode_mcp


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_JSON = REPO_ROOT / "server.json"


@pytest.fixture(scope="module")
def registry_manifest() -> dict:
    if not SERVER_JSON.exists():
        pytest.skip("server.json is absent (installed package, not a checkout)")
    return json.loads(SERVER_JSON.read_text())


def test_title_matches_registry(registry_manifest):
    assert palinode_mcp.SERVER_TITLE == registry_manifest["title"]


def test_description_matches_registry(registry_manifest):
    assert palinode_mcp.SERVER_DESCRIPTION == registry_manifest["description"]


def test_website_url_matches_registry(registry_manifest):
    assert palinode_mcp.SERVER_WEBSITE_URL == registry_manifest["websiteUrl"]


def test_metadata_reaches_the_initialize_response():
    """The point of setting these is that a client actually receives them.

    Asserted against ``create_initialization_options()`` — the object serialised
    into the ``initialize`` result — rather than against the constructor
    arguments, which would only prove the values were passed somewhere.
    """
    options = palinode_mcp.server.create_initialization_options()

    assert options.title == palinode_mcp.SERVER_TITLE
    assert options.description == palinode_mcp.SERVER_DESCRIPTION
    assert options.website_url == palinode_mcp.SERVER_WEBSITE_URL
    # Name and version were already announced; they must not have regressed.
    assert options.server_name == "palinode"
    assert options.server_version == palinode_mcp.__version__


def test_metadata_is_non_empty():
    """An empty string is a valid assignment and a useless listing."""
    for name in ("SERVER_TITLE", "SERVER_DESCRIPTION", "SERVER_WEBSITE_URL"):
        value = getattr(palinode_mcp, name)
        assert isinstance(value, str) and value.strip(), f"{name} must be a non-empty string"
