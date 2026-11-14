# Memory lifecycle and graph layer — design

**Status:** v0.5, for approval. Phase 0 is implemented on `phase0/scoping-durability` (§8). Nothing in §4–§7 is implemented. This document is the single contract for the graph layer; the implementation plan is re-derived from it after approval.

**Supersedes:** v0.1 of this file (which proposed a separate `fact` table and wrongly said `claims` had no writer), and in `2026-09-03-conversation-manifold-design.md`: §2 non-goal "LLM extraction in the drain loop", §6 noun extraction, §7 walker, §8.2.1 injection.

## 1. Problem

The plugin promises vector recall and graph traversal. Measured on the eval harness (200 facts, 800 chatter turns, 150 questions):

| Metric | Value |
|---|---|
| Single-fact recall | 1.00 |
| Multi-hop recall | 0.25 |
| Vector-only recall | 0.72 (served context today is effectively vector-only) |
| Unrelated projects per answer | ~9 |
| Prefetch latency p50 / p95 | 93 / 110 ms (after removing the unused walk) |
| Entities minted: owners / projects / cities | 200 / 0 / 0 of 200 |

The served prompt today is **vector memory with neighbour and claim overlays**. The co-occurrence graph (`noun`, `semantic_edge`, passports) is written but no longer read on the prompt path. Documentation and release claims must say so until §6 is enabled.

## 2. Principles

1. **One semantic authority.** `claims` is the only store of facts. Every writer (rule reception, model extraction, repair, confirmation) produces evidence for claims; nothing else holds facts.
2. **Owner is part of every key.** `owner` means `conversations.agent_identity`, everywhere; there is no separate owner column or mapping table. Retrieval, neighbours, deduplication, provenance, aliases, entities, evidence, jobs, shadow rows and graph identity are keyed by owner, with composite foreign keys so a row cannot reference another owner's row.
3. **Evidence, not counters.** Claim rows are a projection of evidence plus current entity resolution. Confirmation, retraction, deletion, merges and re-extraction change evidence or resolution; affected claims are rebuilt in the same transaction.
4. **Event time, not processing time.** Ordering and timelines use the turn's capture time (`conversations.ts`, set from `captured_at`).
5. **One durable job lifecycle.** Write capture and extraction use the same lease-and-fence semantics; results publish atomically with the fence check.
6. **Relational prompt path.** Postgres tables, BTREE / pg_trgm / HNSW, bounded joins. AGE is not on the prompt path and gets no claims projection without a concrete consumer.
7. **Shadow before serve.** Hybrid retrieval runs beside vector retrieval, off the request path, and is compared offline before it is allowed to change the prompt.
8. **Abstain rather than guess.** An extractor that cannot name a subject or object from the text produces no claim; the turn stays findable by vector search.

## 3. Current state this builds on

- `claims` (V12–V15): `span_id NOT NULL REFERENCES conversations ON DELETE RESTRICT`, `subject`, `verb`, `object`, `subject_canon`, `object_canon`, `polarity`, `act`, `valid` (live / retracted / unknown), `superseded_by`, `uptake`, `seen_count`; live unique key `(subject_canon, verb, object_canon, polarity) WHERE valid='live'`. Repeats merge: `span_id` moves to the latest saying and `seen_count` increments, so earlier sayings are not recorded. An opposite-polarity statement without a repair verdict is stored `unknown` (abstained).
- Writer: `_reception_stage`, user spans only, via `extract_assertions` (verbs `is` and `uses`; subjects starting with a pronoun or determiner are already skipped). `retract_on_repair` retracts on a repaired uptake, either by opposite-polarity match (sets `superseded_by`) or deictically (all live claims of the prior span).
- `aliases` (V12): `surface_norm → canon_id`, global (no owner), one live row per surface, `valid` live/retracted, `source` user_span/doc/manual, no span reference. Written from explicit "aka" equations.
- `uptakes` (V12): assistant→user verdicts; `classify_repair` emits only `repaired` or `unknown`.
- `conversations.claims_status` (V14): pending/ready/none/failed.
- No owner on `claims`, `aliases`, `noun`, `semantic_edge`, passports.

## 4. Data model

### 4.1 Entities

