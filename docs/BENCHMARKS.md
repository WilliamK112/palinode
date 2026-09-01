# Benchmarks

Palinode's memory layer measured on a public long-term-memory benchmark, with the
methodology and the losses stated. Everything here is reproducible from `bench/longmemeval/`;
raw per-question results live in `bench/results/`.

## LongMemEval (S)

[LongMemEval](https://github.com/xiaowu0162/LongMemEval) (Wu et al., ICLR 2025): 500 questions,
each with its own haystack of ~40 chat sessions (~115k tokens), across seven abilities —
single-session user / assistant / preference, multi-session reasoning, knowledge updates,
temporal reasoning, and abstention.

### What was measured

- **Memory layer (constant across rows):** every haystack session is saved as a dated markdown
  note through Palinode's normal indexer — no chat-LLM call at ingest — and recalled with
  hybrid search (BM25 + `bge-m3` vectors, RRF), top-10, one chunk per session. The answerer
  sees only those ten sessions plus the question date.
- **Evidence recall@10:** whether a session containing the answer is in the top-10. This is
  the memory layer's own number.
- **Accuracy:** upstream's judge, `gpt-4o-2024-08-06`, upstream's per-type prompts verbatim,
  temperature 0. Comparable to the numbers in the LongMemEval paper and to vendor tables that
  used the same judge.
- **Answerer (the only thing that varies between rows):** a different reader in each row,
  so the delta between rows isolates reader quality against a fixed retrieval ceiling.

### Results

| | evidence recall@10 | **A** local 30B | **B** Gemini 3 Flash | **C** GPT-5.5 | **D** GPT-4o |
|---|---|---|---|---|---|
| single-session-assistant (56) | 1.000 | 0.929 | 0.982 | 0.982 | 1.000 |
| single-session-user (64) | 0.938 | 0.812 | 0.891 | 0.891 | 0.938 |
| knowledge-update (72) | 1.000 | 0.514 | 0.875 | 0.958 | 0.847 |
| abstention (30) | — | 0.700 | 0.900 | 0.933 | 0.700 |
| multi-session (121) | 0.992 | 0.306 | 0.736 | 0.826 | 0.620 |
| single-session-preference (30) | 0.933 | 0.233 | 0.733 | 0.733 | 0.433 |
| temporal-reasoning (127) | 0.984 | 0.276 | 0.732 | 0.866 | 0.732 |
| **overall (500)** | **0.981** | **0.482** | **0.812** | **0.882** | **0.758** |

Row answerers: **A** `qwen3-coder-30b-a3b-instruct` (4-bit, local, LM Studio) · **B**
`gemini-3-flash-preview` (thinking off) · **C** `gpt-5.5` via the Codex CLI (`codex exec`, read-only
sandbox; its reported token count includes Codex's own system prompt) · **D** `gpt-4o-2024-08-06`
— the reader Zep and Supermemory report with, so row D is the directly comparable number. Embedder in all
rows: `bge-m3` via Ollama. Evidence recall is reported from row B (0.981); row A measured
0.951 on the same haystacks with the same retrieval — the difference is 13 questions that
fell back to keyword-only retrieval during a transient embedder outage, documented in
`bench/results/longmemeval-s-rowA-2026-08-27/`.

### Cost

Per question, mean: ingest **10–12 s** with **zero chat-LLM calls**; retrieval **0.4 s**;
answerer prompt **~22–24k tokens** (ten full sessions). Answer latency: B 2 s, C 6 s (p50).

### Reading the table

- **Retrieval is not the limiter.** The evidence is in the top-10 for 98 % of questions and
  for ≥99 % of multi-session and knowledge-update questions. What a comparison table reports
  is mostly the reader: the same memory layer scores 0.48 with a local 30B model, 0.76 with
  GPT-4o, 0.81 with Gemini 3 Flash, and 0.88 with GPT-5.5.
- **Where we lose, with the evidence in hand:** multi-session and temporal reasoning sit at
  0.73 (B) / 0.83–0.87 (C) despite ≥0.98 recall — the reader has the sessions and still
  fails to combine or compute. Preference (0.73 in both B and C, recall 0.93) is the one type
  where a stronger reader didn't help: the rubric wants the answer *personalised* to facts in
  the sessions, and neither reader reliably does that from ten raw transcripts. Fixing either
  is not a retrieval change. Abstention (0.90 / 0.93) is graded by whether the reader
  declines; retrieval can't help or hurt it.
- **Token cost is the lever we haven't pulled.** Ten full sessions is ~23k tokens per answer.
  A tiered read (abstract → overview → full) or a smaller top-k is the obvious next row.

### Against published numbers

Row D is the configuration other systems report with on LongMemEval_S — `gpt-4o` as both
reader and judge — so it is the only row that belongs in the same table as theirs. Vendor
numbers are self-reported and single-run; ours is too.

| system | reader | overall |
|---|---|---|
| Full-context GPT-4o, no memory system (LongMemEval paper) | GPT-4o | 0.602 |
| Zep | GPT-4o | 0.712 |
| **Palinode, memory layer only (row D)** | GPT-4o | **0.758** |
| Supermemory | GPT-4o | 0.816 |
| Oracle retrieval (paper upper bound) | GPT-4o | ~0.87–0.92 |

Two things to keep in view. Zep and Supermemory report after an extraction + consolidation
step; row D is raw session transcripts with no write-time processing — Palinode's own
consolidation layer is not in the table yet. And the reader dominates the number: rows B and
C, on the same memory, land at 0.81 and 0.88, level with or above the best published rows on
matched-class readers (Supermemory reports 0.852 with Gemini 3 and 0.846 with GPT-5).

### Reader sensitivity to the answer prompt

`gpt-4o-2024-08-06` scored **0.578** under the original answer instruction ("say clearly that
the information is not available in memory — do not guess"): it declined on all 30 abstention
questions and on 152 of its 183 other misses, with the answer-bearing session in the prompt for
143 of them. Re-asked with the same context under a softened instruction ("read all of them
carefully; only if none contain relevant information, say so") it answered those correctly. The
other readers were not affected: Gemini 3 Flash scored 0.760 vs 0.750 on the same 100-question
stratified subset under the two instructions. Row D therefore uses the softened prompt (v2);
rows A–C used v1. Both are in `bench/longmemeval/adapter.py`, versioned and recorded in each
run's metadata; the v1 gpt-4o run is kept in
`bench/results/longmemeval-s-rowD-v1prompt-2026-08-29/` so the effect is reproducible.

### Judge choice

Row A was originally judged with `gemini-2.5-flash` and re-scored with `gpt-4o-2024-08-06`
(`bench/longmemeval/rejudge`): agreement 0.950 on 500 items, with Gemini the stricter judge
(21 flips to correct, 4 to incorrect, concentrated on rubric-graded preference questions).
All numbers above are under the gpt-4o judge.

### Caveats

- Single run per row; no confidence intervals yet.
- LongMemEval's answer key is community-maintained; we used `longmemeval_s_cleaned.json`
  unmodified.
- The answerer prompt is a single generic instruction (v1 for rows A–C, v2 for row D — see
  *Reader sensitivity* above). No per-type prompting.
- Session-level chunks only. These rows measure the memory layer **before** any write-time
  intelligence — no extraction, no consolidation, no reranking. Systems that report after an
  extraction + consolidation step are measuring something Palinode also does (`session_end` →
  project document → consolidation ops) but which is not in this table yet; that row is planned.

### Reproduce

```bash
python -m bench.longmemeval.run --variant s --out bench/results/<name>        # needs LME_ANSWER_* / LME_JUDGE_*
python -m bench.longmemeval.rejudge bench/results/<name> --out bench/results/<name>/rejudge-gpt4o
```

See `bench/longmemeval/README.md` for endpoints, the supervisor for multi-hour runs, and the
fallbacks the harness applies when the embedder misbehaves.
