# Librarian health — scoped vs naive (issue #17)

## Live evidence (hermes:5440, 2026-08-26)

- `SELECT count(*) FROM memory_entries WHERE metadata->>'hash' IS NULL` → **3** (ids 1799-1801)
- Those 3: `metadata={task_id, platform, tool_name, session_id, tool_call_id, write_origin, execution_context}` — no `file_path`, 0 `memory_chunk_nodes` bridges, direct `memory(action=add)` user prefs.
- Good row 1817: `metadata={file_path, hash, language, doc_type, indexed_at, codebase}` + bridge.

## Correct scoped query (file-backed only)

```sql
-- total file-backed
SELECT count(*) FROM memory_entries WHERE metadata->>'file_path' IS NOT NULL;  -- 19

-- drift on file-backed only — should be 0
SELECT count(*) FROM memory_entries
 WHERE metadata->>'file_path' IS NOT NULL AND metadata->>'hash' IS NULL;      -- 0

SELECT count(*) FROM memory_entries
 WHERE metadata->>'file_path' IS NOT NULL AND metadata->>'doc_type' IS NULL;  -- 0

-- combined (librarian_health)
SELECT count(*) FROM memory_entries
 WHERE metadata->>'file_path' IS NOT NULL
   AND (metadata->>'hash' IS NULL OR metadata->>'doc_type' IS NULL);          -- 0
```

Implemented as `Store.librarian_health()` → `{total_file_backed, missing_hash, missing_doc_type}`. See `src/hermes_memory/store.py:239`.
