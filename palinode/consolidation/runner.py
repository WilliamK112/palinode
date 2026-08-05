"""
Consolidation Runner

Orchestrates weekly memory consolidation: daily → curated.
Uses a configurable LLM for distillation (any OpenAI-compatible endpoint).
"""
from __future__ import annotations

import os
import re
import json
import glob
import logging
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

# The propose→apply seam. The nondeterministic half of consolidation is a
# single call shaped (system_prompt, user_prompt) -> (response_text, model_used);
# the deterministic half (parse → executor.apply_operations) runs on its output.
# Making this callable injectable lets the runner→executor path be driven with
# canned op-JSON — no live LLM, no wholesale mock of _consolidate_project. The
# default is the live fallback-chain caller; tests pass a fake that returns
# deterministic op-JSON. Kept here (not a separate module) so the client-factory
# patch seam test_fallback relies on — `runner.get_ollama_client` — stays put.
LlmFn = Callable[[str, str], tuple[str, str]]

import yaml

from palinode.core.config import config
from palinode.core import store, embedder, git_tools
from palinode.core.ollama_client import OllamaError, OllamaRole, get_ollama_client
from palinode.core.parser import split_frontmatter
from palinode.consolidation import status_doc
from palinode.consolidation.op_parse import op_kind, op_reason, parse_operations

logger = logging.getLogger("palinode.consolidation")


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)

def _git_commit(message: str, files: list[str] | None = None) -> None:
    """Commit consolidation mutations through the git_tools choke point.

    One-mutation-one-commit: ``files`` is the explicit list of memory files this
    pass mutated; each gets its own per-file commit so a consolidation touching
    N files produces N commits, never a repo-wide ``git add *.md`` sweep that
    would conflate unrelated working-tree edits under one message. The
    per-file ``message`` is suffixed with the file basename for blameability.

    ``files=None`` is retained only for callers with nothing concrete to stage;
    it is a no-op (we never sweep the repo). All real consolidation/ttl callers
    pass an explicit list.
    """
    if not config.git.auto_commit:
        return
    if not files:
        return
    # De-duplicate while preserving order — a project and its history sibling
    # may be listed more than once across a multi-project pass.
    seen: set[str] = set()
    for file_path in files:
        if file_path in seen or not os.path.exists(file_path):
            continue
        seen.add(file_path)
        base = os.path.basename(file_path)
        git_tools.commit_memory_file(file_path, f"{message} [{base}]")


def _touched_files(target: str) -> list[str]:
    """Files a single project compaction may have mutated.

    The op target itself plus its ``-history.md`` sibling, which the executor
    appends to on SUPERSEDE/ARCHIVE/RETRACT. Mirrors the path derivation in
    ``executor.append_to_history`` so a history append is committed alongside
    its parent mutation rather than swept up later.
    """
    base = re.sub(r"-status\.md$", "", target)
    base = re.sub(r"\.md$", "", base)
    history_path = f"{base}-history.md"
    touched = [target]
    if os.path.exists(history_path):
        touched.append(history_path)
    return touched

def _get_decisions_for_project(project_id: str) -> list[dict]:
    """Fetch active decisions related to a specific project."""
    decisions_dir = os.path.join(config.memory_dir, "decisions")
    if not os.path.exists(decisions_dir):
        return []

    active_decisions = []
    for filepath in glob.glob(os.path.join(decisions_dir, "*.md")):
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
            parts = content.split("---")
            if len(parts) >= 3:
                try:
                    meta = yaml.safe_load(parts[1]) or {}
                    entities = meta.get("entities", [])
                    if f"project/{project_id}" in entities and meta.get("status") != "superseded":
                        active_decisions.append({
                            "id": meta.get("id"),
                            "name": meta.get("name"),
                            "content": parts[2].strip()
                        })
                except Exception as _parse_exc:
                    # Silent skip was hiding corrupt frontmatter — log so
                    # operators can find and fix bad files.
                    # Recovery: run `palinode lint` to surface all parse errors.
                    logger.warning(
                        "palinode.consolidation: YAML parse failed in %r — "
                        "skipping for project decision lookup (run `palinode lint` "
                        "to find all bad files): %s",
                        filepath, _parse_exc,
                    )
                    continue
    return active_decisions

#: Prompt budget for the decision context. Decisions are supplied in full-ish
#: because a truncated constraint is worse than none — the model would see half
#: a rule and treat it as the whole rule — but a project with a long decision
#: record still must not crowd out the notes it is meant to compact.
MAX_DECISIONS_CHARS = 2000
_MAX_DECISION_CHARS = 500


