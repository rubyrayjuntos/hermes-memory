#!/usr/bin/env python3
"""Replay existing conversations through C–F on hermes_test (not live), or
`--live` to backfill hermes_memory in place.

Default: copies `conversations` from hermes_memory onto hermes_test, MERGEs
the AGE flower, then runs extract_nouns / passports / mentions. Does not
insert duplicate conversation rows on live.

`--live`: same C–F against hermes_memory rows (no copy). Stamps drain_status.
Skips verify/bench synthetics. Empty extracts stay unpassported (no invented nouns).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import asyncpg
import psycopg

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "src"))

from hermes_memory.config import dotenv_key_from_file, load_config  # noqa: E402
from hermes_memory.embed import Embedder  # noqa: E402
from hermes_memory.extract_nouns import extract_nouns  # noqa: E402
from hermes_memory.graph_api import _load_dotenv_files, is_synthetic_session  # noqa: E402
from hermes_memory.provider import HybridAgeMemoryProvider, _is_noise  # noqa: E402
from hermes_memory.store import Store  # noqa: E402
from hermes_memory.walk import parse_embedding  # noqa: E402
from hermes_memory.write_outcome import DRAIN_COMPLETE, Kind  # noqa: E402

COPY_SQL = """
INSERT INTO conversations (
    id, session_id, agent_identity, role, content, ts, embedding,
    metadata, processed_at, relations_processed_at,
    processing_attempts, last_error
) VALUES (
    %s, %s, %s, %s, %s, %s, %s::vector, %s::jsonb, %s, %s, %s, %s
)
ON CONFLICT (id) DO NOTHING
"""


def _resolve_dsn(raw: str) -> str:
    pw = os.environ.get("HERMES_PG_PASSWORD", "")
    if pw:
        raw = raw.replace("{pg_password}", pw).replace("***", pw)
    return raw


def _test_dsn(live: str) -> str:
    parsed = urlparse(live)
    db = (parsed.path or "/").rsplit("/", 1)[-1]
    if db != "hermes_memory":
        raise SystemExit(f"refusing source db={db!r}")
    out = urlunparse(parsed._replace(path="/hermes_test"))
    if urlparse(out).path.rsplit("/", 1)[-1] != "hermes_test":
        raise SystemExit("refusing rewritten DSN")
    return out


def copy_conversations(live_dsn: str, test_dsn: str) -> int:
    select_sql = """
        SELECT id, session_id, agent_identity, role, content, ts,
               embedding::text, metadata::text, processed_at,
               relations_processed_at, processing_attempts, last_error
          FROM conversations
         ORDER BY id
    """
    with psycopg.connect(live_dsn) as src, psycopg.connect(test_dsn) as dst:
        with src.cursor() as sc, dst.cursor() as dc:
            sc.execute(select_sql)
            inserted = 0
            while True:
                rows = sc.fetchmany(100)
                if not rows:
                    break
                dc.executemany(COPY_SQL, rows)
                inserted += len(rows)
            dc.execute(
                "SELECT setval(pg_get_serial_sequence('conversations', 'id'), "
                "GREATEST((SELECT COALESCE(max(id), 1) FROM conversations), 1))"
            )
            dst.commit()
            dc.execute("SELECT count(*) FROM conversations")
            total = int(dc.fetchone()[0])
    print(f"copy_batch_rows={inserted} conversations_on_test={total}")
    return total


async def replay(
    target_dsn: str,
    *,
    stamp_drain: bool = False,
    turn_ids: list[int] | None = None,
) -> dict[str, int]:
    cfg = load_config()
    embedder = Embedder(cfg.embed_url, cfg.embed_model, cfg.embed_dim)
    pool = await asyncpg.create_pool(target_dsn, min_size=1, max_size=4)
    store = Store(pool, graph_name=cfg.graph)
    await store.ensure_flower_labels()
    flower = HybridAgeMemoryProvider(config=cfg)
    stats = defaultdict(int)
    label_vecs: dict[str, list[float]] = {}

    async with pool.acquire() as conn:
        if turn_ids is not None:
            rows = await conn.fetch(
                """
                SELECT id, session_id, content, embedding::text AS embedding,
                       (
                         SELECT p.id FROM conversations p
                          WHERE p.session_id = conversations.session_id
                            AND p.id < conversations.id
                          ORDER BY p.id DESC
                          LIMIT 1
                       ) AS prev_id
                  FROM conversations
                 WHERE id = ANY($1::bigint[])
                 ORDER BY session_id, id
                """,
                turn_ids,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, session_id, content, embedding::text AS embedding,
                       NULL::bigint AS prev_id
                  FROM conversations
                 ORDER BY session_id, id
                """
            )

    existing: list[str] = []
    try:
        existing = await store.fetch_noun_labels()
    except Exception:
        existing = []

    pending: list[tuple[int, str, str, list[float], list, int | None]] = []
    labels_needed: set[str] = set()
    for row in rows:
        stats["seen"] += 1
        session_id = str(row["session_id"] or "")
        content = str(row["content"] or "")
        conv_id = int(row["id"])
        if is_synthetic_session(session_id):
            stats["synthetic"] += 1
            continue
        if _is_noise(content):
            stats["noise"] += 1
            pending.append((conv_id, session_id, content, [], [], row["prev_id"]))
            continue
        vec = parse_embedding(row["embedding"])
        mentions = extract_nouns(
            content,
            existing_labels=existing,
            synthetic_session=False,
        )
        for m in mentions:
            if m.label not in existing:
                existing.append(m.label)
            labels_needed.add(m.label)
        pending.append((conv_id, session_id, content, vec, mentions, row["prev_id"]))

    missing_labels = sorted(lab for lab in labels_needed if lab not in label_vecs)
    batch = 64
    for i in range(0, len(missing_labels), batch):
        chunk = missing_labels[i : i + batch]
        embedded = await embedder.embed_texts(chunk)
        for lab, vec in zip(chunk, embedded):
            if vec:
                label_vecs[lab] = vec
        stats["label_embed_batches"] += 1
    stats["unique_labels"] = len(labels_needed)
    stats["unique_labels_embedded"] = len(label_vecs)

    need_turn_embed = [
        (idx, item[0], item[2])
        for idx, item in enumerate(pending)
        if item[4] and len(item[3]) != cfg.embed_dim
    ]
    for i in range(0, len(need_turn_embed), batch):
        chunk = need_turn_embed[i : i + batch]
        embedded = await embedder.embed_texts([c[2][:8000] for c in chunk])
        for (idx, _cid, _content), vec in zip(chunk, embedded):
            if vec:
                conv_id, session_id, content, _old, mentions, pred = pending[idx]
                pending[idx] = (conv_id, session_id, content, vec, mentions, pred)
                stats["reembedded"] += 1

    prev: dict[str, int] = {}
    targeted = turn_ids is not None
    for n, (conv_id, session_id, content, vec, mentions, sql_prev) in enumerate(
        pending, 1
    ):
        if targeted:
            pred = int(sql_prev) if sql_prev is not None else None
        else:
            pred = prev.get(session_id)
        try:
            vertex_id = await flower._link_turn_flower(
                store, conv_id, session_id, content, pred,
            )
        except Exception:
            vertex_id = None
            stats["flower_exc"] += 1
        if not targeted:
            prev[session_id] = conv_id
        if vertex_id is None:
            stats["flower_fail"] += 1
        status = DRAIN_COMPLETE
        if not vec:
            status = Kind.EMBED_NULL.value
        if vertex_id is None:
            status = Kind.GRAPH_DEGRADED.value
        if not mentions:
            stats["no_nouns"] += 1
            if stamp_drain:
                try:
                    await store.set_drain_status(conv_id, status)
                except Exception:
                    stats["stamp_fail"] += 1
            continue
        try:
            noun_ids = await store.write_noun_passports(
                mentions,
                chunk_id=f"conv_{conv_id}",
                source="conversation",
                vertex_id=vertex_id,
                session_id=session_id,
                turn_id=conv_id,
                graph_name=store.graph_name,
            )
        except Exception:
            stats["passport_fail"] += 1
            if stamp_drain:
                try:
                    await store.set_drain_status(conv_id, Kind.GRAPH_DEGRADED.value)
                except Exception:
                    stats["stamp_fail"] += 1
            continue
        stats["passports"] += len(noun_ids)
        if len(vec) != cfg.embed_dim or len(noun_ids) < 2:
            stats["skip_f"] += 1
            if stamp_drain:
                try:
                    await store.set_drain_status(conv_id, status)
                except Exception:
                    stats["stamp_fail"] += 1
            continue
        pairs: list[tuple[int, int]] = []
        src_vecs: dict[int, list[float]] = {}
        confs: dict[tuple[int, int], float] = {}
        for i in range(len(noun_ids) - 1):
            src_id, tgt_id = noun_ids[i], noun_ids[i + 1]
            pairs.append((src_id, tgt_id))
            confs[(src_id, tgt_id)] = float(mentions[i].conf)
            src_vec = label_vecs.get(mentions[i].label)
            if src_vec:
                src_vecs[src_id] = src_vec
        if pairs and src_vecs:
            try:
                await store.upsert_mentions_chain(
                    pairs,
                    turn_id=conv_id,
                    turn_vec=vec,
                    src_vecs=src_vecs,
                    confs=confs,
                )
                stats["chains"] += 1
            except Exception:
                stats["chain_fail"] += 1
                status = Kind.GRAPH_DEGRADED.value
        else:
            stats["skip_f"] += 1
        if stamp_drain:
            try:
                await store.set_drain_status(conv_id, status)
            except Exception:
                stats["stamp_fail"] += 1
        if n % 50 == 0:
            print(
                f"progress n={n}/{len(pending)} passports={stats['passports']} "
                f"chains={stats['chains']}",
                flush=True,
            )

    await pool.close()
    return dict(stats)


