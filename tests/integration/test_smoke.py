"""Integration smoke test — full verify pipeline against a live compose stack.

Runs only under the ``integration`` marker; skips when Docker is unavailable
or the stack can't be brought up. Uses the C2 compose project (port 5450).
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("docker") is None, reason="docker not available"
    ),
]


def _compose(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if "HERMES_PG_PASSWORD" not in env:
        env["HERMES_PG_PASSWORD"] = "ci-local-password"
    return subprocess.run(
        ["docker", "compose", "-f", str(REPO_ROOT / "docker-compose.yml"), *args],
        capture_output=True, text=True, timeout=600, env=env,
        cwd=str(REPO_ROOT),
    )


def _stack_up() -> bool:
    """Bring the compose stack up healthy; True on success."""
    if _compose("up", "-d", "--wait", "--wait-timeout", "120").returncode != 0:
        return False
    # port probe
    try:
        with socket.create_connection(("127.0.0.1", 5450), timeout=5):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def live_stack():
    if not _stack_up():
        pytest.skip("compose stack unavailable")
    yield
    # leave the stack running between module tests; teardown handled by -v down


def test_verify_pipeline(live_stack):
    """The verify CLI passes end-to-end against the live stack."""
    from hermes_memory.verify import main as verify_main

    rc = verify_main([])
    assert rc == 0, "verify.py failed against live compose stack"


def test_bridge_symmetry(live_stack):
    """P8 integration twin: bridge insert/delete is symmetric."""
    import asyncio

    import asyncpg

    from hermes_memory.store import Store

    dsn = os.environ.get("HYBRID_AGE_DSN",
                         f"postgres://hermes:{os.environ.get('HERMES_PG_PASSWORD', 'ci-local-password')}@localhost:5450/hermes_memory")

    async def run():
        conn = await asyncpg.connect(dsn)
        try:
            class _PoolShim:
                """Minimal pool stand-in: acquire() returns an async CM."""
                def __init__(self, c): self._c = c
                def acquire(self):
                    c = self._c
                    class _A:
                        async def __aenter__(self): return c
                        async def __aexit__(self, *a): return False
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
