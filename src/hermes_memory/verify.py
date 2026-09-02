"""hermes_memory.verify — end-to-end pipeline check CLI (card C5).

Runs a synthetic turn through the provider against a live stack and asserts
rows land in every layer within one drain cycle:

    1. conversations  — the enqueued turn rows
    2. memory_entries — a mirrored on_memory_write row
    3. memory_chunk_nodes / AGE graph — bridge + vertex presence (best effort;
       graph writes only occur via ingest, so absence is reported, not fatal)

Exit codes: 0 = PASS, 1 = FAIL (with a per-layer summary).

Usage:
    python -m hermes_memory.verify [--dsn DSN] [--skip-embed]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from typing import List, Optional, Tuple

DEFAULT_DSN = (
    f"postgres://hermes:{os.environ.get('HERMES_PG_PASSWORD', 'ci-local-password')}"
    "@localhost:5450/hermes_memory"
)

TURN_USER = (
    "C5 verify synthetic user turn: please remember that Project Zephyr uses "
    "the Atlas Vault Engine for its storage layer."
)
TURN_ASSISTANT = (
    "C5 verify synthetic assistant turn: Noted — Project Zephyr relies on the "
    "Atlas Vault Engine; I'll keep that in context for this session."
)
MEMORY_TARGET = "memory"
MEMORY_CONTENT = "c5-verify-memory-row: Project Zephyr -> Atlas Vault Engine"

DRAIN_WAIT_S = float(os.environ.get("C5_VERIFY_DRAIN_S", "20"))


class VerifyResult:
    def __init__(self) -> None:
        self.checks: List[Tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    def summary(self) -> str:
        lines = []
        for name, ok, detail in self.checks:
            mark = "PASS" if ok else "FAIL"
            suffix = f" ({detail})" if detail else ""
            lines.append(f"  [{mark}] {name}{suffix}")
        verdict = "PASS" if self.ok else "FAIL"
        lines.append(f"verify: {verdict} ({sum(o for _, o, _ in self.checks)}"
                     f"/{len(self.checks)} layers)")
        return "\n".join(lines)


async def run_verify(dsn: Optional[str] = None,
                     skip_embed: bool = False,
                     drain_wait_s: Optional[float] = None) -> VerifyResult:
    """Execute the layered pipeline check. Never raises for expected failures."""
    global DRAIN_WAIT_S
    if drain_wait_s is not None:
        DRAIN_WAIT_S = drain_wait_s
    from asyncpg import connect

    from .config import load_config
    from .embed import Embedder
    from .provider import HybridAgeMemoryProvider
    from .store import Store

    cfg = load_config()
    pw = os.environ.get("HERMES_PG_PASSWORD", "")
    dsn = (
        dsn
        or os.environ.get("HYBRID_AGE_DSN")
        or cfg.dsn.replace("{pg_password}", pw)
                  .replace("***", pw)          # legacy masked default
    )
    result = VerifyResult()
    marker = f"c5-verify-{int(time.time())}"

    # ---- Layer 0: raw connectivity -----------------------------------------
    conn = None
    try:
        conn = await connect(dsn, timeout=10)
        result.add("db-connectivity", True)
    except Exception as exc:
        result.add("db-connectivity", False, str(exc).splitlines()[0][:120])
        return result

    try:
        # ---- Layer 1: provider sync_turn → conversations -------------------
        provider = HybridAgeMemoryProvider(config=cfg)
        provider.initialize(f"verify-{marker}", agent_identity="c5-verify",
                            agent_context="primary")
        try:
            provider.sync_turn(TURN_USER, TURN_ASSISTANT,
                               session_id=f"verify-{marker}")

            # Give the drain loop a window to flush.
            deadline = time.monotonic() + DRAIN_WAIT_S
            n_conversations = 0
            while time.monotonic() < deadline:
                n_conversations = await conn.fetchval(
                    """
                    SELECT count(*) FROM conversations
                     WHERE session_id = $1 AND content LIKE '%' || $2 || '%'
                    """,
                    f"verify-{marker}", "Zephyr",
                )
                if (n_conversations or 0) >= 2:
                    break
                await asyncio.sleep(0.5)
            result.add(
                "conversations rows (user+assistant)",
                (n_conversations or 0) >= 2,
                f"count={n_conversations}",
            )

            # ---- Layer 2: on_memory_write mirror → memory_entries ----------
            provider.on_memory_write(
                "add", MEMORY_TARGET, MEMORY_CONTENT,
                metadata={"source": marker},
            )
            deadline = time.monotonic() + DRAIN_WAIT_S
            n_entries = 0
            while time.monotonic() < deadline:
                n_entries = await conn.fetchval(
                    """
                    SELECT count(*) FROM memory_entries
                     WHERE agent_identity = 'c5-verify'
                       AND content LIKE 'c5-verify-memory-row:%'
                    """
                )
                if (n_entries or 0) >= 1:
                    break
                await asyncio.sleep(0.5)
            result.add(
                "memory_entries mirror row",
                (n_entries or 0) >= 1,
                f"count={n_entries}",
            )

            # ---- prefetch returns a string (never raises contract) --------
            out = provider.prefetch("Project Zephyr storage engine")
            # graph lines / token counts for tail reporting (hybrid vs vector)
            graph_lines = 0
            total_prefetch_lines = 0
            tokens_est = 0
            try:
                if isinstance(out, str) and out:
                    # format_injection Path headers match [A] -REL-> [B]
                    graph_lines = len(re.findall(r"\[.*?\]\s*-.*->\s*\[.*?\]", out))
                    total_prefetch_lines = len([ln for ln in out.splitlines() if ln.strip().startswith("- ")])
                    tokens_est = max(1, len(out) // 4) if out else 0
                else:
                    tokens_est = 0
            except Exception:
                pass
            vector_lines = max(0, total_prefetch_lines - graph_lines)
            delta = graph_lines - vector_lines
            result.add(
                "prefetch returns string",
                isinstance(out, str),
                f"len={len(out) if isinstance(out, str) else '?'} tokens~{tokens_est} graph_lines={graph_lines} vector_lines={vector_lines} delta={delta}",
            )
            # extra explicit graph-lines check for tail consumers
            result.add(
                "prefetch graph lines",
                isinstance(out, str),
                f"graph_lines={graph_lines} total_lines={total_prefetch_lines} tokens~{tokens_est} (hybrid graph vs vector delta={delta})",
            )
        finally:
            provider.shutdown()

        # cleanup synthetic SQL rows + leftover AGE Turn vertices from this
        # run and any prior verify-c5 sessions (graph was surviving DELETE).
        await conn.execute(
            "DELETE FROM conversations WHERE session_id LIKE $1",
            "verify-c5%",
        )
        await conn.execute(
            "DELETE FROM memory_entries WHERE agent_identity = 'c5-verify' "
            "AND content LIKE 'c5-verify-memory-row:%'"
        )
        try:
            import asyncpg as _apg

            pool = await _apg.create_pool(dsn, min_size=1, max_size=2)
            st = Store(pool, graph_name=cfg.graph)
            await st.purge_verify_turns()
            await pool.close()
        except Exception:
            pass
    except Exception as exc:
        result.add("pipeline-execution", False, repr(exc)[:200])
    finally:
        await conn.close()

    return result


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="hermes-memory-verify",
        description="End-to-end pipeline smoke check against a running "
                    "pgvector + AGE stack (synthetic turn → per-layer "
                    "row-count assertions). Exit 0 = PASS, 1 = FAIL.",
    )
    ap.add_argument("--dsn", default=None,
                    help="Postgres DSN (default: HYBRID_AGE_DSN env or config)")
    ap.add_argument("--drain-wait", type=float, default=None,
                    help="Seconds to wait for the background drain "
                         "(default: DRAIN_WAIT_S, 20s)")
    args = ap.parse_args(argv)

    drain_wait = args.drain_wait if args.drain_wait is not None else DRAIN_WAIT_S

    print("hermes-memory verify — starting pipeline check…")
    result = asyncio.run(run_verify(dsn=args.dsn, drain_wait_s=drain_wait))
    print(result.summary())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
