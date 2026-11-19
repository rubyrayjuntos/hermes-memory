"""Spec 5 T5.1: durable persistence of work_event rows.

Writing here is the ONLY place a work_event-shaped dict (from
work_capture.compute_work_events) touches the database. It never
receives raw tool content -- only the already-bounded summary and hash
that compute_work_events produced.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

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
