"""Versioned, credential-free installation ownership and comparison metadata."""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path

OWNED_ENV_KEYS = frozenset({
    "HERMES_PG_PASSWORD", "HYBRID_AGE_DSN", "HYBRID_AGE_EMBED_URL",
    "HYBRID_AGE_EMBED_MODEL", "HYBRID_AGE_GRAPH",
})


@dataclass(frozen=True)
class InstallState:
    sha: str
    ref: str
    compose_project: str | None
    compose_container: str | None
    compose_file: str | None
    database_host: str
    database_port: int
    database_name: str
    database_user: str
    plugin_dirs: tuple[str, ...]
    skill_dirs: tuple[str, ...]
    config_paths: tuple[str, ...]
    env_paths: tuple[str, ...]
    owned_env_fingerprints: dict[str, str]
    status: str = "installing"
    schema_version: int = 1


def install_state_path(home: Path) -> Path:
    """``home`` is the Hermes home directory, normally ``~/.hermes``."""
    return home / "hermes-memory-install.json"


def _valid_text(value: object) -> bool:
    return (
        isinstance(value, str) and bool(value.strip())
        and not any(ord(char) < 32 for char in value)
        and "://" not in value
    )


def _valid_path(value: object) -> bool:
    return (
        _valid_text(value) and Path(value).is_absolute()
        and ".." not in Path(value).parts
    )


def _validate(payload: object) -> InstallState:
    """Fail closed on unknown fields/types; errors must never echo raw values."""
    def require(condition: bool, field: str) -> None:
        if not condition:
            raise ValueError(f"Invalid installation record: {field}")

    require(isinstance(payload, dict), "object required")
    require(set(payload) == {field.name for field in fields(InstallState)}, "fields")
    require(type(payload["schema_version"]) is int and payload["schema_version"] == 1,
            "schema_version")
    require(isinstance(payload["sha"], str)
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", payload["sha"]) is not None, "sha")
    require(_valid_text(payload["ref"]), "ref")
    require(payload["status"] in ("installing", "complete", "incomplete"), "status")
    require(_valid_text(payload["database_host"])
            and re.fullmatch(r"[A-Za-z0-9_.:-]+", payload["database_host"]) is not None,
            "database_host")
    require(type(payload["database_port"]) is int
            and 1 <= payload["database_port"] <= 65535, "database_port")
    for name in ("database_name", "database_user"):
        require(_valid_text(payload[name])
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", payload[name]) is not None, name)
    compose = [payload[name] for name in ("compose_project", "compose_container", "compose_file")]
    require(all(value is None for value in compose) or all(value is not None for value in compose),
            "compose ownership")
    if payload["compose_file"] is not None:
        require(_valid_path(payload["compose_file"]), "compose_file")
        for name in ("compose_project", "compose_container"):
            require(_valid_text(payload[name])
                    and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", payload[name]) is not None,
                    name)
    for name in ("plugin_dirs", "skill_dirs", "config_paths", "env_paths"):
        require(isinstance(payload[name], (list, tuple))
                and all(_valid_path(value) for value in payload[name]), name)
        payload[name] = tuple(payload[name])
    fingerprints = payload["owned_env_fingerprints"]
    require(isinstance(fingerprints, dict), "owned_env_fingerprints")
    require(set(fingerprints) <= OWNED_ENV_KEYS, "owned_env_fingerprints keys")
    require(all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                for value in fingerprints.values()), "owned_env_fingerprints hashes")
    return InstallState(**payload)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Invalid installation record: duplicate fields")
        result[key] = value
    return result


def load_install_state(path: Path) -> InstallState | None:
    """Return None only for an absent record; malformed records raise ValueError."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeError:
        raise ValueError("Invalid installation record: encoding") from None
    try:
        payload = json.loads(text, object_pairs_hook=_unique_object)
    except (ValueError, TypeError):
        raise ValueError("Invalid installation record: JSON") from None
    return _validate(payload)


def write_install_state(path: Path, state: InstallState) -> None:
    """Validate, fsync, and atomically replace a local record with mode 0600."""
    payload = asdict(_validate(asdict(state)))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
