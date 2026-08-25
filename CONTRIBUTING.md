# Contributing to hermes-memory

Thanks for your interest in contributing.

## Development Setup

```bash
git clone https://github.com/rubyrayjuntos/hermes-memory.git
cd hermes-memory
pip install -r requirements.txt
pip install pytest pytest-asyncio  # for dev
```

## Running Tests

```bash
pytest
```

## Project Structure

| Path | Purpose |
|------|---------|
| `hermes_memory/provider.py` | Main MemoryProvider implementation |
| `hermes_memory/embed.py` | Embedding backend abstraction (Ollama, OpenAI, etc.) |
| `hermes_memory/graph.py` | AGE graph client for vertices/edges/bridge table |
| `legacy/scripts/graph_extractor.py` | Entity extraction from conversations (v0.2 scope) |
| `legacy/scripts/graph_taxonomy.py` | Extraction helpers (label resolution, typed relations; v0.2 scope) |
| `example/init.sql` | Postgres schema |
| `docs/` | Architecture deep dive, AGE quirks |

## Pull Requests

1. Fork the repo
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes
4. Push to your fork
5. Open a PR — fill out the template

## Code Style

- Python 3.11+
- Type hints preferred
- Keep the write path non-blocking (queue + background task)
- Never block the agent loop

## Known Limitations

- Pattern-based extraction (`graph_extractor.py`) misses entities that don't match regex
- AGE has no `ON CREATE SET` — use savepoints + read-modify-write
- `interval '%s hours'` silently becomes 1 hour in psycopg — use `%s * interval '1 hour'`

## License

By contributing, you agree your contributions will be licensed under MIT.