```
entity(owner, id, name, name_norm, type, name_vec vector(768),
       merged_into, created_at)
  PRIMARY KEY (owner, id)
  UNIQUE (owner, name_norm, type)
  FOREIGN KEY (owner, merged_into) REFERENCES entity(owner, id)
entity_alias(owner, surface_norm, entity_id, source, state, span_id, event_time, created_at)
  PRIMARY KEY (owner, surface_norm, entity_id)
  FOREIGN KEY (owner, entity_id) REFERENCES entity(owner, id)
  FOREIGN KEY (owner, span_id)   REFERENCES conversations(agent_identity, id) ON DELETE CASCADE
turn_entity(owner, span_id, entity_id, surface, conf)
  PRIMARY KEY (owner, span_id, entity_id)                       -- retries: ON CONFLICT DO NOTHING
  FOREIGN KEY (owner, span_id)   REFERENCES conversations(agent_identity, id) ON DELETE CASCADE
  FOREIGN KEY (owner, entity_id) REFERENCES entity(owner, id)
```

- `type` ∈ `self`, person, project, place, org, tool, concept, other. Each owner has exactly one `self` entity, created on first use; first-person subjects and objects ("I", "me", "my") resolve to it.
- `entity_alias.source` ∈ `equation` (an explicit user "X aka Y"), `extractor` (a name variant proposed by an extractor), `migration`, `manual`. Only `equation` and `manual` aliases may join two existing entities; `extractor` aliases only attach a new surface to one entity. `state` ∈ live / retracted.
- **Merges.** When an equation joins two existing entities, the newer one gets `merged_into` the older; resolution follows `merged_into`; affected claims are rebuilt (§4.2). When the last live equation that caused a merge is retracted or its span deleted, `merged_into` is cleared and the affected claims are rebuilt again. Merges never cross owners (enforced by the composite key).
- **Concurrent creation.** Entity, alias and `turn_entity` inserts use `INSERT … ON CONFLICT DO NOTHING RETURNING`, then `SELECT` the existing row when nothing is returned. Under READ COMMITTED the second statement sees the concurrent winner. A unique-key collision is never a job failure.
- Composite keys require `UNIQUE (agent_identity, id)` on `conversations`.

**Abstention.** These surfaces never create or match an entity, as subject or object: personal pronouns other than first person singular (he, she, they, it, we, you and their forms), demonstratives (this, that, these, those), indefinites (someone, something, anything), and a generic-noun stoplist (`ABSTAIN_SURFACES` in code: "the project", "the app", "the repo", "the team", "the company", "the thing", "the user", …), plus any surface made only of stopwords. The `rules` tier abstains (it already skips such subjects). The `model` tier may resolve such a surface from its context turns, but its output name must appear, normalized, in the turn or its context turns; otherwise the claim is dropped. A turn whose claims are all dropped stays vector-only.

### 4.2 Claims (evolved) and evidence

`claims` gains, additively: `owner`, `subject_id` (required for new claims), `object_id` (entity, nullable when the object is a literal), `fact_text`, `fact_vec vector(768)`, `embed_model`, `status`, `valid_from`, `valid_to`, plus `UNIQUE (owner, claim_id)` so evidence can reference it with a composite key. An object that is a named thing becomes an entity; a value (a number, date, quantity, or quoted phrase) stays a literal in `object_canon`. An abstained object surface produces no claim.

**Legacy columns.** `status` replaces `valid`. In Phase 1 every reader moves to `status`; `valid` stays as a read-only mirror maintained by a trigger from `status` (grounded and current → live; retracted or superseded → retracted; else unknown) and is dropped in Phase 4. `seen_count` and `claims.uptake` are frozen (no writer); the counts come from evidence. `claims.span_id` loses its `NOT NULL` and its `RESTRICT` foreign key, because provenance moves to evidence and span deletion must be possible.

The current-claim key uses resolved entities, with literal objects prefixed so an entity id can never equal a literal string, and excludes historical intervals so a value can be re-asserted after it was superseded. Because it is built from expressions, it is a unique **index**, not a table constraint:

```
CREATE UNIQUE INDEX claims_current_key ON claims (
    owner, subject_id, verb,
    (CASE WHEN object_id IS NOT NULL THEN 'e:' || object_id::text ELSE 'l:' || object_canon END),
    polarity)
  WHERE status IN ('grounded','unconfirmed') AND valid_to IS NULL;
```

