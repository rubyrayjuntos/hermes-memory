"""Pytest bootstrap + isolated hermes_test fixtures.

- Puts repo root (for tests.strategies) and src/ on sys.path once.
- Provides --migrate flag: only when passed do DB-backed tests run migrations
  against the isolated hermes_test database. Never touches hermes_memory prod.
- No autouse teardown on prod. All DB fixtures are explicit, function or
  session scoped, and operate solely on hermes_test.
- Isolation: tests must opt into `hermes_test_dsn` / `migrated_db` / `db_pool` /
  `store` fixtures. If --migrate is not set, integration/store/idempotent tests
  are skipped with a clear message.
- DB creation: connects to maintenance DB (postgres) and CREATE DATABASE
  hermes_test if missing. No inline DB tampering — delegates to scripts/migrate.py.
"""
from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (_REPO_ROOT / "tests", _REPO_ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--migrate",
        action="store_true",
        default=False,
        help="Run migrations against isolated hermes_test DB before DB tests",
    )
    parser.addoption(
        "--dsn",
        default=None,
        help="Override hermes_test DSN (default: derived from env or localhost)",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "store: hermes_test-backed store tests")
    config.addinivalue_line("markers", "idempotent: migration idempotency tests")


def pytest_collection_modifyitems(config, items):
    """Gate DB tests behind --migrate so CI intent is explicit."""
    if config.getoption("--migrate"):
        return
    skip = pytest.mark.skip(reason="needs --migrate (pass --migrate to run against hermes_test)")
    for item in items:
        if any(m.name in ("integration", "store", "idempotent") for m in item.iter_markers()):
            item.add_marker(skip)

# ---------------------------------------------------------------------------
# DSN helpers — hermes_test only, never hermes_memory
# ---------------------------------------------------------------------------

_HERMES_TEST_DB = "hermes_test"
_DEFAULT_PW = os.environ.get("HERMES_PG_PASSWORD", "ci-local-password")


def _build_hermes_test_dsn(cli_dsn: str | None = None) -> str:
    """Build DSN for hermes_test. Never returns hermes_memory prod DSN."""
    if cli_dsn:
        if "hermes_memory" in cli_dsn and "hermes_test" not in cli_dsn:
            raise ValueError("Refusing DSN that points to prod hermes_memory; use hermes_test")
        return cli_dsn
    env_dsn = os.environ.get("HYBRID_AGE_DSN", "")
    if env_dsn and "hermes_test" in env_dsn:
        pw = os.environ.get("HERMES_PG_PASSWORD", _DEFAULT_PW)
        if "***" in env_dsn and pw:
            env_dsn = env_dsn.replace("***", pw)
        if "{pg_password}" in env_dsn and pw:
            env_dsn = env_dsn.replace("{pg_password}", pw)
        return env_dsn
    pg_test = os.environ.get("PG_DSN_HERMES_TEST", "")
    if pg_test:
        return pg_test
    pw = os.environ.get("HERMES_PG_PASSWORD", _DEFAULT_PW)
    host_port = os.environ.get("HERMES_PG_HOSTPORT", "localhost:5432")
    if os.environ.get("HERMES_PG_PASSWORD") and "5450" in os.environ.get("HYBRID_AGE_DSN", ""):
        host_port = "localhost:5450"
    return f"postgres://hermes:{pw}@{host_port}/{_HERMES_TEST_DB}"


@pytest.fixture(scope="session")
def hermes_test_dsn(request) -> str:
    cli_dsn = request.config.getoption("--dsn")
    dsn = _build_hermes_test_dsn(cli_dsn)
    assert "hermes_test" in dsn, f"hermes_test DSN must contain hermes_test, got {dsn!r}"
    assert "hermes_memory" not in dsn or "hermes_test" in dsn, "must not target prod hermes_memory"
    return dsn


@pytest.fixture(scope="session")
def hermes_test_maintenance_dsn(hermes_test_dsn: str) -> str:
    """DSN for maintenance DB (postgres) to CREATE DATABASE if needed."""
    if "/" in hermes_test_dsn:
        base = hermes_test_dsn.rsplit("/", 1)[0]
        return base + "/postgres"
    return hermes_test_dsn

# ---------------------------------------------------------------------------
# DB lifecycle — explicit, no autouse, hermes_test only
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def ensure_hermes_test_db(hermes_test_dsn: str, hermes_test_maintenance_dsn: str):
    """Ensure hermes_test database exists. Idempotent, no prod teardown."""
    try:
        import psycopg
    except ImportError:
        pytest.skip("psycopg not installed")
    try:
        with psycopg.connect(hermes_test_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
        return hermes_test_dsn
    except Exception:
        pass
    try:
        with psycopg.connect(hermes_test_maintenance_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (_HERMES_TEST_DB,))
                if cur.fetchone() is None:
                    cur.execute(f'CREATE DATABASE "{_HERMES_TEST_DB}" OWNER hermes')
        with psycopg.connect(hermes_test_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                except Exception:
                    pass
                try:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS age;")
                except Exception:
                    pass
    except Exception as exc:
        pytest.skip(f"cannot ensure hermes_test DB: {exc}")
    return hermes_test_dsn


@pytest.fixture(scope="session")
def migrated_db(request, hermes_test_dsn: str, ensure_hermes_test_db):
    """Run scripts/migrate.py against hermes_test when --migrate is set."""
    if not request.config.getoption("--migrate"):
        pytest.skip("needs --migrate")
    repo_root = Path(__file__).resolve().parents[1]
    migrate_py = repo_root / "scripts" / "migrate.py"
    if not migrate_py.exists():
        pytest.skip("scripts/migrate.py not found")
    env = dict(os.environ)
    env["HYBRID_AGE_DSN"] = hermes_test_dsn
    result = subprocess.run(
        [sys.executable, str(migrate_py), "--dsn", hermes_test_dsn],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        pytest.fail(
            f"migrate.py failed against hermes_test:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return hermes_test_dsn

# ---------------------------------------------------------------------------
# Per-test async pool / Store — isolated, truncates only hermes_test tables
# ---------------------------------------------------------------------------

@pytest.fixture
async def db_pool(migrated_db: str):
    """Asyncpg pool connected to migrated hermes_test. Function-scoped isolation."""
    import asyncpg

    pool = await asyncpg.create_pool(migrated_db, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def store(db_pool):
    """Store instance bound to hermes_test pool."""
    from hermes_memory.store import Store

    s = Store(db_pool, graph_name="hermes_knowledge")
    yield s


@pytest.fixture
async def clean_hermes_test_db(db_pool):
    """Truncate hermes_test tables before test. Opt-in, never autouse."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            TRUNCATE conversations, memory_entries, memory_chunk_nodes,
                     doc_chunks, librarian_runs RESTART IDENTITY CASCADE;
            """
        )
    yield
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            TRUNCATE conversations, memory_entries, memory_chunk_nodes,
                     doc_chunks, librarian_runs RESTART IDENTITY CASCADE;
            """
        )