LIVE_DBS = frozenset({"hermes_memory", "hermes_memory_installed"})


def _parse_turn_ids(raw: str | None) -> list[int] | None:
    """None means all rows. A present --turn-ids flag (even empty) is a list."""
    if raw is None:
        return None
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.append(int(part))
    return out


def _redact(msg: str, *secrets: str) -> str:
    out = msg
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[redacted]")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--live",
        action="store_true",
        help="C–F backfill hermes_memory in place (no copy, no hermes_test). "
             "Stamps drain_status. Skips verify/bench synthetics.",
    )
    ap.add_argument(
        "--turn-ids",
        default=None,
        help="Comma-separated conversation ids (with --live). Replays only those "
             "rows. Empty list replays zero rows (does not mean all).",
    )
    ap.add_argument(
        "--installed",
        action="store_true",
        help="With --live, use ~/.hermes/.env HYBRID_AGE_DSN (hermes_memory_installed).",
    )
    args = ap.parse_args()
    _load_dotenv_files()
    if args.installed:
        live = _resolve_dsn(
            dotenv_key_from_file(Path.home() / ".hermes" / ".env", "HYBRID_AGE_DSN")
            or ""
        )
    else:
        live = _resolve_dsn(os.environ.get("HYBRID_AGE_DSN", ""))
    if not live:
        print("missing DSN", file=sys.stderr)
        return 2
    secrets = (live, os.environ.get("HERMES_PG_PASSWORD", ""))
    try:
        if args.live:
            parsed = urlparse(live)
            db = (parsed.path or "/").rsplit("/", 1)[-1]
            if db not in LIVE_DBS:
                print(
                    "refusing --live (database is not hermes_memory "
                    "or hermes_memory_installed)",
                    file=sys.stderr,
                )
                return 2
            print("live_cf_backfill start", flush=True)
            ids = _parse_turn_ids(args.turn_ids)
            stats = asyncio.run(
                replay(live, stamp_drain=True, turn_ids=ids)
            )
            print("live_cf_backfill", " ".join(f"{k}={v}" for k, v in sorted(stats.items())))
            return 0
        test = _test_dsn(live)
        secrets = (live, test, os.environ.get("HERMES_PG_PASSWORD", ""))
        os.environ["HYBRID_AGE_DSN"] = test
        copied = copy_conversations(live, test)
        print(f"copied_conversations={copied}")
        stats = asyncio.run(replay(test))
        print("replay", " ".join(f"{k}={v}" for k, v in sorted(stats.items())))
        return 0
    except Exception as exc:
        print(_redact(f"{type(exc).__name__}: {exc}", *secrets), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
