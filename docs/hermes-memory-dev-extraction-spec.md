# Hermes Memory: dev-ready implementation spec for robust claim extraction

**Repo:** `hermes-memory`. **Baseline:** `main` at `1fd56c0`, and branch `hybrid-fixes` at `a504a65`. That branch adds `f89fadd` (real shadow replay), `e9d5463` (V22 "is functional"), `c21de62` (V22 reverted, probe moved to `lives_in`) and `a504a65` (golden rules_only JSON).
**Source documents:** Ray's draft spec (v0.4 author summary) and the research Page *Hybrid-age: making claim extraction robust*.
**Status:** spec only. Nothing in this document has been changed in the repo.

> **About line references.** `hybrid-fixes` changes no file under `src/` or `sql/`. Every `src/…:line` and `sql/…:line` reference below was checked against `main` and holds on both branches. References into `scripts/eval_memory.py`, `scripts/eval_golden_conversation.py`, `scripts/shadow_replay.py` and `tests/fixtures/golden_conversation.json` point at `hybrid-fixes`. Line numbers will drift as specs land. Treat each reference as the place to start reading.

---

## Constraints (apply to every spec and every PR)

| # | Rule |
|---|------|
| C1 | **Never push to `main`.** Work goes on feature branches. **Ray opens every PR.** The implementing agent pushes its branch and stops there. |
| C2 | **`retrieval_mode` stays `vector` by default** (`config.py:59`; validated at `config.py:201`). New behaviour sits behind flags whose default is off. |
| C3 | **Installed DB `127.0.0.1:5452`:** before **any** write (migration, backfill, re-extract, delete), take a `pg_dump -Fc` backup into `/home/rswan/hermes-memory-backups/`, **ask Ray, and wait for an explicit yes.** Reads are allowed. The rules-v2 backfill is scheduled for after Specs 0–2 merge (Decision 6), and C3 still applies to it. |
| C4 | **Do not touch `127.0.0.1:5450`.** No connections, no migrations, no containers. |
| C5 | **Never use `--reuse-dsn`.** Evals run against throwaway `*_eval` or `hermes_release_gate_*` databases. The scripts already refuse other DSNs (`eval_memory.py`, `eval_golden_conversation.py:31-32`). |
| C6 | **No raw text in shadow tables.** Shadow and eval tables hold only HMAC keys (`shadow.py:21-23`, 16 bytes), ids, counts and latencies. Raw text includes queries, rendered blocks, ask strings and pasted content. |
| C7 | **Migrations take the next free `V<N>` at merge time.** Today `main` and `hybrid-fixes` both stop at `V21` (V22 was reverted in `c21de62`). In this doc they are written `V<N>`, `V<N+1>`… Every migration PR re-checks `ls sql/migrations` right before Ray merges and renames if needed. Migrations must be additive and idempotent (`IF NOT EXISTS`, plus guarded `DO $$` blocks like V18). |
| C8 | **Tests first.** Every task starts with a test that fails on the current code, and the PR shows it failing before the fix. Use DB tests in `tests/integration/` (the `--migrate --dsn "$TEST_DSN"` pattern from the eval doc) and pure tests in `tests/`. |
| C9 | **No invented numbers.** Any threshold, budget or target marked **[tune by eval]** starts at the stated default, and only the Spec 0 harness may change it. |
| C10 | **Pin extractor versions.** Bump `RULES_VERSION`/`MODEL_VERSION` (`extraction_jobs.py:11-12`) whenever rules or the prompt change. They feed `op_id` (`extraction_jobs.py:28-33`) and `claim_evidence.extractor_version` (`V18:176`). Re-extracting the installed DB is a write, so C3 applies. |

### Global invariants (kept from Ray's draft, corrected)
- `claims` + `claim_evidence` stay the single semantic authority. `timeline.rebuild` (`timeline.py:76-147`) owns status and validity. Event time orders timelines (`timeline.py:41-42`).
- **Only `speaker='user'` evidence can ground a claim.** Non-user speakers are unconfirmed by construction (`timeline.py:110-123`). Spec 1 adds `document` and Spec 4 adds assistant claims. Neither may change this rule.
- The facts block stays inside its 400-token cap. The total stays inside `injection_token_cap(max_tokens)` (`tokens.py:27-28`; `max_tokens=1200`, `config.py:47`). New blocks are carved from supporting turns, **never from facts**.
- Every lookup is scoped by owner. Each spec needs a cross-owner test.
- Each spec has a rollback flag or is a pure revert. Nothing advances until the previous gate passes.

