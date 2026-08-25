# hermes-memory

Postgres + Apache AGE Vector & Graph Database with prompt context injection and conversation storage and retrieval.

Drop-in memory provider for [Hermes Agent](https://github.com/nousresearch/hermes-agent) that persists conversations and user memory to Postgres, embeds them with Ollama, and enriches recall with an Apache AGE knowledge graph.

## Quick Start

### Docker quick start

```bash
cp .env.example .env          # set HERMES_PG_PASSWORD
docker compose up -d          # builds ./docker, inits schema + graph automatically
docker compose ps             # wait for "healthy"
```

The image is Apache AGE (PG17) + pgvector; `sql/init/*.sql` create extensions,
the `hermes_knowledge` graph, all labels, tables and indexes on first boot.
Data persists in the `pgdata` volume across `docker compose down/up`.

```bash
# General quick start
# 1. Clone
git clone https://github.com/rubyrayjuntos/hermes-memory.git
cd hermes-memory

# 2. Start Postgres + AGE + pgvector
docker compose up -d

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure Hermes
cp .env.example .env
# Edit .env with your Postgres credentials

# 5. Add to ~/.hermes/config.yaml
# memory:
#   provider: hybrid-age
```

## Architecture

```text
User message
    │
    ▼
Hermes Agent
    │
    ▼
hybrid-age memory provider
    │
    ├─► Ollama embeddings (nomic-embed-text)
    │       │
    │       ▼
    │   pgvector in Postgres
    │       │
    │       ▼
    │   Vector search → top-k chunks
    │       │
    │       ▼
    │   AGE graph expansion via memory_chunk_nodes bridge
    │       │
    │       ▼
    └──► Injected into next prompt as "Relevant memory context"
```

| Layer | Tech | Purpose |
|-------|------|---------|
| Storage | Postgres + pgvector | Embeddings + raw conversation/memory rows |
| Graph | Apache AGE | Entity/relationship graph for cross-referencing |
| Embeddings | Ollama | Local embedding model (`nomic-embed-text`) |
| Bridge | `memory_chunk_nodes` | Links vector chunks to AGE vertices |
| Cron | `graph_extractor.py` | Pattern-based entity extraction from conversations |

## Prerequisites

- Postgres 14+ with `pgvector` and `apache/age` extensions
- Ollama running locally with `nomic-embed-text` model
- Hermes Agent installed

### Postgres Setup

```sql
-- Run example/init.sql after creating the database
\i example/init.sql
```

## Installation

### Option A: Plugin directory (recommended)

```bash
mkdir -p ~/.hermes/plugins/hybrid-age
cp provider.py ~/.hermes/plugins/hybrid-age/
```

### Option B: Hermes skills directory

```bash
cp -r . ~/.hermes/skills/autonomous-ai-agents/hermes-agent/plugins/hybrid-age/
```

## Configuration

Add to `~/.hermes/config.yaml`:

```yaml
memory:
  memory_enabled: true
  user_profile_enabled: true
  provider: hybrid-age
  memory_char_limit: 2200
  user_char_limit: 1375

# Optional: override embedding model/URL
# hybrid_age:
#   embed_url: http://localhost:11434/v1
#   embed_model: nomic-embed-text
#   graph: hermes_knowledge
```

### Environment variables

Set in `~/.hermes/.env` or system environment:

```bash
HERMES_MEMORY_DSN=postgres://hermes:hermesdev@localhost:5432/hermes
HERMES_MEMORY_GRAPH=hermes_knowledge

# Optional overrides
HYBRID_AGE_EMBED_URL=http://localhost:11434/v1
HYBRID_AGE_EMBED_MODEL=nomic-embed-text
```

## Graph Extraction

Run `legacy/scripts/graph_extractor.py` (v0.2 scope) to extract entities from recent conversations into the AGE graph:

```bash
# Dry run — see what would be extracted
python3 legacy/scripts/graph_extractor.py --hours 24 --dry-run

# Live run
python3 legacy/scripts/graph_extractor.py --hours 24

# Via Hermes cron — every 30 minutes
hermes cron add \
  --schedule "*/30 * * * *" \
  --script ~/.hermes/legacy/scripts/graph_extractor.py \
  --no-agent \
  --deliver local
```

Extraction strategy:
- Capitalized multi-word phrases → Person/Project/Technology/Concept
- Known technology/project/organization terms
- Email addresses, GitHub/LinkedIn handles
- Typed relationships from explicit statements
- Bounded co-mention edges

## Verification Queries

```sql
-- Total conversations stored
SELECT COUNT(*) FROM conversations;

-- Memory entries
SELECT COUNT(*) FROM memory_entries;

-- Graph vertex counts
SELECT 'Person' AS label, COUNT(*) FROM hermes_knowledge."Person"
UNION ALL SELECT 'Project', COUNT(*) FROM hermes_knowledge."Project"
UNION ALL SELECT 'Technology', COUNT(*) FROM hermes_knowledge."Technology"
UNION ALL SELECT 'Concept', COUNT(*) FROM hermes_knowledge."Concept"
ORDER BY 2 DESC;

-- Recent graph extractions
SELECT * FROM memory_chunk_nodes LIMIT 10;
```

## File Layout

```
hermes-memory/
├── provider.py                  # MemoryProvider implementation
├── requirements.txt             # Python dependencies
├── pyproject.toml               # Package metadata
├── Dockerfile                   # Postgres + AGE + pgvector image
├── docker-compose.yml           # One-command DB startup
├── .env.example                 # Environment variable template
├── .gitignore
├── LICENSE
├── README.md
├── docs/
│   ├── architecture.md          # Write/recall path diagrams
│   └── age-quirks.md            # AGE gotchas and workarounds
├── example/
│   └── init.sql                 # Postgres schema
└── scripts/
    ├── graph_extractor.py       # Entity extraction from conversations
    └── graph_taxonomy.py        # Extraction helper utilities
```

## How It Works

1. **Turn capture**: After every user/assistant turn, `sync_turn()` enqueues a write to Postgres with an Ollama-generated embedding
2. **Prefetch**: Before each turn, the provider embeds the current query, searches `conversations` and `memory_entries` via cosine similarity, then expands through the AGE graph
3. **Budgeted injection**: Selected facts are injected into the system prompt under `Relevant memory context (hybrid vector + graph):`, capped at ~1,200 tokens

## License

MIT
