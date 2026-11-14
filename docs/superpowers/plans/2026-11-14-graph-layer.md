# Graph Layer (Phases 1–3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the graph from a co-occurrence index into owner-scoped, typed facts that vector search and graph traversal use together, so multi-hop questions are answered from memory.

**Architecture:** A durable extraction stage (off the prompt path) asks a local model for entities and typed relations, resolves entities per owner, and stores provenance-carrying facts in new owner-scoped tables. At prompt time, vector search finds seed turns, facts and entities; a bounded relational traversal follows typed facts for up to two hops; results are fused (RRF), deduplicated (MMR) and packed as a facts block plus supporting turns, every item carrying its span id.

**Tech Stack:** Python 3.11+, asyncpg, Postgres 17 + pgvector (HNSW, 768-d) + pg_trgm, Ollama (OpenAI-compatible endpoint, JSON-schema structured output), pytest + hypothesis. Apache AGE is not on this path.

**Spec:** `docs/superpowers/specs/2026-11-14-graph-layer-design.md` (v0.2). Phase 0 (owner-scoped graph paths, durable write outbox V16, harness metrics) is done on `phase0/scoping-durability`; this plan starts from it.

## Global Constraints

- Every read and write is scoped to the memory owner (`agent_identity`); no query returns another owner's rows.
- Extraction never runs on the prompt path. The prompt path makes no model calls other than one query embedding.
- Facts from assistant turns are `unconfirmed` and never appear in the grounded facts block.
- Migrations are idempotent (`IF NOT EXISTS`, `CREATE OR REPLACE`); new versions are added to `CONSUMER_HEAD_VERSIONS`.
- Embeddings: `nomic-embed-text`, 768-d. Never mix dimensions.
- SQL values are bind parameters; identifiers via `psycopg.sql.Identifier` where dynamic. Never string-concatenate SQL.
- Prefetch latency at 10k turns: p50 ≤ 150 ms, p95 ≤ 300 ms (eval harness).
- Default extraction model `llama3.2:3b` (measured ~4 s/turn warm on the reference GPU; `qwen2.5` ~24 s). Configurable via `hybrid_age.extract_model`.
- Existing `noun`, `semantic_edge`, `aliases`, `claims` and the manifold walk stay untouched in this plan; retiring them is Phase 4.

## Review Focus

- **Model unreachable or invalid JSON:** the turn stays `pending` and is retried; after 3 attempts it falls back to the rule extractor and is marked `fallback`; the drain is never blocked. Test in Task 6.
- **Same entity, different spellings ("Mara", "Mara Velmir", "mara velmir"):** resolves to one entity within an owner; same name in two owners stays two entities; same name with different types is not merged. Test in Task 4.
- **Contradiction ("Mara moved to Osaka" after "based in Lisbon"):** the old fact gets `invalid_at` and is never packed as current. Test in Task 3 and Task 7.
- **Assistant assertion ("You are an architect in Houston"):** stored `unconfirmed`, absent from the grounded block. Test in Task 6 and Task 7.
- **Large owner / empty graph:** traversal fan-out caps keep latency within budget; with no facts, prefetch degrades to today's vector path without error. Test in Task 7.

---

## Phase 0 follow-ups (before Task 1)

The 2026-11-14 spec audit found these against the Phase 0 branch. Each is small, and each lands with a test before Phase 1 starts.

| # | Finding (verified in code) | Change | Test |
|---|---|---|---|
| 0.1 | Prefetch runs the graph walk (~600 ms) but `format_span_injection` never reads `paths`, so the walk never reaches the prompt | Stop calling `_expand_paths` from `_aprefetch` until Task 7 renders facts; keep the method for the pane | Provider test: prefetch does not call `expand_graph`; harness latency p50 drops, recall unchanged |
| 0.2 | `/search` calls `vector_search` without an owner and embeds `q[:800]` while prefetch embeds the enriched, token-capped query | Pass the owner; share one query-embedding helper with prefetch | Unit test: `/search` passes `agent_identity`; both paths embed identical text for the same query |
| 0.3 | Outbox claims, replay dedupe and `owned_turn_ids` are covered only by stubs | Integration tests on a disposable DB: two claimers never get the same row (SKIP LOCKED); a replayed turn with an existing `outbox_id` returns the existing row; `owned_turn_ids` filters by owner | `tests/integration/test_outbox.py` |
| 0.4 | Rows that fail 5 times stay parked in `write_outbox`, invisible | Report `outbox_pending` and `outbox_parked` in `/api/health` and `hermes-memory-verify` | Graph API test asserts both keys |
| 0.5 | `previous_conversation_id` is rebuilt from in-memory `_last_turn_id`; replayed or restarted items lose the `NEXT` link | Resolve the previous turn from the database (same owner and session, highest id below this one) when the in-memory value is absent | Unit test: replayed item with empty `_last_turn_id` links to the stored previous turn |
| 0.6 | Secrets are filtered only at recall; raw content is written to `conversations`, `write_outbox` and the embedding | Redact `SECRET_RE` matches before the outbox record, insert and embed | Unit test: a turn containing a token-shaped secret is stored and embedded redacted |
| 0.7 | Non-interactive uninstall without `--force` raises `EOFError` | Refuse with a clear message when stdin is not a TTY | Uninstall test with closed stdin |

Also correct the harness: names shared by both owners' corpora are excluded from the leakage check (done; the earlier 36 "leaked" answers were 6 shared names).

---

## File Structure

| File | Responsibility |
|---|---|
| `sql/migrations/V17__entity_fact.sql` | `entity`, `entity_alias`, `fact` tables; `conversations.extract_status` |
| `src/hermes_memory/extract_facts.py` | Extraction contract: types, schema, parsing/validation, relation normalization |
| `src/hermes_memory/extractor.py` | `ModelExtractor` (Ollama structured output), `RuleExtractor` fallback, factory |
| `src/hermes_memory/entity_resolve.py` | Name normalization and the pure merge decision |
| `src/hermes_memory/store_facts.py` | `StoreFactsMixin`: entity/alias/fact SQL, extraction queue, fact search, traversal |
| `src/hermes_memory/fusion.py` | RRF fusion and MMR-style dedupe |
| `src/hermes_memory/provider.py` | Extraction worker loop; prefetch integration |
| `src/hermes_memory/provider_helpers.py` | `format_fact_block` |
| `src/hermes_memory/config.py` | Extraction settings |
| `scripts/eval_memory.py` | Fact recall and gate reporting |

---

### Task 1: Schema for entities, aliases, facts and the extraction stage

**Files:**
- Create: `sql/migrations/V17__entity_fact.sql`
- Modify: `src/hermes_memory/schema_guard.py` (`CONSUMER_HEAD_VERSIONS`)
- Test: `tests/test_migration_v17.py`

**Interfaces:**
- Produces: tables `entity(id, owner, name, name_norm, type, name_vec, created_at)`, `entity_alias(id, owner, surface_norm, entity_id, source, created_at)`, `fact(id, owner, subject_id, relation, object_id, object_text, fact_text, fact_vec, span_id, speaker, status, valid_at, invalid_at, extractor, confidence, support, created_at)`; columns `conversations.extract_status`, `conversations.extract_attempts`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migration_v17.py
from pathlib import Path

from hermes_memory.schema_guard import CONSUMER_HEAD_VERSIONS

SQL = Path(__file__).resolve().parents[1] / "sql" / "migrations" / "V17__entity_fact.sql"


