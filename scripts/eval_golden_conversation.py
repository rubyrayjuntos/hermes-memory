#!/usr/bin/env python3
"""Ingest tests/fixtures/golden_conversation.json through the live provider path.

Requires an *_eval or hermes_release_gate_* DSN. Prints claim diff and
vector vs hybrid question results as JSON.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "golden_conversation.json"


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


async def run(dsn: str, fixture_path: Path) -> dict:
    import asyncpg

    from hermes_memory.config import HybridAgeConfig
    from hermes_memory.provider import HybridAgeMemoryProvider

    if not re.search(r"(_eval\b|hermes_release_gate_)", dsn):
        raise SystemExit("refusing: DSN must name an *_eval or hermes_release_gate_* database")
    spec = json.loads(fixture_path.read_text(encoding="utf-8"))
    owner = spec["owner"]
    session = spec["session_id"]
    cfg = HybridAgeConfig(dsn=dsn, prefetch_timeout_s=30.0, retrieval_mode="vector")
    prov = HybridAgeMemoryProvider(config=cfg)
    prov.initialize(session, agent_identity=owner, agent_context="primary")
    expected_rows = 0
    for turn in spec["turns"]:
        if turn["role"] != "user":
            continue
        expected_rows += 2
    # Pair user/assistant in order.
    paired = []
    buf = None
    for turn in spec["turns"]:
        if turn["role"] == "user":
            buf = turn
        elif turn["role"] == "assistant" and buf is not None:
            paired.append((buf, turn))
            buf = None
    for user, assistant in paired:
        prov.sync_turn(user["content"], assistant["content"], session_id=session)
    conn = await asyncpg.connect(dsn)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        n = await conn.fetchval(
            "select count(*) from conversations where agent_identity=$1 and drain_status='complete'",
            owner,
        )
        if n >= expected_rows:
            break
        await asyncio.sleep(0.4)
    jobs_left = await conn.fetchval(
        "select count(*) from durable_job where owner=$1 and kind like 'extract%' and status not in ('done','cancelled')",
        owner,
    )
    rows = await conn.fetch(
        "select subject, verb, object, valid_from, valid_to is null as current, status "
        "from claims where owner=$1 and status='grounded' order by valid_from, claim_id",
        owner,
    )
    got = [
        {
            "subject": r["subject"],
            "verb": r["verb"],
            "object": r["object"],
            "event_time": r["valid_from"].isoformat() if r["valid_from"] else None,
            "current": bool(r["current"]),
            "status": r["status"],
        }
        for r in rows
    ]
    expected = spec["expected_claims"]

    def key(c):
        return (_norm(c["subject"]), _norm(c["verb"]), _norm(c["object"]), bool(c.get("current")))

    exp_keys = [key(c) for c in expected]
    got_keys = [key(c) for c in got]
    missing = [c for c in expected if key(c) not in got_keys]
    extra = [c for c in got if key(c) not in exp_keys]

    def ask(mode: str, text: str) -> str:
        prov.config.retrieval_mode = mode
        return prov.prefetch(text, session_id=session) or ""

    questions = []
    for q in spec["questions"]:
        row = {"id": q["id"], "text": q["text"], "vector": {}, "hybrid": {}}
        for mode in ("vector", "hybrid"):
            injected = ask(mode, q["text"])
            low = injected.lower()
            contains = [s for s in q.get("expect_contains") or [] if s.lower() not in low]
            absent = [s for s in q.get("expect_absent") or [] if s.lower() in low]
            row[mode] = {
                "pass": not contains and not absent,
                "missing": contains,
                "unexpected": absent,
                "chars": len(injected),
            }
        questions.append(row)
    await conn.close()
    prov.shutdown()
    return {
        "owner": owner,
        "complete_rows": n,
        "expected_rows": expected_rows,
        "jobs_left": jobs_left,
        "claims_got": got,
        "missing": missing,
        "extra": extra,
        "questions": questions,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dsn", required=True)
    p.add_argument("--fixture", default=str(FIXTURE))
    args = p.parse_args(argv)
    report = asyncio.run(run(args.dsn, Path(args.fixture)))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
