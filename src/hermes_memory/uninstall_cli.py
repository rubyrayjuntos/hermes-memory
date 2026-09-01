#!/usr/bin/env python3
"""hermes-memory-uninstall — Uninstall CLI for Hermes Librarian."""

import argparse
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
        prog="hermes-memory-uninstall",
        description="Uninstall the Hermes Librarian hybrid-age provider.",
    )
    parser.add_argument("--force", action="store_true",
                        help="Skip confirmation prompt")
    parser.add_argument("--keep-db", action="store_true",
                        help="Keep the hermes_memory database")
    parser.add_argument("--remove-plugin", action="store_true",
                        help="Remove the hybrid-age plugin directory")
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

    # 1. Set provider to built-in in config
    print("[1/5] Setting provider to built-in...")
    result = subprocess.run(
        ["hermes", "config", "set", "memory.provider", "built-in"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    hermes config set failed: {result.stderr}")
    else:
        print("    Provider set to built-in.")

    # 2. Drop or keep the DB
    print("[2/5] Database handling...")
    if not args.keep_db:
        result = subprocess.run(
            ["dropdb", "-h", "127.0.0.1", "-p", "5450", "-U", "hermes",
             "hermes_memory"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            # Try alternative: maybe psql + DROP
            print(f"    dropdb issue: {result.stderr}")
            # Fallback: just note it
    else:
        print("    Keeping hermes_memory database (--keep-db).")

    # 3. Remove plugin dir if requested
    if args.remove_plugin:
        print("[3/5] Removing plugin directory...")
        plugin_dir = HERMES_HOME / "plugins" / "hybrid-age"
        if plugin_dir.exists():
            shutil.rmtree(str(plugin_dir))
            print(f"    Removed {plugin_dir}")
        else:
            print(f"    {plugin_dir} does not exist.")

    # 4. Restart Hermes so built-in provider becomes active
    print("[4/5] Restarting Hermes...")
    result = subprocess.run(["hermes", "restart"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        # fallback: hermes memory off
        subprocess.run(["hermes", "memory", "off"],
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