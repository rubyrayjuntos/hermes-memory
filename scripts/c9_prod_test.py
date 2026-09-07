"""V2/V3: real-turn test against an explicit DSN via the installed package.

Requires HYBRID_AGE_DSN. Does not read ~/.hermes/.env or load
~/.hermes/plugins/hybrid-age — those paths are the old copy-install layout.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

DSN = os.environ.get("HYBRID_AGE_DSN", "").strip()
if not DSN:
    print("c9_prod_test: set HYBRID_AGE_DSN (refuses ~/.hermes/.env)", file=sys.stderr)
    raise SystemExit(2)

from hermes_memory.config import load_config
from hermes_memory.provider import HybridAgeMemoryProvider
import hermes_memory.provider as p

print("provider module:", p.__file__)

cfg = load_config()
print("dsn host/db only:", cfg.dsn.split("@")[-1] if "@" in cfg.dsn else "(unset)", "embed:", cfg.embed_model)


async def main() -> None:
    prov = HybridAgeMemoryProvider(config=cfg)
    prov.initialize("v2-prod-test", agent_identity="c9-verify", agent_context="primary")

    marker = f"c9-{int(time.time())}"
    user = "C9 prod test user turn: remember that Card Nine ships the Atlas recall."
    asst = "C9 prod test assistant turn: Noted — Card Nine's Atlas recall is stored."

    prov.sync_turn(user, asst, session_id=marker)
    prov.on_memory_write(
        "add",
        "memory",
        "c9-verify-memory-row: Card Nine -> Atlas recall",
        metadata={"source": marker},
    )

    nconv = nmem = 0
    for _ in range(60):
        await asyncio.sleep(1)
        import asyncpg

        conn = await asyncpg.connect(DSN, timeout=10)
        nconv = await conn.fetchval(
            "SELECT count(*) FROM conversations WHERE session_id=$1 AND content LIKE '%Atlas%'",
            marker,
        )
        nmem = await conn.fetchval(
            "SELECT count(*) FROM memory_entries WHERE agent_identity='c9-verify' "
            "AND content LIKE 'c9-verify-memory-row:%'"
        )
        await conn.close()
        if nconv >= 2 and nmem >= 1:
            break
    print("conversations rows:", nconv, "memory rows:", nmem)

    out = prov.prefetch("Card Nine Atlas recall storage")
    print("prefetch type:", type(out).__name__, "len:", len(out))
    print("prefetch contains 'Atlas':", "Atlas" in out)
    print(out[:600])
    prov.shutdown()


asyncio.run(main())
