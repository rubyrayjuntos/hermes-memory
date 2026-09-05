# Quality-review remediation — multi-agent plan

> **For agentic workers:** Parent orchestrates waves. Do not start Wave 2
> until Wave 1 is merged on the working tree. One agent owns each file set.
> REQUIRED: parent reviews diffs before the next wave.

**Goal:** Land every leftover item from the 5 Sep 2026 code-quality re-review:
L0–L6 write-outcome policy, health counters, write-path log/CI ban, mypy
widen, docs honesty, recency skip resolution, drop live `AboutConceptLinker`
inheritance.

**Architecture:** A process-local `WriteLedger` (not Postgres) records
`WriteOutcome(stage, kind)` from the drain. `Store.librarian_health()` stays
file-backed drift only (`{total_file_backed, missing_hash, missing_doc_type}`)
so `scripts/librarian_health.py --fail-on-drift` does not see write counters.
Pane `Runtime.health()` / `Runtime.librarian_health()` merge ledger snapshot
ints. Prefetch still never raises.

**Tech Stack:** Python 3.11+, pytest, ruff, mypy (incremental), existing
asyncpg/AGE store. No new runtime dependencies.

**Spec:** [code-quality-review canvas](../../../../.cursor/projects/home-rswan-Documents-hermes-memory/canvases/code-quality-review.canvas.tsx)
(L0–L6 table + `WriteOutcome` shape). Repo rules: `AGENTS.md`.

## Global Constraints

- Python 3.11+; parameterized SQL; Cypher via `age_str` / dollar-quote; SAVEPOINT around Cypher; MERGE not CREATE.
- Loopback only. Never print `.env` or secrets. Prefetch `SECRET_RE` stays display-only.
- Hermes L0 must not raise: `prefetch`, `sync_turn`, `on_memory_write`.
- Do not change `Store.librarian_health()` key set except adding optional extra ints is forbidden — keep the three file-backed keys only.
- Do not split `graph_runtime.py` further. Do not add Poetry.
- Same working tree. No extra worktrees. No commits unless the human asks.
- File ownership is exclusive per wave. If you need a file you do not own, stop and return to the parent.

## Parent quality bar (every wave)

Before the parent accepts a wave:

1. `ruff check src tests` clean.
2. `mypy` clean (whatever `[tool.mypy] files` is at that moment).
3. `pytest -q -m "not integration"` green (182+; new tests must pass).
4. Diff stays inside the agent's file list.
5. No `logger.debug(..., exc_info=True)` added on live write `except Exception` in provider/store/store_merge.
6. AGENTS.md conventions not violated.

**Definition of done (whole plan):**

- `WriteOutcome` + `WriteLedger` exist and provider drain records ENQUEUE / EMBED / SQL_TURN / FLOWER / NOUNS / MENTIONS / MEMORY_SQL.
- Unit tests lock FAILED vs EMBED_NULL vs GRAPH_DEGRADED vs DROPPED.
- Runtime health JSON includes ledger ints; Store health JSON does not grow those keys.
- Write-path debug-on-Exception banned by CI grep.
- L4 MERGE per-statement logs are warning; L5 teardown stays debug.
- mypy files list includes `write_outcome.py` plus at least `schema_guard.py`, `walk.py`, `tokens.py` (and any other module that typechecks without `ignore_errors`).
- CONTRIBUTING says live DDL is `sql/migrations/V*.sql`. Provider module docstring is flower + `extract_nouns`, not ABOUT.
- Recency ABOUT skip is gone (rewritten or deleted + Unreleased changelog waiver).
- `HybridAgeMemoryProvider` does not subclass `AboutConceptLinker`; tests still import `_extract_concepts` from `about_concepts` or `provider` re-exports.

---

## File ownership (do not cross)

