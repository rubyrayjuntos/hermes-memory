"""Compare sql/migrations on disk to migration_history.

Compose init runs once on an empty volume. GitHub CD is tag-only and cannot
reach 127.0.0.1:5450, so it never migrates live. Process start applies
pending V*.sql (advisory lock), then require_schema_head refuses to boot if
history still lags. The head check is a backstop, not the apply path.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Sequence

logger = logging.getLogger("hermes_memory.schema_guard")

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

# GitHub CD cannot reach the loopback volume. One Hermes provider process plus
# an optional pane may boot together; migrate.py's advisory lock serializes
# apply. This is not a rolling multi-instance fleet.
#
# head_check sits next to rolling_deploy on purpose: missing_versions only
# reports expected-not-applied (forward gaps). Extra / unrecognized applied
# versions do not fail. If rolling_deploy ever becomes True, this one-way
# check is insufficient — old instances would boot against a newer head
# without being asked whether they understand that schema.
DEPLOY_TOPOLOGY = {
    "github_cd_migrates_live": False,
    "process_start_applies_migrations": True,
    "require_schema_head_is_backstop": True,
    "instances": "single_provider_plus_optional_pane",
    "rolling_deploy": False,
    "head_check": "expected_not_applied_only",
}

# 257 live conversations have no mentions mesh from the V9 history lag.
# Do not lock a live-shaped (query → turn_id) golden set on that window
# until C–F is backfilled, or those turns will bake ANN-only behavior into
# ground truth.
LIVE_EVAL_POLICY = {
    "backfill_v9_gap_before_golden": True,
    "exclude_unpassported_turns_until_backfill": True,
    "enforced": True,
}

# V9 historical gap: conversation row exists, no noun passport
# (never stamped — the turn predates C–F or C–F never ran).
# drain_status is prospective: this drain's C–F outcome after insert B (V11).
# A V9-gap row has drain_status NULL. A GRAPH_DEGRADED row was stamped now.
# COUNT(drain_status=…) is a different fact. Merging them is the scoring
# split-brain at observability.
UNPASSPORTED_TURNS_SQL = """
SELECT COUNT(*)::bigint
  FROM conversations c
 WHERE NOT EXISTS (
       SELECT 1
         FROM memory_chunk_nodes p
        WHERE p.chunk_id = ('conv_' || c.id::text)
          AND p.source = 'conversation'
          AND p.noun_id IS NOT NULL
 )
"""


# Versions this process must see in migration_history when sql/migrations is
# not shipped next to the plugin (hermes-memory-install). The installer owns
# apply; the plugin only consumes. Bump when plugin code starts depending on
# a newer V*. Do not copy the SQL tree into ~/.hermes/plugins to keep these
# in sync — that is a second source of truth.
CONSUMER_HEAD_VERSIONS = ("V1", "V9", "V10", "V11")


def try_find_migrations_dir() -> Path | None:
    start = Path(__file__).resolve()
    for p in [start, *start.parents]:
        cand = p / "sql" / "migrations"
        if cand.is_dir() and any(cand.glob("V*.sql")):
            return cand
    return None


def find_migrations_dir() -> Path:
    found = try_find_migrations_dir()
    if found is None:
        raise RuntimeError("sql/migrations not found; cannot verify schema head")
    return found


def list_expected_versions(migrations_dir: Path | None = None) -> list[str]:
    root = migrations_dir if migrations_dir is not None else find_migrations_dir()
    versions: list[str] = []
    for p in sorted(root.glob("V*.sql")):
        if not MIGRATION_RE.match(p.name):
            continue
        versions.append(p.stem.split("__")[0])
    return versions


def expected_versions_for_head_check() -> list[str]:
    """Disk V*.sql when present (dev/CI); otherwise CONSUMER_HEAD_VERSIONS.

    Installed plugins have no sibling sql/migrations. Head is the live
    ``migration_history`` table the installer already applied.
    """
    found = try_find_migrations_dir()
    if found is not None:
        return list_expected_versions(found)
    return list(CONSUMER_HEAD_VERSIONS)


def missing_versions(applied: Iterable[str], expected: Sequence[str]) -> list[str]:
    """Forward gaps only: expected versions absent from history.

    Does not flag extra applied versions this code does not know. That is
    DEPLOY_TOPOLOGY["head_check"] == "expected_not_applied_only" and is
    only safe while rolling_deploy is False.
    """
    have = set(applied)
    return [v for v in expected if v not in have]


def parse_eval_kind(payload: object) -> str:
    """Return eval_kind from a golden-set file. Lists default to legacy_substring."""
    if isinstance(payload, dict):
        kind = str(payload.get("eval_kind") or "synthetic")
        return kind
    return "legacy_substring"


def assert_live_shaped_eval_allowed(
    eval_kind: str,
    *,
    unpassported_count: int | None,
) -> None:
    """Refuse live-shaped golden sets until the V9-gap C–F backfill is done.

    ``unpassported_count`` is the passport anti-join (UNPASSPORTED_TURNS_SQL),
    not COUNT(drain_status='graph_degraded'). ``None`` (unknown) fails closed.
    Synthetic and legacy_substring sets are not this gate.
    """
    if eval_kind != "live_shaped":
        return
    if not LIVE_EVAL_POLICY["backfill_v9_gap_before_golden"]:
        return
    if unpassported_count is None or int(unpassported_count) > 0:
        raise RuntimeError(
            "live-shaped golden set blocked until V9-gap C–F backfill "
            f"(unpassported_count={unpassported_count})"
        )


def apply_pending_migrations(dsn: str) -> None:
    """Apply on-disk V*.sql to ``dsn`` via scripts/migrate.py.

    Does not log the DSN. Concurrent callers serialize on migrate.py's
    advisory lock. ``require_schema_head`` must run after this, not instead.
    """
    if not dsn or not (dsn.startswith("postgres://") or dsn.startswith("postgresql://")):
        raise RuntimeError("apply_pending_migrations requires a postgres DSN")
    found = try_find_migrations_dir()
    if found is None:
        logger.info(
            "sql/migrations not in this tree; skip apply "
            "(installer owns schema; require_schema_head reads migration_history)"
        )
        return
    script = found.parent.parent / "scripts" / "migrate.py"
    if not script.is_file():
        raise RuntimeError("scripts/migrate.py not found")
    env = os.environ.copy()
    env["HYBRID_AGE_DSN"] = dsn
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(script.parent.parent),
        check=False,
    )
    if result.returncode != 0:
        logger.error("migrate.py failed rc=%s", result.returncode)
        raise RuntimeError(f"migrate.py failed rc={result.returncode}")
    logger.info("migrate.py complete")
