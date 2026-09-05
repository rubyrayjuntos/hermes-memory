"""Process-local write outcomes for hybrid-age drain.

Counters live on this Python process only. They are not Postgres, so
`Store.librarian_health()` never sees them and a restart zeros them.
`Runtime.health()` / `Runtime.librarian_health()` merge the snapshot for
the pane. `detail` is redacted in `record()` and never enters `snapshot()`.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

_REDACT = re.compile(
    r"(?i)(?:postgres(?:ql)?|mysql|mongodb)://\S+"
    r"|(?:password|passwd|pwd|dsn)\s*[:=]\s*\S+"
)


class Stage(StrEnum):
    ENQUEUE = "enqueue"
    EMBED = "embed"
    SQL_TURN = "sql_turn"
    FLOWER = "flower"
    NOUNS = "nouns"
    MENTIONS = "mentions"
    MEMORY_SQL = "memory_sql"


class Kind(StrEnum):
    OK = "ok"
    SKIPPED = "skipped"
    EMBED_NULL = "embed_null"
    GRAPH_DEGRADED = "graph_degraded"
    DROPPED = "dropped"
    FAILED = "failed"


# Persisted on conversations.drain_status after insert B succeeds.
# L1 SQL failure leaves no row — there is nothing to stamp (accepted loss).
# LEDGER.counts() remain process-local and are a different fact.
DRAIN_COMPLETE = "complete"
DRAIN_STATUSES = frozenset(
    {DRAIN_COMPLETE, Kind.EMBED_NULL.value, Kind.GRAPH_DEGRADED.value}
)


@dataclass(frozen=True)
class WriteOutcome:
    stage: Stage
    kind: Kind
    session_id: str = ""
    turn_id: int | None = None
    detail: str = ""  # exception type + <=80 chars; no DSN/password


def _clip_text(raw: str) -> str:
    """Redact DSN/password fragments and cap at 80 characters."""
    return _REDACT.sub("[redacted]", raw)[:80]


def clip_detail(exc: BaseException) -> str:
    """Exception type plus a short, redacted message (never DSN/password)."""
    return f"{type(exc).__name__}: {_clip_text(str(exc))}"


class WriteLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dropped_writes = 0
        self._writes_failed = 0
        self._embed_null = 0
        self._graph_degraded = 0
        self._last_failed_stage = ""
        self._last_failed_at = ""

    def record(
        self,
        outcome: WriteOutcome,
        *,
        exc: BaseException | None = None,
    ) -> WriteOutcome:
        """Count the outcome. Always clip `detail` (or `exc`) before storing."""
        detail = clip_detail(exc) if exc is not None else (
            _clip_text(outcome.detail) if outcome.detail else ""
        )
        if detail != outcome.detail:
            outcome = replace(outcome, detail=detail)
        with self._lock:
            if outcome.kind is Kind.DROPPED:
                self._dropped_writes += 1
            elif outcome.kind is Kind.FAILED:
                self._writes_failed += 1
                self._last_failed_stage = str(outcome.stage)
                self._last_failed_at = datetime.now(timezone.utc).isoformat()
            elif outcome.kind is Kind.EMBED_NULL:
                self._embed_null += 1
            elif outcome.kind is Kind.GRAPH_DEGRADED:
                self._graph_degraded += 1
        return outcome

    def counts(self) -> dict[str, int]:
        """Keys: dropped_writes, writes_failed, embed_null, graph_degraded."""
        with self._lock:
            return {
                "dropped_writes": self._dropped_writes,
                "writes_failed": self._writes_failed,
                "embed_null": self._embed_null,
                "graph_degraded": self._graph_degraded,
            }

    def snapshot(self) -> dict[str, Any]:
        """counts() plus last_failed_stage and last_failed_at (ISO or empty)."""
        with self._lock:
            return {
                "dropped_writes": self._dropped_writes,
                "writes_failed": self._writes_failed,
                "embed_null": self._embed_null,
                "graph_degraded": self._graph_degraded,
                "last_failed_stage": self._last_failed_stage,
                "last_failed_at": self._last_failed_at,
            }

    def reset(self) -> None:
        with self._lock:
            self._dropped_writes = 0
            self._writes_failed = 0
            self._embed_null = 0
            self._graph_degraded = 0
            self._last_failed_stage = ""
            self._last_failed_at = ""


LEDGER = WriteLedger()