This merges "Acme" and "Acme Corp" once they resolve to one entity, and keeps `project:hermes` and `tool:hermes` apart.

**Verb table.**

```
verb(verb PRIMARY KEY, functional bool NOT NULL, phrase text NOT NULL, created_at)
verb_synonym(phrase_norm PRIMARY KEY, verb REFERENCES verb)
```

`phrase` renders `fact_text` ("works for"). Seeded by migration with the rules verbs (`is`, `uses`; non-functional), the functional set (`based_in`, `owned_by`, `works_for`, `lives_in`, `role_is`, `status_is`) and common non-functional verbs. Verbs are added only by migration; the model's structured output is an enum of the table, so an unknown verb cannot be written. Changing a verb's `functional` flag rebuilds every key with that verb in the same migration.

**Evidence.**

```
claim_evidence(owner, id, claim_id, span_id, speaker, act, polarity,
               subject_surface, object_surface,
               extractor, extractor_version, confidence, event_time,
               state, created_at)
  UNIQUE (owner, span_id, claim_id, extractor, extractor_version)   -- idempotent retries; (owner, span_id)
                                                                     -- prefix serves the delete cascade
  FOREIGN KEY (owner, claim_id) REFERENCES claims(owner, claim_id)
  FOREIGN KEY (owner, span_id)  REFERENCES conversations(agent_identity, id) ON DELETE CASCADE
```

- `speaker`, `act`, `polarity` and the surfaces are those of this evidence event, not the claim's. Storing the surfaces lets merges and alias retractions re-resolve evidence and rebuild claims without re-running extraction.
- `act` ∈ `assert`, `retract`. A `retract` is written by repair handling (§5.3) and targets the claim it corrects.
- `state` ∈ `live`, `withdrawn` (removed by a repair of the span it came from), `superseded_version` (replaced by another extraction of the same span, §5.2). "Withdrawn" is not "retracted": withdrawing removes one piece of support, while a `retract` act is evidence in its own right.

**Timelines.** Every claim with user evidence belongs to exactly one timeline, rebuilt in the transaction that changes any of its evidence:

| Relation | Timeline key | What varies along it |
|---|---|---|
| Functional verb | `(owner, subject_id, verb)` | the object |
| Non-functional verb | `(owner, subject_id, verb, object key)` | the polarity |

Rebuild:

1. Collect the key's live **user** evidence events, ordered by event time (ties by span id).
2. **Corrections.** Each `retract` event cancels the `assert` events it targets (same object and polarity) whose event time is at or before the retract's. Cancelled asserts are ignored. A later assert of the same value is not cancelled.
3. **Intervals.** Walk the remaining asserts in order. For a functional key, a positive assert of a different object opens a new interval; consecutive asserts of the same object extend the current one; a negative assert of the current object ("I no longer live in Austin") closes it with no current value. For a non-functional key, each change of polarity opens a new interval. `valid_from` is the interval's first event time; `valid_to` is the next interval's start or the closing event time; the last open interval has `valid_to = NULL`.
4. **Rows.** Map each interval to a claim row, reusing the row that already holds the most of that interval's evidence (ties: lowest `claim_id`), creating rows for new intervals, and setting `superseded_by` along the chain. Re-point each evidence row to its interval's row. Rows left with no interval and no other evidence become `orphaned`.

Consequences, all testable: NYC → Austin → NYC is three intervals and two NYC rows; a late backfill lands in the interval its event time belongs to; repairing the newest value restores the previous interval as current; repairing a middle value merges its neighbours when they hold the same object; deleting a span removes its events and the rebuild follows. Contradictory polarity is no longer abstained as today's `unknown`: the newer user statement is current. Timelines per key are short, so the rebuild is cheap.

**Claim status**, derived per row after the rebuild:

| Row evidence | Status |
|---|---|
| In a timeline interval (non-cancelled user assert) | `grounded` (current when `valid_to IS NULL`) |
| Only assistant `assert` evidence, no user evidence | `unconfirmed` (outside timelines, never in the grounded block) |
| Only cancelled user asserts | `retracted` |
| No live evidence (spans deleted, evidence withdrawn or superseded) | `orphaned` (kept, never injected) |

