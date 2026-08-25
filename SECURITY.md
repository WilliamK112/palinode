# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Palinode, please report it responsibly.

**Email:** paul@phasespace.co

**What to include:**
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if you have one)

**Response timeline:**
- Acknowledgment within 48 hours
- Assessment and plan within 7 days
- Fix released as soon as practical, with credit to the reporter (unless you prefer anonymity)

**Please do not:**
- Open a public GitHub issue for security vulnerabilities
- Exploit the vulnerability beyond what's needed to demonstrate it

## Scope

Palinode runs locally on your machine. The primary attack surface is:
- Path traversal in file operations (mitigated: all paths validated against PALINODE_DIR)
- API endpoint abuse (mitigated: rate limiting, request size limits, optional bearer auth — see below)
- LLM prompt injection via memory content (mitigated: compaction output is schema-validated and file writes stay inside Palinode)

## API authentication

The Palinode API server (default port 6340) supports an optional bearer-token
auth layer. It is **off by default** to keep local-first development friction
free and **required** when binding the API to a non-loopback address.

| Deployment | Recommended setting | Notes |
|------------|---------------------|-------|
| Local dev (single user, loopback) | No token | Default. The middleware is a no-op when `PALINODE_API_TOKEN` is unset. |
| Multi-user / homelab / Tailscale | Set `PALINODE_API_TOKEN` | Every request must carry `Authorization: Bearer <token>` except `/health` and `/health/watcher`. |
| Any non-loopback bind (`PALINODE_API_HOST` other than `127.0.0.1` / `localhost` / `::1`) | **Token required** | The server refuses to start without `PALINODE_API_TOKEN` (or `PALINODE_API_TOKEN_FILE`). Set `PALINODE_API_ALLOW_UNAUTH=1` to opt out for a deliberately token-less, network-isolated host (e.g. Tailscale-only); it then starts and logs a warning on every start. |
| `PALINODE_API_BIND_INTENT=public` | **Token required** | Declares intentional public exposure and suppresses the bind warning. Refuses to start without a token even if `PALINODE_API_ALLOW_UNAUTH=1` is set. |

The startup gate keys on the **resolved bind host** (`PALINODE_API_HOST` /
`services.api.host`), not on any stated intent. The shipped systemd template
sets `PALINODE_API_HOST` alongside uvicorn's `--host` so the gate sees the real
bind; if you launch uvicorn by hand with `--host 0.0.0.0`, set
`PALINODE_API_HOST=0.0.0.0` too — the app cannot see uvicorn's CLI flags.

### The MCP HTTP transport

`palinode-mcp-http` (default port 6341) is under the **same gate with the same
single opt-out**. Its bind host resolves as `--host` flag > `PALINODE_MCP_HTTP_HOST`
> `127.0.0.1` (the default is loopback). A non-loopback bind with no
`PALINODE_API_TOKEN` refuses to start with the same `REFUSING TO START` message
unless `PALINODE_API_ALLOW_UNAUTH=1` is set — there is no MCP-specific opt-out;
one knob per deployment. `PALINODE_MCP_BIND_INTENT=public` keeps meaning "token
required".

The MCP HTTP transport has **no token of its own**. It reads the same
`PALINODE_API_TOKEN` / `PALINODE_API_TOKEN_FILE` as the API: when set, every
request to `/mcp/` must carry `Authorization: Bearer <token>` (`/healthz` is
exempt) and the transport sends that same token on its own calls to the API. So the
gate's question is really "is the API this transport proxies to protected?" — a
token-less MCP HTTP bind on the network would serve every Palinode tool
(save/search/read/…) to anyone who can reach the port. The shipped systemd and
Nix MCP units bind `0.0.0.0` explicitly (they exist for remote clients) and so
require the token — or the opt-out — exactly like the API unit.

### Generating a token

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Set it in the API server's environment:

```bash
export PALINODE_API_TOKEN=<value>
# or, for docker-secrets / sealed-secrets style deployments:
export PALINODE_API_TOKEN_FILE=/run/secrets/palinode_api_token
```

`PALINODE_API_TOKEN` takes precedence over `PALINODE_API_TOKEN_FILE` when both
are set. Whitespace is stripped. An empty value is treated as "no token".

### Using the token from a client

```bash
curl -H "Authorization: Bearer $PALINODE_API_TOKEN" \
     http://localhost:6340/list
```

Palinode's own clients — the `palinode` CLI, the stdio MCP server
(`palinode-mcp`), and the shell hooks written by `palinode init` — read the
same `PALINODE_API_TOKEN` / `PALINODE_API_TOKEN_FILE` and send the bearer
automatically; export it in their environment and nothing else is needed.

For MCP clients (Claude Code, Zed, Cursor, etc.) over Streamable HTTP, see
[`docs/INSTALL-CLAUDE-CODE.md`](docs/INSTALL-CLAUDE-CODE.md) for the
`headers` block to add to your MCP config.

### Rotating

There is no on-disk token store. To rotate, change the env var (or the file)
and restart the API server. Existing connections fail closed with `401
Unauthorized` and clients reconnect with the new token.

### What this does NOT cover

- Anything beyond the bearer check: there is no per-user identity, no scopes,
  and no rate limiting keyed on the token. For multi-tenant or internet-facing
  exposure, front the API and the MCP HTTP transport with a reverse proxy that
  enforces auth, or restrict access at the network layer (VPN, Tailscale ACLs,
  firewall).

The token comparison is constant-time (`hmac.compare_digest`) and the
expected header is pre-encoded at startup, so the hot path is a single
constant-time byte compare with no per-request format work.

## Supported Versions

Security fixes are applied to the latest release only.
