import { describe, expect, it } from "vitest";
import {
  buildCoreDigest,
  buildRecallContext,
  buildSessionCapture,
  configFromEnv,
  postSessionCapture,
  type FetchFn,
  type PalinodeConfig,
} from "../src/core.js";

const CFG: PalinodeConfig = {
  apiUrl: "http://test:6340",
  maxResults: 3,
  threshold: 0.5,
  triggersOn: true,
  minChars: 12,
  maxChars: 3000,
  timeoutMs: 4000,
  coreMaxFiles: 10,
  coreMaxChars: 4000,
  minMessages: 3,
};

const PROMPT = "how did we decide to handle the deploy rollback for the api?";

/** Route-matching fetch stub. Records calls; unrouted paths 404. */
function stubFetch(routes: Record<string, unknown>, calls: Array<{ url: string; body?: unknown }> = []): FetchFn {
  return (async (url: unknown, init?: { body?: unknown }) => {
    const u = String(url);
    calls.push({ url: u, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const hit = Object.entries(routes).find(([path]) => u.includes(path));
    if (!hit) return new Response("not found", { status: 404 });
    return new Response(JSON.stringify(hit[1]), { status: 200 });
  }) as FetchFn;
}

const failingFetch: FetchFn = (async () => {
  throw new Error("connection refused");
}) as FetchFn;

describe("buildRecallContext", () => {
  it("renders search hits as bounded snippet lines", async () => {
    const fetchFn = stubFetch({
      "/check-triggers": [],
      "/search": {
        results: [
          // score is the fused rank value (~1.0 for any top hit); raw_score
          // is the cosine the threshold knob filters on. Display must use
          // raw_score so the on-screen number matches the tunable scale.
          { rel_path: "decisions/deploy-rollback.md", score: 1.0, raw_score: 0.62, snippet: "git revert + reindex" },
        ],
      },
    });
    const ctx = await buildRecallContext(PROMPT, CFG, fetchFn);
    expect(ctx).toContain("[decisions/deploy-rollback.md] (62%) git revert + reindex");
    expect(ctx).not.toContain("(100%)");
    expect(ctx).toContain("Related memories");
    expect(ctx).toContain("may be stale");
  });

  it("injects fired-trigger content via /read", async () => {
    const fetchFn = stubFetch({
      "/check-triggers": [{ id: "t1", memory_file: "decisions/deploy-rollback.md", score: 0.9 }],
      "/read": { content: "Full rollback decision body." },
      "/search": { results: [] },
    });
    const ctx = await buildRecallContext(PROMPT, CFG, fetchFn);
    expect(ctx).toContain("Trigger fired: decisions/deploy-rollback.md");
    expect(ctx).toContain("Full rollback decision body.");
  });

  it("sends the strict defaults in the search payload", async () => {
    const calls: Array<{ url: string; body?: unknown }> = [];
    const fetchFn = stubFetch({ "/check-triggers": [], "/search": { results: [] } }, calls);
    await buildRecallContext(PROMPT, CFG, fetchFn);
    const search = calls.find((c) => c.url.includes("/search"));
    expect(search?.body).toMatchObject({ limit: 3, threshold: 0.5, max_chars: 300 });
  });

  it("returns null when nothing is recalled", async () => {
    const fetchFn = stubFetch({ "/check-triggers": [], "/search": { results: [] } });
    expect(await buildRecallContext(PROMPT, CFG, fetchFn)).toBeNull();
  });

  it("returns null (never throws) when the API is down", async () => {
    expect(await buildRecallContext(PROMPT, CFG, failingFetch)).toBeNull();
  });

  it("skips trivial prompts before any network call", async () => {
    const calls: Array<{ url: string }> = [];
    const fetchFn = stubFetch({}, calls);
    expect(await buildRecallContext("ok", CFG, fetchFn)).toBeNull();
    expect(calls).toHaveLength(0);
  });

  it("respects channel switches", async () => {
    const calls: Array<{ url: string }> = [];
    const fetchFn = stubFetch({ "/check-triggers": [], "/search": { results: [] } }, calls);
    await buildRecallContext(PROMPT, { ...CFG, maxResults: 0 }, fetchFn);
    expect(calls.some((c) => c.url.includes("/search"))).toBe(false);
    calls.length = 0;
    await buildRecallContext(PROMPT, { ...CFG, triggersOn: false }, fetchFn);
    expect(calls.some((c) => c.url.includes("check-triggers"))).toBe(false);
  });

  it("bounds total context at maxChars", async () => {
    const fetchFn = stubFetch({
      "/check-triggers": [{ memory_file: "a.md" }],
      "/read": { content: "x".repeat(50_000) },
      "/search": { results: [] },
    });
    const ctx = await buildRecallContext(PROMPT, { ...CFG, maxChars: 500 }, fetchFn);
    expect(ctx).not.toBeNull();
    expect(ctx!.length).toBeLessThanOrEqual(500);
  });

  it("caps fired triggers at two files", async () => {
    const calls: Array<{ url: string }> = [];
    const fetchFn = stubFetch(
      {
        "/check-triggers": [
          { memory_file: "a.md" },
          { memory_file: "b.md" },
          { memory_file: "c.md" },
        ],
        "/read": { content: "body" },
        "/search": { results: [] },
      },
      calls,
    );
    await buildRecallContext(PROMPT, CFG, fetchFn);
    expect(calls.filter((c) => c.url.includes("/read")).length).toBe(2);
  });
});

describe("auth", () => {
  it("sends the bearer token when configured, and no header otherwise", async () => {
    let seenAuth: string | null | undefined;
    const fetchFn = (async (_url: unknown, init?: { headers?: Record<string, string> }) => {
      seenAuth = init?.headers?.["Authorization"];
      return new Response("[]", { status: 200 });
    }) as FetchFn;

    await buildRecallContext(PROMPT, { ...CFG, maxResults: 0, token: "sekrit" }, fetchFn);
    expect(seenAuth).toBe("Bearer sekrit");

    await buildRecallContext(PROMPT, { ...CFG, maxResults: 0 }, fetchFn);
    expect(seenAuth).toBeUndefined();
  });
});

describe("buildCoreDigest", () => {
  it("primes the server and renders a bounded core digest", async () => {
    const calls: Array<{ url: string }> = [];
    const fetchFn = stubFetch(
      {
        "/context/prime": {},
        "/list": [
          { file: "projects/palinode.md", name: "Palinode", summary: "memory system" },
        ],
      },
      calls,
    );
    const digest = await buildCoreDigest(CFG, fetchFn, "/tmp/proj", "s1");
    expect(calls.some((c) => c.url.includes("/context/prime"))).toBe(true);
    expect(digest).toContain("[projects/palinode.md] Palinode — memory system");
    expect(digest).toContain("session start");
  });

  it("returns null with no core memories, and skips listing when disabled", async () => {
    const calls: Array<{ url: string }> = [];
    const fetchFn = stubFetch({ "/context/prime": {}, "/list": [] }, calls);
    expect(await buildCoreDigest(CFG, fetchFn)).toBeNull();
    calls.length = 0;
    expect(await buildCoreDigest({ ...CFG, coreMaxFiles: 0 }, fetchFn)).toBeNull();
    expect(calls.some((c) => c.url.includes("/list"))).toBe(false);
  });

  it("is fail-open when the API is down", async () => {
    expect(await buildCoreDigest(CFG, failingFetch)).toBeNull();
  });
});

describe("session capture", () => {
  const entries = (n: number) =>
    Array.from({ length: n }, (_, i) => ({
      type: "message",
      message: { role: "user", content: `prompt ${i}: fix the rollback path` },
    }));

  it("derives the floor payload from entries", () => {
    const payload = buildSessionCapture(entries(4), CFG, "myproj");
    expect(payload).not.toBeNull();
    expect(payload!.summary).toContain("4 messages");
    expect(payload!.summary).toContain("prompt 0: fix the rollback path");
    expect(payload!.source).toBe("pi-extension");
    expect(payload!.project).toBe("myproj");
  });

  it("skips trivial sessions below the message floor", () => {
    expect(buildSessionCapture(entries(2), CFG, "p")).toBeNull();
  });

  it("posts fail-open", async () => {
    const payload = buildSessionCapture(entries(3), CFG, "p")!;
    expect(await postSessionCapture(payload, CFG, failingFetch)).toBe(false);
    const okFetch = stubFetch({ "/session-end": { status: "ok" } });
    expect(await postSessionCapture(payload, CFG, okFetch)).toBe(true);
  });
});

describe("configFromEnv", () => {
  it("shares the Claude Code hook env vars — one set of knobs, every harness", () => {
    const cfg = configFromEnv({
      PALINODE_API_URL: "http://remote:6340",
      PALINODE_API_TOKEN: "t",
      PALINODE_HOOK_RECALL_MAX_RESULTS: "5",
      PALINODE_HOOK_RECALL_THRESHOLD: "0.8",
      PALINODE_HOOK_RECALL_TRIGGERS: "0",
      PALINODE_HOOK_RECALL_MIN_CHARS: "20",
      PALINODE_HOOK_RECALL_MAX_CHARS: "1000",
      PALINODE_HOOK_RECALL_TIMEOUT: "2",
      PALINODE_HOOK_INJECT_MAX_FILES: "5",
      PALINODE_HOOK_INJECT_MAX_CHARS: "2000",
      PALINODE_HOOK_MIN_MESSAGES: "1",
    });
    expect(cfg).toEqual({
      apiUrl: "http://remote:6340",
      token: "t",
      maxResults: 5,
      threshold: 0.8,
      triggersOn: false,
      minChars: 20,
      maxChars: 1000,
      timeoutMs: 2000,
      coreMaxFiles: 5,
      coreMaxChars: 2000,
      minMessages: 1,
    });
  });

  it("defaults match the Claude Code hook defaults", () => {
    const cfg = configFromEnv({});
    expect(cfg.apiUrl).toBe("http://localhost:6340");
    expect(cfg.maxResults).toBe(3);
    // Calibrated default: 0.5 = 98% measured recall; 0.7+ was near-dead.
    expect(cfg.threshold).toBe(0.5);
    expect(cfg.triggersOn).toBe(true);
    expect(cfg.minChars).toBe(12);
    expect(cfg.maxChars).toBe(3000);
    expect(cfg.timeoutMs).toBe(4000);
    expect(cfg.minMessages).toBe(3);
  });
});