| Wave | Agent id | Owns (exclusive) | Must not touch |
|------|----------|------------------|----------------|
| 1 | **contract** | `src/hermes_memory/write_outcome.py` (create), `provider.py`, `tests/test_write_outcome.py` (create), `tests/test_provider_helpers.py` (only if enqueue/init tests need a ledger assertion) | store*, graph_*, docs, CI, ingest |
| 1b | **types** | read-only review of `write_outcome.py` | any write |
| 2a | **store-l4** | `store.py` (log levels + narrow MERGE catches only), `store_merge.py`, `store_concepts.py` | `librarian_health` body/keys, provider, graph_* |
| 2b | **health** | `graph_runtime.py` (`health` / `librarian_health` merge only), `verify.py` (print ledger line), `tests/test_graph_api.py` (health payload keys) | provider, store.py |
| 2c | **docs** | `CONTRIBUTING.md`, `docs/architecture.md` Write Path, `CHANGELOG.md` Unreleased, `provider.py` **module docstring only** (coordinate: if Wave 1 still open, parent applies docstring) | src except that docstring |
| 2d | **recency** | `tests/integration/test_recency_decay.py`, `CHANGELOG.md` one Unreleased bullet if deleting | src/ |
| 2e | **mypy** | `pyproject.toml` `[tool.mypy]`, type fixes only in newly added files | provider.py, store.py unless a file is added to mypy and fails — then fix annotations only |
| 2f | **linker** | `provider.py` class bases + imports (after Wave 1 merged), `tests/test_extract_concepts.py` import path | write_outcome internals |
| 3 | **ci-grep** | `.github/workflows/ci.yml` lint job only | Python behavior |
| 4 | **review** | none — read-only | |

Wave 2c docstring vs Wave 1: **parent applies the provider docstring** after Wave 1 so docs agent never edits `provider.py`.

Wave 2f runs **after** Wave 1 on the same `provider.py` — not in parallel with Wave 1.

---

## Layer policy (copy into every write-path agent prompt)

| Layer | Functions | Swallow? | Outcome |
|-------|-----------|----------|---------|
| L0 | `prefetch`, `sync_turn`, `on_memory_write` | Yes — never raise | prefetch `""` + `logger.exception`; writes enqueue or `DROPPED` |
| L1 | `insert_turn`, memory upsert/replace/remove | Catch once in drain | `Kind.FAILED`, `logger.error` or existing V10 `error` |
| L2 | `embed_text`, turn embed | Yes — NULL vector | `Kind.EMBED_NULL`, `logger.warning`, still insert SQL |
| L3 | flower, nouns, mentions | Yes after B | `Kind.GRAPH_DEGRADED`, `logger.warning` |
| L4 | SAVEPOINT MERGE helpers | Yes, savepoint only | `logger.warning` (not debug) with label/edge class |
| L5 | shutdown, pane embedder, ghost, taxonomy backfill | Yes | debug or warning; **do not** increment write FAILED |
| L6 | `require_schema_head`, bind host | No | raise |

Do not promote L5 to warning-as-failure.

---

### Task 1: Contract — `WriteLedger` + provider recording (Wave 1)

**Agent:** `generalPurpose` (implementation). After green tests, parent dispatches `type-design-analyzer` read-only.

**Files:**
- Create: `src/hermes_memory/write_outcome.py`
- Create: `tests/test_write_outcome.py`
- Modify: `src/hermes_memory/provider.py` (`_enqueue_write`, `_awrite_drain`, `_awrite_turn`, `_awrite_item`)

**Interfaces:**

