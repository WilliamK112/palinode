# bench/longmemeval — LongMemEval × Palinode

Runs [LongMemEval](https://github.com/xiaowu0162/LongMemEval) (Wu et al., ICLR 2025) against a
real Palinode store. 500 questions, six types plus abstention (`_abs` ids). Each question ships
its own haystack (~40 sessions / ~115k tokens for `_s`), so **each question gets a fresh store**:
sessions → dated `daily/` notes → canonical `index_file` → hybrid recall → external answerer →
upstream judge prompt, verbatim.

## Configuration rows (report all three)

| row | flag | what it measures |
|---|---|---|
| save-only | *(default)* | LLM-free ingest + hybrid search. Zero chat-LLM calls before the answerer. |
| consolidated | `--consolidate` | Real `run_consolidation` over the haystack, then search. Needs the consolidation model configured in `palinode.config.yaml`. |
| retrieval-only | `--no-answer` | No LLM at all. Reports **evidence recall@k** (was any `answer_session_id` in the top-k?) — the retrieval ceiling. |

## Models — different vendors, on purpose

```bash
export LME_ANSWER_BASE_URL=https://api.anthropic.com/v1  LME_ANSWER_MODEL=claude-sonnet-5  LME_ANSWER_API_KEY=…
export LME_JUDGE_BASE_URL=https://api.openai.com/v1      LME_JUDGE_MODEL=gpt-4o-2024-08-06   LME_JUDGE_API_KEY=…
```

Upstream judges with `gpt-4o-2024-08-06`, temperature 0, `max_tokens=10`, label = `"yes" in text.lower()`.
Keep that judge for comparability; never judge with the answerer's family. The runner warns if
they match. Embeddings are whatever the pointed-at Palinode config uses (bge-m3 via Ollama by
default); if no embedder is reachable it **degrades to keyword-only and says so** in `meta`.

## Answerer via the Codex CLI (no API key)

`LME_ANSWER_BASE_URL=codex://local LME_ANSWER_MODEL=gpt-5.5` routes the answerer through
`codex exec` on the ChatGPT-subscription OAuth session — read-only sandbox, `--ephemeral`,
`--ignore-user-config --ignore-rules`, empty cwd, prompt on stdin, reply via
`--output-last-message`. Codex prepends its own system prompt (~9k tokens on an empty prompt),
so `prompt_tokens` is Codex's reported total, not ours. Answerer only: the judge must stay the
upstream `gpt-4o-2024-08-06` on the metered API for comparability.

## Re-judging a finished run

```bash
LME_JUDGE_MODEL=gpt-4o-2024-08-06 LME_JUDGE_API_KEY=… \
  python -m bench.longmemeval.rejudge bench/results/<run> --out bench/results/<run>/rejudge-gpt4o
```

Reads `results.json` (or `hypotheses.jsonl` + `--data`), judges every hypothesis with the
upstream prompts, writes the same `results.json`/`report.md` shape plus `summary.agreement`
against the original labels — the judge-agreement number for a row judged with something else.
Resumable via `rows.jsonl`. A run made with `--no-judge` is judged the same way.

## Run

```bash
# smoke: 20 questions, retrieval only — no API keys needed
python -m bench.longmemeval.run --variant s --limit 20 --no-answer --out bench/results/lme-smoke

# full _s, save-only row
python -m bench.longmemeval.run --variant s --out bench/results/lme-s-save-only

# hypotheses only, judge with the upstream script instead
python -m bench.longmemeval.run --variant s --no-judge --out bench/results/lme-s
python LongMemEval/src/evaluation/evaluate_qa.py gpt-4o bench/results/lme-s/hypotheses.jsonl ~/.cache/longmemeval/longmemeval_s_cleaned.json
```

Outputs per run dir: `results.json` (meta + summary + per-question rows incl. retrieved session
ids, token usage, judge raw text), `hypotheses.jsonl` (upstream format), `report.md`.

Dataset is fetched once to `~/.cache/longmemeval/` (`LONGMEMEVAL_DATA` to override). Scratch
store defaults to `/tmp/lme-palinode-store` (`--store-dir` / `LME_STORE_DIR`); it is wiped per
question and never touches a real `PALINODE_DIR`.

## Running for hours without babysitting

Two real failure modes hit the first full run: an unhandled backend exception killed the
process, and a *deterministic* per-input failure (bge-m3 NaN → HTTP 500) was retried
through minutes of backoff per question with nothing outside noticing. The harness now has
three layers against that:

1. **Inside the run** — every finished question is appended to `rows.jsonl`; `--resume` skips
   them, `--retry-errors` re-runs the ones that ended in an error. Deterministic failures
   (NaN, context-length, 4xx) are never retried; transient ones back off 15/45/90 s, dropping
   the pooled Ollama client between tries. Embed-failed files are re-indexed keyword-only;
   a query that won't embed is retried as content words, then keyword-only.
2. **Heartbeat** — `status.json` next to `rows.jsonl` is rewritten at every question start
   and end (`phase`, `qid`, `done`, `total`, `updated_at`).
   Keep `2 × LME_<ROLE>_TIMEOUT_S` under the supervisor's stall threshold: the client makes
   one retry, so a hung backend call costs at most two timeouts before the row is recorded as
   an error — longer than `STALL_MIN` and the watchdog restarts the whole process instead.
3. **Supervisor** — `supervise.sh` runs the command under `caffeinate -i` (no idle sleep),
   appends `--resume --retry-errors`, and restarts it whenever it exits before
   `phase=done` or the heartbeat goes stale for `STALL_MIN` minutes (default 12).
   Gives up after `MAX_RESTARTS` (25). Logs to `<out>/supervise.log`, run output to
   `<out>/run.log`.

```bash
nohup bench/longmemeval/supervise.sh bench/results/lme-s -- \
  python -m bench.longmemeval.run --variant s --out bench/results/lme-s &
```

## Publishing rules

Three runs per row, mean ± CI; report per-type accuracy, evidence recall, prompt tokens per
answer, wall-clock; pin every model version; include the types where we lose.
