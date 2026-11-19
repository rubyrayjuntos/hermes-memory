# Hermes Memory: dev-ready implementation spec for robust claim extraction

**Repo:** `hermes-memory`. **Observed baseline (2026-11-17):** local `main` contains merge commit `ec80c7a`; `origin/main` remains at `1acae35`. The Spec 0 feature branch is based directly on `hybrid-fixes@c7c6e29` so a PR can target `hybrid-fixes` without including main-only changes. No branch was pushed.
**Source documents:** Ray's draft spec (v0.4 author summary) and the research Page *Hybrid-age: making claim extraction robust*.
**Status:** reviewed spec and Spec 0 work are on `spec0-eval-harness`, based on `hybrid-fixes@c7c6e29`. Do not rebase this branch onto the local merge commit `ec80c7a` or target `origin/main` until the hybrid-fixes changes are upstream. Hermes SessionDB's occurrence UID evidence and the memory-provider capture boundary are detailed in T5.1.

> **About line references.** Recheck source line numbers as implementation proceeds. The checked Hermes SessionDB source revision for the T5.1 identity evidence is `8237c7f8ec179c88857f24a174509d985af704b7`.

---

## Constraints (apply to every spec and every PR)

| # | Rule |
|---|------|
| C1 | **Never push to `main`.** Work goes on feature branches. **Ray opens every PR.** The implementing agent pushes its branch and stops there. |
| C2 | **`retrieval_mode` stays `vector` by default** (`config.py`; validated in `load_config`). New assistant extraction, work capture and prior-work serving stay behind off-by-default flags. The approved short-user-turn capture fix and user-authored rules additions are default-on correctness changes once their gates pass; they must not silently change assistant-turn filtering. |
| C3 | **Installed DB `127.0.0.1:5452`:** before **any** write (migration, backfill, re-extract, delete), take a `pg_dump -Fc` backup into `/home/rswan/hermes-memory-backups/`, **ask Ray, and wait for an explicit yes.** Reads are allowed. The rules-v2 backfill is scheduled for after Specs 0–2 merge (Decision 6), and C3 still applies to it. |
| C4 | **Do not touch `127.0.0.1:5450`.** No connections, no migrations, no containers. |
| C5 | **Never use `--reuse-dsn`.** Evals run against throwaway `*_eval` or `hermes_release_gate_*` databases. The scripts already refuse other DSNs (`eval_memory.py`, `eval_golden_conversation.py:31-32`). |
| C6 | **No raw text in shadow tables.** Shadow and eval tables hold only HMAC keys (`shadow.py:21-23`, 16 bytes), ids, counts and latencies. Raw text includes queries, rendered blocks, ask strings and pasted content. |
| C7 | **Migrations take the next free `V<N>` at merge time.** Today `main` and `hybrid-fixes` both stop at `V21` (V22 was reverted in `c21de62`). In this doc they are written `V<N>`, `V<N+1>`… Every migration PR re-checks `ls sql/migrations` right before Ray merges and renames if needed. Migrations must be additive and idempotent (`IF NOT EXISTS`, plus guarded `DO $$` blocks like V18). |
| C8 | **Tests first.** Every task starts with a test that fails on the current code, and the PR shows it failing before the fix. Use DB tests in `tests/integration/` (the `--migrate --dsn "$TEST_DSN"` pattern from the eval doc) and pure tests in `tests/`. |
| C9 | **No invented numbers.** Every threshold, budget or target marked **[tune by eval]** starts at its stated default. Tune it only with the Spec 0 harness and the gate for the spec that owns it; record the before/after result and do not tune against the installed DB. |
| C10 | **Pin extractor versions.** Bump the rules version whenever rules or their segmentation change, and the model version whenever its prompt/schema changes. Keep the rules version used in `extraction_jobs` and `store_claims` synchronized (prefer one shared constant); they feed job `op_id`s and `claim_evidence.extractor_version`. Re-extracting the installed DB is a write, so C3 applies. |

### Global invariants (kept from Ray's draft, corrected)
- `claims` + `claim_evidence` stay the single semantic authority. `timeline.rebuild` (`timeline.py:76-147`) owns status and validity. Event time orders timelines (`timeline.py:41-42`).
- **Only `speaker='user'` evidence can ground a claim.** Non-user speakers are unconfirmed by construction (`timeline.py:110-123`). Spec 1 adds `document` and Spec 4 adds assistant claims. Neither may change this rule.
- The facts block stays inside its 400-token cap. The total stays inside `injection_token_cap(max_tokens)` (`tokens.py:27-28`; `max_tokens=1200`, `config.py:47`). New blocks are carved from supporting turns, **never from facts**.
- Every lookup is scoped by owner. Each spec needs a cross-owner test.
- Each spec has a rollback flag or is a pure revert. The default-on user-capture/rules corrections in Specs 0–2 are the explicit exception: version them, gate them, and roll them back by reverting the feature (do not run an installed-DB re-extraction as rollback). Nothing advances until the previous gate passes.
- Evaluation distinguishes semantic claim projections (`claims`) from evidence provenance (`claim_evidence`). Speaker, segment offsets and extractor version belong to evidence; `current`/timeline status belongs to the rebuilt claim. Do not score these as one flat record.
- Spec 0 is based on `hybrid-fixes@c7c6e29`, which contains the eval harness. Do not base its PR on `origin/main@1acae35` or include local `main@ec80c7a`'s merge diff.

### Per-spec contract
Each spec has six parts: **Goal → Failing tests first → Implementation tasks (Python pointers) → Schema/migration outline → Acceptance criteria & gate → Out of scope.**

---

## Spec 0: Eval harness first (probe fix, golden suite, the 6/30 ingest gap)

**Goal.** Get a trustworthy, reproducible measurement before changing any extraction behaviour. Every later gate depends on this harness.

**Implementation snapshot (2026-11-17):** T0.1–T0.4 are implemented on `spec0-eval-harness`. The rules-only baseline at `docs/evals/2026-11-17-golden-rules-only.json` drained 27/42 rows; the 15 dropped turns (`t02`, `t04`, `t06`, `t08`, `t10`, `t12`, `t14`, `t16`, `t18`, `t20`, `t22`, `t26`, `t28`, `t30`, `t42`) are all short assistant turns. Against 11 mode-independent ideal claims, precision/recall/F1 is `0.556/0.455/0.500`; mode-specific recall is `1.0` and is retained only as a diagnostic. Four expected-claim questions fail because those claims are absent. Vector and hybrid each pass `0.4`; their injected contexts still disagree on four questions, so the aggregate tie alone does not establish ranking equivalence. G0 is accepted on the reviewed rules-only baseline; the first model-mode baseline is deferred to T3.6.

### 0.1 What the `hybrid-fixes` base already brought along (keep it, don't redo it)
- **The supersession probe is fixed.** `scripts/eval_memory.py:116` (`seed_lives_in_probe`) writes NYC→Reykjavik→NYC as functional `lives_in` claims through `Store.record_rule_claims`; `:157` reports `current_objects` and `current_is_nyc` from SQL. This replaces the old substring `has_nyc`/`has_austin` probe from pre-merge `main`. The cities are outside the planted `CITIES` corpus. `tests/integration/test_hybrid_supersession.py` covers the same case.
  - **Caveat for the record:** the probe writes claims directly. It tests timeline and retrieval, not extraction. Label it `timeline_probe` in the JSON. End-to-end extraction is measured by the golden suite (0.3).
