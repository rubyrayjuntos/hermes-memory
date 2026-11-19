"""hermes_memory.config — configuration resolution (yaml > env > defaults).

Config surface per docs/plans/v0.1.md §3.2:

    # ~/.hermes/config.yaml
    hybrid_age:
      dsn_env: HYBRID_AGE_DSN            # secret/endpoint -> env only
      # embed_url_env optional — unset = in-process fastembed (no Ollama)
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
DEFAULT_EXTRACT_URL_ENV = "HYBRID_AGE_EXTRACT_URL"
DEFAULT_EXTRACT_KEY_ENV = "HYBRID_AGE_EXTRACT_KEY"
DEFAULT_EXTRACT_MODE_ENV = "HYBRID_AGE_EXTRACT_MODE"
DEFAULT_EXTRACT_SCHEMA_ENV = "HYBRID_AGE_EXTRACT_MODEL_SCHEMA"

# Local development default only — production sets HYBRID_AGE_DSN.
_DEFAULT_DSN = "postgres://hermes:{pg_password}@localhost:5450/hermes_memory"


@dataclass
class HybridAgeConfig:
    """Resolved runtime configuration for the hybrid-age provider."""

    dsn: str = _DEFAULT_DSN
    # Empty = in-process fastembed (default). Set HYBRID_AGE_EMBED_URL for HTTP override.
    embed_url: str = ""
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
    extract_url: str = ""
    extract_model: str = ""
    extract_timeout_s: float = 60.0
    extract_mode: str = "rules_only"
    extract_model_schema: str = "json_object"
    extract_url_env: str = DEFAULT_EXTRACT_URL_ENV
    extract_key_env: str = DEFAULT_EXTRACT_KEY_ENV
    # Spec 4 T4.1: assistant replies may contribute model-extracted claims,
    # always unconfirmed (speaker='assistant' never promotes to current).
    # Off by default -- enabling this changes which spans reach the model.
    extract_assistant_replies: bool = False
    retrieval_mode: str = "vector"
    shadow_sample_rate: float = 0.2
    decay_half_life_days: float = 30.0
    hnsw_ef_search: int = 100
    # "shared": every chat session of this profile reads and writes one memory (MVP
    # default). "session": each chat session is isolated (the earlier behavior, where a
    # new session could not recall facts from an older one).
    memory_scope: str = "shared"
    _raw: Dict[str, Any] = field(default_factory=dict, repr=False)


MEMORY_SCOPES = ("shared", "session")
SHARED_AGENT_IDENTITY = "default"


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
    cfg.extract_url_env = str(pick("extract_url_env", "", cfg.extract_url_env))
    cfg.extract_key_env = str(pick("extract_key_env", "", cfg.extract_key_env))
    cfg.extract_mode = str(
        os.environ.get(DEFAULT_EXTRACT_MODE_ENV)
        or raw.get("extract_mode")
        or cfg.extract_mode
    ).strip().lower()
    if cfg.extract_mode not in ("rules_only", "rules_then_model"):
        raise ValueError(
            f"invalid extract_mode {cfg.extract_mode!r}; "
            "expected 'rules_only' or 'rules_then_model'"
        )
    cfg.extract_model_schema = str(
        os.environ.get(DEFAULT_EXTRACT_SCHEMA_ENV)
        or raw.get("extract_model_schema")
        or cfg.extract_model_schema
    ).strip().lower()
    if cfg.extract_model_schema not in ("json_object", "json_schema"):
        raise ValueError(
            f"invalid extract_model_schema {cfg.extract_model_schema!r}; "
            "expected 'json_object' or 'json_schema'"
        )
    cfg.retrieval_mode = str(
        os.environ.get("HYBRID_AGE_RETRIEVAL_MODE")
        or raw.get("retrieval_mode")
        or cfg.retrieval_mode
    ).strip().lower()
    if cfg.retrieval_mode not in ("vector", "shadow", "hybrid"):
        raise ValueError(
            f"invalid retrieval_mode {cfg.retrieval_mode!r}; "
            "expected 'vector', 'shadow', or 'hybrid'"
        )
    shadow_sample_rate = os.environ.get("HYBRID_AGE_SHADOW_SAMPLE_RATE")
    if shadow_sample_rate is None:
        shadow_sample_rate = raw.get("shadow_sample_rate", cfg.shadow_sample_rate)
    try:
        cfg.shadow_sample_rate = float(shadow_sample_rate)
    except (TypeError, ValueError) as exc:
        raise ValueError("shadow_sample_rate must be a number from 0.0 to 1.0") from exc
    if not 0.0 <= cfg.shadow_sample_rate <= 1.0:
        raise ValueError("shadow_sample_rate must be a number from 0.0 to 1.0")

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
    cfg.extract_url = os.environ.get(cfg.extract_url_env) or (
        raw.get("extract_url") if isinstance(raw.get("extract_url"), str) else None
    ) or cfg.extract_url
    cfg.extract_model = os.environ.get("HYBRID_AGE_EXTRACT_MODEL") or (
        raw.get("extract_model") if isinstance(raw.get("extract_model"), str) else None
    ) or cfg.extract_model

    _assistant_raw = os.environ.get("HYBRID_AGE_EXTRACT_ASSISTANT_REPLIES")
    if _assistant_raw is None:
        _assistant_raw = raw.get("extract_assistant_replies")
    if _assistant_raw is not None:
        cfg.extract_assistant_replies = str(_assistant_raw).strip().lower() in (
            "1", "true", "yes", "on",
        )

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
                "hnsw_ef_search", "extract_timeout_s"):
        if raw.get(key) is not None:
            try:
                setattr(cfg, key, type(getattr(cfg, key))(raw[key]))
            except (TypeError, ValueError):
                pass

    if raw.get("memory_scope") is not None:
        scope = str(raw["memory_scope"]).strip().lower()
        if scope in MEMORY_SCOPES:
            cfg.memory_scope = scope

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
        "description": (
            "Optional OpenAI-compatible embeddings endpoint. Leave unset for "
            "in-process fastembed (nomic-embed-text, 768-d). Examples: "
            "http://localhost:11434/v1 (Ollama) or https://api.openai.com/v1"
        ),
        "secret": False,
        "required": False,
        "default": "",
        "env_var": "HYBRID_AGE_EMBED_URL",
    },
    {
        "key": "embed_model",
        "description": "Embedding model name (must produce 768-dim vectors; default nomic-embed-text)",
        "secret": False,
        "required": False,
        "default": "nomic-embed-text",
        "type": "text",
    },
    {
        "key": "extract_url",
        "description": (
            "Optional OpenAI-compatible chat-completions endpoint for background "
            "claim extraction. This is separate from the embedding endpoint."
        ),
        "secret": False,
        "required": False,
        "default": "",
        "env_var": "HYBRID_AGE_EXTRACT_URL",
    },
    {
        "key": "extract_model",
        "description": "Chat model name used for background claim extraction",
        "secret": False,
        "required": False,
        "default": "",
        "type": "text",
        "env_var": "HYBRID_AGE_EXTRACT_MODEL",
    },
    {
        "key": "extract_mode",
        "description": (
            "rules_only (default) or rules_then_model (opt-in; model fallback jobs "
            "may invoke the configured endpoint for each new turn)"
        ),
        "secret": False,
        "required": False,
        "default": "rules_only",
        "type": "text",
        "env_var": DEFAULT_EXTRACT_MODE_ENV,
    },
    {
        "key": "extract_model_schema",
        "description": (
            "json_object (default) or json_schema for model extraction. "
            "json_schema is endpoint-dependent and is not silently downgraded."
        ),
        "secret": False,
        "required": False,
        "default": "json_object",
        "type": "text",
        "env_var": DEFAULT_EXTRACT_SCHEMA_ENV,
    },
    {
        "key": "extract_assistant_replies",
        "description": (
            "Off by default. When on (with rules_then_model), assistant replies "
            "may also contribute model-extracted claims -- always unconfirmed; "
            "only a user turn can promote a claim to current."
        ),
        "secret": False,
        "required": False,
        "default": False,
        "type": "boolean",
        "env_var": "HYBRID_AGE_EXTRACT_ASSISTANT_REPLIES",
    },
    {
        "key": "retrieval_mode",
        "description": "vector (default), shadow (compare without changing prompt), or hybrid",
        "secret": False,
        "required": False,
        "default": "vector",
        "type": "text",
        "env_var": "HYBRID_AGE_RETRIEVAL_MODE",
    },
    {
        "key": "shadow_sample_rate",
        "description": "Fraction of queries evaluated in background shadow retrieval",
        "secret": False,
        "required": False,
        "default": 0.2,
        "type": "number",
        "minimum": 0.0,
        "maximum": 1.0,
        "step": 0.05,
        "env_var": "HYBRID_AGE_SHADOW_SAMPLE_RATE",
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


def resolve_agent_identity(scope: str, kwargs: Dict[str, Any]) -> str:
    """Memory owner for a session. ``shared`` pins one owner for the profile, so every
    chat session reads and writes the same memory. ``session`` keeps the per-chat owner."""
    if scope == "shared":
        # A caller that names its own memory owner (verify, tests) keeps it; a chat
        # session's per-chat key does not split the shared memory. Otherwise the owner
        # is the profile, so two profiles on one database never share memory.
        explicit = kwargs.get("agent_identity")
        if explicit and explicit not in (SHARED_AGENT_IDENTITY, kwargs.get("gateway_session_key")):
            return str(explicit)
        return profile_owner(kwargs.get("hermes_home"))
    explicit = kwargs.get("agent_identity")
    if explicit == SHARED_AGENT_IDENTITY:
        explicit = None
    return (
        kwargs.get("gateway_session_key")
        or explicit
        or kwargs.get("agent_workspace")
        or kwargs.get("agent_identity")
        or SHARED_AGENT_IDENTITY
    )


def profile_owner(hermes_home: Any) -> str:
    """Stable memory owner for a Hermes home: the profile name for ``.../profiles/<name>``,
    ``default`` for the root home (or when no home is given)."""
    if not hermes_home:
        return SHARED_AGENT_IDENTITY
    normalized = os.path.normpath(os.path.expanduser(str(hermes_home)))
    name = os.path.basename(normalized)
    parent = os.path.basename(os.path.dirname(normalized))
    if parent == "profiles" and name:
        return name
    return SHARED_AGENT_IDENTITY