**Confirmation** of an assistant claim by the user adds user evidence; the claim joins its timeline as grounded and keeps both evidence rows, so provenance shows who said what.

**Deletion.** Spans are deleted only through the store's delete API, which collects the timeline keys of the span's evidence, deletes the span (evidence, `entity_alias` and `turn_entity` rows cascade), and rebuilds those keys and any affected merges in one transaction. Rows deleted by raw SQL leave stale status, which `verify` detects and repairs (§9).

### 4.3 Graph

The traversable graph is the set of claims with `status = 'grounded'`, `valid_to IS NULL`, and an entity object: edges `subject_id → object_id` labelled `verb`, following `merged_into`. Every claim has an entity subject, so literal-object claims are leaf facts reachable from their subject. The legacy `noun` / `semantic_edge` co-occurrence graph is not extended.

## 5. Write path

### 5.1 Durable jobs

One job lifecycle for capture and extraction:

```
durable_job(owner, id, kind, op_id, session_id, event_time, priority, payload,
            status, claimed_by, lease_expires_at, fence, attempts,
            next_attempt_at, last_error, created_at)
  kind:   capture | extract_rules | extract_model
  status: pending → leased → done | retryable → leased … | failed
  UNIQUE (owner, kind, op_id)
  INDEX  (owner, session_id, kind, event_time) WHERE status IN ('pending','leased','retryable')
```

- **Claim:** `UPDATE … SET status='leased', claimed_by, lease_expires_at = now()+lease, fence = fence+1 … FOR UPDATE SKIP LOCKED`, only for `pending`, `retryable` past `next_attempt_at`, or `leased` with an expired lease; ordered by `priority`, then `event_time`.
- **Leases:** capture 10 min (Phase 0 value); `extract_rules` 60 s; `extract_model` 120 s. A worker heartbeats every quarter lease, only while its fence is current.
- **Publish:** results are written in one transaction that first locks the job row and checks `fence` = the worker's fence; if not, the transaction aborts and nothing is published.
- **Fail:** `retryable` with backoff 5 s, 30 s, 2 min, 10 min; `failed` after 5 attempts. Failures are visible in health and verify.
- **Session order, per tier:** an extraction job is claimable only when no earlier-event-time job of the **same kind**, owner and session is `pending`, `leased` or `retryable` (served by the index above). A slow `extract_model` job never holds up `extract_rules`. A `retryable` job does block later jobs of its tier and session, so the worst-case head-of-line delay is the leases plus the backoff schedule (about 13 minutes) before the job fails and unblocks. Health reports sessions blocked longer than 5 minutes.
- **Priority:** live turns 0, backfill 10. Session order applies to backfill too; different sessions run in parallel, bounded by `extract_rules_workers` (default 2) and `extract_model_workers` (default 1). Backfill is not claimed while live extraction jobs are pending.
- **Payloads** carry ids only (owner, span id, extractor and version), never turn text; workers read text under the job's owner.

The V16/V17 `write_outbox` (op_id-unique, fenced) is the capture instance of this lifecycle; Phase 1 moves it onto `durable_job` with a compatibility migration. Capture payloads necessarily carry the turn text until it is stored.

### 5.2 Extraction job

Created for each substantive stored turn of either speaker: `user` or `assistant` role, and not noise by `turn_filter._is_noise` (at least 40 characters and not a bare acknowledgement). System and tool turns are excluded. Elliptical confirmations ("yes, exactly") are noise and are handled by uptake (§5.3). `op_id = span_id:extractor:extractor_version`.

