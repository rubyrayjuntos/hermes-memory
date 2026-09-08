# Schema / function map — 2026-09-07

This is the load-bearing inventory: table, function, and (for write + recall) cited code. Claims without those three are out of this document.

## Provenance (mandatory)

| Field | Value |
|---|---|
| `pwd` | `/home/rswan/Documents/hermes-memory` |
| `git rev-parse HEAD` | `8c3e0de1c360219c6d5d2f34349a1f44a047fdbe` (`8c3e0de` — `fix: installed plugin verifies schema from migration_history, not sql/`) |
| Branch | `main` tracking `origin/main` |
| Runtime install | **Not an editable clone.** `hermes-memory-install` pins `origin/main` via `git archive`, copies the package into `~/.hermes/plugins/hybrid-age` (not a symlink, no `.git`), `pip install`s that pin into site-packages, and stamps `~/.hermes/plugins/hybrid-age/.hermes-memory-version` (`sha=8c3e0de…`, `ref=origin/main`, `installed_at=2026-09-07T21:15:53Z`). The stalled clone at `/home/rswan/hermes-memory` is **gone**. |

`--live` C–F on `scripts/replay_conversation_manifold.py` is on `main` (`90f8a94`). `.serena/` remains local-only.

Do not treat “the review was from Documents” as “runtime is Documents.” Runtime after install is the pin + `:5452` / `hermes_memory_installed`.

---

## Two stores, two loopback DBs

Two Postgres 17 instances on loopback (do not collapse them):

| Stack | Port / DB | Who owns schema | Role tonight |
|---|---|---|---|
| Dev clone | `:5450` / `hermes_memory` | Compose first-boot `sql/init` (HEAD) + later `migrate.py` | Clone / historical 800+ turns. Documents has **no** `.env`; only `.env.example` names this DSN. |
| Installed pin | `:5452` / `hermes_memory_installed` | Empty volume + pinned `scripts/migrate.py`. **No** `sql/init` mount (`install_cli.write_installed_compose`). | Live Hermes + pane after `hermes-memory-install`. Pin `8c3e0de`. |

On each instance:

1. **Relational + pgvector** (`public.*`) — turns, nouns, mention edges, file chunks, bridge rows.
2. **Apache AGE** graph `hermes_knowledge` — flower (`:Turn` / `:Session` / `NEXT` / `IN_SESSION`) and ingest (`:File` / `:Module` / `:Dependency` / `Imports`).

Conversation-meaning recall walks **SQL** `semantic_edge`, not AGE.

### Installer schema check (landed after the first draft of this map)

The 1d9aa08 draft described an editable clone sharing `:5450`. That is stale.

`hermes-memory-install` does **not** copy `sql/` into `~/.hermes/plugins/hybrid-age`. The plugin tree is package code only. Schema apply is `migrate.py --dsn` against the installed DSN during install. After that, boot is `apply_pending_migrations` (no-op when `sql/migrations` is not next to the plugin) then `Store.require_schema_head()`.

Installed `require_schema_head` does **not** diff a copied `sql/` tree. It compares `CONSUMER_HEAD_VERSIONS` (`V1`, `V9`, `V10`, `V11`) to the live `migration_history` table (`schema_guard.expected_versions_for_head_check` / `store.py:288-312`). Clone/CI still compare on-disk `V*.sql` when that directory exists.

Merging first-boot `sql/init` (HEAD tables) with empty `migration_history` + V1–V11 is the I-INSTALL-1 foot-gun the installer now avoids on `:5452`.

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

DDL-only (reconfirmed unused by `src/` DML at `8c3e0de`; empty on `:5452` tonight — 0 rows each for `sessions`, `messages`, `librarian_chunks`): extractor-queue columns on `conversations` (`processed_at`, `relations_processed_at`, `processing_attempts`, `last_error` — V1 still creates them; no `src/` reader/writer), plus unused V1 AGE labels (`Person`, `Project`, `Technology`, `Organization`, `Domain`, `Skill`, `Tool`, `Repo`, `Standard`, AGE `Mentions` / `CoMentioned` / `SemanticallyRelated`). Live AGE labels stay: `Session`/`Turn`/`NEXT`/`IN_SESSION`, ingest `File`/`Module`/`Dependency`/`Imports`, leftover `Concept`/`ABOUT`.

**Dead-schema prune — deferred (2026-09-07, fourth confirmation).** Not executed. Why:

1. It is a real schema mutation (new `V*.sql` `DROP` / AGE label surgery), not a doc/ticket. Session rule: do not land that on `main` without a CI-gated PR.
2. V1 and `sql/init/02_schema.sql` still `CREATE` these objects. A drop that is not also removed from V1/`sql/init` (or isolated to a later idempotent migration) re-creates them on the next empty-volume install or first-boot compose.
3. AGE `Mentions` must not be mixed with SQL `semantic_edge.verb_type='mentions'`. A “prune unused labels” patch that touches the wrong `Mentions` is the expensive failure mode.
4. `Concept`/`ABOUT` look unused on the provider hot path but `hermes-memory-backfill-about` / `store_concepts` still write them — they are leftover, not dead.

Ticket the prune as its own PR when Documents `:5450` and installed `:5452` both have an explicit DSN story (#74 acceptance). Do not sneak it into installer or pane work.

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
| 5 | `hermes-memory-backfill-about` is ABOUT/Concept; live C–F is `replay_conversation_manifold.py` | [#73](https://github.com/rubyrayjuntos/hermes-memory/issues/73) |
