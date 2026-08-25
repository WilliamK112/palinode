# Palinode for Pi

Persistent memory for the [Pi coding agent](https://pi.dev): sessions start
primed with your core memories, relevant memory is recalled before every
prompt, and sessions are captured on shutdown — all against the same Palinode
store your other harnesses use.

This is a **delivery adapter**, not a second memory system: every capability
comes from the Palinode REST API (`palinode-api`, default `:6340`). The
extension holds no storage, no ranking, no state.

## What it does

| Pi event | What happens |
|----------|--------------|
| `session_start` | Warms server-side session context (`/context/prime`) and queues a bounded digest of your `core: true` memories for the next prompt (`deliverAs: "nextTurn"` — never interrupts) |
| `before_agent_start` | Checks your prospective triggers (`/check-triggers` — server-side cooldowns keep firings self-limiting) and runs a strict-threshold search over the prompt, injecting compact snippets **as a message** before the model answers. Nothing relevant → nothing injected |
| `session_shutdown` | Captures a floor snapshot to `/session-end` (skipping trivial sessions), so a session is never lost even when nobody wrapped it up |

## Why recall arrives as a message, not a system prompt

Pi's `before_agent_start` lets an extension replace the system prompt — and
this extension deliberately never does. Model providers cache your prompt as a
strict prefix (tools → system → messages); per-turn content in the system
prompt would invalidate that entire cached prefix on every turn, costing more
than the recall saves. Messages land *after* the cached prefix, so the cache
survives. The test suite pins this invariant.

## Install

```bash
cd plugins/pi
npm install && npm run build
mkdir -p ~/.pi/agent/extensions/palinode
cp -r package.json dist ~/.pi/agent/extensions/palinode/
```

Pi auto-discovers extensions under `~/.pi/agent/extensions/` (use
`.pi/extensions/` inside a project for project-local install). Make sure
`palinode-api` is running; point the extension at a remote server with
`PALINODE_API_URL`.

## Tuning

Same environment variables as Palinode's Claude Code hooks — one set of
knobs, every harness:

| Variable | Default | Purpose |
|----------|---------|---------|
| `PALINODE_API_URL` | `http://localhost:6340` | Where the API lives |
| `PALINODE_API_TOKEN` | *(unset)* | Bearer token for token-protected deployments |
| `PALINODE_HOOK_RECALL_PROFILE` | `coding` | Recall profile: `coding` (priming + triggers + search), `monitoring` (triggers only), `investigation` (search only, wider), `writing` (priming only), `conversation` (priming + triggers), `minimal` / `off`. Explicit knobs below win over the profile |
| `PALINODE_HOOK_RECALL_MAX_RESULTS` | `3` | Search hits injected per prompt; `0` disables the search channel |
| `PALINODE_HOOK_RECALL_THRESHOLD` | `0.5` | Similarity floor for per-turn search (raw cosine — the same scale as the `%` shown on each injected hit). `0.4` = recall everything plausibly relevant, `0.5` = calibrated default, `0.6` = strict (drops ~1 in 4 relevant memories), `0.7+` = near-silent |
| `PALINODE_HOOK_RECALL_TRIGGERS` | `1` | `0` disables the trigger channel |
| `PALINODE_HOOK_RECALL_MIN_CHARS` | `12` | Prompts shorter than this skip recall entirely |
| `PALINODE_HOOK_RECALL_MAX_CHARS` | `3000` | Total cap on per-turn injected context |
| `PALINODE_HOOK_RECALL_TIMEOUT` | `4` | Per-request timeout (seconds) |
| `PALINODE_HOOK_INJECT_MAX_FILES` | `10` | Max core memories in the session-start digest; `0` disables priming |
| `PALINODE_HOOK_INJECT_MAX_CHARS` | `4000` | Total cap on the session-start digest |
| `PALINODE_HOOK_MIN_MESSAGES` | `3` | Minimum user messages before session capture fires |

## Fail-silent

The extension never blocks Pi. API down, timeout, malformed response — every
path degrades to "no injection, no capture, no error." Check `palinode status`
if memory seems quiet.

## Development

```bash
npm install
npm test        # vitest — runs without a Pi install
npm run build   # tsc → dist/
```

All Palinode logic lives in the shared core (`plugins/core` — pure functions
over an injected `fetch`, compiled into this package's `dist/` alongside the
binding, so the installed extension has no runtime dependencies). The Pi
binding (`src/index.ts`) is structural-typed against the slice of Pi's
extension API it uses, so nothing here depends on Pi's own packages.