### Per-spec contract
Each spec has six parts: **Goal → Failing tests first → Implementation tasks (Python pointers) → Schema/migration outline → Acceptance criteria & gate → Out of scope.**

---

## Spec 0: Eval harness first (probe fix, golden suite, the 6/30 ingest gap)

**Goal.** Get a trustworthy, reproducible measurement before changing any extraction behaviour. Every later gate depends on this harness.

### 0.1 What already exists on `hybrid-fixes` (keep it, don't redo it)
- **The supersession probe is fixed.** `eval_memory.py:seed_lives_in_probe` writes NYC→Reykjavik→NYC as functional `lives_in` claims through `Store.record_rule_claims`. It reports `current_objects` and `current_is_nyc` from SQL, which replaces the old substring `has_nyc`/`has_austin` check at `main:scripts/eval_memory.py:258-259`. The cities are outside the planted `CITIES` corpus. `tests/integration/test_hybrid_supersession.py` covers the same case.
  - **Caveat for the record:** the probe writes claims directly. It tests timeline and retrieval, not extraction. Label it `timeline_probe` in the JSON. End-to-end extraction is measured by the golden suite (0.3).
- **Golden runner:** `scripts/eval_golden_conversation.py`, with fixture `tests/fixtures/golden_conversation.json` (30 turns, 10 expected claims, 8 questions).

### 0.2 Root cause of the 6/30 ingest gap (verified)
`sync_turn` drops every turn that `_is_noise` flags (`provider.py:349-351`). `_is_noise` treats any turn shorter than `TURN_MIN_CHARS = 40` as noise (`turn_filter.py:6`, `turn_filter.py:19-25`). I ran `_is_noise` over the fixture: **exactly 6 of 30 turns survive**, which matches `complete_rows=6`. Every fact turn (`"Home is NYC."` is 12 chars, `"Nightingale uses Postgres."` is 26) is dropped before storage. The 6 survivors are mostly questions and chatter. Two more problems follow:
- The runner expects `2 × user turns` rows (`eval_golden_conversation.py:39-43`, giving 30), so it always waits out the full 900 s deadline (`:56`).
- **This is a production bug, not just an eval bug.** A real `"I live in NYC."` (14 chars) is never stored, so it can never be extracted or retrieved.

Two smaller fixture problems:
- The expected claims use subject `Home` with verb `is`. After the V22 revert, `is` is non-functional (`V18:67-84`), so the expected `current` flags assume semantics the code no longer has.
- The question `city_now` ("Where is my city now?") asks in the first person, but the fixture only states facts about `Home`. Spec 2 adds first-person rules, so the fixture should state facts as `"I live in NYC."`.

### 0.3 Failing tests first
1. `tests/test_turn_filter.py` (new): `_is_noise("I live in NYC.") is False`, `_is_noise("Role is engineer.") is False`, `_is_noise("ok") is True`, `_is_noise("thanks!") is True`. **Fails today.**
2. `tests/test_golden_fixture.py` (new, pure): load the fixture. Assert that every `expected_claims[*]` verb exists in the V18 verb seed list, and that every question has `id`, `text`, `expected_answer`, `expect_contains`, `expect_absent` and `kind`. Fails today because `kind` is missing.
3. `tests/integration/test_golden_ingest.py` (new, DB): run the fixture through the provider path with `--migrate`. Assert that `complete_rows` equals the count of non-noise turns under the **new** rule, and that no question run hits the deadline. Fails today.

### 0.4 Implementation tasks
- **T0.1, noise rule.** Change `turn_filter._is_noise` (`turn_filter.py:19-25`) so a turn is noise only when it is empty or matches the existing ack/noise patterns, or when it is shorter than `TURN_MIN_CHARS` **and** carries no assertion signal. "Assertion signal" means `reception.extract_assertions` or the Spec 2 self-rules return anything, or a first-person cue (`I|my|I'm|we`) is present. Callers are `provider.sync_turn:350`, the noun extraction and the backfill paths (`rg -n _is_noise src/`); they keep the same signature. **Settled (Decision 1):** the exemption applies to user turns only. Assistant turns keep the plain 40-char rule until Spec 4. `TURN_MIN_CHARS` is not lowered globally. Add a role-aware entry point (for example `_is_noise(content, role=...)`, defaulting to the current behaviour) and call it from `sync_turn` with the turn's role. Add test cases: `_is_noise("Noted.", role="assistant") is True`, and a 30-char assistant reply with a fact is still noise.
- **T0.2, runner.** In `eval_golden_conversation.run`:
  - compute `expected_rows` with the same `_is_noise` used by `sync_turn` instead of `2 × user`;
  - stop waiting as soon as `durable_job` holds no pending `extract%` jobs and `complete_rows` is stable;
  - emit `extract_mode` from config instead of hard-coding `"rules_only"` (`:120`);
  - add per-question `kind` and claim-level checks.
