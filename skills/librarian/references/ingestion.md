# Ingestion — hash→chunk→embed→vector+graph+bridge

Per `docs/plans/v0.1.md` §3/§4. `src/hermes_memory/ingest.py: Ingestor._index_file`.

- Walk → `file_hash(path)` SHA-256 → state diff (skip unchanged / flag drift / prune deleted)
- Chunk → `Embedder.embed_text` (Ollama nomic-embed-text 768-dim) → `vec_to_literal`
- `doc_chunks` + `memory_entries` (one row per file keyed by `metadata.file_path`)
- `MERGE (v:File {path: $rel})` then `SET v += {hash, language, indexed_at}` — AGE 1.6 lacks ON CREATE SET
- `MERGE (v:Module {name})` etc., `MERGE (a)-[e:IMPORTS]->(b)` — all inside `SAVEPOINT sp_*` / `RELEASE`, so one bad Cypher never poisons the txn (plan §3.3).

Metadata injected as `meta_common={file_path, hash, language, codebase}` plus `doc_type`/`indexed_at` at provider layer when missing.
