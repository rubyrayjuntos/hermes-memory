from pathlib import Path

import pytest
from scripts.eval_golden_conversation import run

pytestmark = [pytest.mark.store, pytest.mark.integration]

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "golden_conversation.json"


@pytest.mark.asyncio
async def test_golden_provider_ingest_drains_and_is_owner_scoped(
    hermes_test_dsn, db_pool
):
    import asyncpg

    from hermes_memory.config import HybridAgeConfig
    from hermes_memory.provider import HybridAgeMemoryProvider

    report = await run(
        hermes_test_dsn,
        FIXTURE,
        extract_mode="rules_only",
        timeout_s=120,
        poll_interval_s=0.1,
        stable_polls=2,
        allow_test_dsn=True,
    )
    assert report["fixture_turn_count"] == 42
    assert report["complete_rows"] == report["expected_rows"]
    assert report["extraction_jobs"]["rules"] == report["expected_rows"]
    assert report["extraction_jobs"]["pending"] == 0
    assert report["extraction_jobs"]["failed"] == 0
    assert report["claim_metrics"]["expected"] == 11
    assert report["claim_metrics"]["recall"] > 0.455
    assert report["claim_metrics"]["precision"] >= 0.556
    assert report["mode_label_claim_metrics"]["expected"] == 5
    assert report["mode_label_claim_metrics"]["recall"] == 1.0
    assert len(report["dropped_turns"]) == 15
    assert all(
        item["role"] == "assistant" or item["reason"] == "noise_pattern"
        for item in report["dropped_turns"]
    )
    assert report["question_diagnostics"]["missing_expected_claim_ids"] == []

    other_owner = f"{report['owner_id']}-other"
    other_session = f"{other_owner}-session"
    other_provider = HybridAgeMemoryProvider(
        config=HybridAgeConfig(dsn=hermes_test_dsn, prefetch_timeout_s=30.0)
    )
    other_provider.initialize(
        other_session, agent_identity=other_owner, agent_context="primary"
    )
    try:
        assert other_provider.prefetch(
            "What database does Nightingale use?", session_id=other_session
        ) == ""
    finally:
        other_provider.shutdown()

    conn = await asyncpg.connect(hermes_test_dsn)
    try:
        assert await conn.fetchval(
            "SELECT count(*) FROM claims WHERE owner = $1", other_owner
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM claim_evidence WHERE owner = $1", other_owner
        ) == 0
    finally:
        await conn.close()
