"""Spec 5 T5.1: durable persistence of work_event rows.

Writing here is the ONLY place a work_event-shaped dict (from
work_capture.compute_work_events) touches the database. It never
receives raw tool content -- only the already-bounded summary and hash
that compute_work_events produced.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class StoreWorkMixin:
    pool: Any

    async def work_event_capture_batch(self, events: List[Dict[str, Any]]) -> int:
        """Insert each event; a replayed (owner, session_id, source_event_id)
        is a silent no-op (ON CONFLICT DO NOTHING), not an error -- retrying
        the same durable job must never duplicate captured events. Returns
        the count of rows actually inserted (new, not replayed)."""
        if not events:
            return 0
        inserted = 0
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for event in events:
                    row = await conn.fetchrow(
                        "INSERT INTO work_event "
                        "(owner, session_id, span_id, source_event_id, tool, status_code, "
                        " summary, content_hash, refs) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb) "
                        "ON CONFLICT (owner, session_id, source_event_id) DO NOTHING "
                        "RETURNING id",
                        event["owner"], event["session_id"], event.get("span_id"),
                        event["source_event_id"], event["tool"], event.get("status_code"),
                        event["summary"], event["content_hash"],
                        json.dumps(event.get("refs") or {}),
                    )
                    if row is not None:
                        inserted += 1
        return inserted

    async def retract_work_item(self, owner: str, topic_key: str) -> bool:
        """Spec 5 T5.4: an explicit, separate USER operation -- never
        inferred from conversation text by the rules/model extractor
        (see work_status.py: a phrase that merely LOOKS like a retraction
        classifies as 'abandoned', not 'retracted'). Marks the current
        item 'retracted' and restores its predecessor as current if one
        exists, clearing the predecessor's own superseded_by so it is
        genuinely current again, not still pointing forward. Returns
        False (a no-op) when the topic has no current item at all.

        T5.7 (documented 2026-11-21, no code change): this never touches
        the retracted item's work_artifact rows. They are never
        re-verified once their parent item is retracted, so a retracted
        item's content_hash/exists_now stays frozen at whatever it was
        at the last real verification -- even if the underlying file
        changes afterward. A retracted item's artifact hashes are a
        historical record, not a live guarantee."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"work_item_current|{owner}|{topic_key}",
                )
                current = await conn.fetchrow(
                    "SELECT work_item_id FROM work_item_current WHERE owner = $1 AND topic_key = $2",
                    owner, topic_key,
                )
                if current is None:
                    return False
                current_id = int(current["work_item_id"])
                await conn.execute(
                    "UPDATE work_item SET status = 'retracted' WHERE id = $1 AND owner = $2",
                    current_id, owner,
                )
                predecessor = await conn.fetchrow(
                    "SELECT id FROM work_item WHERE owner = $1 AND superseded_by = $2",
                    owner, current_id,
                )
                if predecessor is not None:
                    predecessor_id = int(predecessor["id"])
                    await conn.execute(
                        "UPDATE work_item SET superseded_by = NULL WHERE id = $1 AND owner = $2",
                        predecessor_id, owner,
                    )
                    await conn.execute(
                        "UPDATE work_item_current SET work_item_id = $1 "
                        "WHERE owner = $2 AND topic_key = $3",
                        predecessor_id, owner, topic_key,
                    )
                else:
                    await conn.execute(
                        "DELETE FROM work_item_current WHERE owner = $1 AND topic_key = $2",
                        owner, topic_key,
                    )
        return True

    async def verify_work_artifacts(
        self, owner: str, work_item_id: int, repo_roots: list,
        hmac_key: Optional[bytes] = None,
    ) -> tuple:
        """Spec 5 T5.5: verify every work_artifact row for this item
        against the allowlisted repo_roots, using work_artifact_verify's
        real, local, network-free checks. Returns (verified, skipped)
        counts. An unverifiable artifact (checked=False) is left
        exactly as-is -- kept in the table, exists_now/content_hash/
        verified_at untouched -- never dropped and never written as
        passed. hmac_key gates file content_hash exactly like
        work_event's -- no key, no hash, never an unkeyed fallback."""
        from .work_artifact_verify import verify_one_artifact

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, kind, ref, repo FROM work_artifact "
                "WHERE owner = $1 AND work_item_id = $2",
                owner, work_item_id,
            )
            verified = 0
            skipped = 0
            for row in rows:
                checked, exists_now, content_hash = verify_one_artifact(
                    kind=row["kind"], ref=row["ref"], repo=row["repo"], repo_roots=repo_roots,
                    hmac_key=hmac_key,
                )
                if not checked:
                    skipped += 1
                    continue
                await conn.execute(
                    "UPDATE work_artifact SET exists_now = $1, content_hash = $2, "
                    "verified_at = now() WHERE id = $3 AND owner = $4",
                    exists_now, content_hash, int(row["id"]), owner,
                )
                verified += 1
        return verified, skipped
