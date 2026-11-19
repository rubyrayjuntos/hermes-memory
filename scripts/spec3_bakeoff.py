#!/usr/bin/env python3
"""T3.6 bake-off: rules_only vs rules_then_model on throwaway *_eval DBs.

Never prints DSN passwords or API keys. Never uses --reuse-dsn. Port 5450 refused.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import time
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def _norm(value: str | None) -> str:
    s = re.sub(r"\s+", " ", (value or "").strip().lower())
    if s in {"(self)", "self"}:
        return "i"
    return s


def _norm_role(role: str | None) -> str:
    """Normalize role names for scoring: 'staff engineering' → 'staff engineer'."""
    s = _norm(role)
    if not s:
        return s
    # Gerund (-ing) to noun patterns for common role names
    # "staff engineering" → "staff engineer"
    # "software engineering" → "software engineer"
    # "product management" → "product manager"
    if s.endswith(" engineering"):
        return s[:-3]  # Remove "ing"
    if s.endswith(" management"):
        return s.replace(" management", " manager")
    return s


def _claim_key(claim: dict) -> tuple[str, str, str, str, bool]:
    verb = _norm(claim.get("verb"))
    obj = _norm(claim.get("object"))
    # For role_is verb, normalize the role name
    if verb == "role_is":
        obj = _norm_role(obj)
    return (
        _norm(claim.get("subject")),
        verb,
        obj,
        _norm(claim.get("polarity") or "positive"),
        bool(claim.get("current")),
    )


def _prf(expected, actual) -> dict:
    expected_counts = Counter(expected)
    actual_counts = Counter(actual)
    matched = sum((expected_counts & actual_counts).values())
    expected_n = sum(expected_counts.values())
    actual_n = sum(actual_counts.values())
    precision = matched / actual_n if actual_n else None
    recall = matched / expected_n if expected_n else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else (1.0 if expected_n == actual_n == 0 else 0.0 if expected_n or actual_n else None)
    )
    return {
        "expected": expected_n,
        "actual": actual_n,
        "matched": matched,
        "precision": round(precision, 3) if precision is not None else None,
        "recall": round(recall, 3) if recall is not None else None,
        "f1": round(f1, 3) if f1 is not None else None,
    }


def _validate_dsn(dsn: str) -> None:
    parsed = urlparse(dsn)
    if parsed.port == 5450 or ":5450" in dsn:
        raise SystemExit("refusing: port 5450 is out of scope")
    database = parsed.path.lstrip("/").split("?", 1)[0]
    if not (re.search(r"(?:^|_)eval(?:$|_)", database) or database.startswith("hermes_release_gate_")):
        raise SystemExit("refusing: DSN must name an *_eval or hermes_release_gate_* database")


def _classify_error(text: str) -> str:
    s = (text or "").lower()
    if "429" in s or "rate limit" in s or "rate_limit" in s or "too many requests" in s:
        return "429"
    if "timeout" in s or "timed out" in s:
        return "timeout"
    if "json_schema" in s or "response_format" in s or "invalid json" in s or "schema" in s:
        return "schema_invalid"
    return "other"


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100) * (len(ordered) - 1)))))
    return round(ordered[idx], 1)


async def run_case(
    *,
    dsn: str,
    fixture_path: Path,
    extract_mode: str,
    extract_url: str,
    extract_model: str,
    schema_mode: str,
    key_env: str,
    timeout_s: float,
) -> dict:
    import asyncpg

    from hermes_memory.config import HybridAgeConfig
    from hermes_memory.extract_model_client import OpenAIChatModelExtractor
    from hermes_memory.provider import HybridAgeMemoryProvider
    from hermes_memory.turn_filter import _noise_reason

    spec = json.loads(fixture_path.read_text(encoding="utf-8"))
    turns = spec["turns"]
    if len(turns) % 2:
        raise ValueError("fixture must contain user/assistant pairs")
    expected_rows = 0
    for index in range(0, len(turns), 2):
        for turn in turns[index:index + 2]:
            if _noise_reason(turn["content"][:8000], role=turn["role"]) is None:
                expected_rows += 1

    cfg = HybridAgeConfig(
        dsn=dsn,
        prefetch_timeout_s=30.0,
        retrieval_mode="vector",
        extract_mode=extract_mode,
        extract_url=extract_url,
        extract_model=extract_model,
        extract_model_schema=schema_mode,
        extract_key_env=key_env,
        extract_timeout_s=60.0,
    )
    run_suffix = uuid.uuid4().hex[:12]
    owner = f"{spec.get('owner', 'bakeoff')}-{run_suffix}"
    session = f"{spec.get('session_id', 'bakeoff')}-{run_suffix}"
    provider = HybridAgeMemoryProvider(config=cfg)
    latencies_ms: list[float] = []
    call_errors: list[str] = []
    provider.initialize(session, agent_identity=owner, agent_context="primary")

    if extract_mode == "rules_then_model":
        inner = getattr(provider, "_extraction_model", None)
        if inner is None:
            raise RuntimeError("rules_then_model did not configure an extractor")
        orig = inner.__call__

        async def timed(batch):
            t0 = time.perf_counter()
            try:
                return await orig(batch)
            except Exception as exc:
                call_errors.append(f"{type(exc).__name__}: {exc}"[:500])
                raise
            finally:
                latencies_ms.append((time.perf_counter() - t0) * 1000)

        inner.__call__ = timed  # type: ignore[method-assign]

    conn = None
    t_wall = time.perf_counter()
    try:
        for index in range(0, len(turns), 2):
            user, assistant = turns[index:index + 2]
            provider.sync_turn(user["content"], assistant["content"], session_id=session)
        provider.on_session_end(turns)

        conn = await asyncpg.connect(dsn)
        deadline = time.monotonic() + timeout_s
        snapshot: dict = {}
        while time.monotonic() < deadline:
            row = await conn.fetchrow(
                """
                SELECT count(*)::int AS row_count,
                       count(*) FILTER (WHERE drain_status = 'complete')::int AS complete_rows
                  FROM conversations
                 WHERE agent_identity = $1 AND session_id = $2
                """,
                owner, session,
            )
            jobs = await conn.fetchrow(
                """
                SELECT count(*) FILTER (WHERE kind = 'extract_rules')::int AS rules_jobs,
                       count(*) FILTER (WHERE kind = 'extract_rules' AND status = 'done')::int AS rules_done,
                       count(*) FILTER (WHERE kind = 'extract_model')::int AS model_jobs,
                       count(*) FILTER (WHERE kind = 'extract_model' AND status = 'done')::int AS model_done,
                       count(*) FILTER (WHERE status IN ('pending', 'leased', 'retryable'))::int AS jobs_pending,
                       count(*) FILTER (WHERE status = 'failed')::int AS jobs_failed
                  FROM durable_job
                 WHERE owner = $1 AND session_id = $2
                   AND kind IN ('extract_rules', 'extract_model')
                """,
                owner, session,
            )
            snapshot = {**dict(row), **dict(jobs)}
            if (
                snapshot["row_count"] == expected_rows
                and snapshot["complete_rows"] == expected_rows
                and snapshot["jobs_pending"] == 0
                and snapshot["rules_done"] == snapshot["rules_jobs"]
            ):
                break
            await asyncio.sleep(0.4)
        else:
            snapshot["timed_out"] = True

        claim_rows = await conn.fetch(
            """
            SELECT subject, verb, object, polarity, status, valid_to IS NULL AS current
              FROM claims WHERE owner = $1 ORDER BY claim_id
            """,
            owner,
        )
        actual_claims = [
            {
                "subject": row["subject"],
                "verb": row["verb"],
                "object": row["object"],
                "polarity": row["polarity"],
                "status": row["status"],
                "current": bool(row["current"]),
            }
            for row in claim_rows
            if row["status"] == "grounded"
        ]
        ideal = spec.get("ideal_claims") or []
        claim_metrics = _prf([_claim_key(c) for c in ideal], [_claim_key(c) for c in actual_claims])

        conversation_rows = await conn.fetch(
            "SELECT id, role, content FROM conversations WHERE agent_identity = $1 AND session_id = $2",
            owner, session,
        )
        stored_by_content: dict[tuple[str, str], list] = {}
        for row in conversation_rows:
            stored_by_content.setdefault((row["role"], row["content"]), []).append(row)
        turn_span_ids: dict[str, int] = {}
        for turn in turns:
            key = (turn["role"], turn["content"][:8000])
            matches = stored_by_content.get(key, [])
            if matches:
                turn_span_ids[turn["turn_id"]] = int(matches.pop(0)["id"])

        evidence_rows = await conn.fetch(
            """
            SELECT span_id, subject_surface, verb, object_surface, speaker
              FROM claim_evidence WHERE owner = $1 AND state = 'live'
            """,
            owner,
        )
        pasted_spans = {
            turn_span_ids[turn["turn_id"]]
            for turn in turns
            if turn.get("pasted") and turn["turn_id"] in turn_span_ids
        }
        pasted_false = sum(
            row["speaker"] == "user" and int(row["span_id"]) in pasted_spans
            for row in evidence_rows
        )
        abstain_spans = {
            turn_span_ids[turn["turn_id"]]
            for turn in turns
            if turn.get("expect_no_claim") and turn["turn_id"] in turn_span_ids
        }
        abstain_fp = sum(int(row["span_id"]) in abstain_spans for row in evidence_rows)

        junk = list(spec.get("junk_strings") or []) + list(spec.get("is_junk_strings") or [])
        junk_hits = 0
        for claim in actual_claims:
            obj = str(claim.get("object") or "")
            if any(s and s in obj for s in junk) or obj in junk:
                junk_hits += 1

        job_errors = await conn.fetch(
            """
            SELECT last_error FROM durable_job
             WHERE owner = $1 AND session_id = $2 AND last_error IS NOT NULL
            """,
            owner, session,
        )
        error_counts: Counter[str] = Counter()
        for row in job_errors:
            error_counts[_classify_error(str(row["last_error"]))] += 1
        for err in call_errors:
            error_counts[_classify_error(err)] += 1

        wall_ms = (time.perf_counter() - t_wall) * 1000
        return {
            "fixture": str(fixture_path.name),
            "extract_mode": extract_mode,
            "extract_model": extract_model or None,
            "extract_model_schema": schema_mode,
            "owner": owner,
            "claim_metrics": claim_metrics,
            "actual_claims": actual_claims,
            "pasted_span_false_grounding_count": pasted_false,
            "junk_string_hits": junk_hits,
            "abstain_false_positives": abstain_fp,
            "jobs": snapshot,
            "error_counts": dict(error_counts),
            "call_errors": call_errors[:8],
            "latency_ms": {
                "n": len(latencies_ms),
                "p50": _pct(latencies_ms, 50),
                "p95": _pct(latencies_ms, 95),
                "mean": round(statistics.mean(latencies_ms), 1) if latencies_ms else None,
                "wall_ms": round(wall_ms, 1),
            },
        }
    finally:
        if conn is not None:
            await conn.close()
        provider.shutdown()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--extract-mode", choices=("rules_only", "rules_then_model"), default="rules_only")
    parser.add_argument("--extract-url", default="")
    parser.add_argument("--extract-model", default="")
    parser.add_argument("--schema", default="json_object")
    parser.add_argument("--key-env", default="HYBRID_AGE_EXTRACT_KEY")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)
    _validate_dsn(args.dsn)
    report = asyncio.run(run_case(
        dsn=args.dsn,
        fixture_path=Path(args.fixture),
        extract_mode=args.extract_mode,
        extract_url=args.extract_url,
        extract_model=args.extract_model,
        schema_mode=args.schema,
        key_env=args.key_env,
        timeout_s=args.timeout,
    ))
    text = json.dumps(report, indent=2, default=str)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
