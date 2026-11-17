#!/usr/bin/env python3
"""Ingest the golden conversation through the provider and score its outcomes.

Requires an *_eval or hermes_release_gate_* DSN. Tests may pass hermes_test
explicitly through run(..., allow_test_dsn=True); the CLI never permits it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "golden_conversation.json"
_EXTRACT_MODES = ("rules_only", "rules_then_model")


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _turn_admission(turns: list[dict]) -> tuple[int, list[dict]]:
    from hermes_memory.turn_filter import _noise_reason

    if len(turns) % 2:
        raise ValueError("golden fixture must contain user/assistant turn pairs")
    expected = 0
    dropped = []
    for index in range(0, len(turns), 2):
        user, assistant = turns[index:index + 2]
        if user.get("role") != "user" or assistant.get("role") != "assistant":
            raise ValueError(f"invalid golden turn pair at index {index}")
        for turn in (user, assistant):
            content = turn.get("content")
            if not isinstance(content, str):
                raise ValueError(f"golden turn {turn.get('turn_id', index)} has non-string content")
            reason = _noise_reason(content[:8000], role=turn["role"])
            if reason is None:
                expected += 1
            else:
                dropped.append({
                    "turn_id": turn.get("turn_id", str(index)),
                    "role": turn["role"],
                    "reason": reason,
                })
    return expected, dropped


def _expected_rows(turns: list[dict]) -> int:
    return _turn_admission(turns)[0]


def _claim_key(claim: dict) -> tuple[str, str, str, str, bool]:
    return (
        _norm(claim.get("subject")),
        _norm(claim.get("verb")),
        _norm(claim.get("object")),
        _norm(claim.get("polarity") or "positive"),
        bool(claim.get("current")),
    )


def _evidence_key(
    span_id: int,
    subject: str,
    verb: str,
    obj: str,
    speaker: str,
    extractor_version: str,
) -> tuple[int, None, None, str, str, str, str, str]:
    return (
        int(span_id), None, None, _norm(subject), _norm(verb), _norm(obj),
        _norm(speaker), str(extractor_version),
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


def _status_agreement(expected_claims: list[dict], actual_claims: list[dict]) -> dict:
    expected_statuses = Counter(
        (
            _norm(claim["subject"]), _norm(claim["verb"]), _norm(claim["object"]),
            _norm(claim.get("polarity") or "positive"), _norm(claim["status"]),
            bool(claim["current"]),
        )
        for claim in expected_claims
    )
    actual_statuses = Counter(
        (
            _norm(claim["subject"]), _norm(claim["verb"]), _norm(claim["object"]),
            _norm(claim["polarity"]), _norm(claim["status"]), bool(claim["current"]),
        )
        for claim in actual_claims
    )
    matched = sum((expected_statuses & actual_statuses).values())
    expected = sum(expected_statuses.values())
    return {
        "expected": expected,
        "matched": matched,
        "agreement": round(matched / expected, 3) if expected else None,
    }


def _drained(
    *,
    row_count: int,
    complete_rows: int,
    expected_rows: int,
    rules_jobs: int,
    rules_done: int,
    jobs_pending: int,
    jobs_failed: int,
) -> bool:
    return (
        row_count == expected_rows
        and complete_rows == expected_rows
        and rules_jobs == expected_rows
        and rules_done == expected_rows
        and jobs_pending == 0
        and jobs_failed == 0
    )


def _validate_dsn(dsn: str, *, allow_test_dsn: bool) -> None:
    parsed = urlparse(dsn)
    if parsed.port == 5450 or ":5450" in dsn or re.search(r"(?:^|\s)port\s*=\s*5450(?:\s|$)", dsn):
        raise SystemExit("refusing: port 5450 is out of scope")
    database = parsed.path.lstrip("/").split("?", 1)[0]
    allowed = bool(re.search(r"(?:^|_)eval(?:$|_)", database)) or database.startswith(
        "hermes_release_gate_"
    )
    if allow_test_dsn:
        allowed = allowed or database == "hermes_test"
    if not allowed:
        raise SystemExit(
            "refusing: DSN must name an *_eval or hermes_release_gate_* database"
        )


def _build_config(dsn: str, extract_mode: str | None):
    from hermes_memory.config import HybridAgeConfig

    mode = extract_mode or os.environ.get("HYBRID_AGE_EXTRACT_MODE", "rules_only")
    if mode not in _EXTRACT_MODES:
        raise ValueError(f"invalid extract_mode {mode!r}; expected one of {_EXTRACT_MODES}")
    cfg = HybridAgeConfig(
        dsn=dsn,
        prefetch_timeout_s=30.0,
        retrieval_mode="vector",
        extract_mode=mode,
        extract_url=os.environ.get("HYBRID_AGE_EXTRACT_URL", ""),
        extract_model=os.environ.get("HYBRID_AGE_EXTRACT_MODEL", ""),
    )
    if mode == "rules_then_model" and not (cfg.extract_url.strip() and cfg.extract_model.strip()):
        raise ValueError(
            "rules_then_model requires HYBRID_AGE_EXTRACT_URL and HYBRID_AGE_EXTRACT_MODEL"
        )
    return cfg


def _question_claim_present(expected: dict | None, claims: list[dict]) -> bool | None:
    if expected is None:
        return None
    for claim in claims:
        if (
            _norm(claim.get("subject")) == _norm(expected.get("subject"))
            and _norm(claim.get("verb")) == _norm(expected.get("verb"))
            and _norm(claim.get("object")) == _norm(expected.get("object"))
            and (
                "polarity" not in expected
                or _norm(claim.get("polarity")) == _norm(expected["polarity"])
            )
            and (
                "current" not in expected
                or bool(claim.get("current")) == bool(expected["current"])
            )
        ):
            return True
    return False


def _event_time_agreement(
    expected_evidence: list[dict],
    actual_evidence: list[dict],
    turn_span_ids: dict[str, int],
    fixture_turns: dict[str, dict],
) -> dict:
    actual_by_span: dict[int, list[dict]] = defaultdict(list)
    for evidence in actual_evidence:
        if evidence["event_time"] is not None:
            actual_by_span[int(evidence["span_id"])].append(evidence)
    expected_times: dict[str, datetime] = {}
    actual_times: dict[str, datetime] = {}
    for evidence in expected_evidence:
        turn_id = evidence["source_turn_id"]
        span_id = turn_span_ids.get(turn_id)
        if span_id is None:
            continue
        matching = [
            row for row in actual_by_span.get(span_id, [])
            if _evidence_key(
                row["span_id"], row["subject_surface"], row["verb"], row["object_surface"],
                row["speaker"], row["extractor_version"],
            ) == _evidence_key(
                span_id, evidence["subject"], evidence["verb"], evidence["object"],
                evidence["speaker"], evidence["extractor_version"],
            )
        ]
        if not matching:
            continue
        try:
            expected_times[turn_id] = datetime.fromisoformat(fixture_turns[turn_id]["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        actual_times[turn_id] = min(
            row["event_time"] for row in matching
        )
    turns = list(expected_times.keys())
    concordant = compared = 0
    for left_index, left in enumerate(turns):
        for right in turns[left_index + 1:]:
            expected_delta = expected_times[left] - expected_times[right]
            actual_delta = actual_times[left] - actual_times[right]
            if not expected_delta or not actual_delta:
                continue
            compared += 1
            concordant += (
                expected_delta.total_seconds() < 0
            ) == (
                actual_delta.total_seconds() < 0
            )
    return {
        "matched_turns": len(turns),
        "compared_pairs": compared,
        "agreement": round(concordant / compared, 3) if compared else None,
    }


async def run(
    dsn: str,
    fixture_path: Path,
    *,
    extract_mode: str | None = None,
    timeout_s: float = 900,
    poll_interval_s: float = 0.4,
    stable_polls: int = 2,
    allow_test_dsn: bool = False,
) -> dict:
    import asyncpg

    from hermes_memory.provider import HybridAgeMemoryProvider
    from hermes_memory.tokens import count_tokens

    _validate_dsn(dsn, allow_test_dsn=allow_test_dsn)
    spec = json.loads(fixture_path.read_text(encoding="utf-8"))
    cfg = _build_config(dsn, extract_mode)
    expected_rows, dropped_turns = _turn_admission(spec["turns"])
    expected_claims = spec["expected_claims"].get(cfg.extract_mode)
    expected_evidence = spec["expected_evidence"].get(cfg.extract_mode)
    if expected_claims is None or expected_evidence is None:
        raise ValueError(f"fixture has no expected labels for extract_mode={cfg.extract_mode}")
    ideal_claims = spec.get("ideal_claims")
    if not isinstance(ideal_claims, list) or not ideal_claims:
        raise ValueError("fixture must include a non-empty ideal_claims label set")

    run_suffix = uuid.uuid4().hex[:12]
    owner = f"{spec['owner']}-{run_suffix}"
    session = f"{spec['session_id']}-{run_suffix}"
    provider = HybridAgeMemoryProvider(config=cfg)
    provider.initialize(session, agent_identity=owner, agent_context="primary")
    conn = None
    try:
        for index in range(0, len(spec["turns"]), 2):
            user, assistant = spec["turns"][index:index + 2]
            provider.sync_turn(user["content"], assistant["content"], session_id=session)
        provider.on_session_end(spec["turns"])

        conn = await asyncpg.connect(dsn)
        deadline = time.monotonic() + timeout_s
        last_row_count = None
        stable_count = 0
        snapshot = {}
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
                       count(*) FILTER (
                           WHERE kind = 'extract_rules' AND status = 'done'
                       )::int AS rules_done,
                       count(*) FILTER (
                           WHERE status IN ('pending', 'leased', 'retryable')
                       )::int AS jobs_pending,
                       count(*) FILTER (WHERE status = 'failed')::int AS jobs_failed,
                       count(*) FILTER (WHERE kind = 'extract_model')::int AS model_jobs
                  FROM durable_job
                 WHERE owner = $1 AND session_id = $2
                   AND kind IN ('extract_rules', 'extract_model')
                """,
                owner, session,
            )
            snapshot = {**dict(row), **dict(jobs)}
            if snapshot["jobs_failed"]:
                raise RuntimeError(
                    f"golden extraction failed: {snapshot['jobs_failed']} failed jobs"
                )
            ready = _drained(
                row_count=snapshot["row_count"],
                complete_rows=snapshot["complete_rows"],
                expected_rows=expected_rows,
                rules_jobs=snapshot["rules_jobs"],
                rules_done=snapshot["rules_done"],
                jobs_pending=snapshot["jobs_pending"],
                jobs_failed=snapshot["jobs_failed"],
            )
            row_count = snapshot["row_count"]
            if ready and row_count == last_row_count:
                stable_count += 1
                if stable_count >= stable_polls:
                    break
            else:
                stable_count = 0
            last_row_count = row_count
            await asyncio.sleep(poll_interval_s)
        else:
            raise TimeoutError(
                "golden conversation did not drain before deadline: "
                f"expected_rows={expected_rows}, snapshot={snapshot}"
            )

        conversation_rows = await conn.fetch(
            "SELECT id, role, content, ts FROM conversations "
            "WHERE agent_identity = $1 AND session_id = $2 ORDER BY ts, id",
            owner, session,
        )
        stored_by_content: dict[tuple[str, str], list] = defaultdict(list)
        for row in conversation_rows:
            stored_by_content[(row["role"], row["content"])].append(row)
        turn_span_ids: dict[str, int] = {}
        for turn in spec["turns"]:
            key = (turn["role"], turn["content"][:8000])
            matches = stored_by_content[key]
            if not matches:
                continue
            stored = matches.pop(0)
            turn_span_ids[turn["turn_id"]] = int(stored["id"])
        claim_rows = await conn.fetch(
            """
            SELECT subject, verb, object, polarity, status, valid_to IS NULL AS current
              FROM claims
             WHERE owner = $1
             ORDER BY claim_id
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
        claim_metrics = _prf(
            [_claim_key(claim) for claim in ideal_claims],
            [_claim_key(claim) for claim in actual_claims],
        )
        mode_label_claim_metrics = _prf(
            [_claim_key(claim) for claim in expected_claims],
            [_claim_key(claim) for claim in actual_claims],
        )
        status_agreement = _status_agreement(ideal_claims, actual_claims)
        mode_label_status_agreement = _status_agreement(expected_claims, actual_claims)

        evidence_rows = await conn.fetch(
            """
            SELECT span_id, subject_surface, verb, object_surface, speaker,
                   extractor_version, event_time
              FROM claim_evidence
             WHERE owner = $1 AND state = 'live'
            """,
            owner,
        )
        actual_evidence = [dict(row) for row in evidence_rows]
        expected_evidence_keys = []
        for item in expected_evidence:
            span_id = turn_span_ids.get(item["source_turn_id"])
            if span_id is not None:
                expected_evidence_keys.append(_evidence_key(
                    span_id, item["subject"], item["verb"], item["object"],
                    item["speaker"], item["extractor_version"],
                ))
        actual_evidence_keys = [
            _evidence_key(
                row["span_id"], row["subject_surface"], row["verb"], row["object_surface"],
                row["speaker"], row["extractor_version"],
            )
            for row in actual_evidence
        ]
        evidence_metrics = _prf(expected_evidence_keys, actual_evidence_keys)
        evidence_metrics["segment_offsets_supported"] = False
        evidence_metrics["segment_offsets_null"] = len(actual_evidence)

        pasted_spans = {
            turn_span_ids[turn["turn_id"]]
            for turn in spec["turns"]
            if turn.get("pasted") and turn["turn_id"] in turn_span_ids
        }
        pasted_false_grounding = sum(
            row["speaker"] == "user" and int(row["span_id"]) in pasted_spans
            for row in actual_evidence
        )
        assistant_grounded = sum(
            row["speaker"] == "assistant"
            and any(
                claim["status"] == "grounded"
                and _norm(claim["subject"]) == _norm(row["subject_surface"])
                and _norm(claim["verb"]) == _norm(row["verb"])
                and _norm(claim["object"]) == _norm(row["object_surface"])
                for claim in actual_claims
            )
            for row in actual_evidence
        )

        fixture_turns = {turn["turn_id"]: turn for turn in spec["turns"]}
        event_time_metrics = _event_time_agreement(
            expected_evidence, actual_evidence, turn_span_ids, fixture_turns
        )
        questions = []
        pass_by_mode = {"vector": [], "hybrid": []}
        missing_expected_claim_ids = []
        context_mode_disagreement_ids = []
        for question in spec["questions"]:
            question_row = {
                "id": question["id"],
                "kind": question["kind"],
                "text": question["text"],
                "vector": {},
                "hybrid": {},
            }
            claim_present = _question_claim_present(question.get("expect_claim"), actual_claims)
            if question.get("expect_claim") is not None and claim_present is False:
                missing_expected_claim_ids.append(question["id"])
            for mode in ("vector", "hybrid"):
                provider.config.retrieval_mode = mode
                injected = provider.prefetch(question["text"], session_id=session) or ""
                lower = injected.lower()
                missing = [
                    item for item in question.get("expect_contains", [])
                    if item.lower() not in lower
                ]
                unexpected = [
                    item for item in question.get("expect_absent", [])
                    if item.lower() in lower
                ]
                context_pass = not missing and not unexpected
                question_pass = context_pass and claim_present is not False
                total_tokens = count_tokens(injected)
                facts_tokens = total_tokens if mode == "hybrid" else 0
                question_row[mode] = {
                    "pass": question_pass,
                    "context_pass": context_pass,
                    "claim_present": claim_present,
                    "missing": missing,
                    "unexpected": unexpected,
                    "chars": len(injected),
                    "total_injection_tokens": total_tokens,
                    "facts_tokens": facts_tokens,
                    "expected_answer": question.get("expected_answer"),
                }
                pass_by_mode[mode].append(question_pass)
            if (
                question_row["vector"]["context_pass"]
                != question_row["hybrid"]["context_pass"]
            ):
                context_mode_disagreement_ids.append(question["id"])
            questions.append(question_row)

        def rate(values: list[bool]) -> float | None:
            return round(sum(values) / len(values), 3) if values else None

        return {
            "owner_id": owner,
            "extract_mode": provider.config.extract_mode,
            "fixture_turn_count": len(spec["turns"]),
            "complete_rows": snapshot["complete_rows"],
            "expected_rows": expected_rows,
            "dropped_turns": dropped_turns,
            "drop_reason_counts": dict(sorted(Counter(
                turn["reason"] for turn in dropped_turns
            ).items())),
            "extraction_jobs": {
                "rules": snapshot["rules_jobs"],
                "model": snapshot["model_jobs"],
                "pending": snapshot["jobs_pending"],
                "failed": snapshot["jobs_failed"],
            },
            "claim_metrics": claim_metrics,
            "mode_label_claim_metrics": mode_label_claim_metrics,
            "timeline_status_agreement": status_agreement,
            "mode_label_timeline_status_agreement": mode_label_status_agreement,
            "evidence_metrics": evidence_metrics,
            "event_time_agreement": event_time_metrics,
            "pasted_span_false_grounding_count": pasted_false_grounding,
            "assistant_grounded_evidence_count": assistant_grounded,
            "question_pass_rate": {mode: rate(values) for mode, values in pass_by_mode.items()},
            "question_diagnostics": {
                "missing_expected_claim_ids": missing_expected_claim_ids,
                "context_mode_disagreement_ids": context_mode_disagreement_ids,
            },
            "questions": questions,
        }
    finally:
        if conn is not None:
            await conn.close()
        provider.shutdown()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--fixture", default=str(FIXTURE))
    parser.add_argument(
        "--extract-mode",
        choices=_EXTRACT_MODES,
        default=os.environ.get("HYBRID_AGE_EXTRACT_MODE", "rules_only"),
    )
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args(argv)
    report = asyncio.run(run(
        args.dsn, Path(args.fixture), extract_mode=args.extract_mode, timeout_s=args.timeout
    ))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
