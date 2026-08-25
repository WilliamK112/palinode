# Palinode for Cline

Persistent memory for [Cline](https://cline.bot) (CLI, SDK, Kanban — the
surfaces that load `AgentPlugin`s): sessions start primed with your core
memories, relevant memory is recalled before every prompt, and sessions are
captured as they run — all against the same Palinode store your other
harnesses use.

This is a **delivery adapter**, not a second memory system: every capability
comes from the Palinode REST API (`palinode-api`, default `:6340`). The
plugin holds no storage, no ranking, no state beyond what it has already
injected this session.

## What it does

| Cline hook | What happens |
|------------|--------------|
| `beforeModel` (first model call of the session) | Warms server-side session context (`/context/prime`) and injects a bounded digest of your `core: true` memories |
| `beforeModel` (every new user prompt) | Checks your prospective triggers (`/check-triggers` — server-side cooldowns keep firings self-limiting) and runs a strict-threshold search over the prompt, injecting compact snippets **as a message right after the prompt**. Nothing relevant → nothing injected |
| `afterRun` | Captures a floor snapshot to `/session-end` once the session crosses the message floor, then again each time the session doubles in length — so a session is never lost even though Cline plugins get no session-shutdown hook |

`beforeModel` and `afterRun` are Cline's runtime-native plugin hooks (the
docs' `before_agent_start` and `run_end` stages, respectively — see "Which
Cline API" below).

## Why recall arrives as a message, not a system prompt

Model providers cache your prompt as a strict prefix (tools → system →
messages); per-turn content in the system prompt would invalidate that entire
cached prefix on every turn, costing more than the recall saves. Messages
land *after* the cached prefix, so the cache survives.

Cline's `beforeModel` result cannot even carry a system prompt — the runtime
reads only `messages`, `tools`, `options` and `stop` from it — so on this
harness the message array is the *only* place a plugin can inject. Two
further Cline facts shape the design, and the test suite pins all three:

- **`beforeModel` fires on every model call**, including every tool-loop
  iteration, and its result is applied to that one request — never persisted
  into the conversation. So the recall for a prompt is computed once and then
  re-inserted, byte-identical and at the same position, on every later
  request of the session. Without that replay, the cached prefix would shift
  on the very next call and the plugin would cost more than it saves.
- **Cline's message roles are `user | assistant | tool`** — there is no
  system role — so injected memory is a `role: "user"` message tagged
  `metadata.kind = "palinode-recall"`. Cline itself inserts synthetic
  consecutive user messages, so providers handle the shape.

## Install

```bash
cd plugins/cline
npm install && npm run build

# global (all sessions) — or add --cwd . for this project only
cline plugin install .
```

Or point an SDK session at it directly:

```ts
import { ClineCore } from "@cline/sdk";
await cline.start({
  config: {
    // ...model/runtime config
    pluginPaths: ["/absolute/path/to/plugins/cline/dist/cline/src/index.js"],
  },
});
```

```ts
import { Agent } from "@cline/sdk";
import { createPalinodePlugin } from "./plugins/cline/dist/cline/src/index.js"; // not on npm yet
new Agent({
  // ...
  plugins: [createPalinodePlugin({ apiUrl: "http://localhost:6340", recallProfile: "coding" })],
});
```

Make sure `palinode-api` is running; point the plugin at a remote server
with `PALINODE_API_URL` (and `PALINODE_API_TOKEN` when the server is
token-protected — the plugin sends `Authorization: Bearer` on every call).

`dist/` contains this binding **and** the shared core (`plugins/core`,
compiled alongside it), so the installed package has no runtime dependencies.

## Tuning

Same environment variables as Palinode's Claude Code hooks and Pi extension —
one set of knobs, every harness. `createPalinodePlugin({...})` accepts the
same fields as options and they win over the environment.

| Variable | Default | Purpose |
|----------|---------|---------|
| `PALINODE_API_URL` | `http://localhost:6340` | Where the API lives |
| `PALINODE_API_TOKEN` | *(unset)* | Bearer token for token-protected deployments |
| `PALINODE_HOOK_RECALL_PROFILE` | `coding` | Recall profile: `coding` (priming + triggers + search), `monitoring` (triggers only), `investigation` (search only, wider), `writing` (priming only), `conversation` (priming + triggers), `minimal` / `off`. Explicit knobs below win over the profile |
| `PALINODE_HOOK_RECALL_MAX_RESULTS` | `3` | Search hits injected per prompt; `0` disables the search channel |
| `PALINODE_HOOK_RECALL_THRESHOLD` | `0.5` | Similarity floor for per-turn search (raw cosine — the same scale as the `%` shown on each injected hit). `0.4` = recall everything plausibly relevant, `0.5` = calibrated default, `0.6` = strict, `0.7+` = near-silent |
| `PALINODE_HOOK_RECALL_TRIGGERS` | `1` | `0` disables the trigger channel |
| `PALINODE_HOOK_RECALL_MIN_CHARS` | `12` | Prompts shorter than this skip recall entirely |
| `PALINODE_HOOK_RECALL_MAX_CHARS` | `3000` | Total cap on per-turn injected context |
| `PALINODE_HOOK_RECALL_TIMEOUT` | `4` | Per-request timeout (seconds) |
| `PALINODE_HOOK_INJECT_MAX_FILES` | `10` | Max core memories in the session-start digest; `0` disables priming |
| `PALINODE_HOOK_INJECT_MAX_CHARS` | `4000` | Total cap on the session-start digest |
| `PALINODE_HOOK_MIN_MESSAGES` | `3` | Minimum user messages before the capture floor fires |
| `PALINODE_CLINE_HOOK_DEADLINE_MS` | `2000` | Whole-hook deadline. Cline's plugin sandbox (anything installed with `cline plugin install`) kills a hook — and the plugin process — at 3000 ms, so recall must resolve or give up well inside that |

## Fail-open

The plugin never blocks Cline. API down, timeout, malformed response — every
path degrades to "no injection, no capture, no error", and a prompt whose
recall timed out is not re-queried on the next iteration. Check
`palinode status` if memory seems quiet.

There is no host-side policy doing this for you: in the Cline SDK a hook that
throws fails the run, so fail-open is enforced inside the plugin.

## Which Cline API

Built against the Cline SDK's runtime-native `AgentPlugin` hooks
(`@cline/sdk` 0.0.75): `hooks.beforeModel({ snapshot, request })` returning
`{ messages }`, and `hooks.afterRun({ snapshot, result })`. The docs page
(`docs.cline.bot/sdk/plugins`) names these same seven hooks; its "Hook
Stages" (`before_agent_start`, `run_end`, …) and "Hook Policies"
(`fail_closed`, `timeoutMs`, `retries`) tables describe nothing that exists
on `AgentPlugin` in that SDK, so this plugin does not depend on them.

Recall and capture are limited to the root agent — subagents (agents-squad
style teams) share the plugin instance but get no injection.

## Development

```bash
npm install
npm test        # vitest — runs without a Cline install
npm run build   # tsc → dist/ (binding + shared core)
npm run test:sdk  # opt-in: installs @cline/sdk (--no-save) and type-checks the
                  # plugin against the real AgentPlugin type
```

The plugin is structural-typed against the slice of Cline's plugin API it
uses (`conformance/check.ts` proves the structure matches the real
`AgentPlugin`, and that Cline's `beforeModel` result admits no
`systemPrompt`), so nothing here depends on Cline's own packages at build
time. All Palinode logic lives in `plugins/core`; this package is the wiring.
