# Conversation Manifold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut over conversation ingest from AGE `Concept`/`ABOUT` to Postgres `noun` + passports + `mentions` edges, with one walker shared by prefetch and `/search`, and a Garden-only pane.

**Architecture:** AGE keeps the episodic flower (`Session`/`Turn`/`NEXT`). Semantic switches, passports, and bivalent poles live in Postgres. ANN entry is `conversations.embedding`. `store.expand_graph` becomes a `mentions` beam that returns the existing 7-tuple shape with noun payloads.

**Tech Stack:** Python 3.11+, asyncpg, Postgres 17 + AGE + pgvector, pytest + hypothesis, Ollama `nomic-embed-text` 768-d (fake embedder in tests).

**Spec:** `docs/superpowers/specs/2026-09-03-conversation-manifold-design.md`

## Global Constraints

- One semantic topology: do not MERGE conversation `Concept`/`ABOUT` after Task 5.
- `conversations.id` is `BIGSERIAL`/`BIGINT`. `turn_id`, `last_active_turn`, `provenance_turns` are `BIGINT` / `BIGINT[]`.
- Passport uniques: `(chunk_id, source, noun_id) WHERE noun_id IS NOT NULL` and `(chunk_id, source, vertex_id) WHERE noun_id IS NULL`.
- `chunk_id = conv_{turn_id}`, `source = 'conversation'` (literal).
- Verb on the chain is `mentions` (co-occurrence), not thematic ABOUT.
- `e_src_vec = embed(label)` or skip the edge; never turn-vec on the source pole.
- F skipped when turn embedding is NULL. D+E one txn; F a second.
- `magnitude = least(8, magnitude + conf)`; polarity `+1` only.
- `src_align = max(0, cos(q, e_src))` (same for tgt). `prov_boost` is `1.0` or `0.0`. Empty consensus → `decay = 1.0`.
- `score = composite * (magnitude / 8)`. Canvas ids: `age:`, `noun:`, `conv:`.
- No Concept backfill. V10 `IMPORTS` is not this plan.
- DB tests: `pytest --migrate` on `hermes_test` only. Never `hermes_memory`.
- Do not modify `.github/workflows/`, `.opencode/`, `opencode.json`, or `AGENTS.md`.

## File map

| File | Responsibility |
|---|---|
| `src/hermes_memory/extract_nouns.py` | Mention-order extractor (new). Provider imports it. |
| `src/hermes_memory/walk.py` | Pure pole clamp, `prov_boost`, decay, composite score (new). |
| `sql/migrations/V9__noun_passport_semantic_edge.sql` | Schema cutover. |
| `sql/init/02_schema.sql`, `sql/init/03_indexes.sql` | Fresh-volume mirror of V9. |
| `src/hermes_memory/store.py` | Noun/passport/edge writes; `expand_graph` beam. |
| `src/hermes_memory/provider.py` | Stages A–F; stop ABOUT; queue `previous_conversation_id`. |
| `src/hermes_memory/provider_helpers.py` | `format_triple` accepts noun records. |
| `src/hermes_memory/graph_api.py` | `/search` + catalog use walker + prefixes. |
| `docs/graph/fountain.html` | Garden-only; `mentions` caption; prefixed ids. |
| `README.md` | Copy matches cutover. |
| Tests in §10 of the spec | Extractor, walk, V9, write contract, P0, pack_search. |

Copy `CANONICAL_NAMES` from `legacy/scripts/graph_taxonomy.py` into `extract_nouns.py`. Do not import `legacy/`.

---

### Task 1: Noun extractor (no DB)

**Files:**
- Create: `src/hermes_memory/extract_nouns.py`
- Create: `tests/test_extract_nouns.py`
- Modify: `tests/test_extract_concepts.py` (re-export or delete Title-Case-only tests that contradict the spec)
- Delete or un-skip: `tests/property/test_p3_p4_phrase_extraction.py` (must not remain skipped as “v0.2 graph_extractor”)

**Interfaces:**
- Consumes: nothing from later tasks
- Produces: `extract_nouns(text: str, *, existing_labels: Sequence[str] = (), synthetic_session: bool = False) -> list[NounMention]` where `NounMention` is a dataclass `label: str, type: str | None, conf: float, mention_index: int`

- [ ] **Step 1: Write the failing tests**

