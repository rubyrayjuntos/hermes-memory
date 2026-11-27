<img width="2048" height="1152" alt="hermes_librarian_banner" src="https://github.com/user-attachments/assets/109ea41f-2787-443f-a815-18139f80da4c" />

# 📜 The Hermes Librarian 📜

**Persistent memory & context injection for [Hermes Agent](https://github.com/nousresearch/hermes-agent)** —
combining PostgreSQL + pgvector for semantic recall and Apache AGE for knowledge graph entity expansion, all in a single database.

This is an **unofficial community plugin** (provider name `hybrid-age`).
**Status: v1.0.0, stable** for a single local operator. Tested against Hermes Agent v0.20.x.
All database and API bindings are **loopback only** (`127.0.0.1`).

- **Stable:** vector recall, tiered memory injection with the `memory_get` / `memory_search`
  tools, rule-based claim extraction, the installer and lifecycle CLIs, and the Graph Explorer
  (read-only).
- **Opt-in, off by default:** model claim extraction (`extract_mode: rules_then_model`),
  claims from assistant replies (`extract_assistant_replies`), and hybrid claim retrieval
  (`retrieval_mode: hybrid`).
- **EXPERIMENTAL: work capture** (`work_capture`, default `false`). This covers
  `work_item` / `work_event` capture of what was done, decided, tried or failed, plus
  session-level task splitting. It is off by default, and its gate (G5) has not passed. Turn it
  on only if you want to try it and are happy to inspect the rows yourself.

Production-scale criteria (fleet deploys, auth on non-loopback ports, a human-judged golden set
in CI) are tracked separately in [`RELEASE_CRITERIA.md`](RELEASE_CRITERIA.md).

> *A librarian doesn't just store books — she knows where everything is, retrieves the right volume at the right moment, and quietly discards what's stale.*

Package: `hermes-memory` · Product: **The Hermes Librarian** · License: MIT

### What it does

Hermes forgets everything once a chat ends. The Librarian gives it long-term memory on your own
machine:

1. **It remembers your conversations.** Every turn is stored in a local Postgres database,
   along with an embedding (a numeric fingerprint of its meaning) computed on your machine.
   Nothing is sent to an outside embedding service.
2. **It brings back what's relevant.** Before Hermes answers, the Librarian finds the past turns
   closest to what you just said. It adds a few of them in full, plus short pointers to others,
   in a fenced `<memory>` block.
3. **The agent can dig deeper when it needs to.** If a pointer looks useful, Hermes can call
   `memory_get` to read the full text, or `memory_search` to look for something else. This
   helps most on questions that need several older facts at once (see Evaluation).
4. **It learns facts, not just text.** A background worker pulls out simple claims ("I live in
   Austin", "I work for Acme") and keeps their history. A correction replaces the old value
   instead of contradicting it. These claims and entities live in a graph (Apache AGE) in the
   same database, and you can browse them in the Graph Explorer.
5. **It stays local and scoped.** Memory belongs to a Hermes profile, so profiles don't read
   each other's memories. Ports bind to `127.0.0.1` only. Secrets such as passwords, tokens,
   phone numbers and emails are redacted before stored text is shown to the model.

---

### Choose Your Path

| Persona | Goal | Start Here |
| :--- | :--- | :--- |
| 🚀 **Plugin User** | Install, verify, and run the plugin in Hermes Agent (no git clone required). | [Plugin User Guide](#-plugin-user-guide) |
| 🛠️ **Developer / Integrator** | Clone the repo, run the dev stack, run tests, or inspect configurations. | [Developer & Integrator Guide](#-developer--integrator-guide) |
| 🧭 **Contributor** | Understand the architecture invariants, release gates, and contribution rules. | [Contributor & Architecture Guide](#-contributor--architecture-guide) |

---

### What it Offers / Does Not Offer

| Offers | Does not offer |
|---|---|
| Unofficial `hybrid-age` **MemoryProvider** for Hermes Agent | Official Nous Research plugin |
| Local Postgres + pgvector recall **and** Apache AGE graph walks | Hosted or multi-tenant cloud memory service |
| Fenced `<memory>` prompt injection (`<grounded>` vs `<unconfirmed>` bins) | Replacing `MEMORY.md` / `USER.md` (those remain always-on) |
| Decoupled async claims extraction + typed fact walks | In-process extraction blocking the prompt path |
| Single CLI installer & lifecycle manager (`hermes-memory-install`) | Automated backup under `hermes backup` (export Postgres yourself) |
| Loopback-only security model (`127.0.0.1:5450`, `:5452`, `:7890`) | Open network listening ports |

---

## 🚀 Plugin User Guide

You do **not** need to clone this repository to use the plugin. Source builds
bundle the installation and migration assets into the wheel. The install
path is Git-based; the CD workflow creates GitHub Releases for tagged commits
but does not publish a wheel to a package index.

### Prerequisites

1. **Docker**: Running locally (for the loopback Postgres + Apache AGE container).
2. **Hermes Agent**: Installed with a working chat model (`hermes model`).

Embeddings run **in-process** via ONNX (`fastembed` + `nomic-embed-text`, 768-d). Ollama is **not** required. Optional: set `HYBRID_AGE_EMBED_URL` to an OpenAI-compatible HTTP endpoint (Ollama, OpenAI, …) if you prefer a remote embedder.

Model-based claim extraction is a separate, optional service and is **not**
automatically connected to Hermes's chat model. The default `extract_mode` is
`rules_only`: deterministic rules run and no model-extraction jobs are enqueued.
To opt into model fallback, set `extract_mode: rules_then_model` and configure
an OpenAI-compatible chat-completions endpoint and model:

```bash
export HYBRID_AGE_EXTRACT_MODE=rules_then_model
export HYBRID_AGE_EXTRACT_URL=http://127.0.0.1:11434/v1
export HYBRID_AGE_EXTRACT_MODEL=your-model-name
# Set this only if the endpoint requires a key:
export HYBRID_AGE_EXTRACT_KEY=your-api-key
```

In `~/.hermes/config.yaml`, the equivalent setting is
`hybrid_age.extract_mode: rules_then_model`. Rule extraction still runs for
each stored turn. Model extraction is considered only for user turns where the
rules found no claim or missed a detected entity; questions, acknowledgements,
code/tool output, and exact or near-duplicate turns are skipped. Eligible turns
are collected into one model job per session batch. A full batch runs as soon as it
reaches 8 eligible turns; a partial batch runs after 10 minutes without another
eligible turn, after 30 minutes from the batch's first turn, or when the session
closes. This keeps model jobs independent of the per-turn rule queue. The
provider logs each session-end callback it observes and the number of batches it
flushes, so the Hermes lifecycle hook can be verified in logs.
Model-found facts can therefore arrive later than rule facts. The worker runs
off the prompt path and does not block the chat response.

Switching back to `rules_only` stops new model batches and prevents queued
batches from being processed. `/api/health` reports any pending model jobs
while model extraction is disabled, plus cumulative model calls and reported
prompt/completion tokens and per-completed-batch averages. Endpoints that do
not return token usage leave those token counts unavailable rather than
reporting zero. Ollama endpoints on port 11434 receive `keep_alive: "30m"` to
encourage reuse of the stable system-prompt prefix.

`HYBRID_AGE_EXTRACT_KEY` is optional for local compatible endpoints (the client
uses `ollama` as the default placeholder key). These variables must be present
in the Hermes process environment. Alternatively, `extract_url` and
`extract_model` can be set under `hybrid_age:` in `~/.hermes/config.yaml`;
`extract_timeout_s` (default `60`) is configurable there. The configured
endpoint receives the eligible user turns in a batch, their rule-extracted
findings, the allowed verb table, and compact extraction instructions. Do not
configure a third-party endpoint unless you are comfortable sending those
conversation turns to it. Returned claims must identify one source turn and
pass local verb/name grounding against that turn before publication. Grounding
does **not** prove the relationship is entailed. Treat model-extracted claims
as candidates and inspect them before relying on them for high-stakes decisions.

### Installation

Install from the `v1.0.0` release tag with one command:

```bash
pip install "git+https://github.com/rubyrayjuntos/hermes-memory@v1.0.0" && hermes-memory-install --yes
```

This installs from Git. The wheel is built from the tag and bundles the install and migration
assets, so you don't need to clone the repo. No package is published to PyPI.

The installer automatically:
- Probes Docker and confirms the daemon is healthy.
- Warms the in-process 768-d embedder (downloads the ONNX model on first run, ~100 MB).
- Provisions the local container on `127.0.0.1:5452` and applies migrations.
- Configures Hermes Agent (`hybrid-age` provider) and admits dependencies.
- Starts the inspector API daemon on `127.0.0.1:7890`.
- Runs verification and restarts the Hermes gateway.

### Verification (Optional)

Verification runs automatically during install, but you can re-verify at any time:

```bash
hermes-memory-verify --require-embed
```
*(Exit code `0` indicates all layers passed).*

### Chatting & Seeding Memory

Open a chat session with Hermes:
```bash
hermes chat
```
Seed two or three distinct facts in conversation (e.g., your projects, active tools, or preferences). The Librarian extracts entities and relations in the background and writes them to durable storage.

### Optional: Live Visual Inspector

- **Claims / entities explorer** (owner-scoped graph layer):
  [http://127.0.0.1:7890/api/librarian/explorer](http://127.0.0.1:7890/api/librarian/explorer)
- **Noun fountain** (mention topology):
  [http://127.0.0.1:7890/api/librarian/pane](http://127.0.0.1:7890/api/librarian/pane)

Pin **Graph** in Hermes Desktop via `desktop-plugins/graph-explorer/` (copy into `~/.hermes/desktop-plugins/`).

*(Memory recall works completely autonomously with the visual pane closed).*

### Indexing a Codebase (Optional)

You can index an entire source repository into the Librarian:
```bash
hermes-memory-ingest path/to/repo
```
*(Re-running ingestion is an idempotent no-op for unchanged files).*

### Uninstallation

To cleanly remove the plugin and shut down its dedicated container:
```bash
# Remove plugin and tear down database container
hermes-memory-uninstall --force

# Or preserve the database for future re-installs:
hermes-memory-uninstall --force --keep-db
```

---

## 🛠️ Developer & Integrator Guide

For developers looking to inspect the code, modify migrations, or run tests locally.

### Local Development Setup

```bash
git clone https://github.com/rubyrayjuntos/hermes-memory.git
cd hermes-memory
pip install -e '.[dev]'
```

### Dual-Database Port Architecture (Important)

The codebase strictly enforces two separate database environments to prevent developer experiments from corrupting user memory:

| Environment | Host & Port | Database Name | Managed By | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| **Development** | `127.0.0.1:5450` | `hermes_memory` | `docker compose up -d` | Local tests, migrations, experimentation |
| **Installed Plugin** | `127.0.0.1:5452` | `hermes_memory_installed` | `hermes-memory-install` | Active Hermes Agent runtime plugin |

> [!WARNING]
> `hermes-memory-install` explicitly refuses to attach to the `:5450` dev database without `--reuse-dsn`. Missing `HYBRID_AGE_DSN` raises an error rather than silently defaulting.

To spin up the local development database:
```bash
docker compose up -d
```

### Running Tests

```bash
# 1. Pure-function property tests (Hypothesis, no database required)
pytest tests/property -q

# 2. Standard test suite (excludes integration tests by default)
pytest -q

# 3. Integration test suite (requires dev stack running on :5450)
pytest tests/integration -q -m integration --override-ini="addopts="
```

#### Property Test Suite (P1–P8)
The property test suite guards against data corruption and security invariants:
- **P1**: Cypher injection prevention (unescaped quotes/backslashes).
- **P2**: Malformed MERGE properties handling.
- **P3/P4**: Entity slug idempotence and noise filtration (`extract_nouns`).
- **P5**: Artifact path filtration (ignoring hex/UUID/skip directories).
- **P6**: Deterministic dependency index output.
- **P7**: Silent file-hash state corruption resilience.
- **P8**: Bridge table link symmetry and orphan prevention.

### Offline Release Gate Checks

Before opening a PR, run the automated release gate script to verify formatting, type-checking, and offline wheel packaging:
```bash
python scripts/release_gate.py
```

### Configuration Reference (`config.yaml`)

`hermes-memory-install` writes default settings to `~/.hermes/config.yaml`. Knobs under `hybrid_age:` include:

| Key | Default | Description |
| :--- | :--- | :--- |
| `dsn_env` | `HYBRID_AGE_DSN` | Environment variable containing the Postgres DSN |
| `embed_url_env` | `HYBRID_AGE_EMBED_URL` | Optional HTTP embeddings endpoint; unset = in-process fastembed |
| `embed_model` | `nomic-embed-text` | Embedding model stamp (strictly 768-dim) |
| `extract_url` | empty | Optional OpenAI-compatible chat-completions endpoint for claim extraction |
| `extract_model` | empty | Model name for claim extraction |
| `extract_mode` | `rules_only` | `rules_only` or opt-in `rules_then_model`; eligible user turns are batched (8 turns, 10-minute idle, or session close) |
| `extract_timeout_s` | `60` | Timeout for one model extraction batch request, in seconds |
| `retrieval_mode` | `vector` | `vector` serves current recall, `shadow` compares hybrid in the background, `hybrid` serves grounded claim facts |
| `shadow_sample_rate` | `0.2` | Fraction of queries compared when `retrieval_mode: shadow` |
| `graph` | `hermes_knowledge` | Apache AGE graph name |
| `vector_k` | `12` | Top-$k$ nearest neighbors for vector recall |
| `min_similarity` | `0.58` | Cosine similarity cutoff floor |
| `max_tokens` | `1200` | Token budget for prompt injection block |
| `tiered_injection` | `true` | Inject a few full spans plus pointer summaries, and give the agent `memory_get` / `memory_search`. Set `false` for the old unlimited-full-span behavior. |
| `full_span_cap` | `4` | Maximum number of full spans in the memory block |
| `pointer_cap` | `15` | Maximum number of pointer summaries |
| `pointer_token_budget` | `300` | Token budget for pointer summaries |
| `memory_tool_calls_per_turn` | `5` | Cap on `memory_get` / `memory_search` calls per turn |
| `memory_scope` | `shared` | `shared`: one memory per profile across chats. `session`: each chat is isolated. |
| `extract_assistant_replies` | `false` | Let assistant replies add claims. These are always `unconfirmed`. |
| `work_capture` | `false` | **EXPERIMENTAL.** Capture work items and events from tool calls. |

### Evaluation

There were three rounds of live-model evals before tiered injection became the default. The
first round was null, and it's included here because it shaped the next two. Every round used
`anthropic/claude-sonnet-5` at temperature 0 with a fixed seed. Both conditions had the same
tools, and grading was blind against a rubric frozen before any answers existed. "Off" means
`tiered_injection: false` (unlimited full spans). "On" means tiered injection.

**T8.4: 40 single-fact questions. Null result.**
Source: [`docs/evals/t84_final_graded_report.json`](docs/evals/t84_final_graded_report.json)

| | off | on |
|---|---|---|
| Mean score (n=38 per condition; 2 excluded per side for ground-truth defects) | 0.816 | 0.803 |
| Delta (on − off) | | −0.013 |
| Avg tokens per API call | 2,620 | 2,939 |
| Pointer-follow rate ("on" run) | | 12/40 (0.30) |
| Negative controls answered correctly | 4/4 | 4/4 |

Tiering did not help here. These questions each need one fact. That fact either already fits
in the full-span budget or the model searches for it, so pointers have nothing to add. We kept
the default off and tested that explanation directly. The grading for this round also turned
up a redaction gap, which was fixed before any further hosted calls (see the v1.0.0 changelog,
Security).

**T8.4b: 10 two-fact questions. Positive, but too small to trust.**
Source: [`docs/evals/t84b_final_graded_report.json`](docs/evals/t84b_final_graded_report.json)

| | off | on |
|---|---|---|
| Mean score | 0.450 (n=10) | 0.611 (n=9; 1 excluded as a harness artifact) |
| Delta (on − off) | | +0.161 |
| Avg tokens per API call | 2,823 | 4,081 |
| Pointer-follow rate ("on" run) | | 7/10 (0.70) |

This was the regime we expected pointers to help in, and they did. We still did not treat it
as a pass. With n=9/10 the sample was small, the "off" baseline looked unusually low, and the
token cost per call rose by about 45%. The "+45%" figure is as summarized in
`docs/evals/t84c_final_report.json` → `decision_criteria_check`. Rationale:
`docs/hermes-memory-dev-spec.md` §8.5.3.

**T8.4c: 30 fresh multi-span questions + 5 single-fact controls, two blind graders, criteria
fixed in advance. Pass.**
Source: [`docs/evals/t84c_final_report.json`](docs/evals/t84c_final_report.json)

| | Grader 1 | Grader 2 |
|---|---|---|
| All 35: off → on | 0.457 → 0.600 (+0.143) | 0.457 → 0.643 (+0.186) |
| 30 multi-span: off → on | 0.400 → 0.567 | 0.400 → 0.617 |
| Multi-span paired delta (95% CI) | +0.167 (0.069–0.264) | +0.217 (0.126–0.307) |
| Multi-span win / loss / tie (on vs off) | 11 / 1 / 18 | 13 / 0 / 17 |
| 5 single-fact controls: off → on | 0.800 → 0.800 | 0.800 → 0.800 |
| Tokens per correct point: off → on | 6,125 → 6,836 | 6,125 → 6,378 |

- Grader agreement: 62/70 (0.886).
- Tokens per call: off 2,799, on 4,101 (+46.5%).
- All 4 pre-committed criteria pass (`all_4_criteria_pass: true`). The tokens-per-point
  criterion is recorded as a "borderline-pass".
- Spend for this round: $1.45 (`spend_usd`).

Caveats, also recorded in `docs/hermes-memory-dev-spec.md` §8.5.3:
- The second grader was an independent subagent, but probably from the same model family. It
  was not a different model or a human.
- n=5 is a small control arm.
- Individual fact anchors repeat across some of the 30 question pairs.

**Result:** `tiered_injection` defaults to `true` from v1.0.0 onward. The cost is about 46% more
tokens per call, in exchange for better answers on questions that need several facts. Set
`tiered_injection: false` to opt out.

### LongMemEval: in progress, results coming

A full LongMemEval run (500 questions) is in progress and is not part of v1.0.0.

A 20-question dry run exists (`docs/evals/longmemeval/dryrun_20q_final_report.json`). With 3–4
questions per category, it is **too small to read as an accuracy result**, so we aren't quoting
its scores. It was useful as a shakedown, and it surfaced:
- the known issues below (#118, #119, #123);
- 10 of 381 answer-evidence turns being dropped by the noise filter (`ingest_timing.evidence_turns_dropped_to_noise_filter`
  in the same file).

Full results, with methodology and those caveats, will land in a later release.

### Known issues

- [#118](https://github.com/rubyrayjuntos/hermes-memory/issues/118): text containing a `$`
  (prices, shell variables) doesn't get a graph vertex. The failure is logged and the
  conversation is still stored and retrievable, but the graph is missing those entities.
- [#119](https://github.com/rubyrayjuntos/hermes-memory/issues/119): under very heavy write bursts,
  a memory write can be dropped with only a WARNING log line, when both the queue and its
  durable outbox fallback are saturated.
- [#123](https://github.com/rubyrayjuntos/hermes-memory/issues/123): every stored turn reads the
  whole noun table, so writing gets slower as the database grows. This mostly affects bulk
  imports and very large memories.
- [#125](https://github.com/rubyrayjuntos/hermes-memory/issues/125): a passport-isolation
  property test fails on a clean `main` checkout (pre-existing, not a v1.0.0 regression). No
  known user-facing impact; tracked for root-causing the test's over-strict assertion.

---

## 🧭 Contributor & Architecture Guide

Contributors must adhere to the architectural invariants and release criteria defined in the governing specifications.

### Architectural Core

```text
Prompt Turn ──► vector_search (pgvector HNSW) ──► <memory> Injection Block
                     │
                     ├── shadow mode: claim ANN + entity linking + 2-hop walk
                     │                + RRF/MMR, logged without changing prompt
                     │
                     └── hybrid mode: grounded facts + cited supporting turns
```

The system operates across three coordinated tiers:
1. **Episodic & Conversational Storage**: Postgres `conversations` table, Apache AGE `Session`/`Turn` flower structure, and outbox queues (`write_outbox`).
2. **Semantic Knowledge & Claims**: Typed owner-scoped claims (`claims`), evidence rows, and entities (`V18`), populated by decoupled background workers (`extraction_worker.py`, `V19`).
3. **Retrieval & Shadow Logging**: `vector` remains the default served path. `shadow` computes owner-scoped claim retrieval (HNSW, batched entity linking, bounded 2-hop walk, RRF/MMR) off the prompt path and writes HMAC-keyed comparisons to `retrieval_shadow` (`V20`/`V21`). `hybrid` is opt-in and serves grounded facts with source spans; do not make it the default until the §9 evaluation gates pass.

The conversation-manifold walker has one authoritative relevance formula:
`beam_score = (0.4 * sim + 0.4 * (src_align * tgt_align) * prov_boost + 0.2 * decay) * (magnitude / 8)`.
It is computed at query time; the inspector displays returned components and does not
recompute or persist a competing score.

### Governing Specifications ("The Immune System")

Do not guess or assume architectural rules. Consult the authoritative references:

| Document | Purpose |
| :--- | :--- |
| [docs/specs/HERMES_MEMORY_SPEC.md](docs/specs/HERMES_MEMORY_SPEC.md) | **The Governing Specification**: Narrative source of truth and NOW implementation contracts. |
| [RELEASE_CRITERIA.md](RELEASE_CRITERIA.md) | **Destination Gates**: The only authoritative Open/Met criteria table (Delivery vs Retrieval Quality). |
| [DEFINITION_OF_DONE.md](DEFINITION_OF_DONE.md) | **Evidence Levels**: Required verification standards (user-facing features require Level 6 evidence). |
| [docs/CORRECTIONS.md](docs/CORRECTIONS.md) | **Verified Negative Constraints**: 15 disproven claims and traps to avoid restating. |
| [docs/age-quirks.md](docs/age-quirks.md) | **Engine Quirks**: Apache AGE Cypher nuances, SAVEPOINT wrapping, and syntax invariants. |
| [docs/PANE_INTERACTION_SPEC.md](docs/PANE_INTERACTION_SPEC.md) | **Inspector Contract**: Binding interaction specification for the visual pane. |
| [AGENTS.md](AGENTS.md) | **Agent & PR Guidelines**: Security policies, bot identity, and execution rules. |
| [docs/README.md](docs/README.md) | **Documentation Map**: Guide to living contracts, reports, and [docs/archive/](docs/archive/README.md). |

### Contributing Workflow

1. Check [RELEASE_CRITERIA.md](RELEASE_CRITERIA.md) to identify the target track (Delivery, Retrieval Quality, or Observability & Control).
2. Read [docs/CORRECTIONS.md](docs/CORRECTIONS.md) before writing any graph queries or modifying scoring formulas.
3. Review [CONTRIBUTING.md](CONTRIBUTING.md) for branch naming conventions, review standards, and CI requirements.

---

## License

MIT © 2026 The Hermes Librarian Authors.

## Document changelog

| Version | Created | Modified | Author | Notes |
|---|---|---|---|---|
| 1.0 | 2026-11-14 | 2026-11-14 | Codex | Restore the authoritative beam-score contract and add document provenance. |
