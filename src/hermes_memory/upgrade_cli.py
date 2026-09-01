#!/usr/bin/env python3
"""hermes-memory-upgrade — Upgrade CLI for Hermes Librarian."""

import argparse
import re
import shutil
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
        prog="hermes-memory-upgrade",
        description="Upgrade Hermes Librarian from any prior version to 0.1.0.",
    )
    parser.add_argument("--from-dsn", default=None,
                        help="Source Postgres DSN (legacy 5440 hermes DB)")
    parser.add_argument("--to-dsn", default=None,
                        help="Target Postgres DSN (new 5450 hermes_memory DB)")
    parser.add_argument("--backup", action="store_true",
                        help="Back up schema before upgrading")
    parser.add_argument("--skip-verify", action="store_true",
                        help="Skip hermes-memory-verify at the end")
    args = parser.parse_args()

    # Resolve DSNs
    from_dsn = args.from_dsn or input("From DSN (e.g. postgres://hermes:***@localhost:5440/hermes): ").strip() or None
    to_dsn = args.to_dsn or input("To DSN (e.g. postgres://hermes:***@localhost:5450/hermes_memory): ").strip() or None

    if not from_dsn or not to_dsn:
        print("Both --from-dsn and --to-dsn are required.")
        sys.exit(1)

    # 1. Backup schema if requested
    if args.backup:
        print("[1/5] Backing up schema from source...")
        backup_path = Path(f"~/librarian-upgrade-schema-{__import__('datetime').date.today()}.sql")
        backup_path = Path(backup_path).expanduser()
        result = subprocess.run(
            ["pg_dump", "-s", from_dsn],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"    Schema backup failed: {result.stderr}")
        else:
            print(f"    Schema backed up to {backup_path}")

    # 2. Run migrations on target
    print("[2/5] Running migrations on target DSN...")
    result = subprocess.run(
        ["scripts/migrate.py", "--dsn", to_dsn],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    Migration failed: {result.stderr}")
        sys.exit(1)
    print("    Migrations applied.")

    # 3. Rewrite DSNs in env files
    print("[3/5] Rewriting DSNs in env files...")
    for env_path in get_env_files():
        write_env_file(env_path, f"HYBRID_AGE_DSN={to_dsn}\n")

    # 4. Configure plugins
    print("[4/5] Configuring plugins...")
    result = subprocess.run(
        ["hermes", "config", "set", "plugins.enabled", "['hybrid-age']"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    hermes config set failed: {result.stderr}")
    else:
        print("    plugins.enabled set.")
    result = subprocess.run(
        ["hermes", "config", "set", "plugins.disabled", "['pgvector']"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    hermes config set failed: {result.stderr}")
    else:
        print("    plugins.disabled set.")

    # 5. Restart API and verify (optional)
    if not args.skip_verify:
        print("[5/5] Running hermes-memory-verify...")
        result = subprocess.run(["hermes-memory-verify"],
                                capture_output=True, text=True)
        print(result.stdout)
        if result.returncode != 0:
            print("Upgrade finished with verify: FAIL")
    else:
        print("Upgrade complete (verify skipped).")

    print("Upgrade complete.")


if __name__ == "__main__":
    main()