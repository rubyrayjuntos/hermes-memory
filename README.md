<img width="2048" height="1152" alt="hermes_librarian_banner" src="https://github.com/user-attachments/assets/109ea41f-2787-443f-a815-18139f80da4c" />

# 📜 The Hermes Librarian 📜

**Persistent memory & context injection for [Hermes Agent](https://github.com/nousresearch/hermes-agent)** —
combining PostgreSQL + pgvector for semantic recall and Apache AGE for knowledge graph entity expansion, all in a single database.

This is an **unofficial community plugin** (provider name `hybrid-age`). Status: **Alpha**. Tested against Hermes Agent v0.20.x. All database and API bindings are **loopback only** (`127.0.0.1`).

Beta has not been declared. Its entry bar and remaining blockers are tracked in
[`RELEASE_CRITERIA.md`](RELEASE_CRITERIA.md).

> *A librarian doesn't just store books — she knows where everything is, retrieves the right volume at the right moment, and quietly discards what's stale.*

Package: `hermes-memory` · Product: **The Hermes Librarian** · License: MIT

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
bundle the installation and migration assets into the wheel. The Alpha install
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

Install from an immutable Git revision rather than the moving `main` branch.
This command uses the current Alpha snapshot reviewed here; use a
maintainer-announced release tag or full commit SHA when one is available:

```bash
pip install "git+https://github.com/rubyrayjuntos/hermes-memory@c4f200587136521a8cdd666f9056af4c50cc3bc5" && hermes-memory-install --yes
```

`v0.9.0` is an earlier pre-release for install testing, not the current Alpha
snapshot or a public release.

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
| `graph` | `hermes_knowledge` | Apache AGE graph name |
| `vector_k` | `12` | Top-$k$ nearest neighbors for vector recall |
| `min_similarity` | `0.55` | Cosine similarity cutoff floor |
| `max_tokens` | `1200` | Token budget for prompt injection block |

---

## 🧭 Contributor & Architecture Guide

Contributors must adhere to the architectural invariants and release criteria defined in the governing specifications.

### Architectural Core

```text
Prompt Turn ──► vector_search (pgvector HNSW) ──► 2-hop Claim Walk (claim_walk.py)
                     │                                      │
                     ▼                                      ▼
             RRF Ranking Fusion ──► MMR Diversification ──► <memory> Injection Block
                     │
                     ▼
             retrieval_shadow (V20 Shadow Log)
```

The system operates across three coordinated tiers:
1. **Episodic & Conversational Storage**: Postgres `conversations` table, Apache AGE `Session`/`Turn` flower structure, and outbox queues (`write_outbox`).
2. **Semantic Knowledge & Claims**: Typed owner-scoped claims (`claims`), evidence rows, and entities (`V18`), populated by decoupled background workers (`extraction_worker.py`, `V19`).
3. **Retrieval & Shadow Logging**: HNSW vector search combined with bounded 2-hop claim walks, fused via Reciprocal Rank Fusion (RRF) and Maximal Marginal Relevance (MMR), evaluated against live traffic in shadow mode (`V20`, `shadow.py`).

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
