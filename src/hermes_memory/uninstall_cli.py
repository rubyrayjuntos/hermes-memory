#!/usr/bin/env python3
"""hermes-memory-uninstall — Uninstall CLI for Hermes Librarian."""

import argparse
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from psycopg import Error as PsycopgError
from psycopg.conninfo import conninfo_to_dict

from hermes_memory.install_state import InstallState, install_state_path, load_install_state

HERMES_HOME = Path.home() / ".hermes"


@dataclass(frozen=True)
class UninstallTarget:
    host: str
    port: int
    database: str
    user: str
    password: str | None = None


def resolve_uninstall_target(args: argparse.Namespace, state: InstallState | None) -> UninstallTarget | None:
    """Resolve an explicit or recorded target without guessing a database."""
    if args.dsn is not None:
        try:
            parts = conninfo_to_dict(args.dsn)
            if set(parts) - {"host", "port", "dbname", "user", "password"}:
                raise ValueError
            host, database, user = (parts[key] for key in ("host", "dbname", "user"))
            port = int(parts.get("port", "5432"))
            if not all((host, database, user)) or "," in host or not 1 <= port <= 65535:
                raise ValueError
            return UninstallTarget(host, port, database, user, parts.get("password"))
        except (KeyError, ValueError, TypeError, PsycopgError) as exc:
            # Never include raw DSN or the parser's potentially credential-bearing error.
            raise ValueError("Invalid DSN: specify host, port, database and user") from exc
    if state is not None:
        return UninstallTarget(state.database_host, state.database_port,
                               state.database_name, state.database_user)
    if args.keep_db:
        return None
    raise ValueError("No installation record or explicit DSN; refusing database removal")


def build_compose_down_command(state: InstallState) -> list[str] | None:
    """Build a shutdown command only for recorded Compose ownership."""
    if state.compose_project is None or state.compose_file is None:
        return None
    return ["docker", "compose", "-p", state.compose_project, "-f", state.compose_file, "down"]


def _matches_record(target: UninstallTarget | None, state: InstallState | None) -> bool:
    return target is not None and state is not None and (
        target.host, target.port, target.database, target.user
    ) == (state.database_host, state.database_port, state.database_name, state.database_user)


def main(argv=None, *, home=None, runner=subprocess.run) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes-memory-uninstall",
        description="Uninstall the Hermes Librarian hybrid-age provider.",
    )
    parser.add_argument("--force", action="store_true", help="Skip confirmation prompt")
    parser.add_argument("--keep-db", action="store_true", help="Keep the database")
    parser.add_argument("--remove-plugin", action="store_true", help="Remove the hybrid-age plugin directory")
    parser.add_argument("--dsn", default=None, help="Explicit Postgres DSN of the database to drop")
    args = parser.parse_args(argv)
    home = Path(home) if home is not None else HERMES_HOME

    try:
        state = load_install_state(install_state_path(home))
        target = resolve_uninstall_target(args, state)
    except (ValueError, OSError) as exc:
        print(f"Uninstall refused: {exc}")
        return 1

    if not args.force:
        answer = input("Disable the provider and uninstall the selected resources? [y/N]: ").strip().lower()
        if answer != "y":
            print("Uninstall cancelled.")
            return 0

    if target:
        print(f"Database target: {target.host}:{target.port}/{target.database} (user {target.user})")
    compose = build_compose_down_command(state) if state and _matches_record(target, state) else None
    if compose:
        if not args.keep_db:
            compose.append("--volumes")
        result = runner(compose, capture_output=True, text=True)
        if result.returncode != 0:
            print("Compose shutdown failed; uninstall stopped.")
            return 1

    print("Stopping graph API...")
    try:
        from hermes_memory.graph_api import stop_daemon
        stop_daemon()
    except Exception:
        print("Graph API stop skipped.")

    hermes_bin = shutil.which("hermes") or "hermes"
    result = runner([hermes_bin, "config", "set", "memory.provider", "built-in"],
                    capture_output=True, text=True)
    if result.returncode != 0:
        print("Hermes provider configuration failed.")

    if not args.keep_db and not compose:
        assert target is not None
        env = None
        if target.password:
            env = os.environ.copy()
            env["PGPASSWORD"] = target.password
        result = runner(["dropdb", "-h", target.host, "-p", str(target.port), "-U", target.user,
                         "--", target.database], capture_output=True, text=True, env=env)
        if result.returncode != 0:
            print("Database removal failed.")
            return 1

    if args.remove_plugin:
        for plugin_dir in (home / "plugins" / "hybrid-age",
                           home / "profiles" / "librarian" / "plugins" / "hybrid-age"):
            if plugin_dir.is_symlink():
                plugin_dir.unlink()
            elif plugin_dir.exists():
                shutil.rmtree(plugin_dir)

    result = runner([hermes_bin, "restart"], capture_output=True, text=True)
    if result.returncode != 0:
        runner([hermes_bin, "memory", "off"], capture_output=True, text=True)
    print("Uninstall complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
