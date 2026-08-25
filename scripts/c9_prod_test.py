"""V2/V3: real-turn test against production DSN via installed plugin path."""
import asyncio, importlib.util, sys, time

DSN = [l for l in open('/home/rswan/.hermes/.env') if l.startswith('HYBRID_AGE_DSN=')][0].split('=',1)[1].strip()
import os
os.environ['HYBRID_AGE_DSN'] = DSN

spec = importlib.util.spec_from_file_location(
    "hybrid_age_plugin", "/home/rswan/.hermes/plugins/hybrid-age/__init__.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
from hermes_memory.provider import HybridAgeMemoryProvider  # resolves to installed path
import hermes_memory.provider as p
print("provider module:", p.__file__)

from hermes_memory.config import load_config
cfg = load_config()
print("dsn tail:", cfg.dsn[-20:], "embed:", cfg.embed_model)

async def main():
    prov = HybridAgeMemoryProvider(config=cfg)
    prov.initialize("v2-prod-test", agent_identity="c9-verify", agent_context="primary")

    marker = f"c9-{int(time.time())}"
    user = f"C9 prod test user turn: remember that Card Nine ships the Atlas recall."
    asst = f"C9 prod test assistant turn: Noted — Card Nine's Atlas recall is stored."

    prov.sync_turn(user, asst, session_id=marker)
    prov.on_memory_write("add", "memory",
                         f"c9-verify-memory-row: Card Nine -> Atlas recall",
                         metadata={"source": marker})

    # wait for drain
    for _ in range(60):
        await asyncio.sleep(1)
        import asyncpg
        conn = await asyncpg.connect(DSN, timeout=10)
        nconv = await conn.fetchval(
            "SELECT count(*) FROM conversations WHERE session_id=$1 AND content LIKE '%Atlas%'", marker)
        nmem = await conn.fetchval(
            "SELECT count(*) FROM memory_entries WHERE agent_identity='c9-verify' AND content LIKE 'c9-verify-memory-row:%'")
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