```python
from hermes_memory.extract_nouns import extract_nouns

def test_pascal_and_aliases():
    labels = [n.label for n in extract_nouns(
        "RateLimiter talks to nomic-embed-text via hermes agent"
    )]
    assert labels == ["RateLimiter", "nomic-embed-text", "Hermes Agent"]

def test_first_mint_lowercase_multiword():
    labels = [n.label for n in extract_nouns(
        "the rate limiter talks to nomic-embed-text"
    )]
    assert "RateLimiter" not in labels
    assert "rate limiter" in labels
    assert "nomic-embed-text" in labels

def test_noise_empty():
    assert extract_nouns("Got it. Perfect.") == []

def test_quota_five():
    spans = " ".join(f"`token{i}`" for i in range(12))
    out = extract_nouns(spans)
    assert len(out) <= 5
    assert [n.mention_index for n in out] == sorted(n.mention_index for n in out)

def test_denylist():
    labels = {n.label.lower() for n in extract_nouns("see `id` and `conf` and RateLimiter")}
    assert "id" not in labels
    assert "conf" not in labels
    assert "RateLimiter" in {n.label for n in extract_nouns("see RateLimiter")}

def test_go_without_cue_dropped():
    assert all("go" != n.label.lower() for n in extract_nouns("please go ahead"))

def test_synthetics_on_synthetic_session():
    assert extract_nouns("Project Zephyr and Atlas Vault Engine", synthetic_session=True) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_extract_nouns.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement `extract_nouns.py`**

Include: `_slug`, seeded `CANONICAL_NAMES` (copy keys from `legacy/scripts/graph_taxonomy.py`), `SCHEMA_DENYLIST = frozenset({"id","src","tgt","conf","noun_id","chunk_id","session_id","turn_id","vertex_id","e_src_vec","e_tgt_vec","provenance_turns","magnitude","polarity","verb_type"})` compared slug/lowercase **exact**. Surfaces: backticks, identifier tokens, acronyms ≥2, 2–4 token multiword. Attach via slug + aliases + `existing_labels`. First-mint: collapse whitespace, do not Title-Case identifiers. `conf` table from spec §6.4. Floor 0.3. Quota 5 by `(-conf, mention_index)` then **re-sort kept rows by `mention_index`** for F chain order. `short_term_is_real` logic as in `legacy/scripts/graph_taxonomy.py`. No LLM. No cosine typo-fold.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_extract_nouns.py tests/test_extract_concepts.py tests/property/test_p3_p4_phrase_extraction.py -v`
Expected: PASS (or P3/P4 file deleted)

- [ ] **Step 5: Commit**

```bash
git add src/hermes_memory/extract_nouns.py tests/test_extract_nouns.py tests/test_extract_concepts.py tests/property/test_p3_p4_phrase_extraction.py
git commit -m "$(cat <<'EOF'
feat: mention-order noun extractor for conversation ingest

Replace Title-Case ABOUT harvesting with identifier, alias, and first-mint rules so hubs like RateLimiter land.
EOF
)"
```

---

### Task 2: Pure walker scoring

**Files:**
- Create: `src/hermes_memory/walk.py`
- Create: `tests/test_walk.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `clamp_cos(x: float) -> float` → `max(0.0, x)`
  - `provenance_boost(turn_id: int, provenance_turns: Sequence[int]) -> float` → `1.0` or `0.0`
  - `consensus_decay(*, empty_incident: bool, local_dir: float, query_dir: float, hop: int, lam: float = 0.05) -> float`
  - `beam_score(*, sim: float, src_align: float, tgt_align: float, prov_boost: float, decay: float, magnitude: float) -> tuple[float, float, float]` → `(c, composite, score)` with `c = src_align * tgt_align`, `score = composite * (magnitude / 8.0)`

- [ ] **Step 1: Write the failing tests**

```python
from hermes_memory.walk import beam_score, consensus_decay, provenance_boost, clamp_cos

def test_clamp_negative_pole():
    assert clamp_cos(-0.4) == 0.0
    assert clamp_cos(0.5) == 0.5

def test_prov_boost_binary():
    assert provenance_boost(10, [10, 11]) == 1.0
    assert provenance_boost(9, [10, 11]) == 0.0

