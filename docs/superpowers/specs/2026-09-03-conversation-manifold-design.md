# Conversation manifold — design

**Status:** draft for review. Implementation is gated on approval of this file.

**Date:** 2026-09-03

**Repo:** `hermes-memory` (`hybrid-age` MemoryProvider). Postgres 17 + Apache AGE + pgvector. Embeddings: Ollama `nomic-embed-text`, 768-d. Graph: `hermes_knowledge`.

This document is the source of truth for Approach 3: a semantic manifold in Postgres (nouns, passports, bivalent `mentions` edges) plus an episodic flower in AGE. It supersedes Title-Case `Turn → ABOUT → Concept` as the conversation semantic layer. It does not replace native Hermes `MEMORY.md` / `USER.md`.

---

## 1. Purpose

Conversation ingest is the product vertical. A real chat must land, grow hubs as volume grows, and be inspectable.

After cutover:

1. Each non-noise turn inserts a `conversations` row (the turn).
2. AGE records session order (`Session` → `Turn` → `NEXT`).
3. Extracted labels become global `noun` rows and per-turn passports.
4. Co-occurrence in mention order writes/strengthens `semantic_edge` with verb `mentions`.
5. Search and prefetch walk that table with one score. Fountain draws that walk.

Success for this slice: a handful of real chats produce `conversations` rows, Turns on the flower, nouns that actually appeared in those chats, `/search` hits those turns, and `prefetch` is non-empty when asked about them.

---

## 2. Non-goals

- Dual semantic topology (AGE `Concept`/`ABOUT` **and** `noun`/`semantic_edge` for the same switches).
- A second table named `bridge`. Passports extend `memory_chunk_nodes`.
- Generated midpoint `(e_src_vec + e_tgt_vec) / 2`. Not a geodesic; not indexed.
- Searching edge poles instead of `conversations.embedding`. Poles steer the walk; ANN entry is the chunk.
- Chat rows in `memory_entries`.
- MERGE conversation `Concept` / `ABOUT` after cutover.
- Signed magnitude through `max(0, w − Δ)`. Polarity is a direction bit; on this path it stays `+1`.
- Fountain Garden L2–L4, ghosts, Cockpit, or `3d.html` as the launch pane.
- V10 file-noun projection (time-boxed; see §9.5). Not in this implementation.
- LLM extraction in the drain loop.
- Re-embedding historical turns or backfilling AGE Concepts into `noun`.

---

## 3. Architecture (source of truth)

Two stores. **One semantic topology.**

| Layer | Lives in | Job |
|---|---|---|
| Episodic flower | AGE only | This session, this turn, order in time (`Session`, `Turn`, `IN_SESSION`, `NEXT`) |
| Semantic manifold | Postgres: `noun`, passports on `memory_chunk_nodes`, `semantic_edge` | Shared switches, directed poles, volume |
| Vectors | `conversations.embedding` (768) | ANN seeds. No passport `chunk_vec_snapshot` on this path (`conversations` is insert-only) |

AGE `Concept` / `ABOUT` are **not** the noun. Conversation ingest must not write them. Poles as Cypher properties would put 768-d payloads into AGE and recreate the fight this design ends.

Optional later: project a noun into AGE for layout (`noun.ag_vertex_id`). One direction, layout-only, not walked.

**Isolation:** `noun.label` is globally unique. Competing sessions share nouns. They do not share passports. That is the property test (§10.1).

**One walker:** `store.expand_graph` may change engine (Cypher 2-hop → `semantic_edge` beam). Return shape stays the 7-tuple prefetch and Fountain already format. No parallel score in the pane.

**Writer:** conversation ingest (queued `sync_turn` drain). File ingest is a dated exception until V10 (§9.5).

---

## 4. Schema

Applied as `sql/migrations/V9__noun_passport_semantic_edge.sql` (idempotent, `scripts/migrate.py` advisory lock + checksum). The same DDL is mirrored in `sql/init/02_schema.sql` and `sql/init/03_indexes.sql` so a new volume does not wait on V9.

**Turn id type (once):** live `conversations.id` is `BIGSERIAL` (`sql/init/02_schema.sql`). That is `BIGINT`, not UUID. V9 `turn_id`, `last_active_turn`, and `provenance_turns` are `BIGINT` / `BIGINT[]` to match. Do not introduce UUID here.

