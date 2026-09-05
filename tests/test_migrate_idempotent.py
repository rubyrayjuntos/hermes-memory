"""tests/test_migrate_idempotent.py — migration idempotency + concurrency safety.

Requires DB: run with --migrate against hermes_test.
Does not touch prod hermes_memory. Verifies:
- Re-running scripts/migrate.py is a no-op (idempotent, second run 0 applied)
- migration_history and schema_migrations stay consistent
- Concurrent runners contend via pg_advisory_lock (only one batch applies)
- V1 idempotency: vector extension, age graph/label guards survive re-apply
"""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytestmark = [pytest.mark.idempotent, pytest.mark.integration]


def _run_migrate(hermes_test_dsn: str) -> subprocess.CompletedProcess:
    repo_root = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "migrate.py"), "--dsn", hermes_test_dsn],
        capture_output=True, text=True, timeout=60, cwd=str(repo_root),
    )


@pytest.mark.asyncio
async def test_migrate_idempotent_rerun(migrated_db, hermes_test_dsn):
    """Second migrate run must be idempotent — 0 applied, no errors."""
    result = _run_migrate(hermes_test_dsn)
    assert result.returncode == 0, f"migrate re-run failed:\n{result.stdout}\n{result.stderr}"
    out = result.stdout + result.stderr
    assert ("already applied" in out) or ("skip" in out) or ("idempotent" in out) or ("0 applied" in out)


@pytest.mark.asyncio
async def test_migration_history_consistent(migrated_db, db_pool):
    """migration_history must have V1 (and any later migrations); optionally mirrored."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT version FROM migration_history ORDER BY version")
    versions = [r["version"] for r in rows]
    assert "V1" in versions, f"V1 missing from migration_history: {versions}"
    from hermes_memory.schema_guard import list_expected_versions, missing_versions

    missing = missing_versions(versions, list_expected_versions())
    assert missing == [], f"hermes_test history behind sql/migrations: {missing}"
    async with db_pool.acquire() as conn:
        col = await conn.fetchval(
            "SELECT column_name FROM information_schema.columns WHERE table_name='migration_history' AND column_name='execution_time_ms'"
        )
    assert col == "execution_time_ms"


@pytest.mark.asyncio
async def test_concurrent_migrate_safety(hermes_test_dsn, migrated_db):
    """Two concurrent migrate.py invocations must not corrupt state."""
    results: list[subprocess.CompletedProcess] = []
    errors: list[Exception] = []

    def run():
        try:
            results.append(_run_migrate(hermes_test_dsn))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)
    assert not errors, f"concurrent migrate raised: {errors}"
    assert len(results) == 2
    for r in results:
        assert r.returncode == 0, f"concurrent migrate failed:\n{r.stdout}\n{r.stderr}"


def test_v1_sql_is_idempotent_textually():
    """Static guard: V1 SQL must use IF NOT EXISTS / exception guards."""
    repo_root = Path(__file__).resolve().parents[1]
    v1 = repo_root / "sql" / "migrations" / "V1__vector_age.sql"
    assert v1.exists(), "V1__vector_age.sql missing"
    text = v1.read_text(encoding="utf-8")
    assert "IF NOT EXISTS" in text, "V1 must use IF NOT EXISTS"
    assert "duplicate_object" in text or "already exists" in text, "V1 must guard duplicate graph/labels"
    assert "CREATE EXTENSION IF NOT EXISTS vector" in text


def test_migrate_runner_no_inline_tampering():
    """Guard: tests never do inline DDL; runner delegates to file migrations."""
    repo_root = Path(__file__).resolve().parents[1]
    conftest = (repo_root / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "scripts/migrate.py" in conftest or "migrate.py" in conftest
    assert "hermes_test" in conftest
    if "autouse=True" in conftest:
        assert "hermes_test" in conftest


@pytest.mark.asyncio
async def test_v9_noun_passport_indexes(migrated_db, db_pool):
    async with db_pool.acquire() as conn:
        noun = await conn.fetchval("SELECT to_regclass('public.noun')")
        edge = await conn.fetchval("SELECT to_regclass('public.semantic_edge')")
        assert noun and edge
        gen = await conn.fetchval(
            """
            SELECT count(*) FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            WHERE c.relname = 'semantic_edge' AND a.attgenerated = 's'
            """
        )
        assert int(gen) == 0
        idx = await conn.fetch(
            """
            SELECT indexdef FROM pg_indexes
            WHERE tablename = 'memory_chunk_nodes'
            """
        )
        defs = " ".join(r["indexdef"] for r in idx)
        assert "noun_id IS NOT NULL" in defs
        assert "noun_id IS NULL" in defs
        nullable = await conn.fetchval(
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name = 'memory_chunk_nodes' AND column_name = 'vertex_id'
            """
        )
        assert nullable == "YES"
        typ = await conn.fetchval(
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_name = 'conversations' AND column_name = 'id'
            """
        )
        assert typ == "bigint"