def test_empty_incident_decay_is_one():
    assert consensus_decay(empty_incident=True, local_dir=0.0, query_dir=0.0, hop=1) == 1.0

def test_score_scales_by_magnitude_over_eight():
    c, comp, score = beam_score(
        sim=1.0, src_align=1.0, tgt_align=1.0, prov_boost=1.0, decay=1.0, magnitude=8.0
    )
    assert c == 1.0
    assert abs(score - comp) < 1e-9
    _, _, half = beam_score(
        sim=1.0, src_align=1.0, tgt_align=1.0, prov_boost=1.0, decay=1.0, magnitude=4.0
    )
    assert abs(half - 0.5 * score) < 1e-9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_walk.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement `walk.py` per spec §7.2**

Empty incident must **not** compute `local_dir * query_dir`. Non-empty: `exp(-lam * hop * (1.0 - local_dir * query_dir))` with `hop` in `{1, 2}`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_walk.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hermes_memory/walk.py tests/test_walk.py
git commit -m "$(cat <<'EOF'
feat: bounded mentions-beam score helpers

Clamp pole cosines, binary provenance boost, and decay=1 on empty consensus so the walker cannot invent 0.5.
EOF
)"
```

---

### Task 3: V9 schema + init mirrors

**Files:**
- Create: `sql/migrations/V9__noun_passport_semantic_edge.sql`
- Modify: `sql/init/02_schema.sql`
- Modify: `sql/init/03_indexes.sql`
- Modify: `tests/test_migrate_idempotent.py`

**Interfaces:**
- Consumes: live `conversations.id BIGSERIAL`
- Produces: `noun`, `semantic_edge`, altered `memory_chunk_nodes`, HNSW on `conversations.embedding` and `semantic_edge.e_src_vec`

- [ ] **Step 1: Write failing assertions in `test_migrate_idempotent.py`**

```python
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
        typ = await conn.fetchval(
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_name = 'conversations' AND column_name = 'id'
            """
        )
        assert typ == "bigint"
```

- [ ] **Step 2: Run to verify fail (V9 missing)**

Run: `pytest tests/test_migrate_idempotent.py::test_v9_noun_passport_indexes --migrate -v`
Expected: FAIL (relation noun does not exist) or assertion on indexes

- [ ] **Step 3: Write V9 and mirror into init**

`V9__noun_passport_semantic_edge.sql` must include (idempotent):

```sql
SET search_path = public;

CREATE TABLE IF NOT EXISTS noun (
    id SERIAL PRIMARY KEY,
    label TEXT NOT NULL UNIQUE,
    type TEXT,
    ag_vertex_id BIGINT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS semantic_edge (
    id SERIAL PRIMARY KEY,
    src_noun INT NOT NULL REFERENCES noun(id) ON DELETE CASCADE,
    tgt_noun INT NOT NULL REFERENCES noun(id) ON DELETE CASCADE,
    verb_type TEXT NOT NULL,
    e_src_vec vector(768) NOT NULL,
    e_tgt_vec vector(768) NOT NULL,
    magnitude FLOAT NOT NULL CHECK (magnitude > 0 AND magnitude <= 8),
    polarity SMALLINT NOT NULL DEFAULT 1 CHECK (polarity IN (-1, 1)),
    last_active_turn BIGINT,
    last_active_ts TIMESTAMPTZ,
    provenance_turns BIGINT[] NOT NULL DEFAULT '{}',
    UNIQUE (src_noun, tgt_noun, verb_type)
);

CREATE INDEX IF NOT EXISTS semantic_edge_src_vec_hnsw
    ON semantic_edge USING hnsw (e_src_vec vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

ALTER TABLE memory_chunk_nodes ADD COLUMN IF NOT EXISTS noun_id INT REFERENCES noun(id) ON DELETE CASCADE;
ALTER TABLE memory_chunk_nodes ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE memory_chunk_nodes ADD COLUMN IF NOT EXISTS turn_id BIGINT;
ALTER TABLE memory_chunk_nodes ADD COLUMN IF NOT EXISTS conf FLOAT CHECK (conf >= 0 AND conf <= 1);

ALTER TABLE memory_chunk_nodes DROP CONSTRAINT IF EXISTS memory_chunk_nodes_pkey;

CREATE UNIQUE INDEX IF NOT EXISTS memory_chunk_nodes_passport_uq
    ON memory_chunk_nodes (chunk_id, source, noun_id) WHERE noun_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS memory_chunk_nodes_flower_uq
    ON memory_chunk_nodes (chunk_id, source, vertex_id) WHERE noun_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_conversations_embedding
    ON conversations USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

Mirror the same objects in `02_schema.sql` / `03_indexes.sql` for fresh volumes. Do **not** add a generated midpoint column. Do **not** backfill AGE Concepts.

- [ ] **Step 4: Run migrate + tests**

Run: `python scripts/migrate.py --dsn "$HERMES_TEST_DSN"` then `pytest tests/test_migrate_idempotent.py --migrate -v`
Expected: V9 applied once; second run 0 applied; new test PASS

- [ ] **Step 5: Commit**

```bash
git add sql/migrations/V9__noun_passport_semantic_edge.sql sql/init/02_schema.sql sql/init/03_indexes.sql tests/test_migrate_idempotent.py
git commit -m "$(cat <<'EOF'
feat: V9 noun, passport partial uniques, and semantic_edge poles

Existing volumes and fresh init get the conversation manifold schema without a Concept backfill.
EOF
)"
```

---

### Task 4: Store writes (noun, passport, mentions)

**Files:**
- Modify: `src/hermes_memory/store.py`
- Create: `tests/integration/test_manifold_writes.py`

**Interfaces:**
- Consumes: V9 tables
- Produces:
  - `async def upsert_noun(self, label: str, type: str | None = None) -> int`
  - `async def write_passports(self, rows: list[dict]) -> None` — each dict: `chunk_id`, `source`, `noun_id`, `vertex_id`, `session_id`, `turn_id`, `conf`, `graph_name`. One txn. `ON CONFLICT` on passport unique.
  - `async def upsert_mentions_chain(self, pairs: list[tuple[int, int]], *, turn_id: int, turn_vec: list[float], src_vecs: dict[int, list[float]], confs: dict[tuple[int, int], float]) -> None` — skip pair if `src_vecs` missing that src; `magnitude = least(8, mag+conf)`; EMA only `e_tgt_vec`; `polarity=1`; `verb_type='mentions'`. Own txn.
  - `async def passports_for_conversations(self, conv_ids: Sequence[int]) -> list[dict]` — `chunk_id = conv_{id}`, `source='conversation'`, `noun_id IS NOT NULL`

Do **not** add a `bridge_turn` without `noun_id` for new conversation writes.

- [ ] **Step 1: Write failing integration tests** (`pytest.mark.store`, `--migrate`)

Insert two nouns, one turn id `1` (use a real `conversations` insert), `write_passports` two rows same `chunk_id` different `noun_id`. Assert two rows. Second `write_passports` updates `conf`. `upsert_mentions_chain` one pair; repeat with higher conf; assert `magnitude` capped at 8 after enough repeats; `e_src_vec` unchanged (compare first 4 floats).

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/integration/test_manifold_writes.py --migrate -v`
Expected: FAIL (`upsert_noun` missing)

- [ ] **Step 3: Implement store methods** using parameterized SQL (`$1`…), not string-concatenated identifiers for data. `vec_to_literal` from `embed.py` for vectors.

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_manifold_writes.py --migrate -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hermes_memory/store.py tests/integration/test_manifold_writes.py
git commit -m "$(cat <<'EOF'
feat: store upserts for nouns, passports, and mentions edges

Conversation turns can attach many nouns per chunk without colliding on the Turn vertex id.
EOF
)"
```

---

### Task 5: Provider A–F cutover

**Files:**
- Modify: `src/hermes_memory/provider.py` (`sync_turn`, `_awrite_item`, remove `_link_turn_concepts` ABOUT path, `ensure_*` labels)
- Modify: `tests/integration/test_turn_write_contract.py` (create)

**Interfaces:**
- Consumes: `extract_nouns`, `Store.upsert_noun`, `write_passports`, `upsert_mentions_chain`, `insert_turn`, AGE flower MERGE already in store
- Produces: drain stages A–F as spec §5. Queue item includes `previous_conversation_id`. `self._last_turn_id: dict[str, int]` updated after B. C reads **only** `item["previous_conversation_id"]` (no SQL lookup in C).

- [ ] **Step 1: Write failing tests** in `tests/integration/test_turn_write_contract.py`

Use a fake embedder: `async def embed_text(text)` returns a 768-d vector from `hash(text) % 97` scaled, or `None` when `text == "__fail__"`. Call the provider drain helper or `Store`+extracted stages through a small test seam `async def write_turn_item(provider, item)` if `_awrite_item` stays private — tests may call `_awrite_item` on a constructed provider with fake embedder and test pool.

Cases from spec §10.2: two identifiers mention-order; embed None → F skipped; empty extract → zero passports; no AGE `Concept` created (Cypher count ABOUT from this session’s turns stays 0). `chunk_id` starts with `conv_` and `source='conversation'`.

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/integration/test_turn_write_contract.py --migrate -v`
Expected: FAIL (ABOUT linker still writes Concepts, or new methods unused)

- [ ] **Step 3: Implement A–F**

In `_awrite_item` for `type=="turn"`:
1. A: `vec = await embedder.embed_text(content)`
2. B: `insert_turn(...)` — on failure return
3. Set `self._last_turn_id[session_id] = conv_id` **after** C uses previous
4. C: MERGE Session/Turn/`IN_SESSION`/`NEXT` using `item.get("previous_conversation_id")`. `ensure_*` only Session, Turn, NEXT, IN_SESSION
5. D+E: `extract_nouns`; `upsert_noun` each; `write_passports` in one txn. New writer must **not** insert `noun_id NULL` conversation rows
6. F: if `vec is None` skip; else embed each src label; skip edge if src embed fails; `upsert_mentions_chain`

`sync_turn`: when enqueueing, set `previous_conversation_id` to `self._last_turn_id.get(sid)` (may be None). Two items still two contracts.

Delete ABOUT scoring (`SNAP_COSINE` gating of concepts) from this path. Leave file ingest IMPORTS untouched.

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_turn_write_contract.py --migrate -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hermes_memory/provider.py tests/integration/test_turn_write_contract.py
git commit -m "$(cat <<'EOF'
feat: conversation drain writes nouns and mentions, not ABOUT

Turns always persist in conversations; the flower is best-effort; the manifold follows D+E then F.
EOF
)"
```

---

### Task 6: `expand_graph` mentions beam + prefetch

**Files:**
- Modify: `src/hermes_memory/store.py` (`expand_graph`)
- Modify: `src/hermes_memory/provider.py` (`_expand_paths`, `_aprefetch` stats)
- Modify: `tests/test_store.py` (AGE-hop tests that assume Concept/ABOUT — skip or retarget)
- Create: `tests/integration/test_expand_mentions.py`

**Interfaces:**
- Consumes: `WalkHypothesis` (define in `walk.py` or `store.py`): `noun_id: int`, `chunk_id: str`, `session_id: str`, `turn_id: int`, `sim: float`, `chunk_vec: list[float]`
- Produces: `expand_graph(self, hypotheses, *, q_vec: list[float], hops: int = 2, k: int = 8) -> list[tuple]` 7-tuples `(n, rel, m, w, c, decay, score)` where `n`/`m` are `{"id": str(noun.id), "name": label, "label": "Noun"}`. Optional hop appended by `/search`, not by prefetch. Child hypotheses copy parent `session_id`. File/`doc_chunk` seeds are never passed in.

Keep a thin wrapper if old tests pass `seed_vertex_ids: Sequence[int]`: empty list / unused AGE path must return `[]` and not Cypher-walk ABOUT.

- [ ] **Step 1: Write failing tests**

Two nouns, one `mentions` edge, one hypothesis on src, `q_vec` aligned with `e_src_vec`. Assert `rel == "mentions"`, `w == magnitude`, `c >= 0`, `score == (0.4*sim + 0.4*c*prov + 0.2*decay) * (magnitude/8)` within 1e-6. Hypothesis session B must not appear in returned audit sidecar if you attach `session_id` on a parallel dict — at minimum child tuples must not swap session. Empty hypotheses → `[]`.

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/integration/test_expand_mentions.py --migrate -v`
Expected: FAIL (old Cypher expand still returns AGE vertices)

- [ ] **Step 3: Implement beam in `expand_graph`**

SQL: outgoing edges `WHERE src_noun = $1 AND verb_type = 'mentions'`. Consensus: other `mentions` with `last_active_turn` in window `seed_turn_id - 32`. L2-normalize `e_tgt_vec` before mean. Use `walk.py`. Width: hop1 `limit=max(k*4,16)`; hop2 dest cap 32, `limit=max(k*2,8)`. Drop `score < 0.05`. Prefetch `_expand_paths`: for each conversation seed, `passports_for_conversations`, build hypotheses, call `expand_graph`. Stats line: SQL counts turns/nouns/mentions — **not** `a ABOUT`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_expand_mentions.py tests/test_store.py --migrate -v`
Expected: PASS (old expand tests updated to empty-int → `[]`)

- [ ] **Step 5: Commit**

```bash
git add src/hermes_memory/store.py src/hermes_memory/provider.py tests/integration/test_expand_mentions.py tests/test_store.py
git commit -m "$(cat <<'EOF'
feat: expand_graph walks mentions poles instead of Cypher ABOUT

Prefetch and the pane share one beam score; AGE is no longer the semantic hop engine.
EOF
)"
```

---

### Task 7: `/search` packing, catalog, prefixes

**Files:**
- Modify: `src/hermes_memory/graph_api.py` (`pack_search`, `_asearch`, `_agraph_3d` / catalog)
- Modify: `src/hermes_memory/provider_helpers.py` (`format_triple`)
- Modify: `tests/test_graph_api.py`

**Interfaces:**
- Consumes: 7-tuples with noun dicts; `passports_for_conversations`
- Produces: `pack_search` nodes/edges using **raw** `noun.id` strings internally; pane/catalog applies prefixes `noun:` / `age:` / `conv:` when emitting JSON `id` fields. `format_triple` if `n` is a dict with `name`/`label`, format `[Noun RateLimiter] -mentions-> [Noun pgvector]` without requiring `::vertex` JSON.

Catalog: latest 80 Turns from AGE + Sessions + NEXT/IN_SESSION; nouns from passports for those `turn_id`s; `mentions` among visible nouns. No Concept/ABOUT. IMPORTS not in default catalog.

- [ ] **Step 1: Write failing tests**

```python
def test_format_triple_noun_dict():
    n = {"id": "1", "name": "Postgres", "label": "Noun"}
    m = {"id": "2", "name": "AGE", "label": "Noun"}
    assert "mentions" in format_triple(n, "mentions", m)

def test_pack_search_prefixes():
    n = {"id": "1", "name": "Postgres", "label": "Noun"}
    m = {"id": "2", "name": "AGE", "label": "Noun"}
    out = pack_search("q", 8, 2, [], ["1"], [(n, "mentions", m, 1.0, 0.5, 1.0, 0.2, 1)])
    ids = {node["id"] for node in out["graph"]["nodes"]}
    # If pack_search itself prefixes:
    assert "noun:1" in ids or "1" in ids
```

If prefixes are applied only in the HTTP assembler, test that function instead — but **one** place must prefix so `noun:1 != age:1`. Pin that place in this task: **prefix in `pack_search` and catalog assembler** (`noun:{id}`, `age:{vid}`).

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_graph_api.py -v`
Expected: FAIL on noun dict parse

- [ ] **Step 3: Implement**

`_asearch`: embed → `vector_search` → conversation seeds only into hypotheses → `expand_graph` → `pack_search` with hop suffix 1 then 2. Non-conversation seeds listed but not walked. Catalog query: AGE Turns limit 80 plus SQL nouns/edges. No `/ghost` on pane load (leave endpoint).

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_graph_api.py tests/test_format_injection.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hermes_memory/graph_api.py src/hermes_memory/provider_helpers.py tests/test_graph_api.py
git commit -m "$(cat <<'EOF'
feat: search and catalog emit noun mentions with namespaced ids

Pane search uses the same beam as prefetch; AGE and noun ids cannot collide on the canvas.
EOF
)"
```

---

### Task 8: Fountain + README (Garden-only ship surface)

**Files:**
- Modify: `docs/graph/fountain.html`
- Modify: `README.md` (conversation / walk / diagram rows that still say ABOUT)

**Interfaces:**
- Consumes: prefixed ids and `mentions` edges from `/api/librarian/search` and catalog graph JSON
- Produces: default view with legend Session / Turn / Noun; caption **co-occurrence (mention order)**; no ABOUT/Concept; ghosts off on load; `3d.html` not advertised

- [ ] **Step 1: Write a failing characterization test** if one exists for copy; otherwise add `tests/property/test_edge_filter.py` updates: default visible labels include `mentions`; ABOUT is not required. Do not keep tests that treat ABOUT as the conversation semantic edge.

- [ ] **Step 2: Run to see current ABOUT assumptions fail or skip**

Run: `pytest tests/property/test_edge_filter.py -v`

- [ ] **Step 3: Edit fountain default state**

`AppState.level = 1` locked for ship (ignore stored L2–L4 or clamp). Do not fetch `/api/librarian/graph/ghost` on load. Legend: mentions ≠ thematic purple ABOUT. Click edge shows `w`, `c`, decay, score. Empty catalog copy: seed in chat.

README: replace ABOUT write-path with A–F; walk formula = spec composite × magnitude/8; pane URL unchanged; do not list `3d.html` as the inspector.

- [ ] **Step 4: Grep guard**

Run: `rg -n "ABOUT|Concept" README.md docs/graph/fountain.html` and fix remaining **ship** copy. Legacy `3d.html` may still mention ABOUT; do not advertise it.

- [ ] **Step 5: Commit**

```bash
git add docs/graph/fountain.html README.md tests/property/test_edge_filter.py
git commit -m "$(cat <<'EOF'
docs: Garden pane shows mention co-occurrence, not ABOUT

Ship copy matches the Postgres manifold so volume is visible without a second score.
EOF
)"
```

---

### Task 9: P0 passport isolation (property + migrate)

**Files:**
- Create: `tests/property/test_passport_isolation.py`

**Interfaces:**
- Consumes: Tasks 4–6 (write + walk)
- Produces: Hypothesis test that two text session ids sharing a canonical label never bleed passports

- [ ] **Step 1: Write the failing property test**

```python
from hypothesis import given, settings
from hypothesis import strategies as st

@given(st.sampled_from(["Postgres", "Hermes Agent"]))
@settings(max_examples=8, deadline=None)
@pytest.mark.asyncio
async def test_two_sessions_one_noun_no_bleed(label, db_pool, store, fake_embedder):
    # write two turns, sessions sess-a and sess-b, same label via extract or direct upsert
    ...
    assert await count_noun(store, label) == 1
    hyps_a = hypotheses_for_session("sess-a")
    rows = await store.expand_graph(hyps_a, q_vec=hyps_a[0].chunk_vec, hops=2, k=8)
    # every returned path sidecar session_id == "sess-a"
    # packed seeds for a query seeded on A excerpt do not include B chunk_id
```

Fill the body using `write_passports` / `upsert_mentions_chain` if full provider is heavy; still assert AGE NEXT for A excludes B turn vids.

- [ ] **Step 2: Run to verify fail** if isolation not wired

Run: `pytest tests/property/test_passport_isolation.py --migrate -v`
Expected: FAIL until child hypotheses keep parent `session_id`

- [ ] **Step 3: Fix walker if the test finds a swap** (do not “fix” by splitting nouns)

- [ ] **Step 4: Run full related suite**

Run: `pytest tests/test_extract_nouns.py tests/test_walk.py tests/integration/test_turn_write_contract.py tests/integration/test_expand_mentions.py tests/property/test_passport_isolation.py tests/test_migrate_idempotent.py --migrate -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/property/test_passport_isolation.py src/hermes_memory/store.py
git commit -m "$(cat <<'EOF'
test: two sessions share a noun and do not share passports

Isolation lives on session/turn passports; the walker must not steal the other chat's provenance.
EOF
)"
```

---

## Self-review (plan vs spec)

| Spec section | Task |
|---|---|
| §4 schema, BIGINT ids, partial uniques, no midpoint | 3 |
| §5 A–F, failure map, F skip on null embed, D+E then F | 5 |
| §6 extractor, quota, denylist, two examples | 1 |
| §7 walker, clamp, prov 0/1, empty decay 1.0, 7-tuple | 2, 6 |
| §8 pane prefixes, catalog vs search, Garden | 7, 8 |
| §9 V9, no backfill, IMPORTS not walked | 3, 5, 6 |
| §10 tests | 1–9 |
| V10 | **out of plan** |

No parallel score. No second `bridge` table. No `e_combined_vec`.