def test_v17_is_required_by_installed_plugins():
    assert "V17" in CONSUMER_HEAD_VERSIONS


def test_v17_is_idempotent_and_owner_scoped():
    text = SQL.read_text()
    for table in ("entity", "entity_alias", "fact"):
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in text
    assert text.count("owner TEXT NOT NULL") == 3
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in text
    assert "ADD COLUMN IF NOT EXISTS extract_status" in text
    assert "ON DELETE CASCADE" in text  # deleting a turn removes its facts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_migration_v17.py -v`
Expected: FAIL (`V17` missing; file not found).

- [ ] **Step 3: Write the migration and pin it**

```sql
-- V17__entity_fact.sql — owner-scoped entities and typed facts (graph layer).
-- New tables: noun/aliases/claims stay as-is for the manifold and reception
-- writers until Phase 4. Every row carries its memory owner. Idempotent.
SET search_path = public;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS entity (
    id BIGSERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('person','project','place','org','tool','concept','other')),
    name_vec vector(768),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner, name_norm, type)
);
CREATE INDEX IF NOT EXISTS idx_entity_name_vec ON entity USING hnsw (name_vec vector_cosine_ops);

CREATE TABLE IF NOT EXISTS entity_alias (
    id BIGSERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    surface_norm TEXT NOT NULL,
    entity_id BIGINT NOT NULL REFERENCES entity (id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'extract',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner, surface_norm, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_entity_alias_trgm ON entity_alias USING gin (surface_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_entity_alias_owner ON entity_alias (owner, surface_norm);

CREATE TABLE IF NOT EXISTS fact (
    id BIGSERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    subject_id BIGINT NOT NULL REFERENCES entity (id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    object_id BIGINT REFERENCES entity (id) ON DELETE CASCADE,
    object_text TEXT,
    fact_text TEXT NOT NULL,
    fact_vec vector(768),
    span_id BIGINT NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    speaker TEXT NOT NULL CHECK (speaker IN ('user','assistant')),
    status TEXT NOT NULL CHECK (status IN ('grounded','unconfirmed')),
    valid_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    invalid_at TIMESTAMPTZ,
    extractor TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    support INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (object_id IS NOT NULL OR object_text IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_live_key
    ON fact (owner, subject_id, relation, coalesce(object_id::text, lower(object_text)))
    WHERE invalid_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_fact_subject ON fact (owner, subject_id) WHERE invalid_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_fact_object ON fact (owner, object_id) WHERE invalid_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_fact_vec ON fact USING hnsw (fact_vec vector_cosine_ops);

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS extract_status TEXT
    CHECK (extract_status IN ('pending','done','skipped','fallback','failed'));
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS extract_attempts INT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_conversations_extract_pending
    ON conversations (id) WHERE extract_status = 'pending';
```

In `src/hermes_memory/schema_guard.py`:

```python
CONSUMER_HEAD_VERSIONS = ("V1", "V9", "V10", "V11", "V16", "V17")
```

- [ ] **Step 4: Run tests and apply to a disposable database**

Run: `pytest tests/test_migration_v17.py -v` → PASS
Run: `python scripts/migrate.py --dsn "$EVAL_DSN"` twice → second run reports `0 applied`.

- [ ] **Step 5: Commit**

```bash
git add sql/migrations/V17__entity_fact.sql src/hermes_memory/schema_guard.py tests/test_migration_v17.py
git commit -m "feat(graph): V17 owner-scoped entity, alias and fact tables"
```

---

### Task 2: Extraction contract (types, parsing, relation normalization)

**Files:**
- Create: `src/hermes_memory/extract_facts.py`
- Test: `tests/test_extract_facts.py`

**Interfaces:**
- Produces:
  - `ENTITY_TYPES: tuple[str, ...]` = `("person","project","place","org","tool","concept","other")`
  - `FUNCTIONAL_RELATIONS: frozenset[str]` (one current value per subject): `based_in`, `owned_by`, `works_for`, `lives_in`, `role_is`, `status_is`
  - `@dataclass(frozen=True) ExtractedEntity(name: str, type: str)`
  - `@dataclass(frozen=True) ExtractedFact(subject: str, relation: str, object: str, fact_text: str)`
  - `@dataclass(frozen=True) Extraction(entities: tuple[ExtractedEntity, ...], facts: tuple[ExtractedFact, ...])`
  - `normalize_relation(text: str) -> str`
  - `parse_extraction(raw: str | dict) -> Extraction` (raises `ValueError` on malformed input)
  - `EXTRACTION_SCHEMA: dict` (JSON schema for structured output)
  - `build_messages(text: str, context: list[str]) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_extract_facts.py
import json

import pytest
from hypothesis import given, strategies as st

from hermes_memory.extract_facts import (
    ENTITY_TYPES, Extraction, build_messages, normalize_relation, parse_extraction,
)


def test_relation_phrases_normalize_to_snake_case_verbs():
    assert normalize_relation("is owned by") == "owned_by"
    assert normalize_relation("Based In") == "based_in"
    assert normalize_relation("works for") == "works_for"
    assert normalize_relation("handles") == "handles"


@given(st.text(min_size=1, max_size=40))
def test_normalized_relation_is_snake_case_or_empty(text):
    out = normalize_relation(text)
    assert out == "" or all(c.islower() or c.isdigit() or c == "_" for c in out)


def test_parse_keeps_facts_whose_subject_is_an_entity():
    raw = json.dumps({
        "entities": [{"name": "Quarzon", "type": "project"}, {"name": "Mara Velmir", "type": "person"}],
        "facts": [
            {"subject": "Quarzon", "relation": "owned by", "object": "Mara Velmir"},
            {"subject": "Nobody", "relation": "likes", "object": "tea"},
        ],
    })
    ex = parse_extraction(raw)
    assert isinstance(ex, Extraction)
    assert [(f.subject, f.relation, f.object) for f in ex.facts] == [("Quarzon", "owned_by", "Mara Velmir")]
    assert ex.facts[0].fact_text == "Quarzon owned by Mara Velmir"


def test_parse_maps_unknown_types_to_other_and_drops_blank_names():
    ex = parse_extraction({"entities": [{"name": "X", "type": "Company/Brand"}, {"name": " ", "type": "person"}],
                           "facts": []})
    assert [(e.name, e.type) for e in ex.entities] == [("X", "other")]


def test_parse_rejects_malformed_payloads():
    with pytest.raises(ValueError):
        parse_extraction("not json")
    with pytest.raises(ValueError):
        parse_extraction({"entities": "nope", "facts": []})


def test_messages_carry_context_then_the_turn():
    msgs = build_messages("Mara moved to Osaka.", ["Mara Velmir is based in Lisbon."])
    assert msgs[0]["role"] == "system" and "person" in msgs[0]["content"]
    assert "Mara Velmir is based in Lisbon." in msgs[1]["content"]
    assert msgs[1]["content"].rstrip().endswith("Mara moved to Osaka.")
    assert set(ENTITY_TYPES) >= {"person", "project", "place"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_extract_facts.py -v`
Expected: FAIL (`ModuleNotFoundError: hermes_memory.extract_facts`).

- [ ] **Step 3: Implement**