Do not use `hermes-memory-migrate` (DSN copy) for this change. Do not `create_elabel('Mentions')` on AGE for the walk. The verb lives only on `semantic_edge.verb_type`.

### 4.1 `noun`

| Column | Type | Notes |
|---|---|---|
| `id` | `SERIAL PRIMARY KEY` | Stable string id in the pane: `noun:{id}` |
| `label` | `TEXT NOT NULL UNIQUE` | Canonical display; extractor emits this string |
| `type` | `TEXT` | Optional catalog (`tool`, `technology`, `project`, …). Never AGE `Concept` |
| `ag_vertex_id` | `BIGINT UNIQUE` | Nullable. Unused this release |
| `created_at` | `TIMESTAMPTZ` | Default `now()` |

Index: `label` unique is sufficient for attach-by-slug after the extractor canonicalizes.

### 4.2 `semantic_edge`

| Column | Type | Notes |
|---|---|---|
| `id` | `SERIAL PRIMARY KEY` | Row identity; not used in the walk |
| `src_noun`, `tgt_noun` | `INT REFERENCES noun(id) ON DELETE CASCADE` | |
| `verb_type` | `TEXT NOT NULL` | v1: `mentions` only |
| `e_src_vec` | `vector(768) NOT NULL` | Source pole. Insert = `embed(src label)` or **skip the edge** |
| `e_tgt_vec` | `vector(768) NOT NULL` | Observation pole. Insert = turn vec; strengthen = EMA toward turn vec |
| `magnitude` | `FLOAT` | `CHECK (magnitude > 0 AND magnitude <= 8)` |
| `polarity` | `SMALLINT NOT NULL DEFAULT 1` | `CHECK (polarity IN (-1, 1))`. This path writes `+1` only |
| `last_active_turn` | `BIGINT` | `conversations.id` |
| `last_active_ts` | `TIMESTAMPTZ` | |
| `provenance_turns` | `BIGINT[] NOT NULL DEFAULT '{}'` | Turn ids, not session ids. Unique, cap 32 |

Constraints:

- `UNIQUE (src_noun, tgt_noun, verb_type)`
- **No** generated `(e_src_vec + e_tgt_vec) / 2` column
- One HNSW: `e_src_vec vector_cosine_ops` (“what does X do?”). Target-pole index is not v1

One turn never writes both directions of the same pair.

### 4.3 `memory_chunk_nodes` (passports)

Do not add a second bridge table.

**Add columns:** `noun_id INT REFERENCES noun(id) ON DELETE CASCADE`, `session_id TEXT`, `turn_id BIGINT`, `conf FLOAT CHECK (conf >= 0 AND conf <= 1)`. Keep existing `graph_name` (default `hermes_knowledge`); conversation passports write it.

**Drop** `PRIMARY KEY (chunk_id, source, vertex_id)`. The table has no surrogate PK; the two partial uniques below are identity.

**Replace with two partial unique indexes** (Postgres treats NULLs as distinct; a single PK that includes `noun_id` cannot represent flower-only rows):

```text
UNIQUE (chunk_id, source, noun_id)    WHERE noun_id IS NOT NULL   -- passports
UNIQUE (chunk_id, source, vertex_id)  WHERE noun_id IS NULL       -- flower hook only
```

Column meanings:

| Column | Role |
|---|---|
| `chunk_id` + `source` | Which embedded span. Conversations: `chunk_id = conv_{conversations.id}`, `source = 'conversation'` (literal) |
| `vertex_id` | Episodic AGE hook (Turn, or Session). Nullable if a flower MERGE fails. **Not** a second noun id |
| `noun_id` | Semantic hook. NULL only on legacy/flower-only rows |
| `session_id` | Hermes **text** session id. Never `BIGINT` |
| `turn_id` | `conversations.id` |
| `conf` | Extractor confidence for this mention |

No `chunk_vec_snapshot`. The walker joins `conversations.embedding` via `turn_id`.

Prefetch conversation keys are **only** `conv_{id}`. After cutover, do not resolve conversation seeds as raw `id::text`.

Existing rows keep `vertex_id` and `noun_id NULL` until a new turn writes passports. That is intended.