```python
# write_outcome.py — exact names later tasks import
class Stage(StrEnum):
    ENQUEUE = "enqueue"
    EMBED = "embed"
    SQL_TURN = "sql_turn"
    FLOWER = "flower"
    NOUNS = "nouns"
    MENTIONS = "mentions"
    MEMORY_SQL = "memory_sql"

class Kind(StrEnum):
    OK = "ok"
    SKIPPED = "skipped"
    EMBED_NULL = "embed_null"
    GRAPH_DEGRADED = "graph_degraded"
    DROPPED = "dropped"
    FAILED = "failed"

@dataclass(frozen=True)
class WriteOutcome:
    stage: Stage
    kind: Kind
    session_id: str = ""
    turn_id: int | None = None
    detail: str = ""  # exception type + <=80 chars; no DSN/password

class WriteLedger:
    def record(self, outcome: WriteOutcome) -> None: ...
    def counts(self) -> dict[str, int]:
        """Keys: dropped_writes, writes_failed, embed_null, graph_degraded.
        Values are totals (not per-stage)."""
    def snapshot(self) -> dict[str, Any]:
        """counts() plus last_failed_stage: str, last_failed_at: str (ISO) or empty."""
    def reset(self) -> None:  # tests only

LEDGER = WriteLedger()  # process singleton
```

- Consumes: existing drain control flow; do not change A–F SQL/Cypher.
- Produces: `LEDGER` for Wave 2b health merge.

- [ ] **Step 1: Failing tests** in `tests/test_write_outcome.py`:

```python
from hermes_memory.write_outcome import Kind, Stage, WriteOutcome, WriteLedger

def test_failed_sql_increments_writes_failed():
    led = WriteLedger()
    led.record(WriteOutcome(Stage.SQL_TURN, Kind.FAILED, detail="UndefinedColumn"))
    assert led.counts()["writes_failed"] == 1
    assert led.snapshot()["last_failed_stage"] == "sql_turn"

def test_embed_null_is_not_failed():
    led = WriteLedger()
    led.record(WriteOutcome(Stage.EMBED, Kind.EMBED_NULL))
    assert led.counts()["embed_null"] == 1
    assert led.counts()["writes_failed"] == 0

def test_graph_degraded_after_ok_sql():
    led = WriteLedger()
    led.record(WriteOutcome(Stage.SQL_TURN, Kind.OK, turn_id=1))
    led.record(WriteOutcome(Stage.FLOWER, Kind.GRAPH_DEGRADED))
    c = led.counts()
    assert c["writes_failed"] == 0
    assert c["graph_degraded"] == 1
```

Also add provider-level tests (new functions in the same file or `test_provider_helpers.py`) that call `LEDGER.reset()` then drive `_awrite_turn` with a stub store whose `insert_turn` raises, and assert `LEDGER.counts()["writes_failed"] == 1`. Stub embedder returns a 768-list so EMBED is OK. Second test: `insert_turn` succeeds, `_link_turn_flower` raises, assert `graph_degraded >= 1` and no `writes_failed`.

- [ ] **Step 2:** `pytest tests/test_write_outcome.py -q` fails (module missing).
- [ ] **Step 3:** Implement `write_outcome.py`. Wire `LEDGER.record` at:
  - QueueFull → `Stage.ENQUEUE, Kind.DROPPED` (keep `_dropped_writes` in sync or derive from ledger).
  - embed except / `vec is None` after try → `EMBED, EMBED_NULL`.
  - `insert_turn` except or `conv_id is None` → `SQL_TURN, FAILED` then return.
  - flower except → `FLOWER, GRAPH_DEGRADED`.
  - noun/passport except → `NOUNS, GRAPH_DEGRADED`.
  - mentions except → `MENTIONS, GRAPH_DEGRADED`.
  - memory SQL path except → `MEMORY_SQL, FAILED`.
  - Successful stages may record `OK` (optional; counts() ignores OK).
- [ ] **Step 4:** Prefetch / sync_turn still never raise. `_initialized` behavior unchanged.
- [ ] **Step 5:** `pytest tests/test_write_outcome.py tests/test_provider_helpers.py -q` and `pytest -q -m "not integration"`.
- [ ] **Step 6:** Parent (not this agent) runs `type-design-analyzer` on `write_outcome.py`. Fix only encapsulation nits that break the four count keys.

**DoD:** Tests above pass. `LEDGER.counts()` keys exactly `dropped_writes`, `writes_failed`, `embed_null`, `graph_degraded`. No graph/store/CI edits.

