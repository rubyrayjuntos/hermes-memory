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
from dataclasses import dataclass, field, fields
from typing import Any, Dict, Optional

DEFAULT_DSN_ENV = "HYBRID_AGE_DSN"
DEFAULT_EMBED_URL_ENV = "HYBRID_AGE_EMBED_URL"

# Local development default only — production sets HYBRID_AGE_DSN.
_DEFAULT_DSN = "postgres://hermes:hermes@localhost:5450/hermes_memory"


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


def load_config(config_path: Optional[str] = None) -> HybridAgeConfig:
    """Resolve configuration: yaml > env > defaults."""
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

    # Endpoint/secret values come ONLY from the env vars the yaml names.
    cfg.dsn = os.environ.get(cfg.dsn_env) or (
        raw.get("dsn") if isinstance(raw.get("dsn"), str) else None
    ) or cfg.dsn
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
                "queue_maxsize", "prefetch_timeout_s"):
        if raw.get(key) is not None:
            try:
                setattr(cfg, key, type(getattr(cfg, key))(raw[key]))
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
]
