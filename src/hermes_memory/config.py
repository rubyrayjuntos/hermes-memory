"""hermes_memory.config — configuration resolution (yaml > env > defaults).

Config surface per docs/plans/v0.1.md §3.2:

    # ~/.hermes/config.yaml
    hybrid_age:
      dsn_env: HYBRID_AGE_DSN            # secret/endpoint -> env only
      embed_url_env: HYBRID_AGE_EMBED_URL
      embed_model: nomic-embed-text      # behavior knobs -> yaml
      graph: hermes_knowledge
      vector_k: 12
      min_similarity: 0.55
      max_tokens: 1200

Secrets/endpoints never live in code or committed yaml — they resolve from the
environment variables named by ``dsn_env`` / ``embed_url_env``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_DSN_ENV = "HYBRID_AGE_DSN"
DEFAULT_EMBED_URL_ENV = "HYBRID_AGE_EMBED_URL"

# Local development default only — production sets HYBRID_AGE_DSN.
_DEFAULT_DSN = "postgres://hermes:{pg_password}@localhost:5450/hermes_memory"


@dataclass
class HybridAgeConfig:
    """Resolved runtime configuration for the hybrid-age provider."""

    dsn: str = _DEFAULT_DSN
    embed_url: str = "http://localhost:11434/v1"
    embed_model: str = "nomic-embed-text"
    graph: str = "hermes_knowledge"
    vector_k: int = 12
    min_similarity: float = 0.55
    max_tokens: int = 1200
    embed_dim: int = 768
    queue_maxsize: int = 256
    prefetch_timeout_s: float = 2.0
    dsn_env: str = DEFAULT_DSN_ENV
    embed_url_env: str = DEFAULT_EMBED_URL_ENV
    decay_half_life_days: float = 30.0
    hnsw_ef_search: int = 100
    _raw: Dict[str, Any] = field(default_factory=dict, repr=False)


def _load_yaml_block(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Read the ``hybrid_age:`` block from ~/.hermes/config.yaml (best effort)."""
    path = config_path or os.path.join(
        os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
        "config.yaml",
    )
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    block = doc.get("hybrid_age")
    return dict(block) if isinstance(block, dict) else {}


def _is_hermes_memory_pyproject(path: Path) -> bool:
    try:
        head = path.read_text(encoding="utf-8")[:4000]
    except OSError:
        return False
    return 'name = "hermes-memory"' in head or "name = 'hermes-memory'" in head


def dotenv_paths() -> list[Path]:
    """Clone ``.env`` first (Documents :5450), then ``~/.hermes/.env`` (install :5452).

    ``setdefault`` later, so the first file that defines a key wins. Process
    env already set always wins. Never logs values.
    """
    paths: list[Path] = []
    here = Path.cwd()
    for parent in [here, *here.parents]:
        pyproject = parent / "pyproject.toml"
        env = parent / ".env"
        if pyproject.is_file() and env.is_file() and _is_hermes_memory_pyproject(pyproject):
            paths.append(env)
            break
    for extra in (
        Path.home() / ".hermes" / ".env",
        Path.home() / ".hermes" / "profiles" / "librarian" / ".env",
    ):
        if extra not in paths:
            paths.append(extra)
    return paths


def iter_dotenv_assignments(text: str):
    """Yield (key, value) in file order. First assignment for a key wins at the caller."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        yield key.strip(), value.strip().strip('"').strip("'")


def dotenv_key_from_file(path: Path, key: str) -> str | None:
    """Read one key from a dotenv file (export, comments, whitespace). First wins."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for k, v in iter_dotenv_assignments(text):
        if k == key:
            return v
    return None


def load_dotenv_files() -> None:
    """Pull HYBRID_AGE_* / HERMES_PG_PASSWORD from dotenv files if missing."""
    for path in dotenv_paths():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for key, value in iter_dotenv_assignments(text):
            if key.startswith("HYBRID_AGE_") or key == "HERMES_PG_PASSWORD":
                os.environ.setdefault(key, value)


# Back-compat alias used by graph_runtime / graph_api / replay script.
_load_dotenv_files = load_dotenv_files