---

### Task 2a: Store L3/L4 log levels (Wave 2, parallel)

**Agent:** `generalPurpose`

**Files:** `store.py` (ensure_*_labels, `bridge_turn` debug→warning only), `store_merge.py`, `store_concepts.py` (live merge/fetch failures that affect writes — not purge_verify).

**Do not change** `librarian_health` SQL or return keys.

- [ ] Promote `logger.debug(..., exc_info=True)` on `except Exception` for flower/ABOUT/MERGE write helpers to `logger.warning`.
- [ ] Leave `purge_verify_*` at debug (L5).
- [ ] Narrow L4 MERGE inner catches to `asyncpg.PostgresError` where the block only exists to ROLLBACK TO SAVEPOINT; keep a trailing `Exception` at warning if AGE wraps errors as generic.
- [ ] `pytest -q -m "not integration"` plus `tests/test_store.py` if present.

**DoD:** `rg 'logger.debug\\(.*exc_info' src/hermes_memory/store.py src/hermes_memory/store_merge.py` shows only purge/teardown or none. No health key changes.

---

### Task 2b: Health merge (Wave 2, parallel)

**Agent:** `generalPurpose`

**Files:** `graph_runtime.py` (`health`, `librarian_health` only), `verify.py`, `tests/test_graph_api.py`

```python
# Runtime.health() must become:
{"ok": True, "bind": f"{host}:{port}", **LEDGER.snapshot()}

# Runtime.librarian_health() must become:
{"ok": healthy, **counts, **LEDGER.counts()}
# Store counts keys remain; ledger keys added alongside.
```

- [ ] `verify.py` after pipeline: print `LEDGER.snapshot()` one line (no secrets).
- [ ] Update `tests/test_graph_api.py` health stubs/assertions so extra keys are allowed; assert `dropped_writes` in `Runtime.health()` (use `LEDGER.reset()` in the test).
- [ ] Do not change `scripts/librarian_health.py` fail-on-drift logic (it calls Store, not Runtime).

**DoD:** Existing graph API tests pass. Health JSON includes the four ledger ints.

---

### Task 2c: Docs honesty (Wave 2, parallel)

**Agent:** `comment-analyzer` then a `generalPurpose` apply (or one generalPurpose that follows comment-analyzer rules).

**Files:** `CONTRIBUTING.md`, `docs/architecture.md`, `CHANGELOG.md` Unreleased. Parent edits `provider.py` module docstring after Wave 1:

Replace the ABOUT contract blurb with: live path is Session/Turn flower + `extract_nouns` + SQL `mentions`. Prefetch never raises. Writes enqueue; drain records `WriteOutcome`.

CONTRIBUTING: new DDL is `sql/migrations/V*.sql` (idempotent). `sql/init/` is first-boot compose only.

`docs/architecture.md` Write Path: add the seven-row L0–L6 table from this plan.

README garden contract: do **not** write the words `ABOUT` or `Concept` in `README.md`.

**DoD:** `pytest tests/property/test_edge_filter.py::test_readme_advertises_garden_manifold_only -q` passes.

---

### Task 2d: Recency skip (Wave 2, parallel)

**Agent:** `generalPurpose`

**Files:** `tests/integration/test_recency_decay.py`

The skip reason is `legacy AGE ABOUT recency engine removed`. `expand_graph` now walks SQL `mentions` and uses `consensus_decay`.

- [ ] Prefer: rewrite the test to two `mentions` edges with different `created_at` / magnitude and assert newer ranks first (same store fixtures as other integration tests). No ABOUT labels.
- [ ] If rewrite cannot express the invariant without ABOUT vertices: delete the file and add Unreleased changelog: "Removed skipped ABOUT recency integration; mentions decay stays in `store_expand` / walk tests."
- [ ] Do not leave `pytest.mark.skip` without a tracked Unreleased waiver.

