"""Remove only configuration still owned by the hybrid-age installer."""

import hashlib
from pathlib import Path

import yaml


def remove_owned_env_values(path: Path, expected: dict[str, str]) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        return
    kept = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            if hashlib.sha256(value.strip().encode("utf-8")).hexdigest() == expected.get(key.strip()):
                continue
        kept.append(line)
    if kept != lines:
        path.write_text("".join(kept), encoding="utf-8")


def remove_hybrid_age_config(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    config = yaml.safe_load(text)
    if config is None:
        return
    if not isinstance(config, dict):
        raise ValueError("Config is not a mapping")
    changed = "hybrid_age" in config
    config.pop("hybrid_age", None)
    memory = config.get("memory")
    if isinstance(memory, dict) and memory.get("provider") == "hybrid-age":
        memory["provider"] = "built-in"
        changed = True
    if changed:
        path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
