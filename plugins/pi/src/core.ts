/**
 * Pure Palinode client logic for the Pi extension.
 *
 * Everything here is a plain function over an injected `fetch`, so the whole
 * recall/prime/capture surface is testable without a Pi install. The Pi
 * binding in `index.ts` is deliberately thin: it wires these functions to
 * lifecycle events and nothing else.
 *
 * Design contract (shared with the Claude Code hooks — same knobs, same
 * semantics, same env var names):
 *   - Fail-open everywhere. API down, timeout, bad JSON → null, never throw.
 *   - Silence is the common case and must be free: no recall → no message.
 *   - Injected recall is bounded: few results, tight snippets, total cap.
 */

export interface PalinodeConfig {
  apiUrl: string;
  token?: string;
  /** Search hits injected per prompt; 0 disables the search channel. */
  maxResults: number;
  /** Similarity floor for per-turn search. */
  threshold: number;
  /** Trigger channel on/off. */
  triggersOn: boolean;
  /** Prompts shorter than this skip recall entirely. */
  minChars: number;
  /** Total cap on per-turn injected context. */
  maxChars: number;
  /** Per-request timeout in milliseconds. */
  timeoutMs: number;
  /** Max core memories in the session-start digest; 0 disables priming. */
  coreMaxFiles: number;
  /** Total cap on the session-start digest. */
  coreMaxChars: number;
  /** Minimum user messages before session capture fires. */
  minMessages: number;
}

/** Per-fired-trigger content cap and max fired triggers injected per prompt.
 *  Mirrors the Claude Code hook's constants. */
const TRIGGER_READ_CHARS = 1200;
const TRIGGER_MAX_FIRED = 2;
/** Per-result snippet cap requested from /search. */
const SNIPPET_MAX_CHARS = 300;

type Env = Record<string, string | undefined>;

function num(env: Env, key: string, fallback: number): number {
  const raw = env[key];
  if (raw === undefined || raw === "") return fallback;
  const n = Number(raw);
  return Number.isFinite(n) ? n : fallback;
}

/** Same env vars as the Claude Code hooks — one set of knobs, every harness. */
export function configFromEnv(env: Env = process.env): PalinodeConfig {
  return {
    apiUrl: env.PALINODE_API_URL ?? "http://localhost:6340",
    token: env.PALINODE_API_TOKEN || undefined,
    maxResults: num(env, "PALINODE_HOOK_RECALL_MAX_RESULTS", 3),
    // Raw-cosine floor, calibrated in the server's SearchConfig against real
    // bge-m3 (54 pairs): true matches clear 0.5 at 98% but 0.7 at only 28%.
    // An earlier 0.75 default made the search channel silently dead.
    threshold: num(env, "PALINODE_HOOK_RECALL_THRESHOLD", 0.5),
    triggersOn: env.PALINODE_HOOK_RECALL_TRIGGERS !== "0",
    minChars: num(env, "PALINODE_HOOK_RECALL_MIN_CHARS", 12),
    maxChars: num(env, "PALINODE_HOOK_RECALL_MAX_CHARS", 3000),
    timeoutMs: num(env, "PALINODE_HOOK_RECALL_TIMEOUT", 4) * 1000,
    coreMaxFiles: num(env, "PALINODE_HOOK_INJECT_MAX_FILES", 10),
    coreMaxChars: num(env, "PALINODE_HOOK_INJECT_MAX_CHARS", 4000),
    minMessages: num(env, "PALINODE_HOOK_MIN_MESSAGES", 3),
  };
}

export type FetchFn = typeof fetch;

/** One fail-open request. Any failure — network, HTTP >= 400, bad JSON —
 *  resolves to null. The caller decides what silence means. */