**New writer never inserts `noun_id NULL` conversation rows.** Flower-only uniques exist so legacy `bridge_turn` rows and a brief mixed-version window do not collide. After cutover: zero nouns → AGE Turn only, no `memory_chunk_nodes` row; one or more nouns → passport rows with `noun_id` and `vertex_id` (nullable). Do not write both a flower-only row and passports for the same `conv_{id}`.

### 4.4 Other indexes

`CREATE INDEX IF NOT EXISTS` HNSW on `conversations.embedding` if missing (ANN entry).

---

## 5. Turn write contract

**Input:** one queued item `{type: "turn", session_id: TEXT, role, content, previous_conversation_id}` after noise-skip. `sync_turn` emits **two** items (user, assistant) → two contracts → two `NEXT`s. Secrets stripped before embed. Content capped at 8000 chars.

**Invariant:** the `conversations` row is the turn. Everything else attaches to `conversations.id`. Drain never raises.

### 5.1 Stages (order)

**A. Embed (best-effort)**  
768-d `nomic-embed-text`. Failure → `embedding = NULL`. The turn still inserts. It is not a search seed until a later re-embed (out of this slice).

**B. Insert `conversations` (must succeed)**  
`session_id`, `agent_identity`, `role`, `content`, `embedding`, `metadata.kind` from `classify_session_kind`. Returns `turn_id = conversations.id`.  
If this `INSERT` fails, the queue item is done. Do not retry AGE or manifold. Do not poison the drain.

**C. Episodic flower (best-effort, SAVEPOINT per Cypher)**  
`MERGE` `Session {name: session_id}`, `MERGE` `Turn {name: turn_{id}}` with `session_id`, `turn_id`, content excerpt, `created_at`. `IN_SESSION`. `NEXT` from **`previous_conversation_id` on the queue item** (no hidden “last turn in session” lookup convention).  
Capture `vertex_id = id(Turn)` (NULL on failure).  
Failure here does **not** roll back **B**. Passports get `vertex_id` NULL. `NEXT` can be repaired; the SQL row stands.

`ensure_*` for labels keeps **Session / Turn / NEXT / IN_SESSION** only. Conversation ingest must not `create_vlabel` `Concept` or `create_elabel` `ABOUT`.

**D. Nouns (best-effort)**  
Extract ≤5 `(label, type?, conf)` per §6. Drop `conf < 0.3`. Dedup by slug. Empty list is success: flower may exist, manifold does not grow.  
For each remaining label: `INSERT … ON CONFLICT (label)` on `noun` (do not split a global switch). Keep `noun.id`.

**E. Passports (best-effort, same transaction as D)**  
One row per noun. `ON CONFLICT` on the passport unique `(chunk_id, source, noun_id)`: update `conf`, set `vertex_id` if newly non-null, `session_id`.

Old `bridge_turn` (one row, no `noun_id`) is not used on the conversation path after cutover. Flower-only `noun_id IS NULL` rows may remain from before cutover.

**F. `semantic_edge` (second best-effort transaction)**  
Verb `mentions` — co-occurrence spine, **not** thematic ABOUT. Mentions in order: directed **chain** `n1 → n2 → …` (≤4 edges for 5 nouns). Not a clique.

- **Insert** if `(src_noun, tgt_noun, verb_type)` missing: `e_src_vec = embed(src label)`; if that embed fails, **skip the edge** (do not substitute the turn vector). `e_tgt_vec = turn vec`. If the turn embedding is **NULL**, skip **all** of F (both poles require real 768-d vectors). `magnitude = conf`. `polarity = +1`. `last_active_turn = turn_id`. `provenance_turns = ARRAY[turn_id]`.
- **Strengthen** on conflict: `magnitude := least(8, magnitude + conf)`. `last_active_turn` / `last_active_ts`. Append `turn_id` to `provenance_turns` (unique, cap 32). **EMA only `e_tgt_vec`** toward this turn vec (`α = conf` clipped to `[0.05, 0.4]`). Leave `e_src_vec` stable. Skip strengthen when turn embedding is NULL.

Noun without passport is forbidden (D+E share a txn). Edge without passport is a hole; missing F after a good D+E is acceptable (repair on next co-occurrence).

### 5.2 Failure map

| Failure | Turn exists? | Search seed? | Flower / DISSOLVE? | Walk grows? |
|---|---|---|---|---|
| Embed down | yes | no | if C ok | D+E may run; **F skipped** (no turn vec for `e_tgt_vec`) |
| **B** insert | **no** | no | no | no |
| C AGE | yes | if embedding | no / partial | passports with `vertex_id` NULL |
| D–E manifold | yes | if embedding | if C ok | no |
| F only | yes | if embedding | if C ok | passports yes; edges on next co-occurrence |

