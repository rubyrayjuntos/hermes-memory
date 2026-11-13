#!/usr/bin/env python3
"""hermes-memory-uninstall — Uninstall CLI for Hermes Librarian."""

import argparse
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from psycopg import Error as PsycopgError
from psycopg.conninfo import conninfo_to_dict

from hermes_memory.config_cleanup import remove_hybrid_age_config, remove_owned_env_values
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


def compose_down_env(dsn: str | None) -> dict[str, str]:
    """Environment for ``compose down`` on the recorded file.

    The installer's compose file requires ``HERMES_PG_PASSWORD`` to be non-empty
    during interpolation. ``down`` never connects to Postgres, so a supplied DSN's
    password is used when present, otherwise any existing value, otherwise an
    inert placeholder. The password is never recorded or printed.
    """
    env = os.environ.copy()
    password = urlsplit(dsn).password if dsn else None
    if password:
        env["HERMES_PG_PASSWORD"] = password
    elif not env.get("HERMES_PG_PASSWORD"):
        env["HERMES_PG_PASSWORD"] = "unused-by-compose-down"
    return env


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
    plugin = parser.add_mutually_exclusive_group()
    plugin.add_argument("--keep-plugin", action="store_true", help="Keep plugin and skill directories")
    plugin.add_argument("--remove-plugin", action="store_true", help="Compatibility alias for default cleanup")
    parser.add_argument("--dsn", default=None, help="Explicit Postgres DSN of the database to drop")
    args = parser.parse_args(argv)
    home = Path(home) if home is not None else HERMES_HOME

    try:
        try:
            state = load_install_state(install_state_path(home))
        except ValueError:
            if args.dsn is None and not args.keep_db:
                raise
            print("Invalid installation record; recorded ownership is unavailable.")
            state = None
        target = resolve_uninstall_target(args, state)
    except (ValueError, OSError) as exc:
        print(f"Incomplete: uninstall refused: {exc}")
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
        try:
            result = runner(compose, capture_output=True, text=True, env=compose_down_env(args.dsn))
        except OSError:
            print("Incomplete: Compose shutdown failed; uninstall stopped.")
            return 1
        if result.returncode != 0:
            print("Incomplete: Compose shutdown failed; uninstall stopped.")
            return 1

    failed = False
    print("Stopping graph API...")
    try:
        from hermes_memory.graph_api import stop_daemon
        stop_daemon()
    except Exception:
        print("Graph API stop failed.")
        failed = True

    if not args.keep_db and not compose:
        assert target is not None
        env = os.environ.copy()
        for name in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGDATABASE", "PGUSER",
                     "PGPORT", "PGHOST", "PGOPTIONS", "PGPASSWORD"):
            env.pop(name, None)
        if target.password is not None:
            env["PGPASSWORD"] = target.password
        try:
            result = runner(["dropdb", "-h", target.host, "-p", str(target.port), "-U", target.user,
                             "--", target.database], capture_output=True, text=True, env=env)
        except OSError:
            result = None
        if result is None or result.returncode != 0:
            print("Database removal failed.")
            failed = True

    def safe_path(raw: str, candidates: tuple[Path, ...]) -> Path:
        path = Path(raw)
        if (path not in candidates
                or path.parent.resolve() != home.resolve() / path.parent.relative_to(home)):
            raise ValueError("recorded path is outside its expected location")
        if path.is_symlink() and path not in artifact_paths:
            raise ValueError("recorded configuration path is a symlink")
        return path

    roots = (home, home / "profiles" / "librarian")
    plugin_paths = tuple(root / "plugins" / "hybrid-age" for root in roots)
    skill_paths = tuple(root / "skills" / "librarian-setup" for root in roots)
    artifact_paths = plugin_paths + skill_paths
    if state:
        steps = [(state.env_paths, tuple(root / ".env" for root in roots),
                  lambda p: remove_owned_env_values(p, state.owned_env_fingerprints)),
                 (state.config_paths, tuple(root / "config.yaml" for root in roots),
                  remove_hybrid_age_config)]
        if not args.keep_plugin:
            def remove_artifact(path):
                if path.is_symlink():
                    path.unlink()
                elif path.exists():
                    shutil.rmtree(path)
            steps.extend([(state.plugin_dirs, plugin_paths, remove_artifact),
                          (state.skill_dirs, skill_paths, remove_artifact)])
        for paths, allowed, action in steps:
            for raw in paths:
                try:
                    action(safe_path(raw, allowed))
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    print(f"Cleanup failed for {raw}: {type(exc).__name__}")
                    failed = True

    # The gateway is a host-wide service; restarting it is the operator's call.
    # A running gateway keeps the removed plugin loaded until it restarts.
    print("Restart the Hermes gateway to unload the plugin: hermes gateway restart")
    if not failed and state is not None:
        try:
            install_state_path(home).unlink(missing_ok=True)
        except OSError:
            failed = True
    if failed:
        print("Incomplete: uninstall did not finish; installation record retained.")
        return 1
    print("Uninstall complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
