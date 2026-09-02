#!/usr/bin/env python3
"""hermes-memory-uninstall — Uninstall CLI for Hermes Librarian."""

import argparse
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


def main():
    parser = argparse.ArgumentParser(
        prog="hermes-memory-uninstall",
        description="Uninstall the Hermes Librarian hybrid-age provider.",
    )
    parser.add_argument("--force", action="store_true",
                        help="Skip confirmation prompt")
    parser.add_argument("--keep-db", action="store_true",
                        help="Keep the hermes_memory database")
    parser.add_argument("--remove-plugin", action="store_true",
                        help="Remove the hybrid-age plugin directory")
    parser.add_argument("--dsn", default=None,
                        help="Postgres DSN of the database to drop "
                             "(default: HYBRID_AGE_DSN env or 127.0.0.1:5450/hermes_memory)")
    args = parser.parse_args()

    # Confirmation prompt
    if not args.force:
        prompt = (
            "This will disable the hybrid-age provider, "
            "drop the hermes_memory database (unless --keep-db), "
            "and remove the plugin directory (unless --remove-plugin is off). "
            "Continue? [y/N]: "
        )
        answer = input(prompt).strip().lower()
        if answer != "y":
            print("Uninstall cancelled.")
            sys.exit(0)

    # 0. Stop viz API
    print("[0/5] Stopping graph API...")
    try:
        from hermes_memory.graph_api import stop_daemon
        stop_daemon()
        print("    graph API stopped.")
    except Exception as e:
        print(f"    graph API stop skipped: {e}")

    # 1. Set provider to built-in in config
    print("[1/5] Setting provider to built-in...")
    hermes_bin = shutil.which("hermes") or "hermes"
    result = subprocess.run(
        [hermes_bin, "config", "set", "memory.provider", "built-in"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    hermes config set failed: {result.stderr}")
    else:
        print("    Provider set to built-in.")

    # 2. Drop or keep the DB — derive from configured DSN, not hardcoded 5450
    print("[2/5] Database handling...")
    if not args.keep_db:
        import os
        import urllib.parse as up
        dsn = args.dsn or os.environ.get("HYBRID_AGE_DSN") or "postgres://hermes:ci-local-password@127.0.0.1:5450/hermes_memory"
        # Prefer `dropdb` with --dsn if target provided, else parse host/port/db
        drop_cmd: list[str]
        # psycopg-style dsn parsing: extract parts for dropdb when not using URI directly
        parsed = up.urlparse(dsn)
        if parsed.scheme.startswith("postgres"):
            host = parsed.hostname or "127.0.0.1"
            port = str(parsed.port or "5450")
            user = parsed.username or "hermes"
            db = parsed.path.lstrip("/") or "hermes_memory"
            drop_cmd = ["dropdb", "-h", host, "-p", port, "-U", user, db]
            # Use --no-password style; password via PGPASSWORD env if present
            pw = parsed.password or os.environ.get("HERMES_PG_PASSWORD", "")
            env = None
            if pw:
                import os as _os
                env = _os.environ.copy()
                env["PGPASSWORD"] = pw
            result = subprocess.run(drop_cmd, capture_output=True, text=True, env=env)
        else:
            result = subprocess.run(["dropdb", dsn], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"    dropdb issue: {result.stderr.strip()[:300]}")
            print(f"    (DSN was {parsed.hostname}:{parsed.port}/{parsed.path.lstrip('/')}) — check that this is the intended database.")
    else:
        print("    Keeping hermes_memory database (--keep-db).")

    # 3. Remove plugin dirs if requested (default + librarian profile)
    if args.remove_plugin:
        print("[3/5] Removing plugin directory...")
        for plugin_dir in (
            HERMES_HOME / "plugins" / "hybrid-age",
            HERMES_HOME / "profiles" / "librarian" / "plugins" / "hybrid-age",
        ):
            if plugin_dir.exists():
                shutil.rmtree(str(plugin_dir))
                print(f"    Removed {plugin_dir}")
            else:
                print(f"    {plugin_dir} does not exist.")

    # 4. Restart Hermes so built-in provider becomes active
    print("[4/5] Restarting Hermes...")
    result = subprocess.run([hermes_bin, "restart"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        # fallback: hermes memory off
        subprocess.run([hermes_bin, "memory", "off"],
                        capture_output=True, text=True)
        print("    Hermes restarted (via memory off).")
    else:
        print("    Hermes restarted.")

    # 5. Summary
    print("[5/5] Summary:")
    print("    Provider: built-in")
    if not args.keep_db:
        print("    hermes_memory database: dropped")
    else:
        print("    hermes_memory database: kept")
    if args.remove_plugin:
        print("    Plugin directory: removed")
    else:
        print("    Plugin directory: kept")
    print("Uninstall complete.")


if __name__ == "__main__":
    main()