- **T0.3, fixture v2** (`tests/fixtures/golden_conversation.json`). Keep the current topics and add:
  - first-person functional facts (`I live in NYC` → `I moved to Austin` → `I'm back in NYC`; `I work at Acme`; `My role is engineer`);
  - a negation (`I don't work at Acme anymore`);
  - a pasted email containing `Bob is the owner` and `No, I meant Friday`;
  - a pasted log containing `X uses Y` lines;
  - a code fence;
  - one assistant reply that states a new fact (for Spec 4).

  Set `expected_claims[*]` per extract mode (`rules_only` and `rules_then_model`), with `speaker` and `status` (`grounded`/`unconfirmed`). Pasted facts must be `document`/unconfirmed (Spec 1). Use this questions JSON schema:
  ```json
  {"id": "city_now", "kind": "functional_current", "text": "Where do I live now?",
   "expected_answer": "NYC", "expect_contains": ["NYC"], "expect_absent": ["Austin"],
   "expect_claim": {"subject": "I", "verb": "lives_in", "object": "NYC", "current": true}}
  ```
  `kind` must be one of `functional_current | history | alias | non_functional | negation | paste_not_user | assistant_unconfirmed | abstain`.
- **T0.4, metrics** (the JSON report, also written by `eval_memory.py` where it applies):
  - claim precision, recall and F1 against `expected_claims`, keyed on (`subject`, `verb`, `object`, `current`, `speaker`);
  - event-time agreement;
  - **pasted-span false-grounding count** (should be 0 after Spec 1);
  - per-question pass rate for `vector` and `hybrid`;
  - **facts-block tokens measured separately from total injection tokens.** `shadow_replay.py:213` counts the whole block. Add a `facts_tokens` field from `format_facts`.
- **T0.5, baseline run.** Run on a fresh `*_eval` DB for both modes and commit the JSON under `docs/evals/`. Record the numbers, whatever they are. No target is set yet.

### 0.5 Schema/migration
None.

### 0.6 Acceptance & gate
- Tests 1–3 pass. The golden run finishes without hitting the deadline and reports `complete_rows == expected_rows`.
- The baseline JSON is committed for `rules_only` and `rules_then_model`.
- **Gate G0:** Ray reviews the committed baseline JSON. The T0.1 noise rule is already settled (Decision 1), so G0 waits only on that review. Specs 1–3 start after it.

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
1. A pasted email (`From:`/`Subject:` headers, a `>` quote, a signature) containing `Bob is the owner.` and `No, I meant Friday.` → **0** user claims, **0** repair rows. `Bob is the owner` is stored with `speaker='document'` and status `unconfirmed`.
2. A code fence containing `Config uses Redis` → no claim.
3. A 60-line log with `Worker uses 4GB` lines → no user claim.
4. A mixed turn, `"I live in Austin. Can you clean this up?\n\n<pasted doc>"`, yields exactly 1 user claim (`I lives_in Austin`), and every doc claim is `document`.
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
  - long runs with no first-person tokens.

  Thresholds (minimum run length, line count) are **[tune by eval]**. Optional: `talon` for email quote and signature stripping, behind an import guard.
- **T1.2** `provider._reception_stage`: run `extract_alias_equations`, `classify_repair` and `extract_assertions` on authored segments only. Run `extract_assertions` on pasted/quote segments and record the results as document evidence. Repairs only fire when the authored text is short (**[tune by eval]**, start at the current whole-turn behaviour for turns with no pasted segment).
- **T1.3** `extract_publish.publish_rules` (`:51-55`): same split. `record_rule_claims_on_conn` (`store_claims.py:489-517`) gets a `speaker` override that it passes through to `add_evidence`.
- **T1.4** `store_claims.add_evidence` (`:191-222`) derives the speaker from `info.role` through `_SPEAKERS` (`:99`, `:193`). Add an optional `speaker` parameter and `'document'` to `_SPEAKERS`. Use extractor `rules_document` so that a user claim and a document claim from the same span never collide on `uq_claim_evidence_event` (`V18:184-186`).
- **T1.5** `extraction_worker._load_model_batch_input`: send only authored segments as the turn text. Pass pasted segments as a separate field in `ModelBatchInput` labelled "document; do not attribute to the user".
- **T1.6** `extraction_jobs.should_enqueue_model`: score authored segments only.
- **T1.7** `timeline.rebuild`: no change needed (non-user is already unconfirmed). Add a test that `document` evidence never closes a user interval.
- Bump `RULES_VERSION` to `"2"` (C10).

