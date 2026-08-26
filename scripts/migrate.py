#!/usr/bin/env python3
"""scripts/migrate.py — slim file-based migration runner.

- Discovers sql/migrations/V*.sql sorted lexicographically
- Tracks applied versions in migration_history (with execution_time_ms)
- Concurrent-safe via pg_advisory_lock + transactional inserts
- Idempotent: re-running skips already-applied versions; checksum mismatch raises

Usage:
    python scripts/migrate.py [--dsn DSN] [--migrations-dir DIR] [--dry-run]
    HYBRID_AGE_DSN=postgres://... python scripts/migrate.py
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from pathlib import Path

DEFAULT_ADVISORY_LOCK = 727727727  # stable int for concurrent safety
MIGRATION_RE = re.compile(r"^(V\d+__.*)\.sql$")


def resolve_dsn(cli_dsn: str | None) -> str:
    if cli_dsn:
        return cli_dsn
    env_dsn = os.environ.get("HYBRID_AGE_DSN")
    if env_dsn:
        pw = os.environ.get("HERMES_PG_PASSWORD", "")
        if "***" in env_dsn and pw:
            env_dsn = env_dsn.replace("***", pw)
        if "{pg_password}" in env_dsn and pw:
            env_dsn = env_dsn.replace("{pg_password}", pw)
        return env_dsn
    pw = os.environ.get("HERMES_PG_PASSWORD", "ci-local-password")
    return f"postgres://hermes:{pw}@localhost:5450/hermes_memory"


def ensure_history_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS migration_history (
                version TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                checksum TEXT,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                execution_time_ms INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name='migration_history' AND column_name='execution_time_ms'"
        )
        if cur.fetchone() is None:
            cur.execute("ALTER TABLE migration_history ADD COLUMN IF NOT EXISTS execution_time_ms INTEGER NOT NULL DEFAULT 0")
        cur.execute("SELECT to_regclass('public.schema_migrations')")
        row = cur.fetchone()
        if row and row[0] is not None:
            cur.execute("SAVEPOINT sp_ensure_legacy")
            try:
                cur.execute(
                    """
                    INSERT INTO migration_history (version, filename, checksum, applied_at, execution_time_ms)
                    SELECT version, version || '.sql', NULL, applied_at, 0
                    FROM schema_migrations
                    ON CONFLICT (version) DO NOTHING
                    """
                )
                cur.execute("RELEASE SAVEPOINT sp_ensure_legacy")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_ensure_legacy")


def list_migrations(migrations_dir: Path):
    files = []
    for p in sorted(migrations_dir.glob("V*.sql")):
        m = MIGRATION_RE.match(p.name)
        if not m:
            continue
        version = p.stem.split("__")[0]  # V1, V2, ...
        content = p.read_bytes()
        checksum = hashlib.sha256(content).hexdigest()
        files.append((version, p.name, p, checksum, content.decode("utf-8", errors="replace")))
    return sorted(files, key=lambda x: x[0])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="File-based migration runner")
    ap.add_argument("--dsn", default=None, help="Postgres DSN")
    ap.add_argument("--migrations-dir", default=None, help="Path to sql/migrations")
    ap.add_argument("--dry-run", action="store_true", help="List pending without applying")
    ap.add_argument("--advisory-lock", type=int, default=DEFAULT_ADVISORY_LOCK, help="Advisory lock key")
    args = ap.parse_args(argv)

    dsn = resolve_dsn(args.dsn)

    if args.migrations_dir:
        migrations_dir = Path(args.migrations_dir)
    else:
        repo_root = Path(__file__).resolve().parents[1]
        migrations_dir = repo_root / "sql" / "migrations"
        if not migrations_dir.exists():
            migrations_dir = Path("sql/migrations")

    if not migrations_dir.exists():
        print(f"migrations dir not found: {migrations_dir}", file=sys.stderr)
        return 2

    migrations = list_migrations(migrations_dir)
    if not migrations:
        print(f"no migrations found in {migrations_dir}")
        return 0

    try:
        import psycopg
    except ImportError:
        print("psycopg is required (pip install psycopg)", file=sys.stderr)
        return 2

    print(f"migrate: {len(migrations)} migration(s) discovered in {migrations_dir}")
    for v, fn, _, chk, _ in migrations:
        print(f"  - {v} {fn}  sha256:{chk[:12]}")

    if args.dry_run:
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT version, checksum FROM migration_history")
                    applied = {r[0]: r[1] for r in cur.fetchall()}
                pending = [m for m in migrations if m[0] not in applied]
                print(f"dry-run: {len(pending)} pending, {len(applied)} already applied")
                for v, fn, _, _, _ in pending:
                    print(f"    pending {v} {fn}")
        except Exception as exc:
            print(f"dry-run DB check failed: {exc}", file=sys.stderr)
        return 0

    try:
        with psycopg.connect(dsn, autocommit=False) as conn:
            ensure_history_table(conn)
            conn.commit()

            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (args.advisory_lock,))
                print(f"migrate: acquired advisory lock {args.advisory_lock}")

                cur.execute("SELECT version, checksum FROM migration_history")
                applied = {row[0]: row[1] for row in cur.fetchall()}
                print(f"migrate: {len(applied)} already applied")

                applied_count = 0
                for version, filename, path, checksum, sql_text in migrations:
                    if version in applied:
                        if applied[version] and applied[version] != checksum:
                            print(f"ERROR: checksum mismatch for {version} ({filename}): db {applied[version][:12]} != file {checksum[:12]}", file=sys.stderr)
                            cur.execute("SELECT pg_advisory_unlock(%s)", (args.advisory_lock,))
                            conn.rollback()
                            return 1
                        print(f"  skip {version} ({filename}) — already applied")
                        continue

                    print(f"  apply {version} ({filename}) ...", end=" ", flush=True)
                    start = time.monotonic()
                    try:
                        cur.execute(sql_text)
                        elapsed_ms = int((time.monotonic() - start) * 1000)
                        cur.execute(
                            """
                            INSERT INTO migration_history (version, filename, checksum, execution_time_ms)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (version) DO NOTHING
                            """,
                            (version, filename, checksum, elapsed_ms),
                        )
                        cur.execute("SAVEPOINT sp_legacy_mirror")
                        try:
                            cur.execute(
                                "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING",
                                (version,),
                            )
                            cur.execute("RELEASE SAVEPOINT sp_legacy_mirror")
                        except Exception:
                            cur.execute("ROLLBACK TO SAVEPOINT sp_legacy_mirror")
                        cur.execute("SAVEPOINT sp_audit_mirror")
                        try:
                            cur.execute(
                                """
                                INSERT INTO schema_change_audit (version, filename, checksum, execution_time_ms)
                                VALUES (%s, %s, %s, %s)
                                """,
                                (version, filename, checksum, elapsed_ms),
                            )
                            cur.execute("RELEASE SAVEPOINT sp_audit_mirror")
                        except Exception:
                            cur.execute("ROLLBACK TO SAVEPOINT sp_audit_mirror")
                        conn.commit()
                        print(f"done ({elapsed_ms}ms)")
                        applied_count += 1
                    except Exception as exc:
                        conn.rollback()
                        cur.execute("SELECT pg_advisory_lock(%s)", (args.advisory_lock,))
                        print(f"FAILED: {exc}", file=sys.stderr)
                        cur.execute("SELECT pg_advisory_unlock(%s)", (args.advisory_lock,))
                        return 1

                cur.execute("SELECT pg_advisory_unlock(%s)", (args.advisory_lock,))
                print(f"migrate: complete — {applied_count} applied, {len(applied)} were already applied")
                if applied_count == 0:
                    print("migrate: idempotent — no changes")
            return 0
    except Exception as exc:
        print(f"migrate failed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
