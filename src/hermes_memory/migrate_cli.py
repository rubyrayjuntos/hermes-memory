#!/usr/bin/env python3
"""hermes-memory-migrate — Data migration CLI for Hermes Librarian."""

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HERMES_HOME = Path.home() / ".hermes"


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

    # 3. Apply V6 constraint fix on target
    print("[3/5] Applying V6 constraint fix on target...")
    v6_sql = Path("/home/rswan/hermes-memory/sql/migrations/V6__fix_memory_entries_conflict_target.sql")
    result = subprocess.run(
        ["psql", "-d", target_dsn, "-f", str(v6_sql)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    V6 SQL apply issue: {result.stderr}")
    else:
        print("    V6 constraint fix applied.")

    # 4. Optionally re-ingest codebase
    if not args.data_only:
        print("[4/5] Re-running doc_indexer on target to repopulate bridge + vectors...")
        result = subprocess.run(
            ["hermes-memory-ingest", "--force", target_dsn],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"    doc_indexer failed: {result.stderr}")
        else:
            print("    doc_indexer complete.")

    # 5. Verify
    print("[5/5] Running hermes-memory-verify...")
    result = subprocess.run(["hermes-memory-verify"],
                            capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print("Migration finished with verify: FAIL")

    print("Migration complete.")

    if args.data_only:
        print("(Data-only mode: data dump/restore + V6 fix only)")


if __name__ == "__main__":
    main()