```python
# src/hermes_memory/extract_facts.py
"""Extraction contract: what a model must return for one turn, and how it is validated."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

ENTITY_TYPES: tuple[str, ...] = ("person", "project", "place", "org", "tool", "concept", "other")
FUNCTIONAL_RELATIONS = frozenset({"based_in", "owned_by", "works_for", "lives_in", "role_is", "status_is"})
_STOP = {"is", "are", "was", "were", "the", "a", "an"}

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entities": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "type": {"type": "string", "enum": list(ENTITY_TYPES)}},
            "required": ["name", "type"]}},
        "facts": {"type": "array", "items": {"type": "object", "properties": {
            "subject": {"type": "string"}, "relation": {"type": "string"}, "object": {"type": "string"}},
            "required": ["subject", "relation", "object"]}},
    },
    "required": ["entities", "facts"],
}

SYSTEM_PROMPT = (
    "Extract named entities and factual relations stated in the LAST message only. "
    "Entity types: person, project, place, org, tool, concept, other. "
    "A project is a named piece of work, product or codebase. "
    "Facts are (subject, relation, object) where subject is one of the entities. "
    "Use short relations such as 'owned by', 'based in', 'works for', 'uses'. "
    "Do not infer facts that are not stated. Return JSON only."
)


@dataclass(frozen=True)
class ExtractedEntity:
    name: str
    type: str


@dataclass(frozen=True)
class ExtractedFact:
    subject: str
    relation: str
    object: str
    fact_text: str


@dataclass(frozen=True)
class Extraction:
    entities: tuple[ExtractedEntity, ...]
    facts: tuple[ExtractedFact, ...]


def normalize_relation(text: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP]
    return "_".join(words)


def parse_extraction(raw: str | dict) -> Extraction:
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as exc:
        raise ValueError(f"extraction is not JSON: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("entities"), list) or not isinstance(data.get("facts"), list):
        raise ValueError("extraction must have list fields 'entities' and 'facts'")
    entities: list[ExtractedEntity] = []
    names: set[str] = set()
    for e in data["entities"]:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        if not name:
            continue
        etype = str(e.get("type") or "").strip().lower()
        entities.append(ExtractedEntity(name, etype if etype in ENTITY_TYPES else "other"))
        names.add(name.lower())
    facts: list[ExtractedFact] = []
    for f in data["facts"]:
        if not isinstance(f, dict):
            continue
        subject = str(f.get("subject") or "").strip()
        obj = str(f.get("object") or "").strip()
        relation = normalize_relation(str(f.get("relation") or ""))
        if not subject or not obj or not relation or subject.lower() not in names:
            continue
        facts.append(ExtractedFact(subject, relation, obj, f"{subject} {relation.replace('_', ' ')} {obj}"))
    return Extraction(tuple(entities), tuple(facts))


def build_messages(text: str, context: list[str]) -> list[dict]:
    earlier = "\n".join(f"- {c}" for c in context if c)
    body = (f"Earlier messages (context only):\n{earlier}\n\n" if earlier else "") + f"LAST message:\n{text}"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": body}]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_extract_facts.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/hermes_memory/extract_facts.py tests/test_extract_facts.py
git commit -m "feat(graph): extraction contract with relation normalization"
```

---

### Task 3: Model extractor, rule fallback, and configuration

**Files:**
- Create: `src/hermes_memory/extractor.py`
- Modify: `src/hermes_memory/config.py` (fields `extract_enabled: bool = True`, `extract_model: str = "llama3.2:3b"`, `extract_context_turns: int = 2`, `extract_timeout_s: float = 60.0`, `extract_max_attempts: int = 3`; add each name to the YAML copy loop alongside `vector_k`)
- Test: `tests/test_extractor.py`

**Interfaces:**
- Consumes: `build_messages`, `parse_extraction`, `EXTRACTION_SCHEMA`, `Extraction`, `ExtractedEntity` (Task 2); `extract_nouns` (existing).
- Produces:
  - `class ModelExtractor: name: str; async def extract(self, text: str, context: list[str]) -> Extraction`
  - `class RuleExtractor: name = "rules"; async def extract(self, text: str, context: list[str]) -> Extraction` (entities only, no facts)
  - `def extractor_from_config(cfg: HybridAgeConfig) -> ModelExtractor | RuleExtractor`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_extractor.py
import json
from types import SimpleNamespace

import pytest

from hermes_memory.config import HybridAgeConfig
from hermes_memory.extractor import ModelExtractor, RuleExtractor, extractor_from_config


class FakeCompletions:
    def __init__(self, content):
        self.content, self.calls = content, []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))])


def _client(content):
    return SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(content)))


async def test_model_extractor_requests_structured_json_and_parses_it():
    payload = {"entities": [{"name": "Tobin Kalost", "type": "person"}, {"name": "Osaka", "type": "place"}],
               "facts": [{"subject": "Tobin Kalost", "relation": "based in", "object": "Osaka"}]}
    client = _client(json.dumps(payload))
    ex = await ModelExtractor(client, "llama3.2:3b", timeout_s=5).extract("Tobin Kalost is based in Osaka.", [])
    call = client.chat.completions.calls[0]
    assert call["model"] == "llama3.2:3b" and call["temperature"] == 0
    assert call["response_format"]["type"] == "json_schema"
    assert [(f.subject, f.relation, f.object) for f in ex.facts] == [("Tobin Kalost", "based_in", "Osaka")]


async def test_model_extractor_raises_on_invalid_json_so_the_turn_is_retried():
    with pytest.raises(ValueError):
        await ModelExtractor(_client("not json"), "m", timeout_s=5).extract("x", [])


async def test_rule_extractor_returns_entities_without_facts():
    ex = await RuleExtractor().extract("We migrated Kitchen Kontrol to Postgres last week.", [])
    assert ex.facts == ()
    assert any(e.name.lower() == "postgres" for e in ex.entities)


def test_factory_honors_extract_enabled():
    assert isinstance(extractor_from_config(HybridAgeConfig(extract_enabled=False)), RuleExtractor)
    assert isinstance(extractor_from_config(HybridAgeConfig()), ModelExtractor)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_extractor.py -v`
Expected: FAIL (`ModuleNotFoundError: hermes_memory.extractor`).

- [ ] **Step 3: Implement**

```python
# src/hermes_memory/extractor.py
"""Entity/relation extractors. The model extractor runs in the extraction stage only."""
from __future__ import annotations

import asyncio
import os
from typing import Any

from .extract_facts import EXTRACTION_SCHEMA, ExtractedEntity, Extraction, build_messages, parse_extraction


class ModelExtractor:
    def __init__(self, client: Any, model: str, *, timeout_s: float) -> None:
        self.client, self.model, self.timeout_s = client, model, timeout_s
        self.name = f"model:{model}"

    async def extract(self, text: str, context: list[str]) -> Extraction:
        resp = await asyncio.wait_for(
            self.client.chat.completions.create(
                model=self.model,
                messages=build_messages(text, context),
                temperature=0,
                response_format={"type": "json_schema",
                                 "json_schema": {"name": "extraction", "schema": EXTRACTION_SCHEMA}},
            ),
            timeout=self.timeout_s,
        )
        return parse_extraction(resp.choices[0].message.content or "")


class RuleExtractor:
    name = "rules"

    async def extract(self, text: str, context: list[str]) -> Extraction:
        from .extract_nouns import extract_nouns

        nouns = extract_nouns(text)
        return Extraction(tuple(ExtractedEntity(n.label, "other") for n in nouns), ())


