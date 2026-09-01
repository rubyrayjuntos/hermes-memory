#!/usr/bin/env python3
"""hermes-memory-migrate — Data migration CLI for Hermes Librarian."""

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HERMES_HOME = Path.home() / ".hermes"

def _resolve_migration_sql(name: str) -> Path:
    """Resolve migration SQL via package resources with REPO_ROOT fallback."""
    # Try installed package resources first (importlib.resources)
    try:
        from importlib.resources import files as _files  # py3.9+
        pkg_file = _files("hermes_memory") / ".." / ".." / "sql" / "migrations" / name
        # Fallback if files API doesn't resolve outside package — use REPO_ROOT
    except Exception:
        pass
    # Primary: REPO_ROOT / sql/migrations
    candidate = REPO_ROOT / "sql" / "migrations" / name
    if candidate.exists():
        return candidate
    # Try importlib.resources for sql directory if installed as data
    try:
        import importlib.resources as _res
        with _res.as_file(_res.files("hermes_memory").joinpath(f"../../sql/migrations/{name}")) as p:
            if Path(p).exists():
                return Path(p)
    except Exception:
        pass
    return candidate


def get_env_files():
    return (HERMES_HOME / ".env", HERMES_HOME / "profiles" / "librarian" / ".env")


def write_env_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def main():
    parser = argparse.ArgumentParser(
        prog="hermes-memory-migrate",
        description="Migrate data between Hermes Librarian DSNs.",
    )
    parser.add_argument("--source", default=None,
                        help="Source Postgres DSN")
    parser.add_argument("--target", default=None,
                        help="Target Postgres DSN")
    parser.add_argument("--data-only", action="store_true",
                        help="Skip schema dump, migrate only rows + bridge")
    parser.add_argument("--force", action="store_true",
                        help="Skip confirmation prompts")
    args = parser.parse_args()

    source_dsn = args.source or input("Source DSN: ").strip()
    target_dsn = args.target or input("Target DSN: ").strip()

    if not source_dsn or not target_dsn:
        print("Both --source and --target are required.")
        sys.exit(1)

    # 1. Dump data from source
    print("[1/5] Dumping data from source DSN...")
    dump_path = Path("/tmp/hermes_migrate_dump.dmp")
    result = subprocess.run(
        ["pg_dump", "-a", source_dsn,
         "--exclude-table-data", "pg_*",
         "--exclude-table-data", "sql_*",
         "--exclude-table-data", "information_schema*",
         "-Fc", "-f", str(dump_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    Dump failed: {result.stderr}")
        sys.exit(1)
    print(f"    Dump saved to {dump_path}")

    # 2. Restore to target
    print("[2/5] Restoring data to target DSN...")
    result = subprocess.run(
        ["pg_restore", "-d", target_dsn, str(dump_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    Restore failed: {result.stderr}")
        sys.exit(1)
    print("    Data restored.")

    # 3. Apply V6/V7 constraint fix on target — use REPO_ROOT, fail hard (prefer V7 canonical md5)
    print("[3/5] Applying V7 canonical md5 constraint fix on target...")
    v_sql = REPO_ROOT / "sql" / "migrations" / "V7__canonical_md5_dedup.sql"
    if not v_sql.exists():
        # fallback to V6 if V7 not present (older checkout)
        v_sql = REPO_ROOT / "sql" / "migrations" / "V6__fix_memory_entries_conflict_target.sql"
    # Fallback to package resource if outside repo checkout
    if not v_sql.exists():
        try:
            import importlib.resources as pkg_resources
            # package holds sql as data? try relative to repo root discovery
            alt = Path(__file__).resolve().parents[2] / "sql" / "migrations" / "V7__canonical_md5_dedup.sql"
            if alt.exists():
                v_sql = alt
            else:
                alt2 = Path(__file__).resolve().parents[2] / "sql" / "migrations" / "V6__fix_memory_entries_conflict_target.sql"
                if alt2.exists():
                    v_sql = alt2
        except Exception:
            pass
    if not v_sql.exists():
        print(f"    Migration SQL not found: {v_sql} — aborting migration")
        sys.exit(1)
    result = subprocess.run(
        ["psql", "-d", target_dsn, "-f", str(v_sql)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    {v_sql.name} apply failed: {result.stderr}")
        sys.exit(1)
    else:
        print(f"    {v_sql.name} applied.")

    # 4. Optionally re-ingest codebase — use valid ingest CLI args
    if not args.data_only:
        print("[4/5] Re-running ingest to repopulate bridge + vectors (cwd)...")
        # hermes-memory-ingest takes [path] and --state-file only; DSN comes from env/config
        # Pass target_dsn via env for this subprocess so it hits the right DB
        import os
        env = os.environ.copy()
        env["HYBRID_AGE_DSN"] = target_dsn
        result = subprocess.run(
            [sys.executable, "-m", "hermes_memory.ingest"],
            capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            print(f"    ingest failed: {result.stderr}")
        else:
            print("    ingest complete.")

    # 5. Verify target explicitly
    print("[5/5] Running hermes-memory-verify on target...")
    result = subprocess.run([sys.executable, "-m", "hermes_memory.verify", "--dsn", target_dsn],
                            capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print("Migration finished with verify: FAIL")

    print("Migration complete.")

    if args.data_only:
        print("(Data-only mode: data dump/restore + V6 fix only)")


if __name__ == "__main__":
    main()