def _format_active_decisions(project_id: str) -> str:
    """Render this project's active decisions for the compaction prompt.

    Returns an empty string when there are none, so the caller can omit the
    section entirely rather than send an empty heading.

    The lookup itself (``_get_decisions_for_project``) already excludes
    superseded decisions — a superseded decision is exactly the thing the
    compactor must NOT treat as binding.
    """
    try:
        decisions = _get_decisions_for_project(project_id)
    except Exception as exc:  # pragma: no cover — defensive
        # Context is an improvement to the proposal, never a precondition for
        # it. A malformed decisions/ directory must not stop a compaction pass.
        logger.warning(
            "palinode.consolidation: could not load decisions for %r "
            "(compacting without decision context): %s",
            project_id, exc,
        )
        return ""

    parts: list[str] = []
    total = 0
    for d in decisions:
        title = d.get("name") or d.get("id") or "decision"
        body = (d.get("content") or "").strip()
        if len(body) > _MAX_DECISION_CHARS:
            body = body[:_MAX_DECISION_CHARS].rstrip() + " …[truncated]"
        entry = f"- **{title}**: {body}" if body else f"- **{title}**"
        if total + len(entry) > MAX_DECISIONS_CHARS:
            parts.append(
                f"- …and {len(decisions) - len(parts)} more decision(s) not shown"
            )
            break
        parts.append(entry)
        total += len(entry)

    return "\n".join(parts)


_CONSOLIDATION_SKIP_DIRS = {"daily", "archive", "inbox", "logs", "prompts", "specs"}


#: Default corpus for the weekly consolidation pass.
#:
#: ``daily/`` is the ephemeral capture stream consolidation was built for.
#: ``insights/`` is included because a store built the documented way puts its
#: durable findings there, and leaving them out meant the executor never saw
#: the memories most worth consolidating — the whole defect this default is
#: correcting. The weekly lookback (7 days) bounds each pass to recently
#: touched files rather than the whole corpus.
#:
#: The nightly pass deliberately does NOT use this — see ``run_nightly``.
DEFAULT_CONSOLIDATION_SOURCES: tuple[str, ...] = ("daily", "insights")

#: What the nightly pass reads. Nightly is the lightweight "what happened
#: today" sweep; insights are not a daily activity stream, so widening the
#: weekly default must not silently widen this one too.
NIGHTLY_CONSOLIDATION_SOURCES: tuple[str, ...] = ("daily",)

#: Filenames shaped ``YYYY-MM-DD.md``. Daily notes carry their date in the
#: filename; every other memory carries it in frontmatter.
_DATED_FILENAME = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _note_date(filepath: str, meta: dict) -> str:
    """The date a note is filed under, as ``YYYY-MM-DD``.

    Daily notes are named for their date, and that is authoritative — reading
    frontmatter for them would change long-standing behaviour. Typed memories
    (Insight, Decision, ProjectSnapshot…) are not date-named, so their date
    comes from frontmatter, falling back to mtime.

    Without this, a typed memory's filename fails the cutoff's string
    comparison in whichever direction its first characters happen to sort —
    silently including or excluding it rather than honouring the lookback.
    """
    stem = os.path.basename(filepath)
    dated = _DATED_FILENAME.match(stem)
    if dated:
        return dated.group(1)

    for key in ("last_updated", "created_at", "date"):
        value = meta.get(key)
        if value:
            text = str(value)
            if _DATED_FILENAME.match(text):
                return text[:10]

    return datetime.fromtimestamp(os.path.getmtime(filepath), UTC).strftime("%Y-%m-%d")


