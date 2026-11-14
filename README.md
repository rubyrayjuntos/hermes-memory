<img width="2048" height="1152" alt="hermes_librarian_banner" src="https://github.com/user-attachments/assets/109ea41f-2787-443f-a815-18139f80da4c" />

# 📜 The Hermes Librarian 📜

**Persistent memory & context injection for [Hermes Agent](https://github.com/nousresearch/hermes-agent)** —
combining PostgreSQL + pgvector for semantic recall and Apache AGE for knowledge graph entity expansion, all in a single database.

This is an **unofficial community plugin** (provider name `hybrid-age`). Status: **Alpha**. Tested against Hermes Agent v0.20.x. All database and API bindings are **loopback only** (`127.0.0.1`).

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

You do **not** need to clone this repository to use the plugin. The package bundles all installation and migration assets directly into its release wheel.

### Prerequisites

1. **Docker**: Running locally (for the loopback Postgres + Apache AGE container).
2. **Hermes Agent**: Installed with a working chat model (`hermes model`).

Embeddings run **in-process** via ONNX (`fastembed` + `nomic-embed-text`, 768-d). Ollama is **not** required. Optional: set `HYBRID_AGE_EMBED_URL` to an OpenAI-compatible HTTP endpoint (Ollama, OpenAI, …) if you prefer a remote embedder.

### Installation

Run this single command to install and set up the plugin:

```bash
pip install "git+https://github.com/rubyrayjuntos/hermes-memory" && hermes-memory-install --yes
```

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

If you want to visually explore the memory graph in your browser, open:
[http://127.0.0.1:7890/api/librarian/pane](http://127.0.0.1:7890/api/librarian/pane)

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