### 1.3 Schema/migration: `V<N>__document_speaker.sql`
```sql
-- claim_evidence.speaker CHECK was declared inline in V18:167 (auto-named).
DO $$ DECLARE c text; BEGIN
  SELECT conname INTO c FROM pg_constraint
   WHERE conrelid = 'claim_evidence'::regclass AND contype = 'c'
     AND pg_get_constraintdef(oid) LIKE '%speaker%';
  IF c IS NOT NULL AND c <> 'claim_evidence_speaker_check_v2' THEN
    EXECUTE format('ALTER TABLE claim_evidence DROP CONSTRAINT %I', c);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'claim_evidence_speaker_check_v2') THEN
    ALTER TABLE claim_evidence ADD CONSTRAINT claim_evidence_speaker_check_v2
      CHECK (speaker IN ('user','assistant','document'));
  END IF;
END $$;
ALTER TABLE claim_evidence ADD COLUMN IF NOT EXISTS seg_start INT;
ALTER TABLE claim_evidence ADD COLUMN IF NOT EXISTS seg_end   INT;  -- provenance offsets into conversations.content
```

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

### 2.2 Implementation tasks
- **T2.1** Add `reception.extract_self_assertions(text) -> list[dict]`. It is a small ordered table of anchored templates per canonical verb, returning the **canonical verb name** directly. `store_claims.verb_row` (`:125-136`) accepts canonical names, so no synonym rows are needed. Call it from the same three sites as Spec 1 (authored segments only), after `extract_assertions`.
- **T2.2** Predicate normalization: one table `SELF_TEMPLATES: dict[str, tuple[re.Pattern, ...]]` covering:
  - `lives_in` (live in, moved to, relocated to, back in);
  - `based_in` (I'm based in). **Settled (Decision 2):** it stays a separate functional slot from `lives_in` (today's V18 schema) and does **not** supersede it;
  - `works_for` (work at/for, joined, employed by);
  - `role_is` (my role is, I'm a/an + role noun, I work as).

  All four verbs are already seeded as functional in V18 (`V18:67-81`). Some phrases are also in `verb_synonym` (`V18:82-85`). Trim objects the way `extract_assertions` does (`reception.py:156`: strip and drop the trailing `.`), and also cut trailing time phrases (`last week`, `now`, `anymore`) with a small list. Reject an object over 6 tokens (**[tune by eval]**).
- **T2.3** Negation: the self-templates emit `polarity='negative'` for `don't|no longer|not … anymore|left|quit`. `_NEGATION` dropping stays unchanged for the generic `is`/`uses` rule.
- **T2.4** Subject: emit `"I"`. `ensure_entity` resolves it to `self`. `format_facts` renders `self` as "You" or the owner name (check `provider_helpers.py:416-520`, and add a test if it does not).
- **T2.5** Bump `RULES_VERSION` (C10). **Settled (Decision 6):** the rules-v2 backfill of the installed DB is not part of this spec's PR. It runs as a separate step after Specs 0–2 are merged, only after a `pg_dump -Fc` backup and Ray's explicit OK (C3).

### 2.3 Schema/migration
None. `lives_in`, `based_in`, `works_for` and `role_is` already exist as functional verbs (`V18:67-81`). **Do not** change `is` to functional (that was the reverted V22). Optional: add rows to `verb_synonym` (`moved to`, `relocated to`, `work at`) for the model tier's phrase normalization. That would be an idempotent `INSERT … ON CONFLICT DO NOTHING` in a `V<N+1>` migration, used only if Spec 3 needs it.

### 2.4 Acceptance & gate
- Tests 1–9 pass. On golden v2 (`rules_only`), the `functional_current`, `history` and `negation` questions pass in `vector` and `hybrid`.
- Precision on the abstain set does not drop below the G1 run. Recall targets are **[tune by eval]**.
- **Gate G2:** Ray signs off.

### 2.5 Out of scope
- Slot groups (`lives_in` vs `based_in` superseding each other). **Settled (Decision 2):** not built now. Revisit only if the golden suite shows misses between the two slots. That would need a timeline-key change in `store_claims.timeline_key` (`:135-136`).
- Relative-date parsing, spaCy/coreference (Page phase 3), and gazetteers.

---

## Spec 3: Model-tier fixes

**Goal.** Make `rules_then_model` (opt-in, never default) actually add value on first-person and paraphrased facts.

### 3.1 Current defects (verified)
- **Self subjects can't pass grounding.** `model_extract.grounded_claims` (`:29-68`) needs the subject surface to occur in the turn (`_occurs`, `:16-20`), and `_keep_surface` (`:23-26`) rejects first-person and abstained surfaces. A model that outputs `"user"` or `"I"` for `I live in NYC` gets the claim dropped.
- **No conversational context.** `extraction_worker.py:115` passes `context=[]`.
- **Routing.** `should_enqueue_model` (`extraction_jobs.py:36-70`) skips turns whose nouns rules already "covered". A turn where rules found only `X is Y` but the user also stated a self fact is skipped.
- **The prompt names a verb that isn't allowed.** The batch instructions' example (`extract_model_client.py:18-28`) uses `founded`, which is not in the verb list. There is no first-person example and no schema-level constraint. `response_format` is plain `json_object` (`:63`). Validation happens after the fact (`:81-92`).

### 3.2 Failing tests first (`tests/test_model_extract.py` additions, using a fake model fn; no network)
1. Model output `{"subject":"I","verb":"lives_in","object":"NYC"}` for turn `"I live in NYC"` is kept and normalized to `I`.
2. Output subject `"user"` / `"the user"` is mapped to `I` and kept. Output subject `"I"` for a turn with no first-person token is dropped.
3. A verb outside the allowed list (`founded`) is dropped, and so is an object that does not occur in the turn.
4. Routing: `should_enqueue_model("I moved to Austin; Nightingale is a service", recent)` is True even when `Nightingale` is covered.
5. The client builds a `json_schema` response format with `verb` as an `enum` of the loaded verbs when `extract_model_schema="json_schema"`, and falls back to `json_object` otherwise.

### 3.3 Implementation tasks
- **T3.1** In `model_extract.grounded_claims`, accept first-person subjects when the turn has a first-person token, and map `user|the user|me|myself` to `I`. Objects keep the strict `_occurs` check.
- **T3.2** In `extraction_worker` (`:115`), pass the previous turn of the same session as `context`. This is for the prompt only; grounding stays on the user span.
- **T3.3** In `extraction_jobs.should_enqueue_model`, add the rule "enqueue when a first-person cue is present and the self-templates found nothing". Thresholds are **[tune by eval]**.
- **T3.4** Prompt in `extract_model_client.py:10-28`:
  - generate the verb list from the DB verb rows (one source of truth);
  - replace the `founded` example with allowed verbs;
  - add first-person, negation and abstain examples (question, hypothetical, pasted doc);
  - bump `MODEL_VERSION` (C10).
- **T3.5** Add config `extract_model_schema: "json_object" | "json_schema"` (default `json_object`). With `json_schema`, send an OpenAI-style schema. Ollama's `format`, llama.cpp grammars and NuExtract-style templates all work through OpenAI-compatible servers that support it. Keep the post-validation.
- **T3.6** Model bake-off on golden v2: the current default model vs. one or two small local models (for example Qwen2.5-7B-Instruct, NuExtract). Report P/R and latency per model. No numbers are assumed here. **Settled (Decision 3):** the model and the `extract_model_schema` mode are chosen from these results at Gate G3, not before.

### 3.4 Schema/migration
None. Model and prompt identity go in the existing `claim_evidence.extractor` / `extractor_version` (`V18:175-176`), for example `extractor='model'` and `extractor_version='<MODEL_VERSION>:<model-id>'`. **This replaces Ray's `extractor_model`/`extractor_prompt_version`/`model_version` columns and the `UPDATE … 'unknown'` backfill, which would be redundant writes.**

### 3.5 Acceptance & gate
- Tests 1–5 pass. On golden v2 with `rules_then_model`, claim recall is at least the `rules_only` G2 recall, and precision on the abstain and paste sets is unchanged.
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

### 4.3 Implementation tasks
- **T4.1** Add config `extract_assistant_replies: bool = False`. When true and `extract_mode == "rules_then_model"`, sites 2 and 3 let assistant spans through. Batches stay paired (the user span plus the following reply span). Payloads stay ids-only (`store_jobs.job_add_model_batch`, `store_jobs.py:65`).
- **T4.2** `model_extract.grounded_claims` takes a `speaker` argument. For assistant spans, map `you|your` to `I` and drop subjects `I|we|me`. Ground against the reply text.
- **T4.3** Add `turn_filter.strip_memory_blocks(text)` (Ray's `stripMemoryBlocks` contract, in Python, with the repo's real tags). It removes:
  - `<memory>…</memory>` (`provider_helpers.py:397`);
  - `<PAST_CONTEXT>…</PAST_CONTEXT>` (`provider_helpers.py:252`);
  - the grounded-facts and supporting-turns sections that `format_facts` emits;
  - the future `<prior_work>` block (Spec 6).

  It must handle nested and malformed tags, and must not cut into code fences that merely contain the word "memory". Apply it to extractor input for both speakers. Log only a hash of the extractor input (C6). Also **verify** whether Hermes stores injected context in `conversations.content` at all. If not, the stripper is defense in depth.
- **T4.4** Pasted content summarized in a reply is stored at document trust: if the reply's object grounds only inside a pasted segment of the paired user turn, set the speaker to `document`.
- **T4.5** Promotion on uptake: **built off, and left off.** Write the code path behind `promote_on_uptake: bool = False`. Only explicit confirmation cues right after the assistant statement count ("yes, exactly", "that's right"); the user turn then gets `speaker='user'` assert evidence. Uptake verdicts (`accepted`/`used`) are deliberately unknown today. Turning this on needs a labeled set (golden v2 `assistant_unconfirmed` items plus hand-labeled real sessions) and Ray's approval.

### 4.4 Schema/migration
None (the speaker `assistant` is already allowed at `V18:167`).

### 4.5 Acceptance & gate
- Tests 1–6 pass. With the flag on, every eligible reply is enqueued (the count of assistant spans with a model job equals the count of eligible assistant spans).
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
3. Supersession: retracting `(b)` restores `(a)` as current, using the functional-timeline pattern.
4. `on_session_end` (`provider.py:1528`) returns without waiting for extraction: capture is enqueue-only. A crash between session end and job run loses nothing, because the job re-reads spans from `conversations`.
5. The job payload has ids only (`owner`, `session_id`, first and last span id). No text.
6. Cross-owner isolation.

### 5.3 Implementation tasks
- **T5.1, durable capture.** Do **not** add an in-memory message buffer.
  - User and assistant turns are already durable in `conversations` through `sync_turn` (`provider.py:340-360`), subject to the T0.1 noise fix.
  - Tool results arrive in `sync_turn(messages=…)` (`provider.py:346`), which is currently unused. Persist a **digest** per tool call into `work_event`: tool name, exit/status code, parsed refs (paths, branch, commit SHA, PR number, test pass/fail counts). No raw output. Write it through the existing write queue.
- **T5.2, enqueue.** In `on_session_end`, next to `job_flush_model_batches`, call `store.job_add(owner, 'extract_work', op_id=f"{session}:work:{WORK_VERSION}", payload={span_from, span_to, session_id})` (`store_jobs.py:46`). Use the same pattern at task boundaries if Hermes exposes them.
- **T5.3, worker.** Add `extraction_worker._run_work` next to `_run_rules` (`:61`), claimed in `run_extraction_once` (`:139`). It rebuilds the session from `conversations` and `work_event`, strips memory blocks (T4.3), and has the model (or rules for clear cases) output `{ask_summary, outcome_summary, topic_key, status, reason_span_id, artifacts[]}`.
- **T5.4, status sources, in priority order:** an explicit user verdict span ("that's wrong/fake", "abandon this", "works, thanks"), then eval/test output in `work_event`, then the tool exit code. With no evidence, the status is `unknown`. Never infer `failed` from a missing git object.
- **T5.5, artifact verification** (async, best effort): `git cat-file -e <sha>` and `git rev-parse --verify <branch>` in configured repo roots, plus file existence. **Settled (Decision 5):** config `work_verify_repo_roots: list[str]` starts with only the hermes-memory checkout. Verification runs **only in the extraction worker** (`_run_work` or a follow-up job), never at prefetch, and prefetch only reads the stored `exists_now`/`stale`. Store the result in `verified_at` / `exists_now`. Mark items stale when a referenced path changes after `verified_at` (Spec 6 shows it). An unverifiable artifact is kept and flagged, never dropped.
- **T5.6** `ask_summary`/`outcome_summary` are short model summaries, not pasted text. Cap them at **[tune by eval]** characters. Pasted segments (Spec 1) are excluded from summarization input.

### 5.4 Schema/migration: `V<N+1>__work_memory.sql`
```sql
CREATE TABLE IF NOT EXISTS work_item (
  owner TEXT NOT NULL,
  id BIGSERIAL PRIMARY KEY,
  session_id TEXT NOT NULL,
  topic_key TEXT NOT NULL,                -- normalized task/topic key
  ask_summary TEXT NOT NULL,
  outcome_summary TEXT,
  status TEXT NOT NULL CHECK (status IN ('done','failed','abandoned','unknown')),
  reason TEXT,
  ask_span_id BIGINT NOT NULL,
  outcome_span_id BIGINT,
  reason_span_id BIGINT,
  superseded_by BIGINT REFERENCES work_item(id),
  extractor_version TEXT NOT NULL,
  embedding vector(768),                  -- match embed_dim in config
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (owner, id),
  FOREIGN KEY (owner, ask_span_id)     REFERENCES conversations (agent_identity, id) ON DELETE CASCADE,
  FOREIGN KEY (owner, outcome_span_id) REFERENCES conversations (agent_identity, id) ON DELETE SET NULL (outcome_span_id),
  FOREIGN KEY (owner, reason_span_id)  REFERENCES conversations (agent_identity, id) ON DELETE SET NULL (reason_span_id),
  CONSTRAINT work_item_reason_required CHECK (status NOT IN ('failed','abandoned') OR reason_span_id IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS work_item_current_key ON work_item (owner, topic_key) WHERE superseded_by IS NULL;

CREATE TABLE IF NOT EXISTS work_artifact (
  owner TEXT NOT NULL,
  id BIGSERIAL PRIMARY KEY,
  work_item_id BIGINT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('file','branch','commit','pr','eval')),
  ref TEXT NOT NULL,                      -- path / branch / sha / PR number / eval file
  repo TEXT,
  exists_now BOOLEAN,
  verified_at TIMESTAMPTZ,
  stale BOOLEAN NOT NULL DEFAULT false,
  FOREIGN KEY (owner, work_item_id) REFERENCES work_item (owner, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS work_event (   -- tool-result digests; no raw output
  owner TEXT NOT NULL,
  id BIGSERIAL PRIMARY KEY,
  session_id TEXT NOT NULL,
  span_id BIGINT,
  tool TEXT NOT NULL,
  status_code INT,
  refs JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (owner, span_id) REFERENCES conversations (agent_identity, id) ON DELETE SET NULL (span_id)
);

-- durable_job.kind CHECK is inline in V19:11 (auto-named): drop & re-add with 'extract_work'.
DO $$ DECLARE c text; BEGIN
  SELECT conname INTO c FROM pg_constraint
   WHERE conrelid = 'durable_job'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%kind%';
  IF c IS NOT NULL AND c <> 'durable_job_kind_check_v2' THEN
    EXECUTE format('ALTER TABLE durable_job DROP CONSTRAINT %I', c);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'durable_job_kind_check_v2') THEN
    ALTER TABLE durable_job ADD CONSTRAINT durable_job_kind_check_v2
      CHECK (kind IN ('capture','extract_rules','extract_model','extract_work'));
  END IF;
END $$;
```
Note: `ON DELETE SET NULL (col)` needs Postgres ≥ 15. A plain `SET NULL` on a composite FK would also null `owner`, which is NOT NULL, so span deletion would fail. Check the server version in the installer image. If it is older, use `ON DELETE NO ACTION` and clear the column in `delete_spans`. Because `reason_span_id` is required for failed/abandoned rows, deleting that span should move the item to `unknown`.

Changes from Ray's `work_record`: `BIGSERIAL` instead of `uuid`, which matches every other table. Span foreign keys replace free-text `ask`. Artifacts get their own table instead of JSONB, so staleness can be updated per artifact. `failed`/`abandoned` need a reason span. `unknown` is allowed. The job kind CHECK is changed.

### 5.5 Acceptance & gate
- Tests 1–6 pass. The shadow-replay fixture gives `failed` (verdict) followed by `done` (eval and tests).
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
4. A query for "set up shadow replay" surfaces the `done` item and the superseded `failed` item with its reason.
5. Cross-owner: owner B never sees owner A's items.

### 6.2 Implementation tasks
- **T6.1** Config (`config.py`):
  - `prior_work_mode: str = "off"`, validated like `retrieval_mode` (`:196-205`);
  - `prior_work_token_budget: int = 200` **[tune by eval]**. **Settled (Decision 4):** it starts at 200 and is taken from the supporting-turns share inside the 1200 total (never from the 400 facts cap). It is tuned from `work_shadow` data before G6;
  - `prior_work_top_k: int = 3`.
- **T6.2** Add `store_work.search_work(owner, query_vec, topic_terms, k)`. It does nearest-neighbour search over `work_item.embedding` where `superseded_by IS NULL`, plus the superseded predecessor of each hit when its status is failed. Owner-scoped. Trigger it only on task-intent queries (a simple imperative or verb cue, **[tune by eval]**).
- **T6.3** In `provider._aprefetch` (`provider.py:1100`), in both branches (hybrid `format_facts` at `:1302-1318`, vector `format_span_injection`):
  - **shadow:** `schedule_shadow(record_work_shadow, …)` (`shadow.py:46`), reusing the `retrieval_shadow` sampling (`shadow_sample_rate`, `provider.py:240`);
  - **on:** render `<prior_work>` before the facts with `provider_helpers.format_prior_work(items, budget)`, then reduce the remaining budget that is passed to supporting turns.
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
| 0 | `_is_noise` keeps short facts; fixture schema | golden ingest completes; `complete_rows == expected_rows` | G0 baseline JSON committed |
| 1 | `segment_turn` on email, log, code, quote, mixed | paste → 0 user claims, 0 repairs, `document` unconfirmed | 0 pasted false grounding |
| 2 | self-templates, negation, abstain | NYC→Austin→NYC via extraction; `is` still non-functional | functional / history / negation questions pass |
| 3 | grounding for self, verb enum, routing | model-batch with fake model fn | recall ≥ rules_only; abstain precision kept |
| 4 | `strip_memory_blocks`; you→I mapping | echo dedup; novel → unconfirmed; double-extract idempotent | 0 assistant-only claims in grounded block |
| 5 | status precedence; payload ids-only | shadow-replay fixture failed→done; supersede/restore; no claims rows | jobs drain; session end non-blocking |
| 6 | budget truncation; mode validation | shadow row has no text; facts tokens unchanged | 0 budget violations; 0 cross-owner |

Every spec also runs the existing suites (`tests/test_timeline.py`, `tests/integration/test_claims_*`, `tests/test_span_injection.py`, `tests/integration/test_hybrid_supersession.py`) and a retry/replay check (no duplicate evidence on job retry, via `uq_claim_evidence_event`).

## Rollout order
G0 → (Spec 1 ∥ Spec 2, separate branches; Spec 2 relies on Spec 1's segmenter for the abstain-on-paste test, so merge Spec 1 first) → Spec 3 → Spec 4 (flag off) → Spec 5 (capture flag off until G5) → Spec 6 (`shadow` → `on` only after G6). Each migration takes the next free number when Ray merges it (C7). After Specs 0–2 merge: the installed-DB rules-v2 backfill, after a `pg_dump` backup and Ray's explicit OK (Decision 6, C3).

## Decisions (approved by Ray, Nov 17, 2026)
1. **T0.1 noise rule:** short user turns that carry an assertion signal or a first-person cue are exempt from the 40-char noise rule. Assistant turns keep the plain 40-char rule until Spec 4. `TURN_MIN_CHARS` is not lowered globally. Gate G0 no longer waits on this; it still waits for Ray's baseline review.
2. **`based_in` vs `lives_in`:** they stay separate functional slots (today's V18 schema). Revisit slot groups only if the golden suite shows misses.
3. **Model choice and schema mode:** decided from the T3.6 bake-off results at Gate G3. This is a gate, not an open question.
4. **`prior_work_token_budget`:** starts at 200 **[tune by eval]**, taken from the supporting-turns share within the 1200 total (never from facts), and tuned from `work_shadow` data.
5. **Artifact verification repo roots:** only the hermes-memory checkout to start (`work_verify_repo_roots`, a configurable list). Verification runs only in the worker, never at prefetch.
6. **Installed-DB backfill with rules v2:** runs after Specs 0–2 are merged, only after a `pg_dump -Fc` backup and Ray's explicit OK (C3).

## Removed from Ray's draft, with reasons
- **TypeScript snippets** (`claims.findLive`, `stripMemoryBlocks`, `packContext`): replaced with pointers to the real Python functions.
- **`extractor_model` / `extractor_prompt_version` / `model_version` columns plus the `'unknown'` backfill:** redundant with `claim_evidence.extractor` / `extractor_version`.
- **"Expected V23":** numbering is decided at merge time, and V22 is free again after `c21de62`.
- **"Fake branch caught by git check":** wrong mechanism. Failure comes from verdict, eval or tool evidence (5.1).
- **`uuid` ids, free-text `ask`, an in-memory message collector:** replaced by BIGSERIAL, span FKs and durable capture.
- **Uptake promotion enabled in phase 2:** now built but off until labeled data exists (T4.5).
- **"5th recommendation" / "source note" placeholders and duplicated phase blocks:** removed. The Page is the source of truth, and the missing pieces (paste segmentation, first-person rules, model-tier fixes, eval probe) are now Specs 0–3.
- **The fixed 200-token prior-work cap:** 200 stays as the starting default (Decision 4), but it is marked **[tune by eval]** and taken from the supporting-turns share.
