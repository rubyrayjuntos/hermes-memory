# hermes-memory

Postgres + Apache AGE Vector & Graph Database with prompt context injection and conversation storage and retrieval.

Drop-in memory provider for [Hermes Agent](https://github.com/nousresearch/hermes-agent) that persists conversations and user memory to Postgres, embeds them with Ollama, and enriches recall with an Apache AGE knowledge graph.

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
| Cron | `graph-extractor.py` | Pattern-based entity extraction from conversations |

## Prerequisites

- Postgres 14+ with `pgvector` and `apache/age` extensions
- Ollama running locally
- Hermes Agent installed

### Postgres Setup

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT age;
LOAD 'age';
SELECT create_graph('hermes_knowledge');

-- Conversation storage with vector support
CREATE TABLE IF NOT EXISTS conversations (
    id SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_identity TEXT DEFAULT 'default',
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts TIMESTAMPTZ DEFAULT now(),
    embedding vector(768),
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_conversations_embedding
    ON conversations USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Memory entries with vector support
CREATE TABLE IF NOT EXISTS memory_entries (
    id SERIAL PRIMARY KEY,
    agent_identity TEXT DEFAULT 'default',
    target TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding vector(768),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(agent_identity, target, content)
);

CREATE INDEX IF NOT EXISTS idx_memory_entries_embedding
    ON memory_entries USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Bridge table: vector chunks ↔ AGE graph vertices
CREATE TABLE IF NOT EXISTS memory_chunk_nodes (
    chunk_id TEXT NOT NULL,
    source TEXT NOT NULL,
    vertex_id BIGINT NOT NULL,
    graph_name TEXT NOT NULL DEFAULT 'hermes_knowledge',
    PRIMARY KEY (chunk_id, source, vertex_id)
);

-- Pre-declare graph labels
SELECT create_vlabel('hermes_knowledge', 'Person');
SELECT create_vlabel('hermes_knowledge', 'Project');
SELECT create_vlabel('hermes_knowledge', 'Technology');
SELECT create_vlabel('hermes_knowledge', 'Organization');
SELECT create_vlabel('hermes_knowledge', 'Concept');
SELECT create_vlabel('hermes_knowledge', 'Domain');
SELECT create_vlabel('hermes_knowledge', 'Skill');
SELECT create_vlabel('hermes_knowledge', 'Tool');
SELECT create_elabel('hermes_knowledge', 'RELATED_TO');
SELECT create_elabel('hermes_knowledge', 'USES');
SELECT create_elabel('hermes_knowledge', 'BUILT_WITH');
SELECT create_elabel('hermes_knowledge', 'DEPENDS_ON');
SELECT create_elabel('hermes_knowledge', 'WORKS_ON');
SELECT create_elabel('hermes_knowledge', 'CO_MENTIONED');
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
HYBRID_AGE_DSN=postgres://hermes:hermesdev@localhost:5440/hermes
HYBRID_AGE_EMBED_URL=http://localhost:11434/v1
HYBRID_AGE_EMBED_MODEL=nomic-embed-text
HYBRID_AGE_GRAPH=hermes_knowledge
```

## Graph Extraction

Run `scripts/graph_extractor.py` to extract entities from recent conversations into the AGE graph:

```bash
# Dry run — see what would be extracted
python3 scripts/graph_extractor.py --hours 24 --dry-run

# Live run
python3 scripts/graph_extractor.py --hours 24

# Via cron — every 30 minutes
cronjob(action='create', schedule='*/30 * * * *', script='~/.hermes/scripts/graph_extractor.py', no_agent=True, deliver='local')
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
hybrid-age/
├── provider.py              # MemoryProvider implementation
├── docs/
│   ├── architecture.md      # Deep dive into hybrid-age design
│   └── age-quirks.md        # AGE gotchas and workarounds
├── example/
│   ├── config.yaml          # Example Hermes config
│   └── init.sql             # Postgres schema
└── scripts/
    └── graph_extractor.py   # Entity extraction from conversations
```

## How It Works

1. **Turn capture**: After every user/assistant turn, `sync_turn()` enqueues a write to Postgres with an Ollama-generated embedding
2. **Prefetch**: Before each turn, the provider embeds the current query, searches `conversations` and `memory_entries` via cosine similarity, then expands through the AGE graph
3. **Budgeted injection**: Selected facts are injected into the system prompt under `Relevant memory context (hybrid vector + graph):`, capped at ~1,200 tokens

## License

MIT
