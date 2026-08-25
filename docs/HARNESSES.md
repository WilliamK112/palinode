# Using Palinode from any harness

Palinode is one memory backend with several ways in. Your memories live in one
place — markdown files, git-versioned, indexed by one API server — and every
agent harness you use connects to that same store. Switch editors, run three at
once, move between machines: the memory follows you, not the tool.

This page is the map: what each harness gets, how deep the integration goes,
and where to start.

## Three integration tiers

Every integration is a thin client of the same REST API — no harness gets a
private fork of the capability set. What differs is how much of the memory
loop runs automatically:

| Tier | What runs without you asking | Where |
|------|------------------------------|-------|
| **Native hooks** | Session starts primed with core memories, relevant memory recalled before each prompt, session captured on exit | Claude Code |
| **Native plugin** | The same full loop, wired into the harness's own extension API | Pi, Cline, OpenClaw |
| **MCP** | Explicit tools — the agent searches and saves when it decides to | Everything that speaks MCP |

The tiers stack. Claude Code users typically run hooks **and** MCP: hooks make
memory ambient, MCP tools let the agent dig deeper on demand.

## What each harness gets

| Harness | Tier | Setup |
|---------|------|-------|
| **Claude Code (CLI)** | Hooks + MCP | `palinode init` in your project — scaffolds everything below |
| **Claude Desktop** | MCP | `palinode mcp-config --stdio` → paste into its config |
| **Pi** | Native extension | [plugins/pi/README.md](../plugins/pi/README.md) — per-turn recall, priming, capture |
| **Cline (CLI / SDK)** | Native plugin | [plugins/cline/README.md](../plugins/cline/README.md) — per-turn recall, priming, capture; `cline plugin install` |
| **OpenClaw** | Native plugin + MCP | see the plugin's install guide ([plugin/INSTALL.md](../plugin/INSTALL.md)) |
| **Cursor** | MCP + rules file | `palinode mcp-config --stdio`; `palinode init` also writes `.cursor/rules/palinode.md` |
| **Windsurf** | MCP | `palinode mcp-config --stdio` (or `--http` for a remote server) |
| **Zed** | MCP | `palinode mcp-config --http` |
| **VS Code (Cline / Continue)** | MCP | [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md) — the Cline VS Code extension does not load `AgentPlugin`s yet |
| **Codex CLI** | MCP + AGENTS.md | `~/.codex/config.toml`; `palinode init` appends a memory block to `AGENTS.md` |
| **Antigravity** | MCP + AGENTS.md | native MCP menu; same `AGENTS.md` block |
| **JetBrains (AI Assistant)** | MCP | [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md) |

Per-client config file locations: [MCP-CONFIG-HOMES.md](MCP-CONFIG-HOMES.md).
If a setup misbehaves, `palinode mcp-config --diagnose` shows every config file
your clients actually read.

## The full loop, on Claude Code

Claude Code is the deepest integration today because its hook system covers
the whole session lifecycle. After `palinode init`:

1. **Session start** — the `SessionStart` hook injects a bounded digest of
   your `core: true` memories, so the session begins already knowing your
   standing context.
2. **Every prompt** — the `UserPromptSubmit` hook checks your prospective
   triggers (`palinode_trigger`) and runs a strict-threshold search over the
   prompt, injecting compact snippets *before the model answers*. Nothing
   relevant → it says nothing.
3. **On demand** — the MCP tools (`palinode_search`, `palinode_read`,
   `palinode_save`, …) are there when the agent wants more than the ambient
   layer surfaced.
4. **Session end** — the `SessionEnd` hook captures a snapshot on `/clear`,
   logout, and exit, so a session is never lost even when nobody remembered to
   wrap it up.

Tuning knobs for all three hooks: [examples/hooks/](../examples/hooks/).

### Why injected memory lands in the conversation, not the system prompt

Model providers cache your prompt as a strict prefix: tools, then system
prompt, then messages. Change one byte early in that prefix and everything
after it is re-processed — and re-billed — from scratch. Per-turn memory
injected into the *system prompt* would do exactly that, every turn.

Palinode's hooks inject into the **conversation** instead, after the cached
prefix. The recall arrives fresh each turn; the expensive stable prefix stays
cached. Every native plugin follows the same rule — the Pi and Cline plugins
share one core (`plugins/core`) whose only injection output is a message
body, and each plugin's test suite pins that it never lands in the system
prompt.

## Same contract everywhere

Whatever the tier, the memory contract is identical, because everything calls
the same API:

- **Save with rationale** — decisions carry their why (`palinode_save`).
- **Recall is search, not scrollback** — hybrid keyword + semantic search
  over everything you've ever saved (`palinode_search`).
- **Sessions end captured** — `palinode_session_end` (or the Claude Code
  floor hook) writes the session's outcomes where the next session will find
  them.
- **Everything is auditable** — files + git means `diff`, `blame`, and
  `rollback` work on your agent's memory like on your code.

A memory saved from Cursor is findable from Claude Code, from a cron job via
the CLI, from OpenClaw — the harness is a doorway, not a silo.

## More native integrations

The plugin architecture is deliberately thin — a native integration is an
adapter over the REST API, not a reimplementation — so support for more
harnesses with lifecycle hooks is planned. If your harness of choice exposes a
pre-prompt hook and you want Palinode wired into it, open an issue.