def extractor_from_config(cfg) -> ModelExtractor | RuleExtractor:
    if not getattr(cfg, "extract_enabled", True):
        return RuleExtractor()
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=cfg.embed_url, api_key=os.environ.get("HYBRID_AGE_EMBED_KEY", "ollama"))
    return ModelExtractor(client, cfg.extract_model, timeout_s=cfg.extract_timeout_s)
```

Before implementing `RuleExtractor`, confirm the attribute name returned by `extract_nouns` (`grep -n "class .*Noun\|def extract_nouns" src/hermes_memory/extract_nouns.py`) and use it in place of `n.label` if it differs.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_extractor.py tests/test_config*.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/hermes_memory/extractor.py src/hermes_memory/config.py tests/test_extractor.py
git commit -m "feat(graph): model extractor with structured output and rule fallback"
```

---

### Task 4: Entity resolution and fact storage

**Files:**
- Create: `src/hermes_memory/entity_resolve.py`, `src/hermes_memory/store_facts.py`
- Modify: `src/hermes_memory/store.py` (`class Store(StoreFactsMixin, StoreExpandMixin, StoreMergeMixin, StoreConceptsMixin)`)
- Test: `tests/test_entity_resolve.py`, `tests/integration/test_store_facts.py`

**Interfaces:**
- Consumes: V17 tables (Task 1); `ExtractedEntity`, `ExtractedFact`, `FUNCTIONAL_RELATIONS` (Task 2).
- Produces:
  - `norm_name(name: str) -> str`
  - `choose_entity(name_norm: str, etype: str, candidates: list[dict]) -> int | None` — candidates have `id`, `name_norm`, `type`, `trgm` (0..1), `cos` (0..1 or None)
  - `StoreFactsMixin`:
    - `async def entity_candidates(self, owner: str, name_norm: str, name_vec: str | None, limit: int = 8) -> list[dict]`
    - `async def entity_create(self, owner: str, name: str, name_norm: str, etype: str, name_vec: str | None) -> int`
    - `async def alias_add(self, owner: str, surface_norm: str, entity_id: int) -> None`
    - `async def fact_upsert(self, owner: str, subject_id: int, relation: str, object_id: int | None, object_text: str | None, fact_text: str, fact_vec: str | None, span_id: int, speaker: str, extractor: str, confidence: float) -> int` — same live key increments `support`; a functional relation with a different object sets `invalid_at = now()` on the old live fact first.

- [ ] **Step 1: Write the failing unit tests (pure decision)**

```python
# tests/test_entity_resolve.py
from hermes_memory.entity_resolve import choose_entity, norm_name


def test_norm_name_folds_case_space_and_punctuation():
    assert norm_name("  Mara  Velmir. ") == "mara velmir"


def test_exact_alias_wins():
    c = [{"id": 7, "name_norm": "mara velmir", "type": "person", "trgm": 1.0, "cos": 0.9}]
    assert choose_entity("mara velmir", "person", c) == 7


def test_partial_name_merges_only_with_strong_trigram_and_vector_and_same_type():
    c = [{"id": 7, "name_norm": "mara velmir", "type": "person", "trgm": 0.62, "cos": 0.91}]
    assert choose_entity("mara velmira", "person", c) == 7
    assert choose_entity("mara velmira", "place", c) is None


def test_weak_matches_create_a_new_entity():
    c = [{"id": 7, "name_norm": "mara velmir", "type": "person", "trgm": 0.35, "cos": 0.7}]
    assert choose_entity("tobin kalost", "person", c) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_entity_resolve.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement the decision and the store mixin**

```python
# src/hermes_memory/entity_resolve.py
"""Entity resolution: exact alias, else strong trigram + vector agreement within one type."""
from __future__ import annotations

import re

TRGM_MIN = 0.55
COS_MIN = 0.85


def norm_name(name: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (name or "").lower()))


def choose_entity(name_norm: str, etype: str, candidates: list[dict]) -> int | None:
    same_type = [c for c in candidates if c["type"] == etype]
    for c in same_type:
        if c["name_norm"] == name_norm:
            return int(c["id"])
    best = None
    for c in same_type:
        cos = c.get("cos")
        if c["trgm"] >= TRGM_MIN and cos is not None and cos >= COS_MIN:
            if best is None or (c["trgm"], cos) > (best["trgm"], best["cos"]):
                best = c
    return int(best["id"]) if best else None
```

```python
# src/hermes_memory/store_facts.py
"""Owner-scoped entities, aliases and typed facts (V17). Every query filters on owner."""
from __future__ import annotations

from typing import Any

from .extract_facts import FUNCTIONAL_RELATIONS