- **Golden runner and fixture:** `scripts/eval_golden_conversation.py` and the original fixture (30 turns, 10 expected claims, 8 questions) are on `hybrid-fixes`. Spec 0 replaces that fixture with v2 (42 turns, mode-specific expected claims/evidence, an independent 11-claim ideal set, and 10 typed questions).
- **Shadow replay and supersession test:** `scripts/shadow_replay.py` and `tests/integration/test_hybrid_supersession.py` are on `hybrid-fixes`. The replay reports `p95_facts_tokens` and `p95_total_tokens` at `scripts/shadow_replay.py:378-379`.
- **Migration head:** `V22*` is absent from both branches; the attempted V22 change was reverted in `c21de62`.

### 0.2 Root cause of the 6/30 ingest gap (verified)
On the inherited v1 fixture, `sync_turn` drops every turn that `_is_noise` flags (`provider.py:349-351`). `_is_noise` treated any turn shorter than `TURN_MIN_CHARS = 40` as noise (`turn_filter.py:6`, `turn_filter.py:19-25`). Exactly 6 of 30 v1 turns survived, matching `complete_rows=6`; short fact turns were dropped before storage. Two more problems followed:
- The original runner expected `2 × user turns` rows (`eval_golden_conversation.py:39-43`, giving 30), so it waited out the full 900 s deadline (`:56`).
- **This is a production bug, not just an eval bug.** A real `"I live in NYC."` (14 chars) is never stored, so it can never be extracted or retrieved.

Two smaller v1 fixture problems, corrected in v2:
- The expected claims use subject `Home` with verb `is`. After the V22 revert, `is` is non-functional (`V18:67-84`), so the expected `current` flags assume semantics the code no longer has.
- The question `city_now` ("Where is my city now?") asks in the first person, but the fixture only states facts about `Home`. Spec 2 adds first-person rules, so the fixture should state facts as `"I live in NYC."`.

### 0.3 Failing tests first
1. `tests/test_turn_filter.py`: retains short user assertions while preserving strict default and assistant filtering. The role-aware cases failed before T0.1; they pass now.
2. `tests/test_golden_fixture.py`: checks seeded verbs, mode-specific semantic/evidence labels, source-turn references and typed question schema. The inherited fixture failed; v2 passes.
3. `tests/integration/test_golden_ingest.py`: runs the fixture through the provider path with `--migrate`; checks complete rows and drained extraction jobs. Passed on the isolated 5453 test database.
4. The integration test also verifies owner isolation for claims, evidence and retrieval. Passed.

### 0.4 Implementation tasks
- **T0.1, noise rule.** Change `turn_filter._is_noise` (`turn_filter.py:19-25`) so a turn is noise only when it is empty or matches the existing ack/noise patterns, or when it is shorter than `TURN_MIN_CHARS` **and** carries no assertion signal. "Assertion signal" means `reception.extract_assertions` or the Spec 2 self-rules return anything, or a token-bounded first-person cue (`I|my|I'm|we`) is present. Callers are `provider.sync_turn:350`, the noun extraction and the backfill paths (`rg -n _is_noise src/`); preserve their call compatibility. **Settled (Decision 1):** the exemption applies to user turns only. Assistant turns keep the plain 40-char rule until Spec 4. `TURN_MIN_CHARS` is not lowered globally. Add a role-aware entry point (for example `_is_noise(content, role=...)`); an omitted role retains the legacy strict behavior, while `sync_turn` passes each turn's role. Add test cases: `_is_noise("Noted.", role="assistant") is True`, and a 30-char assistant reply with a fact is still noise.
- **T0.2, runner.** In `eval_golden_conversation.run`:
  - compute expected conversation rows with the same role-aware admission predicate as `sync_turn` (both user and assistant turns, after the same truncation and filtering), instead of assuming `2 × user`;
  - stop waiting only when every expected extraction job is `done` and the row count is stable. Any `failed` job fails the run; pending, leased or retryable jobs are not "drained." Keep a hard timeout as a failure bound, not as the normal completion path;
  - emit `extract_mode` from config instead of hard-coding `"rules_only"` (`:120`);
  - add per-question `kind`, semantic-claim checks and evidence-provenance checks.
- **T0.3, fixture v2** (`tests/fixtures/golden_conversation.json`). Keep the current topics and add:
  - first-person functional facts (`I live in NYC` → `I moved to Austin` → `I'm back in NYC`; `I work at Acme`; `My role is engineer`);
  - a negation (`I don't work at Acme anymore`);
  - a pasted email containing `Bob is the owner` and `No, I meant Friday`;
  - a pasted log containing `X uses Y` lines;
  - a code fence;
  - one assistant reply that states a new fact (for Spec 4).

  Keep expected semantic claims separate from expected evidence. Semantic claim labels carry fields such as `subject`, `verb`, `object`, `polarity`, `current` and projected `status`; evidence labels carry `speaker`, extractor/version and source segment. A claim may have both user and document evidence, so a single expected-claim `speaker` is ambiguous. Pasted-only facts must have document evidence and an unconfirmed projection (Spec 1). Set expected labels per extract mode (`rules_only` and `rules_then_model`). Use this questions JSON schema:
  ```json
  {"id": "city_now", "kind": "functional_current", "text": "Where do I live now?",
   "expected_answer": "NYC", "expect_contains": ["NYC"], "expect_absent": ["Austin"],
   "expect_claim": {"subject": "I", "verb": "lives_in", "object": "NYC", "current": true}}
  ```
  `kind` must be one of `functional_current | history | alias | non_functional | negation | paste_not_user | assistant_unconfirmed | abstain`.
- **T0.4, metrics** (the JSON report, also written by `eval_memory.py` where it applies):
  - semantic claim precision, recall and F1 against the mode-independent ideal labels, keyed on (`subject`, `verb`, `object`, `polarity`, `current`); report mode-specific label metrics separately as a diagnostic, not as the primary recall;
  - event-time agreement and timeline status agreement;
  - **pasted-span false-grounding count** (should be 0 after Spec 1);
  - per-question pass rate for `vector` and `hybrid`;
  - expected rows plus the dropped turn IDs, roles and reason codes (no turn content);
  - per-question diagnostics separating missing expected claims from vector/hybrid context disagreements;
  - **facts-block tokens measured separately from total injection tokens.** Keep the replay's `p95_facts_tokens` and `p95_total_tokens` (`hybrid-fixes:scripts/shadow_replay.py:378-379`) distinct; add a per-run `facts_tokens` field from `format_facts` where needed.
- **T0.5, baseline run.** Run `rules_only` on a fresh `*_eval` DB and record the JSON under `docs/evals/`. Record the numbers, whatever they are. No target is set yet. Defer the `rules_then_model` baseline to the first step of T3.6; G0 does not require model numbers because Specs 1 and 2 are rules-only.

### 0.5 Schema/migration
None.

### 0.6 Acceptance & gate
- Tests 1–4 pass. The golden run finishes without hitting the deadline and reports `complete_rows == expected_rows`.
- The rules-only baseline JSON is recorded and reviewed; its primary claim metrics use the mode-independent ideal labels.
- **Gate G0:** passed on the reviewed rules-only baseline. Record the first model-mode baseline in T3.6 before comparing models. The feature branch is based on `hybrid-fixes@c7c6e29`; the T0.1 noise rule is already settled (Decision 1). Specs 1–3 may start.