1. Lease the job; read the turn and the 4 previous turns of the same owner and session by event time, each truncated to 1,000 characters.
2. Extract in two tiers. The `rules` extractor (`extract_assertions` + alias equations) runs first and publishes immediately, so simple assertions are searchable at once. The `model` extractor (Ollama structured JSON, `llama3.2:3b`, verbs constrained to the verb table, the grounding check of §4.1) runs as a later job for the same span. Model failure → `retryable`; after the limit the `rules` evidence stands.
3. **Publish atomically** (one transaction, fence-checked): resolve entities (live alias exact → trigram similarity ≥ 0.6 **and** name-vector cosine ≥ 0.85 within owner and type → create), insert evidence (idempotent by its unique key), rebuild affected timelines, build `fact_text` from the template `"{subject name} {verb phrase} {object name or literal}"` and embed it with the configured embedding model (stamping `embed_model`), write `turn_entity`, mark the job done.
4. **Supersession across tiers is per claim.** Model evidence marks a `rules` evidence row of the same span `superseded_version` only when both resolve to the same timeline key and value. A rules claim the model did not produce keeps its rules evidence; a model claim the rules did not produce is added.
5. **Re-extraction with a new version of the same extractor replaces per span**: all older-version evidence of that extractor for the span becomes `superseded_version`. A version bump is a deliberate, eval-gated change, so it may remove wrong claims. Before rollout, the re-extraction run writes a diff report: claims that lose all live evidence, and claims added. The report is part of the version's gate.
6. `fact_text` and `fact_vec` are rebuilt when a subject or object entity is renamed or merged, and re-embedded by the existing re-embed path when the embedding model changes.

Measured on the reference GPU: `llama3.2:3b` ~4 s per turn warm, `qwen2.5` ~24 s. Extraction is a backlog, never on the prompt path; un-extracted turns are still found by vector search.

### 5.3 Reception

The reception stage stops writing claims directly. Its rule extraction becomes the `rules` extractor in §5.2. Its repair handling writes `retract` evidence: against the prior span's claims that the repairing turn contradicts (opposite polarity, same key), or against all of the prior span's claims for a deictic repair. Uptake verdicts stay in `uptakes`. One writer path, one lifecycle.

**Confirmation through uptake.** When an uptake verdict says a user turn accepted the preceding assistant turn, the pipeline writes user `assert` evidence (`extractor = uptake`) for the `unconfirmed` claims of that assistant span. `classify_repair` emits only `repaired` or `unknown` today; an `accepted` verdict needs a new, conservative classifier (explicit confirmation cues after an assistant statement or question) that abstains otherwise.

**Intended behaviour changes in Phase 1**, the only ones the "reception unchanged" gate allows: contradictory polarity resolves to the newer statement instead of abstaining; a repair writes evidence instead of flipping `valid` directly (the `valid` mirror shows the same result).

### 5.4 Secret redaction (Phase 6)

Adopted as a fast-follow phase after this spec (§10). Redaction runs at capture, before the job enqueue and the `conversations` insert. Anywhere downstream would leave raw secrets in job payloads, `fact_text`, `fact_vec` and the extraction model's input. Until Phase 6 ships, secrets are filtered only at recall and injection, and documentation must say so. Phase 6 ships with a test corpus of secret-shaped strings (API keys, tokens, private keys, connection strings with passwords) and a corpus of look-alikes (hashes, UUIDs, commit ids, code identifiers) for the false-positive bound in its gate.

## 6. Read path