def _collect_daily_notes(
    lookback_days: int, sources: Sequence[str] | None = None
) -> tuple[list[dict], int]:
    """Collect recent notes from the selected corpora.

    ``sources`` names directories under ``memory_dir`` to scan. This function
    previously scanned ``daily/`` unconditionally, which made the deterministic
    executor — the architecture's headline differentiator — unreachable for
    memories saved the documented way: a store full of typed Insights
    consolidated to ``{"status": "no notes found"}`` because none of them live
    in ``daily/``.

    The default is now ``DEFAULT_CONSOLIDATION_SOURCES``, which includes
    ``insights/``; pass ``sources`` explicitly to narrow or widen it.

    Returns:
        Tuple of (notes list, skipped_count) where skipped_count is the
        number of files whose YAML frontmatter failed to parse.
        Callers surface skipped_count in the consolidation run summary so
        operators know to run ``palinode lint``.
    """
    selected = tuple(sources) if sources else DEFAULT_CONSOLIDATION_SOURCES

    cutoff_date = (_utc_now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    notes = []
    skipped = 0
    candidates: list[str] = []

    for source in selected:
        source_dir = os.path.join(config.memory_dir, source)
        if not os.path.exists(source_dir):
            continue
        candidates.extend(glob.glob(os.path.join(source_dir, "*.md")))

    if not candidates:
        return [], 0

    for filepath in candidates:
        meta: dict = {}

        # Fast path, and the reason daily behaviour is unchanged: a date-named
        # file older than the cutoff is rejected without being opened, exactly
        # as before. Only files whose date must come from frontmatter get read
        # in order to be filtered.
        named = _DATED_FILENAME.match(os.path.basename(filepath))
        if named and named.group(1) < cutoff_date:
            continue

        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    meta = yaml.safe_load(parts[1]) or {}
                    if not isinstance(meta, dict):
                        meta = {}
                    content = parts[2].strip()
                except Exception as _parse_exc:
                    # Silent pass was hiding corrupt frontmatter — log so
                    # operators can find and fix bad files.
                    # Recovery: run `palinode lint` to surface all parse errors.
                    logger.warning(
                        "palinode.consolidation: YAML parse failed in %r — "
                        "frontmatter ignored, body text still collected "
                        "(run `palinode lint` to find all bad files): %s",
                        filepath, _parse_exc,
                    )
                    skipped += 1
                    # body text is kept — better to collect partial content
                    # than silently drop the note.
                    content = parts[2].strip() if len(parts) >= 3 else content

        date_str = _note_date(filepath, meta)
        if date_str < cutoff_date:
            continue

        # Frontmatter `entities:` is the reliable signal for a typed memory —
        # it is what `palinode_save` records, whereas the regex below only sees
        # refs a human happened to write into the body. Daily notes rarely carry
        # it, so the two are unioned rather than one replacing the other.
        declared = meta.get("entities") or []
        if isinstance(declared, str):
            declared = [declared]
        mentions = {
            str(e)
            for e in declared
            if isinstance(e, (str, int)) and str(e).startswith(("project/", "person/"))
        }
        mentions.update(re.findall(r"(project/[\w-]+|person/[\w-]+)", content))
        mentions = list(mentions)

        # Fallback: detect projects by keyword if no entity refs found
        if not any(m.startswith("project/") for m in mentions):
            keyword_map = config.consolidation.keyword_map or {
                "project/palinode": ["Palinode", "palinode", "memory system", "SQLite-vec", "BGE-M3", "palinode_search"],
            }
            content_lower = content.lower()
            for project_ref, keywords in keyword_map.items():
                if any(kw.lower() in content_lower for kw in keywords):
                    mentions.append(project_ref)

        notes.append({
            "filepath": filepath,
            "date": date_str,
            "content": content,
            "mentions": mentions
        })

    return sorted(notes, key=lambda x: x["date"]), skipped

def _group_by_project(daily_notes: list[dict]) -> dict[str, list[dict]]:
    """Group daily notes by the projects they mention."""
    groups = {}
    for note in daily_notes:
        for m in note["mentions"]:
            if m.startswith("project/"):
                pid = m.split("project/")[1]
                if pid not in groups:
                    groups[pid] = []
                groups[pid].append(note)
    return groups

def _build_model_chain() -> list[dict[str, str]]:
    """Build ordered chain from config: primary + fallbacks.

    Returns list of {"model": ..., "url": ...} dicts.
    Primary is always first.
    """
    chain = [{"model": config.consolidation.llm_model, "url": config.consolidation.llm_url}]
    for fb in getattr(config.consolidation, "llm_fallbacks", []):
        chain.append({"model": fb["model"], "url": fb["url"]})
    return chain


def _call_llm_with_fallback(system_prompt: str, user_prompt: str) -> tuple[str, str]:
    """Call the consolidation LLM with fallback chain.

    Tries primary model first. On timeout or HTTP error, tries each
    fallback in order. Returns (response_text, model_used).

    Raises:
        RuntimeError: All models in chain failed.
    """
    chain = _build_model_chain()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    client = get_ollama_client()

    last_error = None
    for i, endpoint in enumerate(chain):
        try:
            # Phase 4: route through the centralized client (CONSOLIDATION
            # role). retries=0 — the fallback chain itself is the retry strategy,
            # so the client shouldn't re-hammer each (slow, 600 s) host.
            result = client.chat_completions(
                messages,
                model=endpoint["model"],
                base_url=endpoint["url"],
                temperature=config.consolidation.llm_temperature,
                max_tokens=config.consolidation.llm_max_tokens,
                timeout=600.0,
                retries=0,
                role=OllamaRole.CONSOLIDATION,
            )
            if i > 0:
                logger.info(f"Fallback model succeeded: {endpoint['model']} @ {endpoint['url']} (primary failed)")
            return result, endpoint["model"]

        except OllamaError as e:
            last_error = e
            logger.warning(f"Model {endpoint['model']} @ {endpoint['url']} failed: {e}")
            continue

    raise RuntimeError(f"All {len(chain)} models failed. Last error: {last_error}")

def _consolidate_project(
    project_id: str,
    notes: list[dict],
    is_nightly: bool = False,
    llm_fn: LlmFn | None = None,
) -> tuple[list[dict], str]:
    """Consolidate a project by generating compaction operations.

    Reads the compaction prompt, extracts facts from the project file,
    sends both to the LLM, returns structured operations.

    Args:
        project_id: Project slug.
        notes: Recent daily notes mentioning this project.
        is_nightly: Use the lightweight nightly prompt.
        llm_fn: The propose seam. ``(system_prompt, user_prompt) ->
            (response_text, model_used)``. Defaults to the live fallback-chain
            caller; tests inject a fake returning deterministic op-JSON so the
            real fact-extraction + parse + executor path runs without an LLM.

    Returns:
        Tuple of (List of operation dicts, model_used).
    """
    # Load compaction prompt
    prompt_file = "nightly-consolidation.md" if is_nightly else "compaction.md"
    prompt_path = os.path.join(config.memory_dir, "specs", "prompts", prompt_file)
    if not os.path.exists(prompt_path):
        prompt_path = os.path.join(config.memory_dir, "specs", "prompts", "compaction.md")
        
    with open(prompt_path) as f:
        system_prompt = f.read()
    
    # Load project file and extract facts
    project_file = os.path.join(config.memory_dir, "projects", f"{project_id}.md")
    status_file = os.path.join(config.memory_dir, "projects", f"{project_id}-status.md")
    
    # Prefer status file for compaction (that's the fast-changing layer)
    target_file = status_file if os.path.exists(status_file) else project_file
    
    with open(target_file) as f:
        file_content = f.read()

    # Extract facts with IDs — body only. A YAML frontmatter list entry uses the
    # same `- item` syntax, and harvesting one as a "fact" is what invited the
    # LLM to propose operations against `entities:`.
    _, file_body = split_frontmatter(file_content)
    facts = []
    for match in re.finditer(r'^[\s]*[-*]\s+(.*?)<!-- fact:(\S+) -->', file_body, re.MULTILINE):
        facts.append({"id": match.group(2), "text": match.group(1).strip()})

    if not facts:
        logger.info(f"No tagged facts in {target_file}, skipping compaction")
        return [], "primary"
    
    # Format for LLM
    facts_text = "\n".join(f"[{f['id']}] {f['text']}" for f in facts)
    
    MAX_NOTES_CHARS = 6000
    notes_parts = []
    total = 0
    for n in reversed(notes):
        entry = f"### {n['date']}\n{n['content'][:1500]}"
        if total + len(entry) > MAX_NOTES_CHARS:
            break
        notes_parts.append(entry)
        total += len(entry)
    notes_parts.reverse()
    notes_text = "\n\n".join(notes_parts)

    # Active decisions for this project, as *constraints* on what may be
    # proposed — not as material to compact. Supplied without a lookback:
    # a decision does not stop governing because nobody touched its file this
    # week, which is the opposite of how a note ages.
    decisions_text = _format_active_decisions(project_id)
    decisions_section = (
        f"\n## ACTIVE_DECISIONS (governing this project)\n\n{decisions_text}\n"
        if decisions_text
        else ""
    )

    user_prompt = f"""## EXISTING_FACTS ({len(facts)} facts from {os.path.basename(target_file)})

{facts_text}
{decisions_section}
## RECENT_NOTES (last {config.consolidation.lookback_days} days)

{notes_text}

Return the operations JSON array."""

    # Call LLM (via the injectable propose seam; default = live fallback chain).
    try:
        result_text, model_used = (llm_fn or _call_llm_with_fallback)(system_prompt, user_prompt)
    except Exception as e:
        logger.error(f"Failed to call LLM for {project_id}: {e}")
        return [], "failed"
    
    # Parse the operations JSON array — extraction + json_repair recovery +
    # nested-list/dict filtering all live in op_parse now.
    return parse_operations(result_text), model_used

def _check_contradictions(
    new_items: list[dict], project_id: str, llm_fn: LlmFn | None = None
) -> list[dict]:
    """Check new items for contradictions against existing knowledge base.

    ``llm_fn`` is the same propose seam — defaults to the live caller;
    tests inject a fake returning a canned contradiction op so the embed/search
    + parse + translate path runs deterministically.
    """
    update_prompt_path = os.path.join(config.memory_dir, "specs", "prompts", "update.md")
    if not os.path.exists(update_prompt_path):
        return [{"operation": "ADD", "item": item} for item in new_items]
        
    with open(update_prompt_path) as f:
        system_prompt = f.read()

    operations = []
    for item in new_items:
        emb = embedder.embed(item.get("content", ""))
        if not emb:
            operations.append({"operation": "ADD", "item": item})
            continue

        # H1: consolidation dedup is an internal candidate lookup, not human
        # recall — use search_internal so recall_count / importance are never
        # bumped regardless of future refactors (ADR-015 H1).
        existing = store.search_internal(
            emb, category=item.get("category"), top_k=5, threshold=0.7,
        )

        if not existing:
            operations.append({"operation": "ADD", "item": item})
            continue

        user_prompt = f"""## Candidate
{json.dumps(item, indent=2)}

## Existing Similar Memories
{json.dumps(existing, indent=2)}

Return the operation as JSON."""

        try:
            result_text, model_used = (llm_fn or _call_llm_with_fallback)(system_prompt, user_prompt)

            json_match = re.search(r"```json\s*([\s\S]*?)```", result_text)
            if json_match:
                result_text = json_match.group(1)

            try:
                operation = json.loads(result_text)
                operation["item"] = item
                operations.append(operation)
            except json.JSONDecodeError:
                operations.append({"operation": "ADD", "item": item, "reason": "LLM parse failed"})
        except Exception as e:
            logger.error(f"Contradiction check failed: {e}")
            operations.append({"operation": "ADD", "item": item, "reason": f"API error: {e}"})

    return operations

def _write_project_summary(project_id: str, consolidation: dict) -> None:
    """Write the consolidated project summary back to the project markdown file."""
    project_file = os.path.join(config.memory_dir, "projects", f"{project_id}.md")
    
    # Simple write, but realistically we'd merge with existing body/frontmatter.
    # We will log the new summary block or replace the content.
    # human-readable UTC log heading. Previously used a literal ``Z``
    # suffix which trips the project-wide ``strftime("...Z")`` audit; ``UTC``
    # is unambiguous and keeps the log scannable.
    now = _utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
    
    status_bullets = consolidation.get("status_bullets", [])
    bullets_text = "\n".join(f"- {b}" for b in status_bullets)
    
    if os.path.exists(project_file):
        with open(project_file, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Append consolidation log
        log_entry = f"\n\n## Consolidation Log ({now})\n{bullets_text}\n"
        content += log_entry
        git_tools.write_memory_file(project_file, content)
    else:
        # Create new
        metadata = {
            "id": f"project-{project_id}",
            "category": "project",
            "name": project_id,
            "entities": [f"project/{project_id}"],
            "last_updated": now
        }
        yaml_front = yaml.dump(metadata, default_flow_style=False)
        content = f"---\n{yaml_front}---\n\n# {project_id}\n\n## Summary\n{bullets_text}\n"
        git_tools.write_memory_file(project_file, content)

def _handle_superseded_decisions(superseded: list[dict]) -> None:
    """Mark superseded decisions in their frontmatter."""
    for decision in superseded:
        decision_path = os.path.join(config.memory_dir, "decisions", f"{decision['id']}.md")
        if not os.path.exists(decision_path):
            continue
            
        with open(decision_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        # Update frontmatter status
        new_content = re.sub(
            r"^status:\s*active", 
            "status: superseded", 
            content, 
            flags=re.MULTILINE
        )
        
        # Add superseded_by if provided
        if "superseded_by" in decision:
            new_content = re.sub(
                r"^superseded_by:\s*\"\"", 
                f"superseded_by: {decision['superseded_by']}", 
                new_content, 
                flags=re.MULTILINE
            )
            
        git_tools.write_memory_file(decision_path, new_content)
        logger.info(f"Marked decision {decision['id']} as superseded")

def _extract_insights(all_notes: list[dict]) -> list[dict]:
    """Extract recurring patterns as insight candidates from notes.
    
    Looks for notes tagged as 'insight' category or containing
    keywords indicating a generalizable lesson.
    """
    if not all_notes:
        return []
        
    insights = []
    insight_keywords = ["lesson:", "insight:", "pattern:", "learned:", "takeaway:", "principle:"]
    
    for note in all_notes:
        content = note.get("content", "").lower()
        category = note.get("category", "").lower()
        
        if category in ("insight", "insights"):
            insights.append(note)
        elif any(kw in content for kw in insight_keywords):
            insights.append({
                **note,
                "category": "insight",
                "extracted": True,
            })
    
    if insights:
        logger.info(f"Extracted {len(insights)} insight candidates from {len(all_notes)} notes")
    return insights

def _archive_daily_notes(notes: list[dict]) -> None:
    """Move processed daily notes to the archive directory."""
    archive_dir = os.path.join(config.memory_dir, "archive")
    os.makedirs(archive_dir, exist_ok=True)
    
    for note in notes:
        try:
            date_prefix = note["date"][:4] # YYYY
            year_dir = os.path.join(archive_dir, date_prefix)
            os.makedirs(year_dir, exist_ok=True)
            new_path = os.path.join(year_dir, os.path.basename(note["filepath"]))
            shutil.move(note["filepath"], new_path)
        except Exception as e:
            logger.error(f"Failed to archive note {note['filepath']}: {e}")

def _fact_ids_before_apply(file_path: str) -> set[str]:
    """Fact ids present in ``file_path`` before the executor runs.

    Captured pre-apply because ARCHIVE removes a fact's marker from the file —
    validating an ARCHIVE's ``fact_id`` against post-apply content alone would
    mark every legitimate archive unresolved.
    """
    if not os.path.exists(file_path):
        return set()
    with open(file_path, encoding="utf-8") as f:
        return status_doc.fact_ids(f.read())


def _update_status_summary(
    file_path: str,
    new_activity: list[dict],
    known_fact_ids: set[str] | None = None,
) -> None:
    """
    Update a -status.md file by merging new activity into existing sections
    rather than rewriting from scratch. Preserves longitudinal history.
    Inspired by NousResearch/hermes-agent trajectory compressor (MIT).

    The audit contract lives in :mod:`palinode.consolidation.status_doc`
    and is shared verbatim with ``palinode repair-status``: op fields are read
    through ``op_kind``/``op_reason`` (the dry-run preview's accessors, so the
    write path can no longer disagree with what ``--dry-run`` showed), a missing
    kind defaults to ``KEEP`` like the executor, unresolvable ``fact_id``s never
    reach the file, the log is bounded, and the frontmatter counts are
    reconciled with the body on every write.

    Args:
        file_path: Absolute path to the status markdown file.
        new_activity: List of operation dicts (``op``/``operation``,
            ``id``/``fact_id``/``ids``, ``reason``/``rationale``).
        known_fact_ids: Fact ids that existed before ``apply_operations`` ran.
            Unioned with the ids currently in the file, so both an ARCHIVE'd
            fact and a freshly minted ``supersedes-*`` id validate.
    """
    if not os.path.exists(file_path):
        return  # No existing status file to update

    if not new_activity:
        return  # Nothing to update

    with open(file_path, encoding="utf-8") as f:
        existing = f.read()

    valid_ids = set(known_fact_ids or set()) | status_doc.fact_ids(existing)
    lines = status_doc.render_log_lines(new_activity, valid_ids)
    if not lines:
        logger.info(
            "No auditable operations to log for %s (%d no-op KEEP(s) suppressed)",
            file_path, len(new_activity),
        )
        return

    frontmatter_block, body = split_frontmatter(existing)
    today = _utc_now().strftime("%Y-%m-%d")
    max_blocks = getattr(
        config.consolidation, "status_log_max_blocks",
        status_doc.DEFAULT_MAX_LOG_BLOCKS,
    )
    body = status_doc.merge_log_entry(body, today, lines, max_blocks=max_blocks)

    if not frontmatter_block:
        # No frontmatter to reconcile — write the merged body as-is rather than
        # refusing, which is what this path has always done.
        updated = body
    else:
        meta = status_doc.desired_frontmatter(frontmatter_block + body)
        if meta is None:
            logger.warning(
                "status frontmatter for %s does not parse — skipping the write "
                "so the log entry is not written into a broken document "
                "(run `palinode repair-status`)", file_path,
            )
            return
        # Reached only when new activity was merged above, so this is a real
        # content change and the receipt is earned.
        meta["last_updated"] = _utc_now().isoformat()
        updated = status_doc.render(meta, body)

    git_tools.write_memory_file(file_path, updated)

    logger.info(f"Updated status summary: {file_path} (+{len(lines)} entries)")


def _proposed_changes(target: str, operations: list[dict]) -> list[dict[str, str]]:
    return [
        {
            "type": op_kind(op),
            "file": target,
            "rationale": op_reason(op),
        }
        for op in operations
        if isinstance(op, dict)
    ]


def run_consolidation(
    lookback_days: int | None = None,
    dry_run: bool = False,
    llm_fn: LlmFn | None = None,
    sources: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Orchestrator for the entire memory consolidation process.

    ``sources`` selects which directories under ``memory_dir`` to consolidate,
    defaulting to ``daily/``. Grouping and targeting are unchanged: notes
    are grouped by the ``project/`` refs they carry and compacted into that
    project's status file, so a source whose memories name no project
    contributes nothing — which the run summary reports as
    ``projects_compacted: 0`` rather than silently.
    """
    from palinode.consolidation.executor import apply_operations

    lookback = lookback_days or config.consolidation.lookback_days
    notes, yaml_skipped = _collect_daily_notes(lookback, sources=sources)
    if not notes:
        if dry_run:
            return {
                "status": "no notes found",
                "processed": 0,
                "processed_notes": 0,
                "projects_compacted": 0,
                "dry_run": True,
                "proposed_changes": [],
            }
        return {"status": "no notes found", "processed": 0}

    if yaml_skipped:
        logger.warning(
            "palinode.consolidation: %d daily note(s) had unparseable YAML frontmatter "
            "— run `palinode lint` to inspect. Proceeding with body text only.",
            yaml_skipped,
        )

    grouped = _group_by_project(notes)
    
    total_stats = {"kept": 0, "updated": 0, "merged": 0, "superseded": 0, "archived": 0}
    projects_processed = 0
    proposed_changes: list[dict[str, str]] = []
    mutated_files: list[str] = []

    for project_id, pnotes in grouped.items():
        try:
            model_used_current = "primary"
            operations, model_used_current = _consolidate_project(project_id, pnotes, llm_fn=llm_fn)
            if not operations:
                continue

            model_used = model_used_current

            # Determine target file
            status_file = os.path.join(config.memory_dir, "projects", f"{project_id}-status.md")
            project_file = os.path.join(config.memory_dir, "projects", f"{project_id}.md")
            target = status_file if os.path.exists(status_file) else project_file

            if dry_run:
                proposed_changes.extend(_proposed_changes(target, operations))
                projects_processed += 1
                logger.info(f"Previewed compaction for {project_id}: {len(operations)} operation(s)")
                continue

            pre_apply_ids = _fact_ids_before_apply(target)
            stats = apply_operations(target, operations)
            for k, v in stats.items():
                total_stats[k] = total_stats.get(k, 0) + v

            # Iteratively append operations to the status file, preserving history
            _update_status_summary(target, operations, known_fact_ids=pre_apply_ids)

            # Track exactly the files this project's compaction touched so the
            # commit stages only them (one-mutation-one-commit).
            mutated_files.extend(_touched_files(target))

            projects_processed += 1
            logger.info(f"Compacted {project_id}: {stats}")

        except Exception as e:
            logger.error(f"Compaction failed for {project_id}: {e}")

    if dry_run:
        result = {
            "status": "success",
            "processed_notes": len(notes),
            "projects_compacted": projects_processed,
            "dry_run": True,
            "proposed_changes": proposed_changes,
        }
        if yaml_skipped:
            result["yaml_parse_errors"] = yaml_skipped
        return result
    
    # Extract insights and archive (only if at least one project compacted successfully)
    _extract_insights(notes)
    if projects_processed > 0:
        _archive_daily_notes(notes)
    else:
        logger.warning("No projects compacted successfully — skipping daily note archival")
    
    
    _git_commit(
        f"palinode: compaction {_utc_now().strftime('%Y-%m-%d')} — "
        f"{total_stats['updated']}u {total_stats['merged']}m "
        f"{total_stats['superseded']}s {total_stats['archived']}a"
        f" (model: {model_used})",
        files=mutated_files,
    )
    
    result: dict[str, Any] = {
        "status": "success",
        "processed_notes": len(notes),
        "projects_compacted": projects_processed,
        **total_stats,
    }
    if yaml_skipped:
        result["yaml_parse_errors"] = yaml_skipped
    return result


def run_nightly(lookback_days: int | None = None, dry_run: bool = False, llm_fn: LlmFn | None = None) -> dict[str, Any]:
    """Lightweight nightly consolidation — process today's daily notes only.

    Restricted to UPDATE and SUPERSEDE ops. No ARCHIVE or MERGE (those
    are weekly concerns). Smaller LLM context = better JSON output.

    Pinned to ``NIGHTLY_CONSOLIDATION_SOURCES`` rather than inheriting the
    weekly default: "today's daily notes only" is this function's contract, and
    an unpinned call would have widened it silently the moment the weekly
    default grew.
    """
    from palinode.consolidation.executor import apply_operations

    lookback = lookback_days or config.consolidation.nightly.lookback_days
    notes, yaml_skipped = _collect_daily_notes(
        lookback, sources=NIGHTLY_CONSOLIDATION_SOURCES
    )
    if not notes:
        if dry_run:
            return {
                "status": "no_new_notes",
                "processed_notes": 0,
                "projects_compacted": 0,
                "dry_run": True,
                "proposed_changes": [],
            }
        return {"status": "no_new_notes", "processed_notes": 0, "projects_compacted": 0}

    if yaml_skipped:
        logger.warning(
            "palinode.consolidation: %d daily note(s) had unparseable YAML frontmatter "
            "— run `palinode lint` to inspect. Proceeding with body text only.",
            yaml_skipped,
        )
    
    grouped = _group_by_project(notes)
    
    total_stats = {"kept": 0, "updated": 0, "merged": 0, "superseded": 0, "archived": 0}
    projects_processed = 0
    model_used = "primary"
    proposed_changes: list[dict[str, str]] = []
    mutated_files: list[str] = []

    for project_id, pnotes in grouped.items():
        try:
            operations, model_used_current = _consolidate_project(project_id, pnotes, is_nightly=True, llm_fn=llm_fn)
            if not operations:
                continue

            model_used = model_used_current

            # Enforce allows ops restriction
            allowed_ops = set(config.consolidation.nightly.allowed_ops)
            operations = [op for op in operations if op.get("op", op.get("operation", "")).upper() in allowed_ops]
            if not operations:
                continue

            # Determine target file
            status_file = os.path.join(config.memory_dir, "projects", f"{project_id}-status.md")
            project_file = os.path.join(config.memory_dir, "projects", f"{project_id}.md")
            target = status_file if os.path.exists(status_file) else project_file

            if dry_run:
                proposed_changes.extend(_proposed_changes(target, operations))
                projects_processed += 1
                logger.info(f"Previewed nightly compaction for {project_id}: {len(operations)} operation(s)")
                continue

            pre_apply_ids = _fact_ids_before_apply(target)
            stats = apply_operations(target, operations, nightly_policy=True)
            for k, v in stats.items():
                total_stats[k] = total_stats.get(k, 0) + v

            _update_status_summary(target, operations, known_fact_ids=pre_apply_ids)

            mutated_files.extend(_touched_files(target))

            projects_processed += 1
            logger.info(f"Nightly compacted {project_id}: {stats}")

        except Exception as e:
            logger.error(f"Nightly compaction failed for {project_id}: {e}")
            
    # Nightly does NOT archive daily notes (left for weekly)

    if dry_run:
        nightly_result = {
            "status": "success",
            "processed_notes": len(notes),
            "projects_compacted": projects_processed,
            "dry_run": True,
            "proposed_changes": proposed_changes,
        }
        if yaml_skipped:
            nightly_result["yaml_parse_errors"] = yaml_skipped
        return nightly_result
    
    if projects_processed > 0:
        _git_commit(
            f"palinode: nightly {_utc_now().strftime('%Y-%m-%d')} — "
            f"{total_stats['updated']}u {total_stats['superseded']}s"
            f" (model: {model_used})",
            files=mutated_files,
        )
    
    nightly_result: dict[str, Any] = {
        "status": "success",
        "processed_notes": len(notes),
        "projects_compacted": projects_processed,
        **total_stats,
    }
    if yaml_skipped:
        nightly_result["yaml_parse_errors"] = yaml_skipped
    return nightly_result