async function apiJson(
  cfg: PalinodeConfig,
  fetchFn: FetchFn,
  path: string,
  init?: { method?: string; body?: unknown },
): Promise<unknown | null> {
  try {
    const headers: Record<string, string> = {};
    if (init?.body !== undefined) headers["Content-Type"] = "application/json";
    if (cfg.token) headers["Authorization"] = `Bearer ${cfg.token}`;
    const res = await fetchFn(`${cfg.apiUrl}${path}`, {
      method: init?.method ?? (init?.body !== undefined ? "POST" : "GET"),
      headers,
      body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
      signal: AbortSignal.timeout(cfg.timeoutMs),
    });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

interface SearchHit {
  rel_path?: string;
  file_path?: string;
  /** Post-fusion rank value — ~1.0 for any query's top hit. Display fallback only. */
  score?: number;
  /** Raw cosine — the scale the threshold knob filters on. Preferred for display. */
  raw_score?: number;
  snippet?: string;
  content?: string;
}

interface FiredTrigger {
  memory_file?: string;
}

/**
 * Per-turn recall: prospective triggers + strict-threshold search.
 * Returns the context block to inject, or null when there is nothing to say.
 */
export async function buildRecallContext(
  prompt: string,
  cfg: PalinodeConfig,
  fetchFn: FetchFn = fetch,
): Promise<string | null> {
  if (prompt.length < cfg.minChars) return null;

  let sections = "";

  if (cfg.triggersOn) {
    const fired = await apiJson(cfg, fetchFn, "/check-triggers", {
      body: { query: prompt },
    });
    if (Array.isArray(fired)) {
      for (const t of (fired as FiredTrigger[]).slice(0, TRIGGER_MAX_FIRED)) {
        if (!t.memory_file) continue;
        const read = (await apiJson(
          cfg,
          fetchFn,
          `/read?file_path=${encodeURIComponent(t.memory_file)}`,
        )) as { content?: string } | null;
        const body = read?.content;
        if (body) {
          sections += `\n### Trigger fired: ${t.memory_file}\n${body.slice(0, TRIGGER_READ_CHARS)}\n`;
        }
      }
    }
  }

  if (cfg.maxResults > 0) {
    const hits = (await apiJson(cfg, fetchFn, "/search", {
      body: {
        query: prompt,
        limit: cfg.maxResults,
        threshold: cfg.threshold,
        max_chars: SNIPPET_MAX_CHARS,
      },
    })) as { results?: SearchHit[] } | SearchHit[] | null;
    const results = Array.isArray(hits) ? hits : hits?.results;
    if (Array.isArray(results) && results.length > 0) {
      const lines = results
        .map((r) => {
          const path = r.rel_path ?? r.file_path ?? "?";
          // raw_score, not score: the fused rank value reads ~100% for the
          // top hit of ANY query; raw cosine is the threshold knob's scale,
          // so showing it makes the lever tunable from what the user sees.
          const pct = Math.floor((r.raw_score ?? r.score ?? 0) * 100);
          const body = (r.snippet ?? r.content ?? "").replace(/\n/g, " ");
          return `- [${path}] (${pct}%) ${body}`;
        })
        .join("\n");
      sections += `\n### Related memories\n${lines}\n`;
    }
  }

  if (!sections) return null;

  const context = `## Palinode recall (this prompt)

Retrieved from persistent memory; may be stale — verify before relying on
it. More detail: palinode_search / palinode_read.
${sections}`;

  return context.slice(0, cfg.maxChars);
}

interface CoreListEntry {
  file?: string;
  name?: string;
  summary?: string;
}

/**
 * Session-start priming: warm server-side session context, then return a
 * bounded digest of `core: true` memories — or null when there are none.
 */
export async function buildCoreDigest(
  cfg: PalinodeConfig,
  fetchFn: FetchFn = fetch,
  cwd: string = process.cwd(),
  sessionId = "",
): Promise<string | null> {
  // Warm /context/prime; result deliberately ignored (older servers 404).
  await apiJson(cfg, fetchFn, "/context/prime", {
    body: { cwd, session_id: sessionId },
  });

  if (cfg.coreMaxFiles <= 0) return null;

  const listing = await apiJson(cfg, fetchFn, "/list?core_only=true");
  if (!Array.isArray(listing) || listing.length === 0) return null;

  const lines = (listing as CoreListEntry[])
    .slice(0, cfg.coreMaxFiles)
    .map((e) => {
      const name = e.name ?? "untitled";
      const summary = e.summary ? ` — ${e.summary}` : "";
      return `- [${e.file ?? "?"}] ${name}${summary}`;
    })
    .join("\n");

  const context = `## Palinode memory (session start)

Persistent memory is connected. Recall details with the palinode_search /
palinode_read tools — they read the live store; session notes are NOT
files in this repo.

Core memories:
${lines}`;

  return context.slice(0, cfg.coreMaxChars);
}

/** The subset of a Pi session entry the capture floor reads. */
export interface SessionEntryLike {
  type?: string;
  role?: string;
  message?: { role?: string; content?: unknown };
}

function entryRole(e: SessionEntryLike): string | undefined {
  return e.role ?? e.message?.role ?? e.type;
}

function entryText(e: SessionEntryLike): string {
  const c = e.message?.content;
  if (typeof c === "string") return c;
  if (Array.isArray(c)) {
    return c
      .map((b) => (typeof b === "object" && b && "text" in b ? String((b as { text: unknown }).text) : ""))
      .join(" ");
  }
  return "";
}

/**
 * Session capture floor: derive the minimal /session-end payload from the
 * session entries. Returns null when the session is too trivial to keep
 * (fewer than `minMessages` user messages) — same gate as the Claude Code
 * floor hook.
 */
export function buildSessionCapture(
  entries: SessionEntryLike[],
  cfg: PalinodeConfig,
  project: string,
): { summary: string; project: string; source: string; decisions: string[]; blockers: string[] } | null {
  const userEntries = entries.filter((e) => entryRole(e) === "user");
  if (userEntries.length < cfg.minMessages) return null;

  const firstPrompt = entryText(userEntries[0])
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 200);

  return {
    summary: `Auto-captured (pi session_shutdown, ${userEntries.length} messages). Topic: ${firstPrompt}`,
    project,
    source: "pi-extension",
    decisions: [],
    blockers: [],
  };
}

/** POST the capture; fail-open. Returns true when the API accepted it. */
export async function postSessionCapture(
  payload: NonNullable<ReturnType<typeof buildSessionCapture>>,
  cfg: PalinodeConfig,
  fetchFn: FetchFn = fetch,
): Promise<boolean> {
  const res = await apiJson(cfg, fetchFn, "/session-end", { body: payload });
  return res !== null;
}
