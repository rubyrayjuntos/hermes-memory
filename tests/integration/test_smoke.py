"""Integration smoke — verify CLI + bridge symmetry against a live Postgres.

Prefers ``HYBRID_AGE_DSN`` (CI uses hermes_test on 5432). Locally, if that
env is unset, brings up docker compose on 5450 when Docker is available.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration

_DEFAULT_COMPOSE_DSN = (
    f"postgres://hermes:{os.environ.get('HERMES_PG_PASSWORD', 'ci-local-password')}"
    "@localhost:5450/hermes_memory"
)


def _compose(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("HERMES_PG_PASSWORD", "ci-local-password")
    return subprocess.run(
        ["docker", "compose", "-f", str(REPO_ROOT / "docker-compose.yml"), *args],
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
        cwd=str(REPO_ROOT),
    )


def _stack_up() -> bool:
    if _compose("up", "-d", "--wait", "--wait-timeout", "120").returncode != 0:
        return False
    try:
        with socket.create_connection(("127.0.0.1", 5450), timeout=5):
            return True
    except OSError:
        return False


def _can_connect(dsn: str) -> bool:
    try:
        import asyncio

        import asyncpg

        async def _probe() -> None:
            conn = await asyncpg.connect(dsn, timeout=5)
            await conn.close()

        asyncio.run(_probe())
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def live_dsn() -> str:
    env_dsn = os.environ.get("HYBRID_AGE_DSN")
    if env_dsn and _can_connect(env_dsn):
        return env_dsn
    if shutil.which("docker") is None:
        pytest.skip("HYBRID_AGE_DSN unreachable and docker not available")
    if not _stack_up():
        pytest.skip("compose stack unavailable")
    return _DEFAULT_COMPOSE_DSN


def test_verify_pipeline(live_dsn: str) -> None:
    """The verify CLI passes end-to-end against the reachable DSN."""
    from hermes_memory.verify import main as verify_main

    rc = verify_main(["--dsn", live_dsn])
    assert rc == 0, "verify.py failed against the live database"


def test_bridge_symmetry(live_dsn: str) -> None:
    """P8 integration twin: bridge insert/delete is symmetric."""
    import asyncio

    import asyncpg

    from hermes_memory.store import Store

    async def run() -> None:
        conn = await asyncpg.connect(live_dsn)
        try:

            class _PoolShim:
                """Minimal pool stand-in: acquire() returns an async CM."""

                def __init__(self, c):
                    self._c = c

                def acquire(self):
                    c = self._c

                    class _A:
                        async def __aenter__(self):
                            return c

                        async def __aexit__(self, *a):
                            return False

                    return _A()

            store = Store(_PoolShim(conn), graph_name="hermes_knowledge")
            await conn.execute(
                """
                INSERT INTO memory_chunk_nodes (chunk_id, source, vertex_id, graph_name)
                VALUES ('p8test:0', 'doc_chunk', 999999001, 'hermes_knowledge')
                ON CONFLICT DO NOTHING
                """
            )
            ids = await store.bridge_vertex_ids(["p8test:0"])
            assert ids == ["999999001"]
            await conn.execute("DELETE FROM memory_chunk_nodes WHERE chunk_id = 'p8test:0'")
            ids = await store.bridge_vertex_ids(["p8test:0"])
            assert ids == []
        finally:
            await conn.close()

    asyncio.run(run())