### 0.7 Out of scope
LongMemEval/LoCoMo runs, re-extracting the installed DB, and changing ranking.

---

## Spec 1: Paste segmentation and a `document` speaker

**Goal.** Pasted material (docs, logs, emails, articles, code) must never become user-grounded claims or trigger repairs. Today the rules run over the whole turn and record every match as `speaker=user`:
- inline reception: `provider._reception_stage`, `provider.py:662-702` (aliases at `:665`, repair at `:677`, assertions at `:689`);
- the durable rules job: `extract_publish.publish_rules`, `extract_publish.py:51-55`;
- the model path's rule findings: `extraction_worker._load_model_batch_input`, `extraction_worker.py:40-46`.

Nothing detects pastes. `turn_filter.py` only measures length and noise. `extraction_jobs.should_enqueue_model` (`:36-70`) only screens code and tool output for the model job. Note also that `sync_turn` cuts content at 8000 chars (`provider.py:357`). Segment offsets are relative to the stored text.

### 1.1 Failing tests first (`tests/test_segment_turn.py`, pure, plus `tests/integration/test_paste_claims.py`, DB)
1. A pasted email (`From:`/`Subject:` headers, a `>` quote, a signature) containing `Bob is the owner.` and `No, I meant Friday.` → **0** user-speaker evidence rows for the pasted assertions and **0** repair rows from the pasted correction. `Bob is the owner` may create an unconfirmed claim projection backed by `speaker='document'` evidence.
2. A code fence containing `Config uses Redis` → no claim evidence; do not run assertion extraction on `kind=code`.
3. A 60-line log with `Worker uses 4GB` lines → no user claim.
4. A mixed turn, `"Nightingale uses Postgres. Can you clean this up?\n\n<pasted doc>"`, yields exactly 1 user claim, and every claim parsed from the pasted segment is backed by `document` evidence.
5. A short authored repair, `"No, I meant Spot is a poodle."`, still repairs (regression guard for `classify_repair`, `reception.py:119-132`).
6. A cross-owner test: document claims for owner A are invisible to owner B.

### 1.2 Implementation tasks
- **T1.1** Add `turn_filter.segment_turn(text) -> list[Segment]`, where `Segment(start, end, kind)` and `kind` is `authored | pasted | code | quote`. Deterministic heuristics:
  - fenced code;
  - `>`-quoted runs;
  - email header blocks (`From:|To:|Subject:|Date:` at line start);
  - signature delimiters (`-- `, `Sent from`);
  - log-like runs (timestamp or level prefixes on a run of consecutive lines);
  - markdown tables;
  - long runs with no first-person tokens, but only as a weak signal combined with another structural/payload signal. Absence of first-person language alone must never turn an otherwise ordinary authored assertion into a paste.

  Thresholds (minimum run length, line count) are **[tune by eval]**. Keep segmentation deterministic and dependency-free in this spec; do not add an optional parser whose presence changes segmentation between installs.
- **T1.2** `provider._reception_stage`: run `extract_alias_equations`, `classify_repair` and `extract_assertions` on authored segments only. Run `extract_assertions` on pasted/quoted prose and record results as document evidence; do not run assertion extraction on `kind=code`. Carry each source segment's half-open offsets with its extracted evidence. Preserve current repair behavior for turns with no pasted segment; when a turn is segmented, repair cues must occur in authored text. Do not introduce a length cutoff for repairs without a separate failing test and eval-backed threshold.
- **T1.3** `extract_publish.publish_rules` (`:51-55`): same split. `record_rule_claims_on_conn` (`store_claims.py:489-517`) gets a `speaker` override that it passes through to `add_evidence`.
- **T1.4** `store_claims.add_evidence` (`:191-222`) derives the speaker from `info.role` through `_SPEAKERS` (`:99`, `:193`). Add a validated speaker override and optional `seg_start`/`seg_end` (support both consistently through `record_rule_claims_on_conn`), plus `'document'` in the speaker allowlist. Use extractor `rules_document` so user and document evidence from the same span do not collide on `uq_claim_evidence_event` (`V18:184-186`). The partial unique key still permits only one live row for the same claim/evidence identity per extractor version; define segment offsets as provenance for that row, not as a promise to retain duplicate identical occurrences.
- **T1.5** `extraction_worker._load_model_batch_input`: send only authored segments as model turns. Do not send pasted segments to the model in this spec: model output is currently published against the source span's role, so a separate prompt field alone cannot guarantee document attribution. Document claims come from the deterministic rules path. A future model-based document extractor needs an explicit source-kind field and speaker-aware publish path.
- **T1.6** `extraction_jobs.should_enqueue_model`: score authored segments only.
- **T1.7** `timeline.rebuild`: no change needed (non-user is already unconfirmed). Add a test that `document` evidence never closes a user interval.
- Bump `RULES_VERSION` to `"2"` (C10).

### 1.3 Schema/migration: `V<N>__document_speaker.sql`
```sql
-- claim_evidence.speaker CHECK was declared inline in V18:167 (auto-named).
ALTER TABLE claim_evidence DROP CONSTRAINT IF EXISTS claim_evidence_speaker_check;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'claim_evidence'::regclass
       AND conname = 'claim_evidence_speaker_check_v2'
  ) THEN
    ALTER TABLE claim_evidence ADD CONSTRAINT claim_evidence_speaker_check_v2
      CHECK (speaker IN ('user','assistant','document'));
  END IF;
END $$;
ALTER TABLE claim_evidence ADD COLUMN IF NOT EXISTS seg_start INT;
ALTER TABLE claim_evidence ADD COLUMN IF NOT EXISTS seg_end   INT;  -- half-open offsets into stored conversations.content
```
Offsets are relative to the stored, at-most-8000-character turn, not the original unbounded input. Require both offsets to be null or satisfy `0 <= seg_start <= seg_end <= char_length(content)` at write time; document which segment wins if the same evidence is found in multiple identical segments.

### 1.4 Acceptance & gate
- Tests 1–6 pass, and the existing `tests/integration/test_claims_repair.py` and `test_claims_timeline.py` still pass.
- On golden v2, pasted-span false grounding is **0**. User-claim recall is not below the G0 baseline.
- **Gate G1:** Ray signs off. Rollback means reverting the code: the CHECK widening is additive, and old code never writes `document`.

