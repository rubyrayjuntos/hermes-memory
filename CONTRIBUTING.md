# Contributing to hermes-memory

Thanks for your interest in contributing.

## Development Setup

```bash
git clone https://github.com/rubyrayjuntos/hermes-memory.git
cd hermes-memory
cp .env.example .env          # set HERMES_PG_PASSWORD
docker compose up -d          # integration tests need this stack
pip install -e '.[dev]'
```

## Branch & kanban flow

Work is tracked on the kanban board in
[`docs/plans/board.md`](docs/plans/board.md); card specs and acceptance criteria
live in [`docs/plans/v0.1.md`](docs/plans/v0.1.md) §5, with frozen interface
contracts in §3–4.

- One `wip/<card-id>` branch per card; merge to `main` only at **Verified**;
  delete the branch the same day it merges.
- Columns: `Backlog → Claimed → In Review → Verified → Done`.
- Two-stage review (spec compliance → code quality) before a card is Verified.
- Interface contracts (`v0.1.md` §3/§4) are frozen — propose changes to the
  controller rather than drifting them.

## Running Tests

```bash
pytest tests/property -q       # pure-function property tests (P1–P8); no DB needed — must pass
pytest -q                      # default addopts already exclude integration
pytest tests/integration -q -m integration --override-ini="addopts="   # requires docker compose stack up
hermes-memory-verify           # end-to-end pipeline smoke check; exit non-zero = fail
```

**Requirements for a PR:** the property suite must pass. Integration tests and
`verify.py` require a running `docker compose` stack (`HERMES_PG_PASSWORD` set).

## Migration / DDL rules

Schema changes follow [plan §3.3](docs/plans/v0.1.md):

- Every Cypher statement inside a SAVEPOINT; failed statements roll back to the
  savepoint, never poison the transaction.
- AGE 1.6 lacks `ON CREATE SET` / `ON MATCH SET` — use MERGE-then-SET
  (`MERGE ... RETURN v;` then `SET v += {props}`), keeping the merge key out of
  the SET map.
- Batch MERGEs (≥50/txn).
- Vertex IDs crossing into JS/UI must be stringified (bigint precision).
- New DDL goes in `sql/init/` or a migration script under `sql/`; build HNSW
  indexes after bulk load, not before.

## Project Structure

| Path | Purpose |
|------|---------|
| `src/hermes_memory/provider.py` | `HybridAgeMemoryProvider` (Hermes `MemoryProvider` ABC) |
| `src/hermes_memory/config.py` | Config resolution: yaml > env > defaults |
| `src/hermes_memory/config_schema.py` | Powers `hermes memory setup hybrid-age` |
| `src/hermes_memory/embed.py` | Embedding backend (OpenAI-compatible / Ollama, 768-dim) |
| `src/hermes_memory/store.py` | SQL/Cypher data access (savepoints mandatory) |
| `src/hermes_memory/ingest.py` | Doc/codebase indexing CLI |
| `src/hermes_memory/verify.py` | Pipeline smoke-check CLI |
| `docker/Dockerfile`, `docker-compose.yml` | AGE PG17 1.6 + pgvector stack on 127.0.0.1:5450 |
| `sql/init/*.sql` | Extensions, schema, indexes (first-boot init) |
| `legacy/scripts/graph_*.py` | v0.2-scope extraction prototypes — not shipped |
| `docs/` | Architecture deep dive, AGE quirks, plans |

## Keeping your local in sync (trunk-based — please read before you start)

`main` is the **only durable branch**. Every `wip/<card-id>` is a disposable scratch checkout that is deleted the day its PR squash-merges. Nothing lives long outside `main`.

```bash
# run before you start AND before you stop — always
git fetch --prune && git status && git worktree list && git branch -vv

# make pulls auto-sync and auto-prune (one-time)
git config --global fetch.prune true
git config --global pull.rebase true
git config --global branch.autoSetupMerge simple
```

**Maintainers (push to `origin` directly):**

```bash
# after any PR merges — yours or anyone else's — fast-forward local trunk
git checkout main && git pull --ff-only origin main
# then delete the disposable branch (local + remote were scratch)
git branch -d wip/my-fix 2>/dev/null || git branch -D wip/my-fix
git push origin --delete wip/my-fix 2>/dev/null || true
git fetch --prune
```

**External contributors (fork workflow — required for OSS):**

```bash
# once
gh repo fork rubyrayjuntos/hermes-memory --clone
git remote add upstream https://github.com/rubyrayjuntos/hermes-memory.git

# every time before you branch
git fetch --all --prune
git checkout main && git pull upstream main && git push origin main
git checkout -b fix/my-thing

# when upstream PRs merge, your local is stale until you sync
gh repo sync              # or: git fetch upstream && git checkout main && git reset --hard upstream/main
```

Branch protection requires `main` to be up to date before merge, so CI will warn `branch out of date` until you pull. If you are behind, rebase: `git fetch upstream && git rebase upstream/main`.

## Pull Requests

1. Fork the repo (or claim a board card)
2. Create a `wip/<card-id>` or feature branch — never push `stash`, `local-sync`, or snapshot branches
3. Commit your changes (card prefix, e.g. `C4: ...`)
4. Ensure the property suite passes (and integration tests if you touched store/ingest)
5. Open a PR — fill out the template; when it squash-merges, delete the branch the same day (see above)

## Code Style

- Python 3.11+
- Type hints preferred
- Keep the write path non-blocking (queue + background task)
- Never block the agent loop

## Known Limitations

- Pattern-based extraction (`legacy/scripts/graph_extractor.py`) misses entities
  that don't match regex — and is v0.2 scope anyway.
- AGE has no `ON CREATE SET` / `ON MATCH SET` — use MERGE-then-SET.
- `interval '%s hours'` silently becomes 1 hour in psycopg — use `%s * interval '1 hour'`.

See [docs/age-quirks.md](docs/age-quirks.md) for the full list of AGE gotchas.

## License

By contributing, you agree your contributions will be licensed under MIT.