Modes (`hybrid_age.retrieval_mode`): `vector` (default, today's served path), `shadow` (serve vector, compute hybrid in the background), `hybrid` (serve hybrid).

1. Embed the query once.
2. Candidate arms, all owner-scoped, run concurrently on separate pool connections:
   - **Turns:** turn-vector ANN.
   - **Claims:** `fact_vec` ANN over grounded, current claims.
   - **Linking:** split the query into 1–3-grams, drop grams that start or end with a stopword or consist only of stopwords or abstained surfaces ("where does raul" → "raul"), deduplicate, and resolve exact and trigram matches in **one** batched query (`unnest($1::text[])` against live `entity_alias` and `entity`). Additionally, the top 3 entities by `name_vec` cosine to the full query embedding, if ≥ 0.80. There is no separate noun-phrase extractor.
   - **Turn entities:** entities of the top turns via `turn_entity`.

   Every retrieval transaction sets `SET LOCAL hnsw.iterative_scan = relaxed_order` (pgvector 0.8.6 in the installed image; verified available), because each owner is a small share of rows and plain HNSW stops after `ef_search` candidates.
3. **Traverse** from linked entities: at most 2 hops; each hop extends only from the previous hop's frontier; a visited set of entities prevents cycles and revisits; per node at most 8 edges, chosen by cosine of `fact_vec` to the query; at most 20 claims per hop.
4. **Fuse** the turn, claim, linking and traversal arms with RRF (k = 60). **Diversify** the fused list with MMR (λ = 0.7) using each candidate's own vector (turn embedding or `fact_vec`, both the configured 768-d model).
5. **Pack** within the existing injection budget (hard cap 1,200 tokens): a grounded facts block first, at most 12 facts and 400 tokens (claim text, event date, span id), then supporting turns. The block header lists every span id it contains so an agent's citation can be checked. Unconfirmed claims are never in the grounded block.
6. **Shadow** runs after the vector response is returned, in one background task (at most one in flight; a query arriving while it is busy is skipped), for a sampled share of queries (`shadow_sample_rate`, default 0.2; the eval harness uses 1.0). It records owner, vector ids, hybrid ids, per-arm latency and a query key in `retrieval_shadow`. The query key is HMAC-SHA256 of the normalized query under a per-installation secret written to `.env` at install, truncated to 16 bytes; no query text is stored. Rows expire after 30 days.

**Initial values** live in `config.py` under `hybrid_age.*` and change only with an eval run that re-checks §9:

| Parameter | Initial |
|---|---|
| Max hops / edges per node / claims per hop | 2 / 8 / 20 |
| RRF k / MMR λ | 60 / 0.7 |
| Facts block | 12 facts, 400 tokens |
| Linking name-vector threshold / top-n | 0.80 / 3 |
| Entity resolution trigram / name-vector | 0.6 / 0.85 |
| Shadow sample rate | 0.2 |

**Latency budget** at 10k turns, p95, excluding the query embedding (measured and reported separately): turns ≤ 40 ms, claims ≤ 30 ms, linking ≤ 30 ms then traversal ≤ 40 ms, fusion + MMR + packing ≤ 15 ms. The critical path is linking + traversal + fusion, about 85 ms. The harness reports each arm.

## 7. Coexistence, migration, rollback

### 7.1 Ownership during coexistence

| Data | Writer until cutover | Reader until cutover |
|---|---|---|
| `noun`, `semantic_edge`, passports | manifold drain (unchanged) | pane only |
| `claims` + `claim_evidence`, entities, `entity_alias` | extraction jobs (§5.2) | prompt in `shadow`/`hybrid`; pane after Phase 4 |
| `aliases` | none after the Phase 1 migration (legacy, read-only) | none; dropped in Phase 4 |

### 7.2 Migration (Phase 1)

Additive tables and columns, except the `claims.span_id` constraint relaxation (§4.2). Steps, idempotent:

1. **Owners.** Each claim gets `owner` from its span's `agent_identity`. Every claim has a span today (`span_id NOT NULL`), so none are unowned.
2. **Entities.** Subjects and entity-like objects are resolved within the owner. A claim whose subject or object abstains under §4.1 is kept as `orphaned` with its evidence (not injected, counted in verify).
3. **Evidence** (`extractor = rules`, version `legacy`, speaker `user`, event time of `span_id`, which today is the latest saying; earlier sayings were never recorded):
   - `valid = live` → one `assert`.
   - `valid = retracted` → one `assert` plus one `retract`: on the superseding claim's span when `superseded_by` is set, otherwise on the repairing user span from `uptakes` (`value = 'repaired'`), otherwise on the claim's own span with event time one microsecond after the assert.
   - `valid = unknown` → one `assert`. The timeline rebuild then resolves the contradiction by event time (§5.3 change).
4. **Rebuild** every timeline key. `seen_count` and `claims.uptake` are left as frozen history.
5. **Aliases.** If the database has exactly one owner (the default `shared` scope), live `aliases` rows are copied into `entity_alias` with `source = migration`. Otherwise they are not copied; the extraction jobs re-derive them from the original "aka" equations when history is backfilled (Phase 5). No owner inference by text search.
6. **Check** in `verify`: every `valid = live` claim from before the migration is grounded and current afterwards, except listed `unknown`-contradiction resolutions; every `retracted` one is not current.

### 7.3 Rollback

Two separate switches:

- `hybrid_age.retrieval_mode = vector` restores today's prompt exactly. This is the rollback.
- `hybrid_age.extraction_model = off` stops the model tier only; its jobs stay `pending` until it is turned back on. This is load shedding, not rollback. The `rules` tier is today's reception and is always on; a defect in it is rolled back by releasing the previous version. Schema changes are additive apart from the `span_id` relaxation, which an older release tolerates.

### 7.4 Retirement (Phase 4)

The pane switches to entities and claims; the co-occurrence writers stop; `aliases` and the `valid` mirror are dropped. **Pane parity** means: entity search, an entity's current and historical claims, each claim's evidence list with spans and speakers, and the claims graph around an entity. It does not mean reproducing the noun or semantic-edge views. The pane labels its two graphs: the AGE Session/Turn flower is **episodic** memory (what was said, when), the claims graph is **factual** memory (what is true now and was true before).

AGE is kept (decision, §11): its existing uses — the episodic flower, code-ingest File/Imports, and the pane's graph views — are unchanged by this design. No AGE projection of claims is built unless a concrete Cypher consumer exists.

## 8. Phase 0 (implemented)

- Owner scoping: graph-path provenance, neighbours (`PARTITION BY owner, session`), turn dedupe, `previous_turn`, `conversations_by_ids`, pane `/search` (optional owner).
- Durable capture: V16 `write_outbox`; V17 op_id uniqueness on outbox and conversations with `ON CONFLICT`; fenced leases; event-time previous-turn links.
- Prefetch no longer runs the unused walk.
- Health and verify report outbox pending and parked counts.
- Evidence: cross-owner integration tests with a shared session id fail on the previous code; six concurrent writes of one op store one row; a stale worker cannot settle a reclaimed row; 300-turn burst with immediate shutdown persists every row after replay.

Open from Phase 0: heartbeats (leases currently expire at 10 minutes with fencing only; added with `durable_job` in Phase 1); write-path secret redaction (Phase 6).

## 9. Acceptance gates

All measured by `scripts/eval_memory.py` on its planted corpus, automatically (no human or model judge).

| Gate | Target | Definition |
|---|---|---|
| Extraction recall (Phase 2) | ≥ 0.90 | Share of planted facts present as grounded, current claims with the correct subject and object entities. A store metric, not a retrieval metric. |
| Single-fact recall | ≥ 0.95 | Share of single-fact questions whose gold names all appear in the injected text. |
| Multi-hop recall | ≥ 0.80 (today 0.25) | The same, over the two-hop questions. |
| Hybrid lift | ≥ +0.15 absolute | Hybrid minus vector-only multi-hop recall, same questions, same corpus. |
| Unrelated context | ≤ 2 | Mean count of planted project names in the injected text that are not on the gold path (the harness `noise` metric). |
| Cross-owner leakage | 0 | Two owners with identical entity names and session ids; checked on every surface: turn ANN, claim ANN, linking, traversal, neighbours, facts block and evidence rendering, `turn_entity`, entity merges, job payloads and their context turns, `retrieval_shadow` rows. |
| Timelines under reordering | current value matches the event-time timeline | Out-of-order backfill, repair of newest and middle values, span deletion, re-assertion of old values, contradictory polarity. |
| Migration | no unlisted state change | §7.2 step 6. |
| Extraction restart / replay / concurrency | no duplicate evidence, no lost jobs | Kill workers mid-publish; replay; parallel workers on one session; concurrent creation of one entity. |
| Prefetch latency at 10k turns | p50 ≤ 150 ms, p95 ≤ 300 ms; per-arm budgets in §6 | Served path in `hybrid` mode. |
| Filtered recall with many owners | no drop | Eval owner < 5% of rows versus a single-owner database. |
| Observability | present | Health and verify report extraction jobs pending / leased / retryable / failed, blocked sessions, claims by status (including orphaned), and stale-status drift. |
| Secret redaction (Phase 6) | 100% of the secret corpus redacted on every surface; ≤ 2% of the look-alike corpus redacted | Conversations, job payloads, claims, embeddings, extractor input. |

## 10. Phases

| Phase | Work | Gate |
|---|---|---|
| 1 | `durable_job` (capture moved onto it); owner + composite keys on claims; entities and `entity_alias`; verb table; `claim_evidence`; timeline rebuild; delete API; migration (§7.2); reception writes evidence | Migration gate; timelines gate; reception unchanged except §5.3's listed changes |
| 2 | Extraction jobs (rules then model), atomic fenced publish, per-claim tier supersession, re-extraction diff report, backfill priority | Restart / replay / concurrency; cross-owner; extraction recall ≥ 0.90 |
| 3 | Retrieval arms, path-constrained traversal, RRF + MMR, facts block; `shadow` mode | All §9 retrieval gates measured in shadow |
| 4 | `hybrid` default after gates; pane on entities/claims with parity (§7.4); retire co-occurrence writers; drop `aliases` and `valid` | Pane parity; rollback rehearsed |
| 5 | 10k / 50k-turn scale, backfill of existing history | Latency gates at scale |
| 6 | Write-path secret redaction at capture (§5.4) — fast follow | Secret redaction gate |

## 11. Decisions recorded

1. `claims` is the single semantic authority (user decision, 2026-11-14).
2. Model extraction is allowed in the job worker, never on the prompt path; default `llama3.2:3b`.
3. Assistant output is stored as evidence and stays `unconfirmed` until a user confirms it.
4. Prompt-path latency budget: p95 ≤ 300 ms at 10k turns.
5. Write-path secret redaction: adopted as Phase 6, a fast follow after this spec (user decision, 2026-11-14).
6. Apache AGE is kept for its existing uses; no claims projection without a Cypher consumer (user decision, 2026-11-14).
7. Re-extraction by a new version of the same extractor replaces per span, gated by a diff report; supersession across tiers is per claim.
8. Contradictory polarity resolves to the newer user statement (replaces today's abstention).

## Sources

- [Combining pgvector and Apache AGE (Microsoft)](https://techcommunity.microsoft.com/t5/microsoft-blog-for-postgresql/combining-pgvector-and-apache-age-knowledge-graph-semantic/ba-p/4508781); [Azure: AGE performance](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/generative-ai-age-performance); AGE issues [2137](https://mail-archive.com/dev@age.apache.org/msg07346.html), [2348](https://mail-archive.com/dev@age.apache.org/msg08099.html).
- [Zep: A Temporal Knowledge Graph Architecture for Agent Memory](https://arxiv.org/html/2501.13956v1); [HippoRAG 2](https://arxiv.org/html/2502.14802); [Robust GraphRAG](https://arxiv.org/pdf/2603.14828).

## Changelog

| Version | Created | Modified | Author | Notes |
|---|---|---|---|---|
| 0.1 | 2026-11-14 | 2026-11-14 | Claude | Research and plan for a typed, owner-scoped graph layer. |
| 0.2 | 2026-11-14 | 2026-11-14 | Claude | Rewritten after code review: claims as the single authority with evidence rows, owner-composite keys, one durable job lifecycle with fenced atomic publish, event-time supersession, shadow mode, coexistence and rollback. Phase 0 recorded as implemented. |
| 0.3 | 2026-11-14 | 2026-11-14 | Claude | Spec review: current-claim key on entity ids and excluding historical intervals; full-timeline supersession recompute; `UNIQUE (owner, claim_id)`; job key includes owner; per-session extraction order; two-tier rules→model extraction with a verb table; confirmation through uptake; redaction placement; batched entity linking; iterative HNSW scan; AGE projection deferred. |
| 0.4 | 2026-11-14 | 2026-11-14 | Claude | Functional timelines rebuilt from user-assert evidence events into interval rows; prefixed expression unique index; session order per extractor tier; entity subject required; evidence state `withdrawn` (distinct from claim `retracted`); ambiguous alias rows dropped in migration. Decisions: keep AGE; redaction as Phase 6. |
| 0.5 | 2026-11-14 | 2026-11-14 | Claude | Spec review: pronoun/generic abstention and a per-owner `self` entity; retract defined as a correction event in the rebuild; polarity timelines for non-functional keys; migration preserves retracted/unknown state; per-claim tier supersession, per-span version replacement with diff report; separate rollback and load-shedding switches; `aliases` legacy and read-only; owner = `agent_identity`; `status` replaces `valid`; `claims.span_id` RESTRICT removed and a delete API; `op_id` with extractor and job kinds; leases, backoff, session-order index, priority, id-only payloads; entity race handling; verb table schema, `fact_text`, substantive turn, context window; initial read-path values, per-arm latency, async sampled shadow with HMAC query key; gate definitions and leakage matrix. |