def load_config(config_path: Optional[str] = None) -> HybridAgeConfig:
    """Resolve configuration: yaml names env keys; secrets come from env after dotenv.

    Missing ``HYBRID_AGE_DSN`` (or yaml-named ``dsn_env``) after dotenv is an
    error. There is no silent fallback to ``_DEFAULT_DSN`` (:5450).
    """
    load_dotenv_files()
    raw = _load_yaml_block(config_path)
    cfg = HybridAgeConfig(_raw=raw)

    def pick(yaml_key: str, env_name: str, current: Any) -> Any:
        if yaml_key in raw and raw[yaml_key] is not None:
            return raw[yaml_key]
        val = os.environ.get(env_name)
        if val:
            return val
        return current

    cfg.dsn_env = str(pick("dsn_env", "", cfg.dsn_env))
    cfg.embed_url_env = str(pick("embed_url_env", "", cfg.embed_url_env))

    explicit_dsn = os.environ.get(cfg.dsn_env) or (
        raw.get("dsn") if isinstance(raw.get("dsn"), str) else None
    )
    if not explicit_dsn:
        raise RuntimeError(
            f"missing {cfg.dsn_env}: set it in the process environment, the clone "
            ".env (Documents :5450/hermes_memory), or ~/.hermes/.env after install. "
            "Refusing silent fallback to :5450/hermes_memory."
        )
    cfg.dsn = explicit_dsn
    cfg.embed_url = os.environ.get(cfg.embed_url_env) or (
        raw.get("embed_url") if isinstance(raw.get("embed_url"), str) else None
    ) or cfg.embed_url

    if raw.get("embed_model"):
        cfg.embed_model = str(raw["embed_model"])
    elif os.environ.get("HYBRID_AGE_EMBED_MODEL"):
        cfg.embed_model = os.environ["HYBRID_AGE_EMBED_MODEL"]

    if raw.get("graph"):
        cfg.graph = str(raw["graph"])
    elif os.environ.get("HYBRID_AGE_GRAPH"):
        cfg.graph = os.environ["HYBRID_AGE_GRAPH"]

    for key in ("vector_k", "min_similarity", "max_tokens", "embed_dim",
                "queue_maxsize", "prefetch_timeout_s", "decay_half_life_days",
                "hnsw_ef_search"):
        if raw.get(key) is not None:
            try:
                setattr(cfg, key, type(getattr(cfg, key))(raw[key]))
            except (TypeError, ValueError):
                pass

    # also allow env override for decay_half_life_days
    if os.environ.get("HYBRID_AGE_DECAY_HALF_LIFE_DAYS"):
        try:
            cfg.decay_half_life_days = float(os.environ["HYBRID_AGE_DECAY_HALF_LIFE_DAYS"])
        except (TypeError, ValueError):
            pass

    return cfg


CONFIG_SCHEMA_FIELDS = [
    {
        "key": "dsn",
        "description": "Postgres DSN (postgres://user:pass@host:port/db) for the pgvector+AGE database",
        "secret": True,
        "required": True,
        "env_var": "HYBRID_AGE_DSN",
    },
    {
        "key": "embed_url",
        "description": "OpenAI-compatible embeddings endpoint (Ollama serves http://localhost:11434/v1)",
        "secret": False,
        "required": False,
        "default": "http://localhost:11434/v1",
        "env_var": "HYBRID_AGE_EMBED_URL",
    },
    {
        "key": "embed_model",
        "description": "Embedding model name (must produce 768-dim vectors)",
        "secret": False,
        "required": False,
        "default": "nomic-embed-text",
        "type": "text",
    },
    {
        "key": "graph",
        "description": "Apache AGE graph name for the knowledge layer",
        "secret": False,
        "required": False,
        "default": "hermes_knowledge",
        "type": "text",
    },
    {
        "key": "vector_k",
        "description": "How many vector hits to fetch per prefetch",
        "secret": False,
        "required": False,
        "default": 12,
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
    },
    {
        "key": "min_similarity",
        "description": "Minimum cosine similarity for a hit to be injected",
        "secret": False,
        "required": False,
        "default": 0.55,
        "type": "number",
        "minimum": 0.0,
        "maximum": 1.0,
        "step": 0.05,
    },
    {
        "key": "max_tokens",
        "description": "Token budget of the injected recall block (hard cap 1200)",
        "secret": False,
        "required": False,
        "default": 1200,
        "type": "integer",
        "minimum": 200,
        "maximum": 1200,
    },
    {
        "key": "decay_half_life_days",
        "description": "Half-life in days for recency decay exp(-age/half_life) used in graph expansion scoring",
        "secret": False,
        "required": False,
        "default": 30,
        "type": "number",
        "minimum": 1,
        "maximum": 365,
        "env_var": "HYBRID_AGE_DECAY_HALF_LIFE_DAYS",
    },
]