AGE stays SAVEPOINT-isolated. Manifold SQL runs after **B** commits. D+E one txn; F a second.

---

## 6. Noun extraction

**Job:** from one message, produce ≤5 `(label, type?, conf)` in **mention order** for stage D. Empty list is valid.

Not this section: minting thematic `ABOUT` / `USES` edges (later verb pass). Cosine to the turn does not decide whether a noun exists. `SNAP_COSINE` and `_ABOUT_PATTERN` retire on the conversation path.

### 6.1 Candidate surfaces (union, mention order)

1. Backticks / inline code
2. Identifier tokens: CamelCase / PascalCase, `snake_case`, `kebab-case`, dotted names, `Name123`
3. Acronyms length ≥ 2 (ambiguous shorts still need the §6.3 gate)
4. Multiword names: two to four tokens, not all stopwords. Message casing does not matter; slug-snap does

Title Case is one source, not the only source.

**One token is allowed** when it is an identifier shape (CamelCase, hyphen, underscore, dot, digit) or a gated acronym. The old “no single-word concepts” rule is what killed `RateLimiter` / `pgvector`.

### 6.2 Attach vs mint

1. Normalize surface → slug. Apply seeded **`CANONICAL_NAMES`** (existing alias table: `postgresql` → `Postgres`, `pgvector` → `pgvector`, `hermes agent` → `Hermes Agent`, …). That string is `noun.label`.
2. **First-mint casing:** if no alias and no existing slug, persist the first-seen surface after whitespace collapse, preserving identifier shape (do not Title-Case `nomic-embed-text` into `Nomic Embed Text`).
3. If slug matches an existing `noun`, **attach**. Do not mint a spelling variant.
4. Else mint if the candidate passed identifier / multiword / backtick rules.
5. Typo-fold cosine onto existing labels is **not required to ship**. If omitted, attach is slug + `CANONICAL_NAMES` only.

### 6.3 Must not mint

- Noise already dropped by `_is_noise`
- All-stopword phrases
- Bare English one-words (`decide`, `pulling`, `memory` as a common noun)
- Ambiguous shorts (`go`, `git`, `ai`, …) unless `short_term_is_real` (uppercase acronym in text, or a tech cue in the same sentence)
- Verify synthetics (`Project Zephyr`, `Atlas Vault Engine`) on synthetic sessions
- Strings longer than 45 chars, or more than four tokens
- **Schema-token denylist:** structural names that are table/column noise, including `id`, `src`, `tgt`, `conf`, and the same class of tokens (`noun_id`, `chunk_id`, `session_id`, `turn_id`, `vertex_id`, `e_src_vec`, `e_tgt_vec`, `provenance_turns`, `magnitude`, `polarity`, `verb_type`). Comparison is slug/lowercase exact, not substring (so `session` as English is not banned; `session_id` is)

### 6.4 Confidence

| Evidence | `conf` |
|---|---|
| Backtick / code span | 0.90 |
| Alias or exact existing slug | 0.85 |
| Identifier shape | 0.75 |
| Multiword name (any case) | 0.70 |
| Gated acronym / short term | 0.60 |

Floor 0.3 (write contract). Same slug, several rules: **max** conf, keep first mention index.

### 6.5 Quota

After filters, keep at most **5**, ranked `(-conf, mention_index)` (higher conf first; ties keep earlier mention). This is the code-paste spam guard. Chain order for F is mention order among the **kept** five, not among dropped extras.

### 6.6 Runtime

Pure Python + regex in the drain. No LLM. Alias table is data, not Cypher.

**Examples:**

- `"RateLimiter talks to nomic-embed-text via hermes agent"` → `RateLimiter`, `nomic-embed-text`, `Hermes Agent` (alias), mention order.
- `"the rate limiter talks to nomic-embed-text"` → multiword first-mint `rate limiter` (not rewritten to PascalCase) plus `nomic-embed-text`. Add a `CANONICAL_NAMES` alias if those must collapse.
- `"Got it. Perfect."` → `[]`.

---

## 7. Walker

