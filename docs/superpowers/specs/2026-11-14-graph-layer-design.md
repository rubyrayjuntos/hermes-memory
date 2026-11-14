# Memory lifecycle and graph layer — design

**Status:** v0.4, for approval. Phase 0 is implemented on `phase0/scoping-durability` (§8). Nothing in §4–§7 is implemented. This document is the single contract for the graph layer; the implementation plan is re-derived from it after approval.

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

1. **One semantic authority.** `claims` is the only store of facts. Every writer (rule reception, model extraction, repair) produces evidence for claims; nothing else holds facts.
2. **Owner is part of every key.** Retrieval, neighbours, deduplication, provenance, aliases, entities, evidence and graph identity are keyed by owner, with composite foreign keys so a row cannot reference another owner's row.
3. **Evidence, not counters.** A claim's state is derived from its evidence rows. Confirmation, retraction, deletion and re-extraction change evidence; the claim's state is recomputed in the same transaction.
4. **Event time, not processing time.** Ordering and supersession use the turn's capture time (`conversations.ts`, set from `captured_at`).
5. **One durable job lifecycle.** Write capture and extraction use the same lease-and-fence semantics; results publish atomically with the fence check.
6. **Relational prompt path.** Postgres tables, BTREE / pg_trgm / HNSW, bounded joins. AGE is not on the prompt path and gets no claims projection without a concrete consumer.
7. **Shadow before serve.** Hybrid retrieval runs beside vector retrieval and is compared offline before it is allowed to change the prompt.

## 3. Current state this builds on

- `claims` (V12–V15): `span_id`, `subject`, `verb`, `object`, `subject_canon`, `object_canon`, `polarity`, `act`, `valid` (live/retracted/unknown), `superseded_by`, `uptake`, `seen_count`; live unique key `(subject_canon, verb, object_canon, polarity)`. Written by `_reception_stage` from user spans via `extract_assertions` ("X is/uses Y"); repairs retract via uptake.
- `aliases` (V12): `surface_norm` → `canon_id`, globally unique per live surface; written from explicit "aka" equations.
- `uptakes` (V12): assistant→user reception verdicts.
- `conversations.claims_status` (V14): pending/ready/none/failed.
- No owner on `claims`, `aliases`, `noun`, `semantic_edge`, passports.

## 4. Data model

### 4.1 Entities

```
entity(owner, id, name, name_norm, type, name_vec vector(768), created_at)
  PRIMARY KEY (owner, id)
  UNIQUE (owner, name_norm, type)
entity_alias(owner, surface_norm, entity_id, source, created_at)
  PRIMARY KEY (owner, surface_norm, entity_id)
  FOREIGN KEY (owner, entity_id) REFERENCES entity(owner, id)
turn_entity(owner, span_id, entity_id, surface, conf)          -- passports to entities
  FOREIGN KEY (owner, span_id)   REFERENCES conversations(agent_identity, id)
  FOREIGN KEY (owner, entity_id) REFERENCES entity(owner, id)
```

`type` ∈ person, project, place, org, tool, concept, other. `aliases` migrates into `entity_alias` (§7.3). Composite keys require `UNIQUE (agent_identity, id)` on `conversations`.

### 4.2 Claims (evolved) and evidence

`claims` gains, additively: `owner`, `subject_id` (required for new claims: an unresolved subject creates an entity of type `other`, so every claim is traversable from its subject), `object_id` (entity, nullable when the object is a literal), `fact_text`, `fact_vec vector(768)`, `status`, `valid_from`, `valid_to`, plus `UNIQUE (owner, claim_id)` so evidence can reference it with a composite key. When entities merge later, claims are re-pointed to the surviving entity in the same transaction.

The current-claim key uses resolved entities, with literal objects prefixed so an entity id can never equal a literal string, and excludes historical intervals so a value can be re-asserted after it was superseded (NYC → Austin → NYC). Because it is built from expressions, it is a unique **index**, not a table constraint:

```
CREATE UNIQUE INDEX claims_current_key ON claims (
    owner, subject_id, verb,
    (CASE WHEN object_id IS NOT NULL THEN 'e:' || object_id::text ELSE 'l:' || object_canon END),
    polarity)
  WHERE status IN ('grounded','unconfirmed') AND valid_to IS NULL;
```

