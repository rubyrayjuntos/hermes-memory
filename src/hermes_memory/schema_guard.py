"""Compare sql/migrations on disk to migration_history.

Live fell behind hermes_test because compose init runs once, pytest --migrate
keeps the test DB current, and CD never runs migrate.py against the local
volume. Provider init must refuse to write if history is not at head.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Sequence

MIGRATION_RE = re.compile(r"^(V\d+__.*)\.sql$")

# Existing rows stay NULL until rewritten. ANN still returns them.
# Implicitly nomic-embed-text / 768 — the only model this schema has ever used.
# A second embed model MUST backfill or exclude NULL first; do not treat NULL
# as whatever HybridAgeConfig.embed_model happens to be at query time.
LEGACY_NULL_EMBED_POLICY = {
    "decision": "trust_nomic_768",
    "ann_excludes_null": False,
    "backfill": False,
}


def find_migrations_dir() -> Path:
    start = Path(__file__).resolve()
    for p in [start, *start.parents]:
        cand = p / "sql" / "migrations"
        if cand.is_dir() and any(cand.glob("V*.sql")):
            return cand
    raise RuntimeError("sql/migrations not found; cannot verify schema head")


def list_expected_versions(migrations_dir: Path | None = None) -> list[str]:
    root = migrations_dir or find_migrations_dir()
    versions: list[str] = []
    for p in sorted(root.glob("V*.sql")):
        if not MIGRATION_RE.match(p.name):
            continue
        versions.append(p.stem.split("__")[0])
    return versions


def missing_versions(applied: Iterable[str], expected: Sequence[str]) -> list[str]:
    have = set(applied)
    return [v for v in expected if v not in have]
