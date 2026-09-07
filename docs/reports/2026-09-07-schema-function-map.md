# Schema / function map — 2026-09-07

This is the load-bearing inventory: table, function, and (for write + recall) cited code. Claims without those three are out of this document.

## Provenance (mandatory)

| Field | Value |
|---|---|
| `pwd` | `/home/rswan/Documents/hermes-memory` |
| `git rev-parse HEAD` | `1d9aa0851f586bb8b4c0597869d7293ebe5157cf` (`1d9aa08` — `docs: add OC-MVP-3 as the pane meaning bar (#69)`) |
| Branch | `main` tracking `origin/main` |
| Editable install | **Not this tree.** `pip show hermes-memory` → `Editable project location: /home/rswan/hermes-memory` (stalled clone). Hermes writes still follow that install unless `PYTHONPATH`/`pip install -e` is retargeted at Documents. Same Postgres (`127.0.0.1:5450`). |

`--live` C–F on `scripts/replay_conversation_manifold.py` is on GitHub in the commit that lands this line. `.serena/` remains local-only.

Do not treat “the review was from Documents” as “runtime is Documents.”

---

## Two stores

Postgres 17 on loopback `:5450`:

1. **Relational + pgvector** (`public.*`) — turns, nouns, mention edges, file chunks, bridge rows.
2. **Apache AGE** graph `hermes_knowledge` — flower (`:Turn` / `:Session` / `NEXT` / `IN_SESSION`) and ingest (`:File` / `:Module` / `:Dependency` / `Imports`).

Conversation-meaning recall walks **SQL** `semantic_edge`, not AGE.

---

## Tables the hot paths actually use

| Table | Written by | Read by |
|---|---|---|
| `conversations` | `Store.insert_turn`, `Store.set_drain_status` | `Store.vector_search`, `conversations_by_ids`, `previous_conversation_id`, pane health, `schema_guard.UNPASSPORTED_TURNS_SQL` |
| `noun` | `Store._upsert_noun_on_conn` via `write_noun_passports` | `passports_for_conversations`, `expand_graph` JOINs, `fetch_noun_labels` |
| `semantic_edge` | `Store.upsert_mentions_chain` (`verb_type='mentions'` only) | `StoreExpandMixin.expand_graph` |
| `memory_chunk_nodes` | `write_noun_passports`; `bridge_turn` / ingest `_bridge` | `passports_for_conversations`, `count_unpassported_turns` |
| `doc_chunks` | `Ingestor._index_file` | `Store.vector_search` (ANN arm 2) |
| `memory_entries` | ingest `_index_file`; `Store.upsert_memory_entry` / `replace` / `remove` | `vector_search` (`file_path IS NULL` only); `librarian_health` |

DDL-only (confirmed unused by `src/`): `sessions`, `messages`, `librarian_chunks`, extractor-queue columns on `conversations`, most V1 AGE labels. Safe to prune (not ticketed here; do not mix with SQL `mentions`).

---

## Write path (`sync_turn` → `_awrite_turn`)

| Stage | Function | Database effect |
|---|---|---|
| A | `Embedder.embed_text` | None. Failure → LEDGER `EMBED_NULL`. |
| B | `Store.insert_turn` | `INSERT conversations` with `embedding`, `embed_model`, `embed_dim`. |
| B′ | `_stamp_drain` → `set_drain_status` | `UPDATE conversations.drain_status`. |
| C | `_link_turn_flower` → `merge_vertices_batched` / `merge_edges_batched` | AGE Turn/Session + NEXT/IN_SESSION. Failure → `graph_degraded`. |
| D | `extract_nouns` | Pure Python. |
| E | `Store.write_noun_passports` | `noun` + passport rows on `memory_chunk_nodes`. |
| F | `Store.upsert_mentions_chain` | Adjacent pairs only; `semantic_edge`. |

`insert_turn` stamps version columns:

```354:357:src/hermes_memory/store.py
                INSERT INTO conversations
                    (session_id, agent_identity, role, content, embedding, metadata,
                     embed_model, embed_dim)
```

V9 gap (`UNPASSPORTED_TURNS_SQL`) ≠ V11 `drain_status`. LEDGER is process RAM, not a table.

---

## Recall path (`prefetch` → `_aprefetch`)

1. `_enrich_query` (in-memory).
2. `embed_text(query[:800])`.
3. `Store.vector_search` k=12 — `memory_entries` (no `file_path`), `doc_chunks`, interactive `conversations`.
4. Drop `similarity < 0.55` and `SECRET_RE`.
5. `_expand_paths` → `passports_for_conversations` → `expand_graph` (SQL `mentions`, ≤2 hops).
6. `beam_score` then `_budget_seeds` then `format_injection`.

```76:91:src/hermes_memory/walk.py
def beam_score(...):
    c = float(src_align) * float(tgt_align)
    composite = (
        0.4 * float(sim)
        + 0.4 * c * float(prov_boost)
        + 0.2 * float(decay)
    )
    return c, composite, composite * (float(magnitude) / 8.0)
```

File seeds do not enter `expand_graph`.

---

## `trust_nomic_768` vs ingest (issue 1)

The four policy sites all say **legacy / pre-V10 / existing rows**:

- `schema_guard.LEGACY_NULL_EMBED_POLICY` — “Existing rows stay NULL until rewritten”
- `Store.vector_search` — “Legacy NULL”
- `README.md` — “Pre-V10 rows are left NULL on purpose”
- `AGENTS.md` — “Legacy NULL … unstamped rows”

None of them say ingest may keep minting new NULLs.

The spec says the opposite of ingest: “writes stamp them” (`HERMES_MEMORY_SPEC.md` §1) and the §2 NOW flowchart `INSERT conversations / memory_entries (stamped embed_model/embed_dim)`. That is true for `insert_turn` and `upsert_memory_entry`. It is false for `Ingestor._index_file`.

`test_insert_sql_names_embed_version_columns` only inspects `insert_turn` and `upsert_memory_entry`.

---

## Tickets from this map

Opened 2026-09-07 from this tree. Priority is data correctness, not pane polish or the golden set.

| # | Finding | Issue |
|---|---|---|
| 2 (loudest) | Dim mismatch on `semantic_edge` UPDATE `continue`s; no `GRAPH_DEGRADED` | [#70](https://github.com/rubyrayjuntos/hermes-memory/issues/70) |
| 1 | Ingest writes new NULL `embed_model`/`embed_dim`; policy was for leftover rows | [#71](https://github.com/rubyrayjuntos/hermes-memory/issues/71) |
| 3 | Adjacent-only mention chains are an RQ *construction* ceiling | [#72](https://github.com/rubyrayjuntos/hermes-memory/issues/72) |
| 5 | `hermes-memory-backfill` is ABOUT/Concept; live C–F is `replay_conversation_manifold.py` | [#73](https://github.com/rubyrayjuntos/hermes-memory/issues/73) |