**Job:** ANN on `conversations.embedding` → passports → `mentions` beam → one 7-tuple. Fountain only draws it. Prefetch `_expand_paths` and `GET /api/librarian/search` call the same `expand_graph`.

### 7.1 Entry

- Query `q`. Embed. Fail → empty walk (search: existing Ollama hint).
- ANN over `conversations.embedding`, existing `min_similarity` (0.72) and `k`. Interactive kinds only (same filter as today’s `vector_search` conversation arm).
- Join `conversations.id` → `memory_chunk_nodes` where `chunk_id = conv_{id}` AND `source = 'conversation'` AND `noun_id IS NOT NULL`.
- **One conversation hit → one hypothesis per passport**, not one `noun_id` per row.

```text
h = { noun_id, chunk_id: conv_{id}, session_id, turn_id,
      sim: cos(q, conv.embedding), chunk_vec: conv.embedding }
```

Missing passports: seed still injects (vector hit); beam is empty. Allowed.

File / `doc_chunk` / `memory_entry` ANN hits may still appear in `vector_search` union. They **do not** enter the `mentions` beam until V10.

### 7.2 Beam

Max hops = 2. Drop hop if scaled `score` < 0.05. Width matches today’s search packing: hop 1 `limit = max(k * 4, 16)`; hop 2 destinations capped at 32, `limit = max(k * 2, 8)`; prefetch keeps at most 16 paths.

For each active hypothesis, outgoing `semantic_edge` where `src_noun = h.noun_id` and `verb_type = 'mentions'`.

**Poles only** (no `e_combined_vec`):

| Term | Vector |
|---|---|
| `src_align` | `max(0, cos(q, e.e_src_vec))` |
| `tgt_align` | `max(0, cos(q, e.e_tgt_vec))` |
| Consensus | incident **`e_tgt_vec`**, L2-normalized **before** the mean |

If `e_src_vec` is missing, the write contract skipped the edge; the walker never sees it.

**Provenance:** `prov_boost = 1.0` iff **`h.turn_id` ∈ `e.provenance_turns`**, else **`0.0`** (middle term drops). Never `h.session_id ∈ provenance_turns`. Do not use a soft `0.5`.

**Child hypotheses** keep the **parent passport’s `session_id`**. Do not swap in another session’s passport for the same noun.

**Consensus window:** incident `mentions` edges with `last_active_turn` within `max(turn_id) − 32` of the seed turn (or 32 globally if unknown). `λ = 0.05`. `Δ` = hop index (1, then 2).

Empty incident set → **`decay = 1.0`** (skip the consensus dots). Non-empty:

```text
local_dir = cos(e.e_tgt_vec, consensus)    # both unit vectors; result is a scalar
query_dir = cos(e.e_tgt_vec, q)
decay     = exp(−λ · Δ · (1.0 − local_dir * query_dir))
```

**Score** (bounded band; polarity is not a multiplier):

```text
composite = 0.4 * h.sim + 0.4 * (src_align * tgt_align) * prov_boost + 0.2 * decay
score     = composite * (e.magnitude / 8)
```

No secondary or parallel score. This score governs beam selection and is the tuple `score`.

### 7.3 7-tuple

Shape: `(n, rel, m, w, c, decay, score)`. Optional 8th element remains hop for `/search` packing.

`n` and `m` are **noun records** `{id, name: label, label: "Noun"}` with **stable string `id` = `noun.id`** (not AGE agtype). Adapters in `pack_search` / `format_triple` / `parse_agtype_vertex` consumers must accept this payload. Canvas prefixes are applied in the pane layer (§8.3), not inside the tuple id.

| Slot | Meaning |
|---|---|
| `rel` | `mentions` |
| `w` | `e.magnitude` only (not `× polarity`) |
| `c` | `src_align * tgt_align` after the `max(0, ·)` clamp (steering strength in `[0, 1]`), **not** `h.sim` |
| `decay` | neighbor-modulated factor in `(0, 1]` |
| `score` | beam composite above |

`h.sim` stays on the **seed**, not in `c`. Audit ids (`turn_id`, `session_id`, `chunk_id`) travel **beside** the tuple. Episodic citation is AGE `NEXT` from that `turn_id`; it is not the walk.

---

## 8. Ship surface (viz + `/search`)

Advertised URL unchanged: `http://127.0.0.1:7890/api/librarian/pane` → `docs/graph/fountain.html`.

