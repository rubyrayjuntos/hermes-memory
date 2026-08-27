# Verification

- `src/hermes_memory/verify.py:run_verify` — synthetic turn `Zephyr/Atlas` → `conversations` (2 rows) → `on_memory_write` mirror `memory_entries` → `prefetch` string. Never raises; 8s cap, <2s warm. Exit 0=PASS.
- `scripts/librarian_health.py` — `Store.librarian_health()` scoped check; `--fail-on-drift` exits 1 on drift, 2 on DB unreachable.
- Tests: `pytest -m "not integration" -q` (62 pass), `pytest tests/property -q` (18 pass), integration `pytest -m integration` needs compose 5450.

Run before every PR and after every ingest.