This merges "Acme" and "Acme Corp" once they resolve to one entity, and keeps `project:hermes` and `tool:hermes` apart. `verb` is normalized through a verb table (`works_for` ← "employed by", "works at", …) before upsert, and the model's structured output constrains verbs to that table, so functional supersession fires reliably.

```
claim_evidence(owner, id, claim_id, span_id, speaker, act, polarity,
               extractor, extractor_version, confidence, event_time,
               state, created_at)
  UNIQUE (owner, span_id, claim_id, extractor, extractor_version)   -- idempotent retries; (owner, span_id)
                                                                     -- prefix serves the delete cascade
  FOREIGN KEY (owner, claim_id) REFERENCES claims(owner, claim_id)
  FOREIGN KEY (owner, span_id)  REFERENCES conversations(agent_identity, id) ON DELETE CASCADE
```

`state` ∈ `live`, `withdrawn` (removed by a repair of the span it came from), `superseded_version` (an older extractor version for the same span). "Withdrawn" is deliberately not "retracted": withdrawing evidence removes one piece of support, while a user `retract` act is evidence in its own right and can make the claim `retracted`.

**Claim status is derived** from live evidence, recomputed in the transaction that changes evidence:

| Live evidence | Claim status |
|---|---|
| Any user `assert` | `grounded` |
| Only assistant evidence | `unconfirmed` |
| User `retract` newer (event time) than the newest `assert` | `retracted` |
| None (spans deleted, or all evidence withdrawn or superseded) | `orphaned` (kept, never injected) |

**Supersession** applies to functional relations (`based_in`, `owned_by`, `works_for`, `lives_in`, `role_is`, `status_is`). For these, claim rows are **intervals derived from evidence**, not rows that evidence picks. Whenever evidence changes for a functional key `(owner, subject_id, verb)`, the key's timeline is rebuilt in the same transaction:

1. Collect the key's live user `assert` evidence events (any object), ordered by event time.
2. Collapse consecutive events with the same object into one interval: `valid_from` = the first event's time, `valid_to` = the next interval's `valid_from` (NULL for the last).
3. Map each interval to a claim row: reuse the row that already holds that interval's evidence, create a row for a new interval, and mark rows left without an interval `orphaned`.
4. Re-point each evidence row to the claim row of its interval; set `superseded_by` along the chain.

NYC → Austin → NYC is three intervals and two NYC rows; a late backfill or retry lands in the interval its event time belongs to, never on whichever NYC row happens to be current. Retracting or deleting the newest value restores the previous interval as current. Non-functional relations have no intervals: one row per current key. Timelines per key are short, so the rebuild is cheap.

**Confirmation** of an assistant claim by the user adds user evidence; the claim becomes grounded and keeps both evidence rows, so provenance shows who said what.

**Deletion** of a span cascades to its evidence only; the claim's status is recomputed (possibly `orphaned`), never deleted while other evidence supports it.

### 4.3 Graph

The traversable graph is the set of claims with `status = 'grounded'`, `valid_to IS NULL`, and an entity object: edges `subject_id → object_id` labelled `verb`. Every claim has an entity subject (§4.2), so literal-object claims are leaf facts reachable from their subject. Legacy claims whose subject cannot be resolved during migration get an entity created for them (type `other`). The legacy `noun` / `semantic_edge` co-occurrence graph is not extended.

## 5. Write path

### 5.1 Durable jobs

One job lifecycle for capture and extraction:

```
durable_job(owner, id, kind, op_id, payload, status, claimed_by, lease_expires_at,
            fence, attempts, last_error, created_at)
  status: pending → leased → done | retryable → leased … | failed (after N attempts)
  UNIQUE (owner, kind, op_id)
```

- **Claim:** `UPDATE … SET status='leased', claimed_by, lease_expires_at = now()+lease, fence = fence+1 … FOR UPDATE SKIP LOCKED`, only for `pending`, `retryable`, or `leased` with an expired lease.
- **Heartbeat:** a worker extends `lease_expires_at` while it works (only if its fence is current).
- **Publish:** results are written in one transaction that first locks the job row and checks `fence` = the worker's fence; if not, the transaction aborts and nothing is published.
- **Fail:** `retryable` with backoff; `failed` after the limit; failures are visible in health and verify.
- **Session order (extraction), per tier:** an extraction job for `(owner, session, extractor)` is claimable only when no earlier-event-time job of the **same extractor** for that session is `pending`, `leased` or `retryable`. Corrections see the claims of earlier turns from the same tier, and a slow or retrying `model` job never holds up the `rules` job for a later turn. A `failed` job does not block its session.