Default is **one view** (today’s Garden, always on). No L2–L4 as the ship default. No ghost fetch on load. No `ABOUT` / `Concept` in the legend. Do not color `mentions` as thematic. Caption: **co-occurrence (mention order)**.

`docs/graph/3d.html` is not the launch pane. Leave the file; stop pointing README at it.

### 8.1 Catalog vs search

| Mode | Trigger | Nodes | Edges |
|---|---|---|---|
| Catalog | pane load / reload | Noun | every `mentions` edge (`pack_search`, no query) |
| Search | `GET /api/librarian/search?q=&k=8&hops=2` | Noun; seeds marked | the same `pack_search` graph, walker-scoped to the query |

Catalog is the unscoped manifold. Search is the same view, focused by ANN → passports → 1–2 hop beam. Session and turn stay on noun `props`. `IMPORTS` off by default. AGE `Session` / `Turn` / `IN_SESSION` / `NEXT` are not catalog gems.

### 8.2 `/search` wiring

Same engine as prefetch:

1. Embed `q`
2. ANN (conversation arm for the beam)
3. Fan-out passports → hypotheses
4. Beam hops 1–2
5. `pack_search`: `seeds`, `graph.nodes`, `graph.edges`, `paths[]` with `weight` / `cosine` / `decay` / `score` / `hop` plus `session_id` / `turn_id`. That JSON is **prompt.debug** (Fountain `reconstructInjection`). It is not the prefetch string.

Seed conversations are list rows (excerpt, `sim`, `chunk_id = conv_{id}`), not AGE vertices. Highlight uses seed **noun** ids from passports.

If pane search and prefetch selected **different turns**, that is a bug. The pane dump **must** keep scores and Cypher; prefetch **must not**.

### 8.2.1 Injection surfaces

| Artifact | Function | Contents |
|---|---|---|
| `prompt.rendered` | `format_injection` | `<PAST_CONTEXT>` dated turn bodies. Qualitative `high relevance` / `related` only. |
| `prompt.debug` | Fountain `reconstructInjection` / `format_debug_injection` | `[SEED Vn]`, `[Path: A -REL-> B]`, `w` / `c` / `decay` / `score`, persisted counts |

Ranking is ``beam_score`` only (hop keep/drop and injection ``best_score``). ANN ``1 - (embedding <=> q)`` is candidate generation, not a second weighted sum. Filter: top N, dedupe `turn_id`, max 2 per session. Hops resolve extra **turn bodies** via `provenance_turns`; inject that text. Verbalize `You previously linked X to Y` only when a hop has no turn text. Never inject `w=`, `c=`, `decay=`, `%`, the formula, noun-id chains, or `persisted N turns`.

### 8.3 ID prefixes (canvas)

AGE ids and `noun.id` collide as raw decimals. Force-layout ids:

- `age:{vertex_id}` — Session / Turn
- `noun:{noun.id}` — switches
- `conv:{conversations.id}` — search seed rows in the side list (not a third vertex kind unless the user opens that turn)

Passport `vertex_id` places a Noun next to its Turn. It is not a noun id.

### 8.4 Click / empty / stats

- Turn → snippet + session
- Noun → label, type, recent passports
- `mentions` edge → `w` (magnitude), `c` (pole alignment), decay, score — not “ABOUT”
- Empty catalog → seed in chat
- Search, no seeds → keep catalog dimmed; list says no hits

Stats line: **turns / nouns / mentions** (SQL). Do not inject `a ABOUT`. Live AGE counts, if shown, are flower-only.

Ghosts, Cockpit, DISSOLVE as power tools: not default; not required to ship this slice.

CORS / loopback bind unchanged.

---

## 9. Cutover

One idempotent migration plus one code cut. Old DBs boot the new writer. No dual semantic topology after the new process is up.

### 9.1 Data: do not backfill Concepts

Live `Concept` / `ABOUT` are Title-Case residue. **Do not** copy them into `noun` / `semantic_edge`. Existing `conv_*` rows stay flower-only until the next real turn runs D–F. Volume starts at cutover.

Do not `DETACH DELETE` AGE Concepts in V9. Pane and walker do not read that label.

### 9.2 Code cut (same PR as V9 apply)

