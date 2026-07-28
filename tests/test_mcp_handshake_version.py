"""The MCP initialize handshake must advertise palinode's version, not the SDK's.

`Server.version` is optional in the mcp SDK, and its fallback is
`pkg_version("mcp")` — the SDK's own version. Omitting it is therefore not
"no version advertised", it is "the wrong version advertised", and it is the
version surface users actually see: every client renders `serverInfo` in its
connection UI. Before this was fixed, palinode announced itself as "v1.27.0"
and the number tracked whatever mcp release happened to be installed.

The /status and /health surfaces were corrected separately; neither reaches the
handshake, which is why this went unnoticed there.
"""
from __future__ import annotations

from importlib.metadata import version as pkg_version

import palinode
import palinode.mcp as mcp_module


def test_handshake_advertises_palinode_version():
    """serverInfo.version is palinode's, and the name is unchanged."""
    opts = mcp_module.server.create_initialization_options()

    assert opts.server_name == "palinode", opts.server_name
    assert opts.server_version == palinode.__version__, (
        f"initialize advertises {opts.server_version!r}, expected "
        f"{palinode.__version__!r} — clients render this as palinode's version"
    )


def test_server_version_is_set_explicitly():
    """The bug was an omission, so pin the presence, not just the value.

    With `version=` absent the SDK substitutes its own version silently — no
    error, no warning, just a wrong number in every client's UI.
    """
    assert mcp_module.server.version is not None, (
        "Server(...) was constructed without `version=`; the SDK will fall back "
        "to its own package version in the initialize handshake"
    )


def test_handshake_version_is_not_the_sdk_version():
    """Guard the specific regression: advertising the mcp SDK's version.

    Skipped in the pathological case where the two genuinely coincide, so the
    test pins the defect rather than an accident of numbering.
    """
    sdk_version = pkg_version("mcp")
    if palinode.__version__ == sdk_version:  # pragma: no cover — coincidence only
        return

    opts = mcp_module.server.create_initialization_options()
    assert opts.server_version != sdk_version, (
        f"initialize is advertising the mcp SDK version ({sdk_version}) as "
        "palinode's — `version=` has been dropped from the Server(...) call"
    )