The V16/V17 `write_outbox` (op_id-unique, fenced) is the capture instance of this lifecycle; Phase 1 moves it onto `durable_job` with a compatibility migration.

### 5.2 Extraction job

Created for each substantive stored turn (both speakers), keyed `op_id = span_id:extractor_version`.

1. Lease the job; read the turn and up to N previous turns of the same owner and session (by event time).
2. Extract in two tiers. The `rules` extractor (`extract_assertions` + alias equations, today's reception rules) runs first and publishes immediately, so simple assertions are searchable at once. The `model` extractor (Ollama structured JSON, verbs constrained to the verb table) runs as a later job for the same span; its evidence marks that span's `rules` evidence `superseded_version`. Model failure → `retryable`; after the limit the `rules` evidence stands.
3. **Publish atomically** (one transaction, fence-checked): resolve entities (alias exact → trigram + name-vector within owner and type → create), upsert claims by live key, insert evidence (idempotent by its unique key), recompute affected claim status and supersession, write `turn_entity`, mark the job done.
4. Re-extraction with a new extractor version inserts new evidence and marks the span's older-version evidence `superseded_version` in the same transaction.

Measured on the reference GPU: `llama3.2:3b` ~4 s per turn warm, `qwen2.5` ~24 s. Extraction is a backlog, never on the prompt path; un-extracted turns are still found by vector search.

### 5.3 Reception

The reception stage stops writing claims directly. Its rule extraction becomes the `rules` extractor in §5.2; its repair classification writes `retract` evidence against the prior span's claims; uptake verdicts stay in `uptakes`. One writer path, one lifecycle.

**Confirmation through uptake.** Users confirm elliptically ("yes, exactly"), and extracting that turn alone yields nothing. When an uptake verdict says a user turn accepted the preceding assistant turn, the pipeline writes user `assert` evidence (`extractor = uptake`) for the `unconfirmed` claims of that assistant span. Today `classify_repair` emits only `repaired` or `unknown`; an `accepted` verdict needs a new, conservative classifier (explicit confirmation cues after an assistant statement or question) and abstains otherwise.

### 5.4 Secret redaction (Phase 6)

Adopted as a fast-follow phase after this spec (§10). Redaction runs at capture, before the outbox/job enqueue and the `conversations` insert. Anywhere downstream would leave raw secrets in job payloads, `fact_text`, `fact_vec` and the extraction model's input. Until Phase 6 ships, secrets are filtered only at recall and injection, and documentation must say so.

## 6. Read path

Modes (`hybrid_age.retrieval_mode`): `vector` (default, today's served path), `shadow` (serve vector, record hybrid), `hybrid` (serve hybrid).

1. Embed the query once.
2. Candidate arms, all owner-scoped: turn vectors; claim vectors (`fact_vec`, grounded and current only); entity linking from the query; entities of the top turns via `turn_entity`. Entity linking extracts and deduplicates the query's 1–3-grams in Python and resolves exact and trigram matches in **one** batched query (`unnest($1::text[])` against `entity_alias` and `entity`); `name_vec` similarity is used only for the top few noun phrases or the full query embedding, never per n-gram.
   Filtered vector search: each owner is a small share of the rows, and plain HNSW stops after `ef_search` candidates regardless of how many passed the owner filter. Every retrieval transaction sets `SET LOCAL hnsw.iterative_scan = relaxed_order` (pgvector 0.8.6 in the installed image; verified available).
3. Traverse the graph from linked entities, at most 2 hops, path-constrained: each hop extends only from the previous hop's frontier, returning only claims on a path, with per-node fan-out caps and query-relevance ordering (cosine of `fact_vec` to the query).
4. Fuse turn, entity, claim and traversal arms with RRF; diversify with MMR over embeddings (not text similarity).
5. Pack a grounded facts block (claim text, event date, span id) before supporting turns, within the token budget. The block header lists every span id it contains so an agent's citation can be checked. Unconfirmed claims are never in the grounded block.
6. Shadow mode records, per query: vector ids, hybrid ids, latency, owner, query hash — in `retrieval_shadow` — for offline comparison.

## 7. Coexistence, migration, rollback

### 7.1 Ownership during coexistence

| Data | Writer until cutover | Reader until cutover |
|---|---|---|
| `noun`, `semantic_edge`, passports | manifold drain (unchanged) | pane only |
| `claims` + `claim_evidence`, entities | extraction jobs (§5.2) | prompt in `shadow`/`hybrid`; pane after Phase 4 |
| `aliases` | frozen after migration | none |

### 7.2 Migration

Additive columns and tables only. Existing `claims` rows get `owner` from their span's `agent_identity`, one evidence row each (`extractor = rules`, version `legacy`), and status recomputed. Existing `aliases` rows get an owner only when exactly one owner has a span whose text contains both surfaces. Rows that match no owner, or more than one, are dropped; they are re-derivable from text by the extraction jobs.

### 7.3 Rollback

`retrieval_mode = vector` restores today's prompt exactly; disabling extraction stops new evidence. New tables are additive and can be left in place.

### 7.4 Retirement (Phase 4)

The pane switches to entities and claims; the co-occurrence writers stop. AGE is kept (decision, §11): its existing uses — the episodic Session/Turn flower, code-ingest File/Imports, and the pane's graph views — are unchanged by this design. No AGE projection of claims is built unless a concrete Cypher consumer exists.

## 8. Phase 0 (implemented)

- Owner scoping: graph-path provenance, neighbours (`PARTITION BY owner, session`), turn dedupe, `previous_turn`, `conversations_by_ids`, pane `/search` (optional owner).
- Durable capture: V16 `write_outbox`; V17 op_id uniqueness on outbox and conversations with `ON CONFLICT`; fenced leases; event-time previous-turn links.
- Prefetch no longer runs the unused walk.
- Health and verify report outbox pending and parked counts.
- Evidence: cross-owner integration tests with a shared session id fail on the previous code; six concurrent writes of one op store one row; a stale worker cannot settle a reclaimed row; 300-turn burst with immediate shutdown persists every row after replay.

Open from Phase 0: heartbeats (leases currently expire at 10 minutes with fencing only; added with `durable_job` in Phase 1); write-path secret redaction (Phase 6).

## 9. Acceptance gates (eval harness, before `hybrid` becomes default)

| Gate | Target |
|---|---|
| Multi-hop recall (true two-hop questions) | ≥ 0.80 (today 0.25) |
| Single-fact recall | ≥ 0.95 |
| Hybrid minus vector-only on multi-hop | clearly positive |
| Unrelated context per answer | ≤ 2 |
| Cross-owner leakage (vector, graph, neighbours, evidence) | 0 |
| Supersession and retraction | never injected as current |
| Extraction restart / replay / concurrency | no duplicate evidence, no lost jobs |
| Prefetch latency at 10k turns | p50 ≤ 150 ms, p95 ≤ 300 ms |
| Filtered recall with many owners (eval owner < 5% of rows) | no drop versus a single-owner database |
| Supersession under reordering (backfill, retraction, deletion) | current value matches the event-time timeline |

## 10. Phases

| Phase | Work | Gate |
|---|---|---|
| 1 | `durable_job`; owner + composite keys on claims and aliases; entities; `claim_evidence`; migration of existing rows; reception moved onto evidence | Migration idempotent; reception behaviour unchanged by tests |
| 2 | Extraction job (rules then model), atomic fenced publish, re-extraction versions | Restart / replay / concurrency / cross-owner tests; fact recall ≥ 0.90 |
| 3 | Retrieval arms, path-constrained traversal, RRF + MMR, facts block; `shadow` mode | Gates in §9 measured in shadow |
| 4 | `hybrid` default after gates; pane on entities/claims; retire co-occurrence writers | Pane parity; rollback rehearsed |
| 5 | 10k / 50k-turn scale, backfill of existing history | Latency gates at scale |
| 6 | Write-path secret redaction at capture (§5.4) — fast follow | Secret-shaped strings absent from conversations, job payloads, claims, embeddings and extractor input |

## 11. Decisions recorded

1. `claims` is the single semantic authority (user decision, 2026-11-14).
2. Model extraction is allowed in the job worker, never on the prompt path; default `llama3.2:3b`.
3. Assistant output is stored as evidence and stays `unconfirmed` until a user confirms it.
4. Prompt-path latency budget: p95 ≤ 300 ms at 10k turns.
5. Write-path secret redaction: adopted as Phase 6, a fast follow after this spec (user decision, 2026-11-14).
6. Apache AGE is kept for its existing uses; no claims projection without a Cypher consumer (user decision, 2026-11-14).

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