| Stop | Start |
|---|---|
| `_link_turn_concepts` MERGE `Concept` / `ABOUT` | Stages A–F; verb `mentions` |
| `bridge_turn` without `noun_id` on the conversation path | Passport INSERT in the D+E txn |
| Prefetch/search Cypher 2-hop expand | Beam on `semantic_edge`; 7-tuple as §7.3 |
| Stats `a ABOUT` | turns / nouns / mentions |
| Queue item without prior id | `previous_conversation_id` for `NEXT` |

File ingest: still `MERGE` File / Module / `IMPORTS` in AGE this PR. Catalog default and the walker do not traverse `IMPORTS`.

### 9.3 Rollout order

1. Apply V9 (idempotent). Old process can still insert flower-only rows (`noun_id NULL`).
2. Deploy provider + store + pane that write/read the manifold.
3. Confirm: one chat → `conversations` row + Turn + ≥0 passports; `/search` and prefetch share the walker.
4. Do not run both old and new writers against one DB after step 2.

V9 must not require a data backfill to succeed. Failure map unchanged: **B** is the only hard fail.

### 9.4 Fresh volumes

`sql/init/02_schema.sql` + `03_indexes.sql` match V9. New compose does not depend on migrate.py for nouns/passports/edges.

### 9.5 `IMPORTS` time-box (V10)

Until **V10**, AGE `IMPORTS` is layout-only if someone opens a File filter; not in catalog default; not in prefetch/search beam.

**V10** is the next schema PR after the conversation path is green, **same release train — do not slip past the following numbered migration**. Project File/Module/Dependency names into `noun` + passports (`source` ≠ `conversation`) + `semantic_edge` (`verb_type = 'imports'`). Then ingest stops treating AGE as the semantic home for files.

If V10 is not opened in that slot, ingest must stop writing walkable file semantics rather than grow a second topology. No silent “forever IMPORTS-on-AGE.”

V10 is **not** this implementation.

---

## 10. Tests

Unit and Hypothesis tests always. DB tests: `pytest --migrate` on **`hermes_test` only**. Fake embedder (deterministic 768-d unit vectors) for manifold tests; no Ollama in property tests. Do not touch live `hermes_memory`.

### 10.1 P0 — Two sessions, same noun, no passport bleed

`tests/property/test_passport_isolation.py`.

Sessions `sess-a`, `sess-b` (text ids). Both extract the same canonical label. Writer runs A–F.

Invariants:

1. Exactly one `noun` row for that label
2. Two passports, distinct `session_id` and `turn_id`
3. Beam from `session_id = sess-a`: child hypotheses keep `sess-a`; `prov_boost = 1.0` iff `h.turn_id ∈ provenance_turns` else `0.0`; audit `turn_id`s ⊂ A’s turns; packed seed excerpts are A’s `conv_*`
4. AGE `NEXT` chain for A does not include B’s Turns

Hypothesis: two distinct session strings, 1–5 shared labels, 1–3 turns each.

### 10.2 Write path

`tests/integration/test_turn_write_contract.py`:

| Case | Expect |
|---|---|
| Two identifiers, mention order | Row; Turn + `NEXT` if `previous_conversation_id` set; D+E one txn; F chain `mentions`; `e_src_vec` = embed(label); magnitude ≤ 8 |
| Embedder returns None | Row, `embedding` NULL; **B** succeeded; **F skipped** |
| AGE MERGE fails | Row remains; passports `vertex_id` NULL |
| Src-label embed fails | Skip that edge; passports remain |
| Repeat same pair | `least(8, mag+conf)`; `e_src_vec` unchanged; `e_tgt_vec` EMA; provenance unique, cap 32 |
| Empty extract | Row + flower; zero new nouns/passports/edges |
| User + assistant | Two contracts, two `NEXT`s |

Assert no new AGE `Concept` / `ABOUT`. `chunk_id` only `conv_{turn_id}`; `source` literal `conversation`.

### 10.3 Extraction (no DB)

Successor to `tests/test_extract_concepts.py`. Keep the two §6 examples **distinct** (do not require PascalCase unless the fixture string is PascalCase or the alias is seeded):

- `"RateLimiter talks to nomic-embed-text via hermes agent"` → `RateLimiter`, `nomic-embed-text`, `Hermes Agent`
- `"the rate limiter talks to nomic-embed-text"` → `rate limiter`, `nomic-embed-text` (not rewritten to `RateLimiter`)
- noise → `[]`; quota 5 by `(-conf, mention_index)`; denylist; `CANONICAL_NAMES`; `go` without cue dropped; synthetics on synthetic session dropped

