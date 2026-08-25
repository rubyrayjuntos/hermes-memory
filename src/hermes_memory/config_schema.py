"""hermes_memory.config_schema — drives `hermes memory setup hybrid-age`.

Re-exports the declarative schema from config.py so both the packaged entry
point and an installed plugin copy expose it.
"""
from __future__ import annotations

from .config import CONFIG_SCHEMA_FIELDS


def get_config_schema() -> list:
    return [dict(f) for f in CONFIG_SCHEMA_FIELDS]