class StoreFactsMixin:
    pool: Any

    async def entity_candidates(self, owner: str, name_norm: str, name_vec: str | None, limit: int = 8) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT e.id, e.name_norm, e.type,
                       max(similarity(a.surface_norm, $2)) AS trgm,
                       CASE WHEN $3::vector IS NULL OR e.name_vec IS NULL THEN NULL
                            ELSE 1 - (e.name_vec <=> $3::vector) END AS cos
                  FROM entity e JOIN entity_alias a ON a.entity_id = e.id AND a.owner = e.owner
                 WHERE e.owner = $1 AND (a.surface_norm = $2 OR a.surface_norm % $2)
                 GROUP BY e.id
                 ORDER BY trgm DESC
                 LIMIT $4
                """,
                owner, name_norm, name_vec, limit,
            )
        return [dict(r) for r in rows]

    async def entity_create(self, owner: str, name: str, name_norm: str, etype: str, name_vec: str | None) -> int:
        async with self.pool.acquire() as conn:
            entity_id = await conn.fetchval(
                """
                INSERT INTO entity (owner, name, name_norm, type, name_vec)
                VALUES ($1, $2, $3, $4, $5::vector)
                ON CONFLICT (owner, name_norm, type) DO UPDATE SET name = entity.name
                RETURNING id
                """,
                owner, name, name_norm, etype, name_vec,
            )
        await self.alias_add(owner, name_norm, int(entity_id))
        return int(entity_id)

    async def alias_add(self, owner: str, surface_norm: str, entity_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO entity_alias (owner, surface_norm, entity_id) VALUES ($1, $2, $3) "
                "ON CONFLICT (owner, surface_norm, entity_id) DO NOTHING",
                owner, surface_norm, entity_id,
            )

    async def fact_upsert(self, owner: str, subject_id: int, relation: str, object_id: int | None,
                          object_text: str | None, fact_text: str, fact_vec: str | None, span_id: int,
                          speaker: str, extractor: str, confidence: float) -> int:
        status = "grounded" if speaker == "user" else "unconfirmed"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if relation in FUNCTIONAL_RELATIONS and status == "grounded":
                    await conn.execute(
                        """
                        UPDATE fact SET invalid_at = now()
                         WHERE owner = $1 AND subject_id = $2 AND relation = $3 AND invalid_at IS NULL
                           AND status = 'grounded'
                           AND coalesce(object_id::text, lower(object_text)) IS DISTINCT FROM
                               coalesce($4::bigint::text, lower($5))
                        """,
                        owner, subject_id, relation, object_id, object_text,
                    )
                fact_id = await conn.fetchval(
                    """
                    INSERT INTO fact (owner, subject_id, relation, object_id, object_text, fact_text, fact_vec,
                                      span_id, speaker, status, extractor, confidence)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::vector, $8, $9, $10, $11, $12)
                    ON CONFLICT (owner, subject_id, relation, coalesce(object_id::text, lower(object_text)))
                        WHERE invalid_at IS NULL
                    DO UPDATE SET support = fact.support + 1,
                                  status = CASE WHEN EXCLUDED.status = 'grounded' THEN 'grounded' ELSE fact.status END
                    RETURNING id
                    """,
                    owner, subject_id, relation, object_id, object_text, fact_text, fact_vec,
                    span_id, speaker, status, extractor, confidence,
                )
        return int(fact_id)
```

- [ ] **Step 4: Write the integration tests (disposable database)**

```python
# tests/integration/test_store_facts.py
import pytest

pytestmark = pytest.mark.integration


async def test_same_name_two_owners_stays_two_entities(store, turn_id):
    a = await store.entity_create("alice", "Mara Velmir", "mara velmir", "person", None)
    b = await store.entity_create("bob", "Mara Velmir", "mara velmir", "person", None)
    assert a != b
    assert [c["id"] for c in await store.entity_candidates("alice", "mara velmir", None)] == [a]


async def test_functional_relation_supersedes_and_duplicates_add_support(store, turn_id):
    mara = await store.entity_create("alice", "Mara Velmir", "mara velmir", "person", None)
    lisbon = await store.entity_create("alice", "Lisbon", "lisbon", "place", None)
    osaka = await store.entity_create("alice", "Osaka", "osaka", "place", None)
    f1 = await store.fact_upsert("alice", mara, "based_in", lisbon, None, "Mara based in Lisbon", None,
                                 turn_id, "user", "test", 1.0)
    again = await store.fact_upsert("alice", mara, "based_in", lisbon, None, "Mara based in Lisbon", None,
                                    turn_id, "user", "test", 1.0)
    assert again == f1
    await store.fact_upsert("alice", mara, "based_in", osaka, None, "Mara based in Osaka", None,
                            turn_id, "user", "test", 1.0)
    async with store.pool.acquire() as conn:
        live = await conn.fetch("select object_id from fact where owner='alice' and invalid_at is null")
    assert [r["object_id"] for r in live] == [osaka]


async def test_assistant_facts_are_unconfirmed_and_never_supersede(store, turn_id):
    mara = await store.entity_create("alice", "Mara Velmir", "mara velmir", "person", None)
    await store.fact_upsert("alice", mara, "based_in", None, "Lisbon", "Mara based in Lisbon", None,
                            turn_id, "user", "test", 1.0)
    await store.fact_upsert("alice", mara, "based_in", None, "Houston", "Mara based in Houston", None,
                            turn_id, "assistant", "test", 1.0)
    async with store.pool.acquire() as conn:
        rows = await conn.fetch("select object_text, status from fact where owner='alice' and invalid_at is null "
                                "order by id")
    assert [(r["object_text"], r["status"]) for r in rows] == [("Lisbon", "grounded"), ("Houston", "unconfirmed")]
```

Add `store` and `turn_id` fixtures to `tests/integration/conftest.py`: `store` connects to `$EVAL_DSN` (refuse unless the database name ends in `_eval`), truncates `fact, entity_alias, entity` before each test; `turn_id` inserts one `conversations` row for owner `alice` and returns its id.

- [ ] **Step 5: Run and commit**

Run: `pytest tests/test_entity_resolve.py -v` → PASS
Run: `EVAL_DSN=... pytest -m integration tests/integration/test_store_facts.py -v` → PASS

```bash
git add src/hermes_memory/entity_resolve.py src/hermes_memory/store_facts.py src/hermes_memory/store.py \
        tests/test_entity_resolve.py tests/integration/test_store_facts.py tests/integration/conftest.py
git commit -m "feat(graph): owner-scoped entity resolution and fact storage with supersession"
```

---

### Task 5: Mark turns for extraction on write

**Files:**
- Modify: `src/hermes_memory/provider.py` (`_awrite_turn`), `src/hermes_memory/store.py` (`insert_turn`)
- Test: `tests/test_extract_marking.py`

**Interfaces:**
- Consumes: `conversations.extract_status` (Task 1); `_is_noise` (existing `turn_filter`).
- Produces: every newly inserted turn has `extract_status = 'pending'`, or `'skipped'` when `_is_noise(content)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_extract_marking.py
from hermes_memory.store import extract_status_for


def test_substantive_turns_are_pending_and_noise_is_skipped():
    assert extract_status_for("Quarzon is owned by Mara Velmir and runs on Postgres.") == "pending"
    assert extract_status_for("ok thanks") == "skipped"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_extract_marking.py -v` → FAIL (`ImportError`).

- [ ] **Step 3: Implement**

In `src/hermes_memory/store.py`, module level:

```python
def extract_status_for(content: str) -> str:
    from .turn_filter import _is_noise

    return "skipped" if _is_noise(content or "") else "pending"
```

In `insert_turn`, add `extract_status` to the `INSERT INTO conversations (...)` column list and pass `extract_status_for(content)` as a new bind parameter `$9`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_extract_marking.py tests/test_store.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/hermes_memory/store.py tests/test_extract_marking.py
git commit -m "feat(graph): queue substantive turns for extraction"
```

---

### Task 6: Durable extraction worker

**Files:**
- Modify: `src/hermes_memory/store_facts.py`, `src/hermes_memory/provider.py`
- Test: `tests/test_extraction_worker.py`

**Interfaces:**
- Consumes: `extractor_from_config` (Task 3), `norm_name`, `choose_entity` (Task 4), `StoreFactsMixin` methods (Task 4), `Embedder.embed_text` (existing).
- Produces:
  - `StoreFactsMixin.claim_extraction_batch(self, limit: int, max_attempts: int) -> list[dict]` — rows `id, agent_identity, role, content, session_id`; `FOR UPDATE SKIP LOCKED`, increments `extract_attempts`.
  - `StoreFactsMixin.recent_turns(self, owner: str, session_id: str, before_id: int, n: int) -> list[str]`
  - `StoreFactsMixin.mark_extraction(self, conv_id: int, status: str) -> None`
  - `HybridAgeMemoryProvider._extract_one(self, row: dict) -> str` (returns the status written)
  - `HybridAgeMemoryProvider._extraction_loop(self) -> None` started in `_ainit`, cancelled in `shutdown` like `_outbox_task`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_extraction_worker.py
from hermes_memory.config import HybridAgeConfig
from hermes_memory.extract_facts import ExtractedEntity, ExtractedFact, Extraction
from hermes_memory.provider import HybridAgeMemoryProvider


class Store:
    def __init__(self):
        self.entities, self.facts, self.status = {}, [], {}

    async def recent_turns(self, owner, session_id, before_id, n):
        return ["Earlier context."]

    async def entity_candidates(self, owner, name_norm, name_vec, limit=8):
        eid = self.entities.get((owner, name_norm))
        return [{"id": eid, "name_norm": name_norm, "type": "person", "trgm": 1.0, "cos": 1.0}] if eid else []

    async def entity_create(self, owner, name, name_norm, etype, name_vec):
        self.entities[(owner, name_norm)] = len(self.entities) + 1
        return self.entities[(owner, name_norm)]

    async def alias_add(self, owner, surface_norm, entity_id):
        pass

    async def fact_upsert(self, owner, subject_id, relation, object_id, object_text, fact_text, fact_vec,
                          span_id, speaker, extractor, confidence):
        self.facts.append((owner, relation, speaker, object_id, object_text))
        return len(self.facts)

    async def mark_extraction(self, conv_id, status):
        self.status[conv_id] = status


class Extractor:
    name = "fake"

    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    async def extract(self, text, context):
        if self.error:
            raise self.error
        return self.result


class Embedder:
    async def embed_text(self, text):
        return [0.0, 1.0]


def _provider(extractor, attempts_limit=3):
    p = HybridAgeMemoryProvider.__new__(HybridAgeMemoryProvider)
    p.store, p.embedder, p._extractor = Store(), Embedder(), extractor
    p.config = HybridAgeConfig(embed_dim=2, extract_max_attempts=attempts_limit)
    return p


EX = Extraction(
    (ExtractedEntity("Tobin Kalost", "person"), ExtractedEntity("Osaka", "place")),
    (ExtractedFact("Tobin Kalost", "based_in", "Osaka", "Tobin Kalost based in Osaka"),),
)
ROW = {"id": 5, "agent_identity": "alice", "role": "user", "content": "Tobin Kalost is based in Osaka.",
       "session_id": "s", "extract_attempts": 1}


async def test_user_turn_yields_grounded_owner_scoped_fact_and_done():
    p = _provider(Extractor(EX))
    assert await p._extract_one(ROW) == "done"
    assert p.store.facts == [("alice", "based_in", "user", 2, None)]
    assert p.store.status[5] == "done"


async def test_assistant_turn_facts_are_marked_with_assistant_speaker():
    p = _provider(Extractor(EX))
    await p._extract_one({**ROW, "role": "assistant"})
    assert p.store.facts[0][2] == "assistant"


async def test_model_failure_leaves_pending_until_attempts_run_out_then_falls_back():
    p = _provider(Extractor(error=ValueError("bad json")))
    assert await p._extract_one(ROW) == "pending"
    assert await p._extract_one({**ROW, "extract_attempts": 3}) == "fallback"
    assert p.store.status[5] == "fallback"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_extraction_worker.py -v` → FAIL (`_extract_one` missing).

- [ ] **Step 3: Implement the store queue methods**

```python
# append to StoreFactsMixin in src/hermes_memory/store_facts.py
    async def claim_extraction_batch(self, limit: int, max_attempts: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE conversations c SET extract_attempts = c.extract_attempts + 1
                 WHERE c.id IN (SELECT id FROM conversations
                                 WHERE extract_status = 'pending' AND extract_attempts < $2
                                 ORDER BY id LIMIT $1 FOR UPDATE SKIP LOCKED)
                RETURNING c.id, c.agent_identity, c.role, c.content, c.session_id, c.extract_attempts
                """,
                limit, max_attempts,
            )
        return [dict(r) for r in rows]

    async def recent_turns(self, owner: str, session_id: str, before_id: int, n: int) -> list[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT content FROM conversations WHERE agent_identity = $1 AND session_id = $2 AND id < $3 "
                "ORDER BY id DESC LIMIT $4",
                owner, session_id, before_id, n,
            )
        return [r["content"] for r in reversed(rows)]

    async def mark_extraction(self, conv_id: int, status: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE conversations SET extract_status = $2 WHERE id = $1", conv_id, status)
```

- [ ] **Step 4: Implement the worker in the provider**

```python
    async def _extract_one(self, row: dict) -> str:
        """Extract, resolve and store facts for one turn. Returns the status written."""
        from .embed import vec_to_literal
        from .entity_resolve import choose_entity, norm_name
        from .extractor import RuleExtractor

        owner = row["agent_identity"] or "default"
        speaker = "assistant" if row["role"] == "assistant" else "user"
        context = await self.store.recent_turns(owner, row["session_id"] or "", row["id"],
                                                self.config.extract_context_turns)
        extractor = self._extractor
        status = "done"
        try:
            extraction = await extractor.extract(row["content"], context)
        except Exception:
            logger.warning("extraction failed conv=%s attempt=%s", row["id"], row.get("extract_attempts"),
                           exc_info=True)
            if int(row.get("extract_attempts") or 0) < self.config.extract_max_attempts:
                return "pending"  # stays pending; claimed again later
            extractor, status = RuleExtractor(), "fallback"
            extraction = await extractor.extract(row["content"], context)

        ids: dict[str, int] = {}
        for ent in extraction.entities:
            name_norm = norm_name(ent.name)
            if not name_norm:
                continue
            vec = await self.embedder.embed_text(ent.name)
            vec_lit = vec_to_literal(vec) if vec else None
            found = choose_entity(name_norm, ent.type, await self.store.entity_candidates(owner, name_norm, vec_lit))
            entity_id = found if found is not None else await self.store.entity_create(
                owner, ent.name, name_norm, ent.type, vec_lit)
            await self.store.alias_add(owner, name_norm, entity_id)
            ids[ent.name.lower()] = entity_id
        for fact in extraction.facts:
            subject_id = ids.get(fact.subject.lower())
            if subject_id is None:
                continue
            object_id = ids.get(fact.object.lower())
            vec = await self.embedder.embed_text(fact.fact_text)
            await self.store.fact_upsert(
                owner, subject_id, fact.relation, object_id, None if object_id else fact.object,
                fact.fact_text, vec_to_literal(vec) if vec else None, row["id"], speaker,
                extractor.name, 1.0)
        await self.store.mark_extraction(row["id"], status)
        return status

    async def _extraction_loop(self) -> None:
        while True:
            try:
                rows = await self.store.claim_extraction_batch(8, self.config.extract_max_attempts)
                for row in rows:
                    await self._extract_one(row)
                if not rows:
                    await asyncio.sleep(EXTRACT_IDLE_S)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("extraction loop error", exc_info=True)
                await asyncio.sleep(EXTRACT_IDLE_S)
```

Add `EXTRACT_IDLE_S = 5.0` beside `OUTBOX_REPLAY_INTERVAL_S`. In `_ainit`, after the outbox task: `self._extractor = extractor_from_config(self.config)` and `self._extract_task = asyncio.create_task(self._extraction_loop())`. In `shutdown`, cancel `_extract_task` the same way as `_outbox_task`. A turn left `pending` (claimed but not finished at shutdown) is claimed again on the next start because `extract_status` is still `pending`.

- [ ] **Step 5: Run tests and commit**

Run: `pytest tests/test_extraction_worker.py tests/test_write_outbox.py -v` → PASS

```bash
git add src/hermes_memory/provider.py src/hermes_memory/store_facts.py tests/test_extraction_worker.py
git commit -m "feat(graph): durable extraction worker with retry and rule fallback"
```

---

### Task 7: Hybrid retrieval — fact search, entity linking, bounded traversal, fusion, packing

**Files:**
- Create: `src/hermes_memory/fusion.py`
- Modify: `src/hermes_memory/store_facts.py`, `src/hermes_memory/provider.py` (`_aprefetch`), `src/hermes_memory/provider_helpers.py`
- Test: `tests/test_fusion.py`, `tests/test_fact_prefetch.py`, `tests/integration/test_traverse_facts.py`

**Interfaces:**
- Consumes: V17 tables, `norm_name` (Task 4).
- Produces:
  - `rrf(lists: list[list[str]], k: int = 60) -> list[tuple[str, float]]` — keys are stable ids such as `fact:12`, `conv:41`
  - `dedupe_by_text(items: list[dict], key: str = "text", threshold: float = 0.9) -> list[dict]`
  - `StoreFactsMixin.fact_search(self, owner: str, q_vec: str, k: int) -> list[dict]` — live facts, rows `id, fact_text, status, span_id, subject_id, object_id, valid_at, sim`
  - `StoreFactsMixin.link_entities(self, owner: str, surfaces: list[str], limit: int = 8) -> list[int]`
  - `StoreFactsMixin.traverse_facts(self, owner: str, seed_ids: list[int], q_vec: str, hops: int = 2, fanout: int = 8, limit: int = 24) -> list[dict]` — same row shape as `fact_search` plus `hop`
  - `format_fact_block(facts: list[dict]) -> str`

- [ ] **Step 1: Write the failing pure tests**

```python
# tests/test_fusion.py
from hermes_memory.fusion import dedupe_by_text, rrf


def test_rrf_rewards_items_found_by_several_lists():
    fused = rrf([["fact:1", "conv:9"], ["fact:2", "fact:1"]])
    assert fused[0][0] == "fact:1"


def test_dedupe_drops_near_identical_text():
    items = [{"text": "Mara based in Lisbon"}, {"text": "Mara based in Lisbon."}, {"text": "Quarzon owned by Mara"}]
    assert [i["text"] for i in dedupe_by_text(items)] == ["Mara based in Lisbon", "Quarzon owned by Mara"]
```

```python
# tests/test_fact_prefetch.py
from hermes_memory.provider_helpers import format_fact_block


def test_fact_block_lists_grounded_facts_with_span_ids_and_never_unconfirmed():
    block = format_fact_block([
        {"id": 1, "fact_text": "Quarzon owned by Mara Velmir", "status": "grounded", "span_id": 52,
         "valid_at": "2026-11-14"},
        {"id": 2, "fact_text": "Ray based in Houston", "status": "unconfirmed", "span_id": 58,
         "valid_at": "2026-11-14"},
    ])
    assert "Quarzon owned by Mara Velmir" in block and 'span="52"' in block
    assert "Houston" not in block
    assert block.startswith("<facts spans=\"52\">")
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_fusion.py tests/test_fact_prefetch.py -v` → FAIL (imports missing).

- [ ] **Step 3: Implement fusion and the block formatter**

```python
# src/hermes_memory/fusion.py
"""Reciprocal rank fusion and near-duplicate removal for prefetch candidates."""
from __future__ import annotations

from difflib import SequenceMatcher


def rrf(lists: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranked in lists:
        for rank, key in enumerate(ranked):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def dedupe_by_text(items: list[dict], key: str = "text", threshold: float = 0.9) -> list[dict]:
    kept: list[dict] = []
    for item in items:
        text = str(item.get(key) or "").strip().rstrip(".").lower()
        if any(SequenceMatcher(None, text, str(k.get(key) or "").strip().rstrip(".").lower()).ratio() >= threshold
               for k in kept):
            continue
        kept.append(item)
    return kept
```

```python
# add to src/hermes_memory/provider_helpers.py
def format_fact_block(facts: list[dict]) -> str:
    """Grounded facts only, each with the span it came from. The header lists every span
    id in the block so an agent's citation can be checked against what it was given."""
    from html import escape

    grounded = [f for f in facts if f.get("status") == "grounded"]
    if not grounded:
        return ""
    spans = ",".join(str(f["span_id"]) for f in grounded)
    lines = [f'  <fact span="{f["span_id"]}" since="{str(f.get("valid_at") or "")[:10]}">'
             f'{escape(str(f["fact_text"]))}</fact>' for f in grounded]
    return f'<facts spans="{spans}">\n' + "\n".join(lines) + "\n</facts>"
```

- [ ] **Step 4: Implement the store reads**

```python
# append to StoreFactsMixin
    async def fact_search(self, owner: str, q_vec: str, k: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, fact_text, status, span_id, subject_id, object_id, valid_at,
                       1 - (fact_vec <=> $2::vector) AS sim
                  FROM fact
                 WHERE owner = $1 AND invalid_at IS NULL AND fact_vec IS NOT NULL
                 ORDER BY fact_vec <=> $2::vector
                 LIMIT $3
                """,
                owner, q_vec, k,
            )
        return [dict(r) for r in rows]

    async def link_entities(self, owner: str, surfaces: list[str], limit: int = 8) -> list[int]:
        wanted = [s for s in surfaces if s]
        if not wanted:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT entity_id FROM entity_alias
                 WHERE owner = $1 AND (surface_norm = ANY($2::text[]) OR surface_norm % ANY($2::text[]))
                 LIMIT $3
                """,
                owner, wanted, limit,
            )
        return [int(r["entity_id"]) for r in rows]

    async def traverse_facts(self, owner: str, seed_ids: list[int], q_vec: str, hops: int = 2,
                             fanout: int = 8, limit: int = 24) -> list[dict]:
        if not seed_ids:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH RECURSIVE walk(entity_id, hop) AS (
                    SELECT unnest($2::bigint[]), 0
                  UNION
                    SELECT nxt.entity_id, w.hop + 1
                      FROM walk w
                      CROSS JOIN LATERAL (
                        SELECT CASE WHEN f.subject_id = w.entity_id THEN f.object_id ELSE f.subject_id END AS entity_id
                          FROM fact f
                         WHERE f.owner = $1 AND f.invalid_at IS NULL AND f.status = 'grounded'
                           AND (f.subject_id = w.entity_id OR f.object_id = w.entity_id)
                         ORDER BY f.fact_vec <=> $3::vector
                         LIMIT $5) nxt
                     WHERE w.hop < $4 AND nxt.entity_id IS NOT NULL
                )
                SELECT DISTINCT ON (f.id) f.id, f.fact_text, f.status, f.span_id, f.subject_id, f.object_id,
                       f.valid_at, 1 - (f.fact_vec <=> $3::vector) AS sim, w.hop
                  FROM walk w
                  JOIN fact f ON f.owner = $1 AND f.invalid_at IS NULL
                             AND (f.subject_id = w.entity_id OR f.object_id = w.entity_id)
                 ORDER BY f.id, w.hop
                 LIMIT $6
                """,
                owner, seed_ids, q_vec, hops, fanout, limit,
            )
        return [dict(r) for r in rows]
```

Integration test (`tests/integration/test_traverse_facts.py`): seed alice's facts `Quarzon owned_by Mara`, `Mara based_in Lisbon`, and bob's fact `Mara based_in Osaka` (bob's own entities); traverse from alice's Quarzon with hops=2 must return both alice facts (hop 0 and hop 1) and never bob's; with an invalidated `based_in` fact it must not be returned; with `seed_ids=[]` it returns `[]`.

- [ ] **Step 5: Integrate into prefetch**

In `_aprefetch`, after the turn `vector_search` (same `emb`), when `self.store` has `fact_search`:

```python
                q_lit = vec_to_literal(emb)
                fact_hits = await self.store.fact_search(self._agent_identity, q_lit, self.config.vector_k)
                from .entity_resolve import norm_name
                surfaces = [norm_name(w) for w in re.findall(r"[A-Z][\w-]+(?:\s+[A-Z][\w-]+)*", query)]
                seed_entities = await self.store.link_entities(self._agent_identity, surfaces)
                seed_entities += [h["subject_id"] for h in fact_hits[:3]]
                graph_facts = await self.store.traverse_facts(self._agent_identity, sorted(set(seed_entities)), q_lit)
```

Fuse `fact_hits` and `graph_facts` with `rrf([[f"fact:{h['id']}" for h in fact_hits], [f"fact:{g['id']}" for g in graph_facts]])`, keep the top 8 distinct facts, apply `dedupe_by_text(..., key="fact_text")`, and prepend `format_fact_block(selected_facts)` to the existing injection inside `<memory>`. Facts count against the same `max_tokens` budget before turns. When `fact_search` is absent or returns nothing, the existing path runs unchanged.

Add to `tests/test_fact_prefetch.py` a provider-level test with a fake store whose `vector_search` returns one turn and whose `fact_search` / `traverse_facts` return one grounded and one unconfirmed fact: the block contains the grounded fact, not the unconfirmed one, and the turn still appears.

- [ ] **Step 6: Run and commit**

Run: `pytest tests/test_fusion.py tests/test_fact_prefetch.py tests/test_provider_helpers.py -v` → PASS
Run: `EVAL_DSN=... pytest -m integration tests/integration/test_traverse_facts.py -v` → PASS

```bash
git add src/hermes_memory/fusion.py src/hermes_memory/store_facts.py src/hermes_memory/provider.py \
        src/hermes_memory/provider_helpers.py tests/test_fusion.py tests/test_fact_prefetch.py \
        tests/integration/test_traverse_facts.py
git commit -m "feat(graph): hybrid prefetch with fact search, entity linking and bounded traversal"
```

---

### Task 8: Harness gates and the acceptance run

**Files:**
- Modify: `scripts/eval_memory.py`
- Test: `tests/test_eval_memory.py`

**Interfaces:**
- Consumes: V17 tables; existing harness corpus (`Fact` with `project`, `owner`, `city`).
- Produces: summary keys `fact_recall` (share of gold relations present as live facts: `project owned_by owner`, `owner based_in city`), `extract_backlog` (pending turns at query time), and `gates` (pass/fail per row of the spec's acceptance table).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_eval_memory.py
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("eval_memory", Path(__file__).resolve().parents[1] / "scripts" / "eval_memory.py")
em = importlib.util.module_from_spec(spec)
spec.loader.exec_module(em)


def test_gates_report_each_threshold():
    summary = {"recall_by_kind": {"multihop": 0.85, "lexical": 0.97, "owner_lookup": 0.96},
               "noise_projects_per_answer_mean": 1.5, "latency_ms": {"p50": 120, "p95": 280},
               "leakage": {"answers_with_foreign_names": 0}, "fact_recall": 0.92,
               "entity_coverage": {"projects": 0.97, "owners": 1.0, "cities": 0.99}}
    gates = em.evaluate_gates(summary)
    assert all(gates.values())
    summary["recall_by_kind"]["multihop"] = 0.5
    assert em.evaluate_gates(summary)["multihop_recall"] is False
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_eval_memory.py -v` → FAIL (`evaluate_gates` missing).

- [ ] **Step 3: Implement**

```python
GATES = {
    "multihop_recall": lambda s: s["recall_by_kind"].get("multihop", 0) >= 0.80,
    "single_fact_recall": lambda s: min(s["recall_by_kind"].get("lexical", 0),
                                        s["recall_by_kind"].get("owner_lookup", 0)) >= 0.95,
    "noise": lambda s: s["noise_projects_per_answer_mean"] <= 2,
    "latency_p50": lambda s: s["latency_ms"]["p50"] <= 150,
    "latency_p95": lambda s: s["latency_ms"]["p95"] <= 300,
    "leakage": lambda s: s["leakage"]["answers_with_foreign_names"] == 0,
    "fact_recall": lambda s: (s.get("fact_recall") or 0) >= 0.90,
    "entity_coverage": lambda s: min(v or 0 for v in s["entity_coverage"].values()) >= 0.95,
}


def evaluate_gates(summary: dict) -> dict[str, bool]:
    return {name: bool(check(summary)) for name, check in GATES.items()}
```

Compute `fact_recall` after ingest: for each corpus `Fact`, check a live `fact` row joins `entity` names (`project` → `owned_by` → `owner`, `owner` → `based_in` → `city`) for the `eval` owner. Wait for `extract_status <> 'pending'` on all `eval` turns (bounded by `--drain-timeout`) before querying, and report `extract_backlog`. Entity coverage switches to the `entity` table for the `eval` owner. Add `summary["gates"] = evaluate_gates(summary)`.

- [ ] **Step 4: Run the acceptance evaluation**

Run: `pytest tests/test_eval_memory.py -v` → PASS
Run on a disposable database (migrated through V17): `PYTHONPATH=src python scripts/eval_memory.py --dsn "$EVAL_DSN" --facts 200 --chatter 800 --queries 150 --other-facts 50`
Expected: every entry in `gates` is `true`. Record the JSON in the PR description. If a gate fails, stop and report which, with the numbers; do not tune thresholds to pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/eval_memory.py tests/test_eval_memory.py
git commit -m "eval: fact recall and acceptance gates for the graph layer"
```

---

### Task 9: Documentation and supersession

**Files:**
- Modify: `docs/superpowers/specs/2026-11-14-graph-layer-design.md` (status → implemented for Phases 1–3, measured results), `docs/superpowers/specs/2026-09-03-conversation-manifold-design.md` (supersession notes on §2 non-goal, §6, §7), `RELEASE_CRITERIA.md` (graph rows with evidence), `README.md` (extraction model requirement and settings), `CHANGELOG.md`.

**Interfaces:** none (documentation only).

- [ ] **Step 1:** Add at the top of each superseded manifold section: `> **Superseded by** docs/superpowers/specs/2026-11-14-graph-layer-design.md §3 (YYYY-MM-DD). Kept for history.`
- [ ] **Step 2:** README: document `hybrid_age.extract_enabled`, `extract_model` (default `llama3.2:3b`; `ollama pull llama3.2:3b`), the extraction backlog, and that extraction runs after the turn is stored.
- [ ] **Step 3:** RELEASE_CRITERIA: add graph-layer rows with the acceptance JSON as evidence (automated; level 6 still requires a human-observed transcript).
- [ ] **Step 4:** CHANGELOG `[Unreleased]`: entity, fact and extraction stage; hybrid prefetch.
- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs README.md RELEASE_CRITERIA.md CHANGELOG.md
git commit -m "docs: graph layer supersession, settings and acceptance evidence"
```

---

## Self-review notes

- Spec §3.2–§3.5 map to Tasks 1–7; §4 gates to Task 8; supersession to Task 9.
- Design change from spec v0.1, recorded in v0.2: new owner-scoped tables instead of extending `noun` / `aliases` / `claims`, because those carry global uniqueness that the manifold and reception writers rely on.
- Extraction latency (measured: ~4 s/turn for `llama3.2:3b`, ~24 s for `qwen2.5` on the reference GPU) makes extraction a backlog, not a per-turn step. Retrieval reads whatever has been extracted; un-extracted turns are still found by vector search.
- Phases 4 (AGE mirror, retire the co-mention walk) and 5 (scale, backfill of existing history) get their own plans once this plan's gates pass.

## Changelog

| Version | Created | Modified | Author | Notes |
|---|---|---|---|---|
| 0.1 | 2026-11-14 | 2026-11-14 | Claude | Phases 1–3 implementation plan: entity, fact and extraction stage; hybrid retrieval; acceptance gates. |