### 1.5 Out of scope
Document entities and ingestion (episode-type sources such as Graphiti's `text` vs `message`), and summarizing pastes (Spec 4 covers that at document trust).

---

## Spec 2: First-person functional rules (rules v2)

**Goal.** Capture the facts people actually state about themselves, deterministically. Today:
- `extract_assertions` only matches `<Capitalized> is|uses <obj>` (`reception.py:89-92`, `:140-163`);
- first-person subjects are dropped (`_STOP_FIRST`, `reception.py:78-83`, check at `:153`);
- negations are dropped (`_NEGATION`, `:88`, `:155`).

So `I live in NYC` → `I moved to Austin` never produces a functional `lives_in` timeline. The `self` entity already exists: `entities.SELF_NAME`/`is_first_person` (`entities.py:14`, `:17`), and `store_claims.ensure_entity` maps first-person surfaces to it (`store_claims.py:77-78`).

### 2.1 Failing tests first (`tests/test_reception_self.py`, plus `tests/integration/test_self_timeline.py`)
1. `"I live in NYC."` → `{subject:"I", verb:"lives_in", object:"NYC", polarity:"positive"}`.
2. `"I moved to Austin last week."` → `lives_in Austin`. The relative time is not parsed; event time is the turn time.
3. `"I'm back in NYC."` → `lives_in NYC`.
4. `"I work at Acme"` / `"I work for Acme"` / `"I joined Acme"` → `works_for Acme`.
5. `"My role is staff engineer"` / `"I'm a staff engineer"` → `role_is staff engineer`. Abstain on `"I'm tired"` and `"I'm not sure"`.
6. `"I don't work at Acme anymore"` → `works_for Acme`, polarity `negative`. The DB test asserts the interval is closed through `timeline.py:93-98`.
7. DB: NYC→Austin→NYC through **extraction** (not direct writes) leaves exactly one current `lives_in NYC` for `self`.
8. Abstain on questions (`"Do I live in NYC?"`), hypotheticals (`"If I lived in Austin…"`), reported speech (`"She said I work at Acme"`) and pasted segments (Spec 1).
9. `Home is NYC` still produces non-functional `is`. V22 stays reverted, and there is a regression test for that.
10. A self fact stored for owner A cannot be returned or used to alter owner B's timeline.

### 2.2 Implementation tasks
- **T2.1** Add `reception.extract_self_assertions(text) -> list[dict]`. It is a small ordered table of anchored templates per canonical verb, returning the **canonical verb name** directly. `store_claims.verb_row` (`:125-136`) accepts canonical names, so no synonym rows are needed. Call it from the same three sites as Spec 1 (authored segments only), after `extract_assertions`.
- **T2.2** Predicate normalization: one table `SELF_TEMPLATES: dict[str, tuple[re.Pattern, ...]]` covering:
  - `lives_in` (live in, moved to, relocated to, back in);
  - `based_in` (I'm based in). **Settled (Decision 2):** it stays a separate functional slot from `lives_in` (today's V18 schema) and does **not** supersede it;
  - `works_for` (work at/for, joined, employed by);
  - `role_is` (my role is, I'm a/an + role noun, I work as).

  All four verbs are already seeded as functional in V18 (`V18:67-81`). Some phrases are also in `verb_synonym` (`V18:82-85`). Trim objects the way `extract_assertions` does (`reception.py:156`: strip and drop the trailing `.`), and also cut trailing time phrases (`last week`, `now`, `anymore`) with a small list. Reject an object over 6 tokens (**[tune by eval]**).
- **T2.3** Negation: self-templates emit `polarity='negative'` for explicit `don't|no longer|not … anymore` forms. Treat `left|quit` as positive state changes only when the sentence names the destination/employer transition; otherwise abstain rather than inventing a negative object. `_NEGATION` dropping stays unchanged for the generic `is`/`uses` rule. Tests must cover a prior matching functional value (closed interval) and no prior value (no positive claim manufactured).
- **T2.4** Subject: emit `"I"`. `ensure_entity` resolves it to `self`. `format_facts` renders `self` as "You" or the owner name (check `provider_helpers.py:416-520`, and add a test if it does not).
- **T2.5** Bump `RULES_VERSION` (C10). **Settled (Decision 6):** the rules-v2 backfill of the installed DB is not part of this spec's PR. It runs as a separate step after Specs 0–2 are merged, only after a `pg_dump -Fc` backup and Ray's explicit OK (C3).

### 2.3 Schema/migration
None. `lives_in`, `based_in`, `works_for` and `role_is` already exist as functional verbs (`V18:67-81`). **Do not** change `is` to functional (that was the reverted V22). Optional: add rows to `verb_synonym` (`moved to`, `relocated to`, `work at`) for the model tier's phrase normalization. That would be an idempotent `INSERT … ON CONFLICT DO NOTHING` in a `V<N+1>` migration, used only if Spec 3 needs it.

### 2.4 Acceptance & gate
- Tests 1–10 pass. On golden v2 (`rules_only`), the `functional_current`, `history` and `negation` questions pass in `vector` and `hybrid`.
- Precision on the abstain set does not drop below the G1 run. Recall targets are **[tune by eval]**.
- **Gate G2:** Ray signs off.

### 2.5 Out of scope
- Slot groups (`lives_in` vs `based_in` superseding each other). **Settled (Decision 2):** not built now. Revisit only if the golden suite shows misses between the two slots. That would need a timeline-key change in `store_claims.timeline_key` (`:135-136`).
- Relative-date parsing, spaCy/coreference (Page phase 3), and gazetteers.

---

## Spec 3: Model-tier fixes

**Goal.** Make `rules_then_model` (opt-in, never default) actually add value on first-person and paraphrased facts.

### 3.1 Current defects (verified)
- **Model-generated self aliases are not normalized.** `model_extract.grounded_claims` (`:29-68`) already accepts a literal first-person subject such as `"I"` when it occurs in the turn (`_keep_surface`, `:23-26`; `_occurs`, `:16-20`). But outputs such as `"user"` fail the turn-occurrence check, and `"the user"` is abstained. Normalize those model aliases to `"I"` only when the source turn has a first-person cue.
- **No conversational context.** `extraction_worker.py:115` passes `context=[]`.
- **Routing.** `should_enqueue_model` (`extraction_jobs.py:36-70`) skips turns whose nouns rules already "covered". A turn where rules found only `X is Y` but the user also stated a self fact is skipped.
- **The prompt names a verb that isn't allowed.** The batch instructions' example (`extract_model_client.py:18-28`) uses `founded`, which is not in the verb list. There is no first-person example and no schema-level constraint. `response_format` is plain `json_object` (`:63`). Validation happens after the fact (`:81-92`).

### 3.2 Failing tests first (`tests/test_model_extract.py` additions, using a fake model fn; no network)
1. Model output `{"subject":"I","verb":"lives_in","object":"NYC"}` for turn `"I live in NYC"` remains accepted as a regression control (this already passes today).
2. Output subject `"user"` / `"the user"` is mapped to `I` and kept only when the turn contains a first-person cue (fails today). Output subject `"I"` for a turn with no first-person token is dropped.
3. A verb outside the allowed list (`founded`) is dropped, and so is an object that does not occur in the turn.
4. Routing: `should_enqueue_model("I moved to Austin; Nightingale is a service", recent)` is True even when `Nightingale` is covered.
5. The client builds a `json_schema` response format with `verb` as an `enum` of the loaded verbs when `extract_model_schema="json_schema"`, and uses `json_object` when that mode is configured.
6. DB/model-batch test: a span id from owner A cannot be loaded or published as evidence for owner B.

### 3.3 Implementation tasks
- **T3.1** In `model_extract.grounded_claims`, preserve the existing direct `"I"` behavior and map `user|the user|me|myself` to `I` only when the turn has a first-person cue. Objects keep the strict `_occurs` check.
- **T3.2** In `extraction_worker` (`:115`), pass the immediately previous turn of the same session as a separately labeled prompt-context field. Context may help interpret the user turn, but it is never grounding evidence: `grounded_claims` must check the subject and object against that specific user span only. Do not pass prior-turn context to its grounding corpus.
- **T3.3** In `extraction_jobs.should_enqueue_model`, add the rule "enqueue when a first-person cue is present and the self-templates found nothing". Thresholds are **[tune by eval]**.
- **T3.4** Prompt in `extract_model_client.py:10-28`:
  - generate the verb list from the DB verb rows (one source of truth);
  - replace the `founded` example with allowed verbs;
  - add first-person, negation and abstain examples (question, hypothetical, pasted doc);
  - bump `MODEL_VERSION` (C10).
- **T3.5** Add config `extract_model_schema: "json_object" | "json_schema"` (default `json_object`) with explicit validation and environment/YAML precedence following the existing config pattern. With `json_schema`, send the documented OpenAI-compatible schema with the loaded verb enum. Support is endpoint-dependent: do not claim every OpenAI-compatible server implements it, and do not silently downgrade a requested schema mode. Keep post-validation in both modes and surface unsupported-format errors through the existing extraction-job retry/log path.
- **T3.6** First record the deferred `rules_then_model` baseline on golden v2, then run the model bake-off: the current default model vs. one or two small local models (for example Qwen2.5-7B-Instruct, NuExtract). Report P/R and latency per model. No numbers are assumed here. **Settled (Decision 3):** the model and the `extract_model_schema` mode are chosen from these results at Gate G3, not before.

### 3.4 Schema/migration
None. Model and prompt identity go in the existing `claim_evidence.extractor` / `extractor_version` (`V18:175-176`), for example `extractor='model'` and `extractor_version='<MODEL_VERSION>:<model-id>'`. **This replaces Ray's `extractor_model`/`extractor_prompt_version`/`model_version` columns and the `UPDATE … 'unknown'` backfill, which would be redundant writes.**

### 3.5 Acceptance & gate
- Tests 1–6 pass. On golden v2 with `rules_then_model`, claim recall is at least the `rules_only` G2 recall, and precision on the abstain and paste sets is unchanged.
- **Gate G3:** Ray picks the model and the schema mode from the T3.6 bake-off results (Decision 3). Until then, `rules_then_model` stays opt-in with the current model and `json_object`.

### 3.6 Out of scope
Making `rules_then_model` the default, and fine-tuning.

---

## Spec 4: Assistant replies as unconfirmed evidence

**Goal.** Let assistant replies (which often organize and decide) contribute claims, **always unconfirmed**, without letting memory feed itself. Promotion stays off until labeled data exists.

### 4.1 Where replies are blocked today (the "three sites", now named)
1. `provider._reception_stage` (`provider.py:662`): rules return early on `role != "user"`. **Keep this.** Rules stay user-only.
2. `provider._enqueue_extraction_jobs` (`provider.py:775`): the model batch only enqueues user turns. **Open this, behind a flag.**
3. `extraction_worker._load_model_batch_input` (`extraction_worker.py:40`): skips non-user spans. **Open this, behind a flag.**

(The fourth site, `extract_publish.publish_rules:51`, stays user-only.)

The speaker needs no new code. `add_evidence` already maps span role `assistant` to `speaker='assistant'` (`store_claims.py:99`, `:193`). `timeline.rebuild` already keeps such claims unconfirmed (`timeline.py:110-123`).

### 4.2 Failing tests first (Ray's echo cases, kept)
1. **Echo backs up a user fact:** the user says `I live in NYC` and the reply says `Noted: you live in NYC`. Claim count stays 1, live evidence count is 2 (one user, one assistant), and status stays `grounded`. This works through `placeholder_claim` and `rebuild_key` (`store_claims.py:174-189`, `:244-292`) under `claims_current_key` (`V18:129-133`).
2. **A novel assistant-only fact** (`Your deploy target is Fly.io` with no user mention) creates an `unconfirmed` claim and is **never** rendered in the grounded facts block (`format_facts`).
3. **A reply with no fact** creates no claim.
4. **Cross-owner:** owner A's assistant claims are invisible to owner B.
5. **Feedback loop:** extracting the same reply twice adds no new evidence (`uq_claim_evidence_event`). A reply that quotes an injected `<memory>` block produces no claims from inside the block.
6. **Resolving "you":** `you/your` in the reply maps to `I` (self). The assistant's own `I/we` never becomes a self subject.
7. A reply containing only a stripped memory block is not sent to the model; a mixed reply yields claims only from its remaining assistant-authored text.

### 4.3 Implementation tasks
- **T4.1** Add config `extract_assistant_replies: bool = False`. When true and `extract_mode == "rules_then_model"`, sites 2 and 3 let assistant spans through. Batches stay paired (the user span plus the following reply span). Payloads stay ids-only (`store_jobs.job_add_model_batch`, `store_jobs.py:65`).
- **T4.2** `model_extract.grounded_claims` takes a `speaker` argument. For assistant spans, map `you|your` to `I` and drop subjects `I|we|me`. Ground against the reply text.
- **T4.3** Add `turn_filter.strip_memory_blocks(text)` (Ray's `stripMemoryBlocks` contract, in Python, with the repo's real tags). It removes:
  - `<memory>…</memory>` (`provider_helpers.py:397`);
  - `<PAST_CONTEXT>…</PAST_CONTEXT>` (`provider_helpers.py:252`);
  - the grounded-facts and supporting-turns sections that `format_facts` emits;
  - the future `<prior_work>` block (Spec 6).

  Recognize only the exact known tags, track nesting, and preserve code fences. Remove an unmatched opening block through end-of-input; remove an unmatched closing tag without discarding surrounding prose. If stripping leaves no content, skip extraction. Apply it to extractor input for both speakers. Do not log the content; if correlation is necessary, log only a keyed digest and non-sensitive counts. Also **verify** whether Hermes stores injected context in `conversations.content` at all. If not, the stripper is defense in depth.
- **T4.4** Do not relabel an assistant reply as document evidence merely because its object also appears in a paste. Evidence speaker describes who made the statement; source provenance describes where its content originated. In this phase, ground assistant claims only against the assistant reply and keep them `speaker='assistant'`/unconfirmed. Do not pass pasted text as grounding context. If later work needs provenance from a paste to an assistant summary, define and test a separate source-span/segment reference first; it must not promote the claim to user-grounded.
- **T4.5** Do not implement uptake promotion or add a dormant `promote_on_uptake` path in this spec. Assistant evidence remains unconfirmed. Promotion requires a labeled set (golden v2 `assistant_unconfirmed` items plus hand-labeled real sessions), a separately reviewed promotion policy, and Ray's explicit approval.

### 4.4 Schema/migration
None (the speaker `assistant` is already allowed at `V18:167`).

### 4.5 Acceptance & gate
- Tests 1–7 pass. With the flag on, every eligible non-empty reply is enqueued (the count of assistant spans with a model job equals the count of eligible assistant spans after stripping).
- **0** assistant-only claims appear in the grounded block on golden v2, and no user `current` value changes because of a reply.
- Model-call cost per session is reported (async; nothing on the prompt path).
- **Gate G4:** Ray signs off. Rollback means setting the flag to false. Existing assistant claims stay unconfirmed and are never rendered.

### 4.6 Out of scope
Turning promotion on, and grounding from assistant evidence.

---

## Spec 5: Work memory (`work_item` / `work_artifact`)

**Goal.** Remember what was done, decided, tried and failed, so the agent doesn't redo or re-attempt work. This is a separate record type and **never goes in `claims`**.

### 5.1 Corrected example (replaces Ray's "fake branch")
Branch `shadow-replay` holds an earlier `scripts/shadow_replay.py` with placeholder metrics. `shadow-replay-v2` (merged into `hybrid-fixes` as `f89fadd`) is the real replay. **Both branches exist in git.** The first attempt failed on *outcome* (the metrics weren't real), not on existence. So the failure status comes from the user's or a reviewer's verdict and the eval output, **not from a git check**. Git only checks that artifacts still exist and whether they are stale.

### 5.2 Failing tests first (`tests/integration/test_work_memory.py`)
1. Fixture of three sessions:
   - (a) build shadow replay → user verdict "these numbers are fake / placeholder" → `work_item.status='failed'`, with the reason taken from the verdict span;
   - (b) rebuild on `shadow-replay-v2` → eval JSON present, tests pass → `status='done'`, artifacts = branch, commit `f89fadd`, file `scripts/shadow_replay.py`;
   - (c) a later session asks for shadow replay again → `(b)` is retrievable as current, and `(a)` is marked superseded but still queryable.
2. No row is written to `claims` or `claim_evidence` by work capture.
3. Supersession: explicitly retracting `(b)` marks it `retracted`, restores `(a)` as current, and clears `(a).superseded_by`, using the owner/topic current-pointer row. Replacing a current item and updating that pointer are atomic.
4. `on_session_end` (`provider.py:1528`) returns without waiting for extraction: capture is enqueue-only. A crash between session end and job run loses nothing, because the job re-reads spans from `conversations`.
5. The job payload has ids only (`owner`, `session_id`, first and last span id). No text.
6. Cross-owner isolation.
7. Deleting the sole reason span for a failed/abandoned item atomically changes it to `unknown` before the span is deleted, so the reason constraint and data-deletion contract both hold.
8. Retrying the same durable work job does not duplicate its work item or artifacts.

### 5.3 Implementation tasks
- **T5.1, durable capture.** Do **not** add an in-memory message buffer.
  - User and assistant turns are already durable in `conversations` through `sync_turn` (`provider.py:340-360`), subject to the T0.1 noise fix.
  - **Verified Hermes source evidence (2026-11-17):** checked against the clean local Hermes Agent checkout at `8237c7f8ec179c88857f24a174509d985af704b7`. `agent/message_metadata.py:92-94` defines `mint_uid()` as `uuid4().hex`; `hermes_state_messages.py:304-323` mints an assistant call's UID and pairs it onto the tool-result row; `hermes_state_common.py:560-561` persists `tool_call_uids` and `tool_call_uid`; `hermes_state_identity.py:77-89` restores them. `tests/hermes_state/test_message_uid.py:284-329` verifies pairing, restore, compaction, copies and import. This establishes a durable occurrence key within SessionDB—not a provider ID and not a cross-session/global identity. Repeated provider IDs in one assistant response share a UID; a folded row may retain multiple UIDs for repeated occurrences. `agent/message_sanitization.py:542-552` documents that providers reuse IDs, new collisions are renamed, and historical IDs are not rewritten. The memory provider receives `messages` in `sync_turn` (`memory_provider.py:151-157`; manager `:460-466`), but hybrid-age currently ignores that argument and stores only user/assistant text. The executor does not pass `tool_call_id` to `handle_tool_call` (`tool_executor.py:1707-1710`), and hybrid-age's hook is a stub (`provider.py:1566-1567`); do not rely on a separate tool-event hook.
  - **Re-verified (2026-11-19), zero drift:** same commit `8237c7f8ec179c88857f24a174509d985af704b7` still HEAD. Full narrower contract -- the three facts T5.1 needs (stable uid presence, id reuse/collision handling, exact fields on the live dict) -- is written out in `docs/hermes-tool-call-identity-contract.md`, with a matching fixture (`tests/fixtures/hermes_messages_contract.py`) and a contract test (`tests/test_hermes_messages_contract.py`) that runs the fixture through hermes-agent's own functions whenever a local checkout is importable (skips, doesn't fail, otherwise). **Correction the contract test caught on its first real run:** the live dict keys are `_tool_call_uid` / `_tool_call_uids` (leading underscore) -- not `tool_call_uid`/`tool_call_uids`. Use the underscore-prefixed keys everywhere in T5.1.
  - Capture tool digests from `messages` after queuing the corresponding conversation turns, with one `work_event` per tool occurrence. Prefer `tool_call_uid` as `source_event_id`. A `tool_call_id` fallback is allowed only when it is unique within the durable session history and paired with an occurrence discriminator; never use the raw provider ID alone. If no stable occurrence key can be established, skip that event rather than inventing identity, and expose a reason-coded skip count without logging event IDs or content. Prefix source-key kinds to prevent UID/fallback collisions. Persist only allowlisted, bounded digests when `work_capture` is enabled (default false): tool name/status, repository-relative paths, branch/commit/PR refs and test counts. Never store tool arguments, command lines, raw output, secrets or arbitrary response fields.
  - Tests must cover replay of one UID (same row), distinct UIDs with a reused provider ID (distinct rows), preservation of IDs through redaction, the safe fallback, and the no-stable-key case.
- **T5.2, enqueue.** Add `work_capture: bool = False` using existing config validation conventions. In `on_session_end`, enqueue `extract_work` next to the existing model-batch flush (`store_jobs.py:46`) only when enabled. Keep the existing conversation-write drain as a durability barrier; enqueue only after the session's spans and tool digests are durable. The callback may wait for capture/queue work as it does today, but must not wait for `extract_work` execution. Use a stable op id and id-only payload bounded to the session/span range. Use the same pattern at task boundaries only if Hermes exposes a durable boundary.
- **T5.3, worker.** Add `extraction_worker._run_work` next to `_run_rules` (`:61`), claimed in `run_extraction_once` (`:139`). It rebuilds the session from `conversations` and `work_event`, strips memory blocks (T4.3), and has the model (or rules for clear cases) output `{ask_summary, outcome_summary, topic_key, status, reason, reason_span_id, reason_event_id, artifacts[]}`. The extractor may emit only `done|failed|abandoned|unknown`; `retracted` is a separate explicit user action, never a model prediction. Validate all source ids against the same owner/session. Persist the work item, artifacts, current pointer and fenced job settlement atomically, following `publish_rules`; the stable job `op_id` is the retry-idempotency key. Under an owner/topic lock, insert the successor, upsert `work_item_current`, and mark the predecessor superseded in the same transaction.
- **T5.4, status sources, in priority order:** an explicit, unambiguous user verdict span, then eval/test output in `work_event`, then a tool status only when the tool contract makes it authoritative. Phrases such as "works, thanks" are not automatically verdicts without a tested cue/label policy. With no evidence, the status is `unknown`. Never infer `failed` from a missing git object. An explicit retraction is handled as a separate user operation: mark the current item `retracted`, restore its predecessor as current if one exists, and do so transactionally.
- **T5.5, artifact verification** (async, best effort): verify refs only inside configured repo roots using subprocess argument arrays (never a shell), bounded timeouts, and validated refs; do not fetch or otherwise access the network. **Settled (Decision 5):** config `work_verify_repo_roots: list[str]` starts with only the hermes-memory checkout. Verification runs **only in the extraction worker** (`_run_work` or a follow-up job), never at prefetch, and prefetch only reads the stored `exists_now`/`stale`. Store the result in `verified_at` / `exists_now`. Mark items stale when a referenced path changes after `verified_at` (Spec 6 shows it). An unverifiable artifact is kept and flagged, never dropped.
- **T5.6** `ask_summary`, `outcome_summary` and `reason` are short model summaries, not pasted text. Cap each at an eval-tuned character limit. Pasted segments (Spec 1) are excluded from summarization input.
- **T5.7** Treat summaries and artifact refs as persisted user data: enforce the character/field limits before write, owner-scope every read and delete, and ensure the documented span/session deletion path removes or de-identifies dependent work data consistently. No logs or shadow rows may contain these summaries.

### 5.4 Schema/migration: `V<N+1>__work_memory.sql`
```sql
CREATE TABLE IF NOT EXISTS work_event (   -- tool-result digests; no raw output
  owner TEXT NOT NULL,
  id BIGSERIAL PRIMARY KEY,
  session_id TEXT NOT NULL,
  span_id BIGINT,
  source_event_id TEXT NOT NULL,          -- stable tool-call/event id for replay deduplication
  tool TEXT NOT NULL,
  status_code INT,
  refs JSONB NOT NULL DEFAULT '{}',        -- allowlisted, size-capped paths/refs/counts only
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (owner, id),
  UNIQUE (owner, session_id, source_event_id),
  FOREIGN KEY (owner, span_id) REFERENCES conversations (agent_identity, id) ON DELETE SET NULL (span_id)
);

CREATE TABLE IF NOT EXISTS work_item (
  owner TEXT NOT NULL,
  id BIGSERIAL PRIMARY KEY,
  session_id TEXT NOT NULL,
  topic_key TEXT NOT NULL,                -- normalized task/topic key
  source_op_id TEXT NOT NULL,              -- stable extract_work op_id; retry idempotency
  ask_summary TEXT NOT NULL,
  outcome_summary TEXT,
  status TEXT NOT NULL CHECK (status IN ('done','failed','abandoned','unknown','retracted')),
  reason TEXT,
  ask_span_id BIGINT NOT NULL,
  outcome_span_id BIGINT,
  reason_span_id BIGINT,
  reason_event_id BIGINT,
  superseded_by BIGINT,
  extractor_version TEXT NOT NULL,
  embedding vector(768),                  -- this provider's fixed embedding dimension
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (owner, id),
  UNIQUE (owner, topic_key, id),
  UNIQUE (owner, source_op_id),
  FOREIGN KEY (owner, ask_span_id) REFERENCES conversations (agent_identity, id) ON DELETE CASCADE,
  FOREIGN KEY (owner, outcome_span_id) REFERENCES conversations (agent_identity, id) ON DELETE SET NULL (outcome_span_id),
  FOREIGN KEY (owner, reason_span_id) REFERENCES conversations (agent_identity, id) ON DELETE SET NULL (reason_span_id),
  FOREIGN KEY (owner, reason_event_id) REFERENCES work_event (owner, id),
  FOREIGN KEY (owner, superseded_by) REFERENCES work_item (owner, id),
  CONSTRAINT work_item_reason_required CHECK (
    status NOT IN ('failed','abandoned') OR
    (reason IS NOT NULL AND (reason_span_id IS NOT NULL OR reason_event_id IS NOT NULL))
  )
);

CREATE TABLE IF NOT EXISTS work_item_current (
  owner TEXT NOT NULL,
  topic_key TEXT NOT NULL,
  work_item_id BIGINT NOT NULL,
  PRIMARY KEY (owner, topic_key),
  FOREIGN KEY (owner, topic_key, work_item_id) REFERENCES work_item (owner, topic_key, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS work_artifact (
  owner TEXT NOT NULL,
  id BIGSERIAL PRIMARY KEY,
  work_item_id BIGINT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('file','branch','commit','pr','eval')),
  ref TEXT NOT NULL,                      -- repository-relative path / branch / sha / PR number / eval identifier
  repo TEXT,
  exists_now BOOLEAN,
  verified_at TIMESTAMPTZ,
  stale BOOLEAN NOT NULL DEFAULT false,
  UNIQUE (owner, work_item_id, kind, ref),
  FOREIGN KEY (owner, work_item_id) REFERENCES work_item (owner, id) ON DELETE CASCADE
);

-- durable_job.kind CHECK is inline in V19:11 (auto-named): drop & re-add with 'extract_work'.
ALTER TABLE durable_job DROP CONSTRAINT IF EXISTS durable_job_kind_check;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'durable_job'::regclass
       AND conname = 'durable_job_kind_check_v2'
  ) THEN
    ALTER TABLE durable_job ADD CONSTRAINT durable_job_kind_check_v2
      CHECK (kind IN ('capture','extract_rules','extract_model','extract_work'));
  END IF;
END $$;
```
Note: `ON DELETE SET NULL (col)` needs Postgres ≥ 15; the installer image is PostgreSQL 17. A plain `SET NULL` on a composite FK would also null `owner`, which is NOT NULL. `work_event` must be created before `work_item` because a failed/abandoned item may cite an event as its reason. The `(owner, superseded_by)` FK is deliberately composite to prevent cross-owner supersession. `work_item_current` is the authoritative current pointer; it avoids an unworkable insert ordering with a partial unique index on `superseded_by IS NULL` (the successor id is not available before inserting it). Under an owner/topic lock, insert the successor, atomically upsert the current pointer and mark its predecessor as superseded. The 768 dimension is fixed by this provider's embedding contract; reject incompatible `embed_dim` rather than implying the SQL vector type follows runtime config. When a reason span is deleted, the delete path must atomically set the item to `unknown` before deleting the span, and clear/retain its reason according to the data-deletion policy so the reason constraint remains valid.

Changes from Ray's `work_record`: `BIGSERIAL` instead of `uuid`, which matches nearby tables. Span foreign keys replace free-text `ask`. Artifacts get their own table instead of JSONB, so staleness can be updated per artifact. Failed/abandoned items require a bounded reason and a same-owner source span or tool event. `unknown` is allowed; `retracted` is reserved for an explicit user retraction and is never model-predicted. Tool-event replay has a stable unique key. The job kind CHECK is changed.

### 5.5 Acceptance & gate
- Tests 1–8 pass. The shadow-replay fixture gives `failed` (verdict) followed by `done` (eval and tests).
- `on_session_end` latency is unchanged within noise. The `extract_work` jobs drain in the eval run.
- **Gate G5:** Ray signs off. **Applying the migration to `:5452` follows C3.** Rollback: stop enqueueing (`work_capture: bool = False`). The tables stay and are not surfaced.

### 5.6 Out of scope
Procedural or skill libraries (Voyager-style), and reflection summaries (Generative Agents). Both are later candidates.

---

## Spec 6: Prior-work surfacing (`prior_work_mode = off | shadow | on`)

**Goal.** Before work starts, show a short "prior work" block. Start in shadow mode and serve only after the gate passes.

### 6.1 Failing tests first
1. With `prior_work_mode="off"` (the default), prefetch output is byte-identical to today.
2. With `shadow`, prefetch output is unchanged. One `work_shadow` row is written, holding `query_key` (16 bytes from `shadow.query_key`), `work_ids` and `latency_ms`, and **no text columns**. A test asserts the table has no `text`/`varchar` column apart from `owner`.
3. With `on`:
   - the `<prior_work>` block lists the current item with status, the reason for failed items, and artifact refs with a stale flag;
   - the facts-block token count is identical to `off` (the facts cap is never borrowed);
   - total tokens are at most `injection_token_cap(max_tokens)`;
   - prior work stays within `prior_work_token_budget`, truncating the oldest items first.
   - rendered summaries and refs are escaped as data and clearly labeled untrusted; they cannot introduce new prompt-control markup.
4. A query for "set up shadow replay" surfaces the `done` item and the superseded `failed` item with its reason.
5. Cross-owner: owner B never sees owner A's items.

### 6.2 Implementation tasks
- **T6.1** Config (`config.py`):
  - `prior_work_mode: str = "off"`, validated like `retrieval_mode` (`:196-205`);
  - `prior_work_token_budget: int = 200` **[tune by eval]**. **Settled (Decision 4):** it starts at 200 and is taken from the supporting-turns share inside the 1200 total (never from the 400 facts cap). It is tuned from `work_shadow` data before G6;
  - `prior_work_top_k: int = 3`.
- **T6.2** Add `store_work.search_work(owner, query_vec, topic_terms, k)`. It does nearest-neighbour search over current items joined through `work_item_current`, plus the superseded predecessor of each hit when its status is failed. Owner-scoped. Trigger it only on task-intent queries (a simple imperative or verb cue, **[tune by eval]**).
- **T6.3** In `provider._aprefetch` (`provider.py:1100`), in both branches (hybrid `format_facts` at `:1302-1318`, vector `format_span_injection`):
  - **shadow:** `schedule_shadow(record_work_shadow, …)` (`shadow.py`), reusing the `retrieval_shadow` sampling (`shadow_sample_rate`);
  - **on:** render `<prior_work>` before the facts with `provider_helpers.format_prior_work(items, budget)`. Count the complete rendered prior-work block (including tags) against the total cap, preserve the existing facts budget, and pass the reduced remaining total budget to supporting-turn formatting.
- **T6.4** Add `<prior_work>` to `strip_memory_blocks` (T4.3).
- **T6.5** Offline comparison: extend `scripts/shadow_replay.py` to join `work_shadow` ids with the golden v2 labels.

### 6.3 Schema/migration: `V<N+2>__work_shadow.sql`
```sql
CREATE TABLE IF NOT EXISTS work_shadow (
  id BIGSERIAL PRIMARY KEY,
  owner TEXT NOT NULL,
  query_key BYTEA NOT NULL CHECK (octet_length(query_key) = 16),
  work_ids JSONB NOT NULL DEFAULT '[]',
  latency_ms JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_work_shadow_owner_time ON work_shadow (owner, created_at);
```
This mirrors `retrieval_shadow` (`V20:5-13`).

### 6.4 Acceptance & gate
- Tests 1–5 pass. A shadow run over golden v2 and replayed real sessions shows **0** budget violations and **0** cross-owner rows.
- Repeated-work rate and failed-approach re-attempt rate are reported (definitions in the Page's eval section). Targets are **[tune by eval]**.
- **Gate G6:** Ray explicitly approves turning `on`. Rollback means setting the mode to `off`. No deploy is needed.

### 6.5 Out of scope
Changes to the UI or skins, and automatic re-verification schedules.

---

## Test matrix (from Ray's draft, corrected)

| Spec | Unit (pure) | Integration (DB, `--migrate`) | Eval gate |
|---|---|---|---|
| 0 | `_is_noise` keeps short facts; fixture schema and ideal labels | golden ingest completes; dropped-turn reasons; owner isolation | G0 rules-only baseline reviewed |
| 1 | `segment_turn` on email, log, code, quote, mixed | paste → 0 user evidence rows, 0 repairs, document evidence unconfirmed | 0 pasted false grounding |
| 2 | self-templates, negation, abstain | NYC→Austin→NYC via extraction; `is` still non-functional; owner isolation | functional / history / negation questions pass |
| 3 | grounding for self, verb enum, routing | model-batch with fake model fn; owner-scoped spans cannot cross batches | recall ≥ rules_only; abstain precision kept |
| 4 | `strip_memory_blocks`; you→I mapping; empty-after-strip skip | echo dedup; novel → unconfirmed; double-extract idempotent | 0 assistant-only claims in grounded block |
| 5 | status precedence; payload ids-only | shadow-replay fixture failed→done; supersede/restore via explicit retraction; reason-span deletion; retry dedup; no claims rows | jobs drain; session end non-blocking |
| 6 | budget truncation; mode validation | shadow row has no text; facts tokens unchanged | 0 budget violations; 0 cross-owner |

Every spec also runs the existing suites (`tests/test_timeline.py`, `tests/integration/test_claims_*`, `tests/test_span_injection.py`, `tests/integration/test_hybrid_supersession.py`) and a retry/replay check (no duplicate evidence on job retry, via `uq_claim_evidence_event`).

## Rollout order
G0 → Spec 1 → Spec 2 → Spec 3 → Spec 4 (flag off) → Spec 5 (`work_capture` off until G5) → Spec 6 (`shadow` → `on` only after G6). Specs 1 and 2 may be developed in parallel only on a shared feature base that already contains the segmenter; Spec 2 cannot merge first. Each migration takes the next free number when Ray merges it (C7). After Specs 0–2 merge: the installed-DB rules-v2 backfill, after a `pg_dump` backup and Ray's explicit OK (Decision 6, C3).

## Decisions (approved by Ray, Nov 17, 2026)
1. **T0.1 noise rule:** short user turns that carry an assertion signal or a first-person cue are exempt from the 40-char noise rule. Assistant turns keep the plain 40-char rule until Spec 4. `TURN_MIN_CHARS` is not lowered globally.
2. **`based_in` vs `lives_in`:** they stay separate functional slots (today's V18 schema). Revisit slot groups only if the golden suite shows misses.
3. **Model choice and schema mode:** decided from the T3.6 bake-off results at Gate G3. This is a gate, not an open question.
4. **`prior_work_token_budget`:** starts at 200 **[tune by eval]**, taken from the supporting-turns share within the 1200 total (never from facts), and tuned from `work_shadow` data.
5. **Artifact verification repo roots:** only the hermes-memory checkout to start (`work_verify_repo_roots`, a configurable list). Verification runs only in the worker, never at prefetch.
6. **Installed-DB backfill with rules v2:** runs after Specs 0–2 are merged, only after a `pg_dump -Fc` backup and Ray's explicit OK (C3).
7. **Model baseline timing:** G0 uses the reviewed rules-only baseline; record the first `rules_then_model` baseline as the first step of T3.6.

## Removed from Ray's draft, with reasons
- **TypeScript snippets** (`claims.findLive`, `stripMemoryBlocks`, `packContext`): replaced with pointers to the real Python functions.
- **`extractor_model` / `extractor_prompt_version` / `model_version` columns plus the `'unknown'` backfill:** redundant with `claim_evidence.extractor` / `extractor_version`.
- **"Expected V23":** numbering is decided at merge time, and V22 is free again after `c21de62`.
- **"Fake branch caught by git check":** wrong mechanism. Failure comes from verdict, eval or tool evidence (5.1).
- **`uuid` ids, free-text `ask`, an in-memory message collector:** replaced by BIGSERIAL, span FKs and durable capture.
- **Uptake promotion path:** deferred; no code path is built until labeled data and a separately reviewed policy exist (T4.5).
- **"5th recommendation" / "source note" placeholders and duplicated phase blocks:** removed. The Page is the source of truth, and the missing pieces (paste segmentation, first-person rules, model-tier fixes, eval probe) are now Specs 0–3.
- **The fixed 200-token prior-work cap:** 200 stays as the starting default (Decision 4), but it is marked **[tune by eval]** and taken from the supporting-turns share.
