"""Test-only seeding helpers for the store.

Production indexing goes ``index_file → reconcile.apply → write_chunk_row /
replace_entities``; the old ``store.upsert_chunks`` / ``store.upsert_entities``
convenience wrappers had no production caller and were removed. Tests
that need to seed rows without a file on disk use these thin wrappers, which
are built on the same cursor-level primitives the reconcile path uses.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from palinode.core import store


def upsert_chunks(
    chunks_data: list[dict[str, Any]], skip_unchanged: bool = True
) -> dict[str, Any]:
    """Write chunk dicts (id, file_path, section_id, content, embedding, ...)
    through :func:`store.write_chunk_row` under one transaction.

    Returns ``{"written", "vec_ok", "fts_ok"}`` — the same shape the removed
    store wrapper returned, so assertions on per-index health still work.
    """
    written = 0
    vec_ok = True
    fts_ok = True

    with store.transaction() as db:
        cursor = db.cursor()
        for chunk in chunks_data:
            content_hash = hashlib.sha256(chunk["content"].encode()).hexdigest()

            if skip_unchanged:
                existing = cursor.execute(
                    "SELECT content_hash FROM chunks WHERE id = ?",
                    (chunk["id"],),
                ).fetchone()
                if existing and existing["content_hash"] == content_hash:
                    continue

            metadata = chunk.get("metadata", {})
            row_vec_ok, row_fts_ok = store.write_chunk_row(
                cursor,
                chunk_id=chunk["id"],
                file_path=chunk["file_path"],
                section_id=chunk["section_id"],
                category=chunk.get("category", ""),
                content=chunk["content"],
                metadata_json=json.dumps(metadata, default=str),
                content_hash=content_hash,
                meta_hash=store.meta_hash(metadata),
                created_at=chunk.get("created_at"),
                last_updated=chunk.get("last_updated"),
                embedding=chunk["embedding"],
            )
            vec_ok = vec_ok and row_vec_ok
            fts_ok = fts_ok and row_fts_ok
            written += 1

    return {"written": written, "vec_ok": vec_ok, "fts_ok": fts_ok}


def upsert_entities(file_path: str, metadata: dict[str, Any]) -> None:
    """Replace a file's entity rows from a metadata dict via
    :func:`store.replace_entities` under one transaction."""
    with store.transaction() as db:
        store.replace_entities(
            db.cursor(),
            file_path,
            metadata.get("entities", []),
            metadata.get("category", ""),
            store.utc_now_z(),
        )