**DoD:** No unconditional skip for ABOUT recency. `pytest -q -m "not integration"` unchanged. Integration file either passes under `--migrate` or is gone.

---

### Task 2e: Widen mypy (Wave 2, parallel)

**Agent:** `generalPurpose`

**Files:** `pyproject.toml` `[tool.mypy] files`, plus annotation-only fixes in newly listed modules.

Start from current list (`age_cypher`, `turn_filter`, `session_kind`, `store_vec`, `extract_nouns`). Add `write_outcome.py` (after Wave 1), `schema_guard.py`, `walk.py`, `tokens.py`. Try `config.py` — if it needs more than 15 annotation lines, drop it and report.

Keep `follow_imports = skip`, `ignore_missing_imports = true`, `python_version = "3.12"`.

**DoD:** `mypy` exits 0. CI lint job unchanged except it already runs `mypy`.

---

### Task 2f: Drop live AboutConceptLinker (after Wave 1)

**Agent:** `generalPurpose`

**Files:** `provider.py` (bases + import), `tests/test_extract_concepts.py`

```python
class HybridAgeMemoryProvider(MemoryProvider):
    ...
# Keep re-exports:
from .about_concepts import SNAP_COSINE, _extract_concepts, _slug, ...
```

- [ ] Grep `AboutConceptLinker` — only `about_concepts.py` and maybe backfill.
- [ ] `pytest tests/test_extract_concepts.py tests/test_provider_helpers.py tests/test_write_outcome.py -q`

**DoD:** Provider MRO does not include `AboutConceptLinker`. Tests still pass.

---

### Task 3: CI write-path log ban (Wave 3)

**Agent:** `generalPurpose`

**Files:** `.github/workflows/ci.yml` lint job only.

Add a step after ruff:

```bash
# Live write modules: Exception handlers must not hide at debug.
if rg -n 'logger\.debug\([^)]*exc_info' \
     src/hermes_memory/provider.py \
     src/hermes_memory/store.py \
     src/hermes_memory/store_merge.py \
   | rg -v 'purge_verify|shutdown|loop stop|pool close|incomplete init'; then
  echo "write-path Exception logged at debug"; exit 1
fi
```

Tune the allowlist so current L5 lines in provider shutdown still pass. Re-run the script locally before finishing.

**DoD:** Lint job fails if someone reintroduces write-path debug+exc_info. Poetry stays gone.

---

### Task 4: Parent integration review (Wave 4)

**Agent:** parent runs `code-reviewer` then `pr-test-analyzer` (read-only). Parent runs:

```bash
ruff check src tests
mypy
pytest -q -m "not integration"
pytest tests/property -q
```

Fix any FAIL yourself (do not spawn a fifth writer on `provider.py` without pausing others).

**DoD:** Reviewer finds no L0 raise regressions, no Store health key break, no README ABOUT leak.

---

## Orchestration schedule

```text
Wave 1:   [contract]  →  parent review  →  [types read-only]
Wave 2:   [store-l4] [health] [docs] [recency] [mypy] in parallel
          then [linker] (needs provider from Wave 1)
Wave 3:   [ci-grep]
Wave 4:   [code-reviewer] + [pr-test-analyzer] + parent pytest
```

## Sub-agent prompt template (parent fills SCOPE)

```text
You are agent {ID} on hermes-memory (/home/rswan/Documents/hermes-memory).
Read AGENTS.md. You own ONLY these files: {FILES}.
If you need another file, stop and report BLOCKED.
Task: {TASK SECTION from this plan, copied verbatim}.
DoD: {DoD from that task}.
Quality: ruff + your tests green. No commit. No secrets.
Return: files changed, tests run, leftover risks, BLOCKED if any.
```

## Out of scope (explicit)

- Further `graph_runtime.py` split.
- Package version bump off 0.1.0.
- Multi-tenant / bind beyond loopback.
- Secret-stripping stored turns.
- Implementing ingest through more Store write APIs beyond schema_head (already done).
