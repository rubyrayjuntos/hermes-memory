# Architecture — hybrid-age Memory Provider

## Components

| Component | File | Responsibility |
|-----------|------|----------------|
| `provider.py` | `provider.py` | MemoryProvider implementation for Hermes |
| `graph_extractor.py` | `scripts/` | Cron-driven entity extraction into AGE |
| Postgres tables | `init.sql` | `conversations`, `memory_entries`, `memory_chunk_nodes` |
| AGE graph | Postgres | Entity vertices + typed edges in `hermes_knowledge` |

## Write Path

```text
sync_turn(user_content, assistant_content)
    │
    ▼
_enqueue_write({type: "turn", ...})
    │
    ▼
Background asyncio task (_awrite_drain)
    │
    ├─► Ollama embeddings.create(model=nomic-embed-text, input=text)
    │       │
    │       ▼
    │   Python list (768-dim)
    │
    ├─► Convert to pgvector literal
    │
    └─► INSERT INTO conversations/memory_entries
```

Key points:
- Non-blocking: writes go to an `asyncio.Queue(maxsize=256)`
- Drops if queue is full — never blocks the agent loop
- Secrets filtered via regex before embedding/write

## Recall Path

```text
prefetch(query)
    │
    ▼
_aprefetch(query)
    │
    ├─► Embed query (Ollama)
    ├─► Vector search: SELECT ... ORDER BY embedding <=> $1 LIMIT 12
    │       │
    │       ▼
    │   top-k seeds with similarity scores
    │
    ├─► _expand_graph(seeds)
    │       │
    │       ├─► Look up vertex IDs via memory_chunk_nodes
    │       └─► Cypher traversal: MATCH (n)-[r]->(m) LIMIT 40
    │
    └─► _budget(items) + _format(selected)
            │
            ▼
        Block of text injected into system prompt
```

Key points:
- Timeout: 0.8s hard cap on prefetch
- Filters: minimum similarity 0.55, secret regex filter
- Budget: ~1,200 tokens (~4,800 chars) max per turn
- Graph edges weighted by repetition frequency

## Isolation

- Board/tenant separation at the Hermes dispatcher level
- `agent_identity` scopes all writes (defaults to session key or profile name)
- Bridge table includes `graph_name` for multi-tenant graphs
