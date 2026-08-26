---
name: "🐛 Bug Report"
about: Something in the memory pipeline misbehaved.
title: '[BUG]: '
labels: 'bug, triage'
---
## What Happened
<!-- A clear description of the failure. Include the pipeline stage if known:
     write path (sync_turn / on_memory_write), extraction cron, recall (prefetch), or migration. -->

## Expected Behavior

## Actual Behavior

## Evidence
<!-- Logs, verify.py output, bench results, SQL query results. Redact credentials. -->

## Environment
- Hermes version:
- Install method: plugin directory / pip entry point
- Embedding provider: Ollama (nomic-embed-text) / OpenAI (text-embedding-3-small)
