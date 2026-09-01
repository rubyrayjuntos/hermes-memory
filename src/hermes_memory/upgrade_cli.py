#!/usr/bin/env python3
"""hermes-memory-upgrade — Upgrade CLI for Hermes Librarian."""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HERMES_HOME = Path.home() / ".hermes"


def get_env_files():
    return (HERMES_HOME / ".env", HERMES_HOME / "profiles" / "librarian" / ".env")


def write_env_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def merge_env_file(path: Path, updates: dict) -> None:
    """Read-modify-write: preserve existing keys, update only *updates*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    order: list[str] = []
    raw_lines: list[str] = []
    if path.exists():
        raw_lines = path.read_text().splitlines()
        for line in raw_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, v = stripped.split("=", 1)
            k = k.strip()
            if k not in existing:
                order.append(k)
            existing[k] = v.strip()
    for k, v in updates.items():
        if k not in existing:
            order.append(k)
        existing[k] = v
    out_lines: list[str] = []
    seen: set[str] = set()
    if path.exists():
        for line in raw_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                out_lines.append(line)
                continue
            if "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k in updates:
                    out_lines.append(f"{k}={existing[k]}")
                    seen.add(k)
                elif k in existing and k not in seen:
                    out_lines.append(f"{k}={existing[k]}")
                    seen.add(k)
                else:
                    if k not in seen:
                        out_lines.append(line)
                        seen.add(k)
            else:
                out_lines.append(line)
    for k in order:
        if k not in seen:
            out_lines.append(f"{k}={existing[k]}")
            seen.add(k)
    path.write_text("\n".join(out_lines) + "\n")


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

    # 1. Backup schema if requested — actually write to backup_path
    if args.backup:
        print("[1/5] Backing up schema from source...")
        backup_path = (Path.home() / f"librarian-upgrade-schema-{__import__('datetime').date.today()}.sql")
        result = subprocess.run(
            ["pg_dump", "-s", "-f", str(backup_path), from_dsn],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"    Schema backup failed: {result.stderr}")
            sys.exit(1)
        try:
            backup_path.write_text(result.stdout)
            print(f"    Schema backed up to {backup_path} ({len(result.stdout)} bytes)")
        except OSError as e:
            print(f"    Schema backup write failed: {e}")
            sys.exit(1)

    # 2. Run migrations on target — use absolute path + python -m
    print("[2/5] Running migrations on target DSN...")
    migrate_script = REPO_ROOT / "scripts" / "migrate.py"
    result = subprocess.run(
        [sys.executable, str(migrate_script), "--dsn", to_dsn],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    Migration failed: {result.stderr}")
        sys.exit(1)
    print("    Migrations applied.")

    # 3. Rewrite DSNs in env files — merge, do not truncate
    print("[3/5] Rewriting DSNs in env files...")
    for env_path in get_env_files():
        merge_env_file(env_path, {"HYBRID_AGE_DSN": to_dsn})

    # 4. Configure plugins
    print("[4/5] Configuring plugins...")
    hermes_bin = shutil.which("hermes") or "hermes"
    result = subprocess.run(
        [hermes_bin, "config", "set", "plugins.enabled", "['hybrid-age']"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    hermes config set failed: {result.stderr}")
    else:
        print("    plugins.enabled set.")
    result = subprocess.run(
        [hermes_bin, "config", "set", "plugins.disabled", "['pgvector']"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    hermes config set failed: {result.stderr}")
    else:
        print("    plugins.disabled set.")

    # 5. Restart API and verify (optional) — pass target DSN explicitly
    if not args.skip_verify:
        print("[5/5] Running hermes-memory-verify...")
        result = subprocess.run([sys.executable, "-m", "hermes_memory.verify", "--dsn", to_dsn],
                                capture_output=True, text=True)
        print(result.stdout)
        if result.returncode != 0:
            print("Upgrade finished with verify: FAIL")
    else:
        print("Upgrade complete (verify skipped).")

    print("Upgrade complete.")


if __name__ == "__main__":
    main()