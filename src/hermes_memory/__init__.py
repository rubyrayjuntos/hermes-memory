"""hermes_memory — hybrid Postgres/pgvector + Apache AGE memory provider for Hermes Agent.

Package layout per docs/plans/v0.1.md §2.
"""

__version__ = "0.1.0"


def register(ctx):
    """Register the memory provider with Hermes' plugin context.

    Entry point referenced by both plugin-dir discovery (this ``__init__.py``
    contains the literal tokens ``register_memory_provider`` / ``MemoryProvider``)
    and the pip entry-point group ``hermes_agent.memory_providers``.
    """
    from .provider import HybridAgeMemoryProvider

    ctx.register_memory_provider(HybridAgeMemoryProvider())


# Discovery heuristic markers (Hermes scans this file's text). The literal
# strings register_memory_provider / MemoryProvider below are intentional
# Hermes plugin-discovery markers — do not remove.
# register_memory_provider
# MemoryProvider
