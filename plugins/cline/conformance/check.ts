/**
 * SDK conformance check — opt-in, not part of `npm test`.
 *
 * The plugin is structurally typed so it builds without `@cline/sdk`. This
 * file proves the structure still matches the real `AgentPlugin` type, and
 * that Cline's `beforeModel` result cannot carry a system prompt (the
 * ADR-019 §4 invariant, enforced by the host's own types).
 *
 *   npm run test:sdk     # installs @cline/sdk with --no-save, then type-checks this file
 *
 * Last verified against @cline/sdk 0.0.75.
 */
import type { AgentPlugin } from "@cline/sdk";
import plugin, { createPalinodePlugin } from "../src/index.js";

const fromEnv: AgentPlugin = plugin;
const fromOptions: AgentPlugin = createPalinodePlugin({
  apiUrl: "http://localhost:6340",
  token: "token",
  threshold: 0.5,
  recallProfile: "coding",
});

type Hooks = NonNullable<AgentPlugin["hooks"]>;
type BeforeModel = NonNullable<Hooks["beforeModel"]>;
type AfterRun = NonNullable<Hooks["afterRun"]>;
const beforeModel: BeforeModel = plugin.hooks.beforeModel;
const afterRun: AfterRun = plugin.hooks.afterRun;

// `AgentBeforeModelResult` must not admit `systemPrompt`: if a future SDK adds
// it, this line stops compiling and the invariant needs a runtime guard.
type BeforeModelResult = NonNullable<Awaited<ReturnType<BeforeModel>>>;
type AdmitsSystemPrompt = "systemPrompt" extends keyof BeforeModelResult ? true : false;
const admitsSystemPrompt: AdmitsSystemPrompt = false;

void [fromEnv, fromOptions, beforeModel, afterRun, admitsSystemPrompt];