Un-skip or delete `tests/property/test_p3_p4_phrase_extraction.py`. Do not leave “v0.2 graph_extractor” as the skip reason.

### 10.4 Walker + pane

- 7-tuple field meanings as §7.3. No `e_combined_vec` in SQL or Python
- Hops ≤ 2; file/`doc_chunk` seeds absent from the beam
- `pack_search`: prefixes `age:`, `noun:`, `conv:`; one `score`; prefetch and `/search` same `expand_graph`
- Catalog JSON: no `Concept`/`ABOUT`; `mentions` not labeled thematic; `noun:1` ≠ `age:1`

### 10.5 V9 / init

Extend `tests/test_migrate_idempotent.py`: second migrate → 0 applied; both partial uniques exist; `noun` / `semantic_edge` present; **no** generated midpoint column. Init mirror: `02_schema.sql` defines the same constraints.

### 10.6 Out of this suite

Fountain L2–L4, ghosts, real Ollama in property tests, live prod DB, importing ABOUT into nouns, V10 `IMPORTS` projection.

CI: unit + property (no DB) on every PR; `--migrate` on the job that already runs store tests.

---

## 11. Amendment log (locked)

These are closed. Do not reopen in implementation without a spec revision.

**Architecture**

- Two stores; no dual *semantic* topology
- Passport uniqueness includes `noun_id` (realized as partial uniques in V9)
- `vertex_id` is the Turn hook, not a noun id
- `expand_graph` engine may change; 7-tuple contract stays
- `IMPORTS` on AGE is dated (V10)

**Write contract**

- Verb on the chain is `mentions` (co-occurrence), not thematic `ABOUT`
- `e_src_vec = embed(label)` or skip the edge; never turn-vec on the source pole
- `magnitude = least(8, magnitude + conf)`; polarity `+1` only
- D+E one txn; F a second; `previous_conversation_id` on the queue item
- `chunk_id = conv_{turn_id}`; `source = 'conversation'`
- F skipped when turn embedding is NULL (`e_tgt_vec` is NOT NULL)
- New writer never inserts `noun_id NULL` conversation rows (legacy flower-only only)

**Extraction**

- First-mint casing + seeded `CANONICAL_NAMES`
- Quota `(-conf, mention_index)`
- Schema-token denylist

**Walker**

- One hypothesis per passport (`memory_chunk_nodes`, not a `bridge` table)
- `e_src_vec` / `e_tgt_vec` only; no midpoint column
- Provenance is `turn_id`
- Noun label payloads in the 7-tuple; `w = magnitude`; `c = src_align * tgt_align` after `max(0, cos)`; `score = composite * (magnitude / 8)`
- Consensus: 32-turn window, `Δ` = hop index, `λ = 0.05`; empty incident → `decay = 1.0`
- `prov_boost` is `1.0` or `0.0`
- `conversations.id` / `turn_id` / `provenance_turns` are `BIGINT`

**Pane**

- Prefixes `age:`, `noun:`, `conv:`
- Single walker shared with prefetch
- Catalog vs search

**Cutover**

- V9 + init mirrors; no Concept backfill
- Partial unique indexes for passports vs flower-only rows
- Strict code cut in the same PR
- V10 time-box

---

## 12. Implementation boundary

This file is the design. The next step after **human approval of this spec** is an implementation plan (writing-plans), not code.

Primary code surfaces (for the later plan, not to start now):

- `sql/migrations/V9__noun_passport_semantic_edge.sql`, `sql/init/02_schema.sql`, `sql/init/03_indexes.sql`
- `src/hermes_memory/provider.py` (A–F, extraction, queue `previous_conversation_id`, stop ABOUT)
- `src/hermes_memory/store.py` (`expand_graph` beam, passport writes)
- `src/hermes_memory/graph_api.py` + `docs/graph/fountain.html` (`pack_search`, catalog, prefixes)
- Tests listed in §10
- README pane copy: `mentions` as co-occurrence; stats without ABOUT; `3d.html` unlisted

Do not modify `.github/workflows/`, `.opencode/`, `opencode.json`, or `AGENTS.md` as part of this work unless a later human PR says so.
