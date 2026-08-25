"""hermes_memory — hybrid Postgres/pgvector + Apache AGE memory provider for Hermes Agent.

Package layout per docs/plans/v0.1.md §2. Provider code lands in card C3.
"""

__version__ = "0.1.0"


def register(ctx):
    """Register the memory provider with Hermes' plugin context.

    Entry point referenced by both plugin-dir discovery (this ``__init__.py``
    contains the literal tokens ``register_memory_provider`` / ``MemoryProvider``)
    and the pip entry-point group ``hermes_agent.memory_providers``.

    Raises NotImplementedError until a real provider exists (plan card C3).
    """
    raise NotImplementedError(
        "hybrid-age memory provider is not implemented yet; "
        "see docs/plans/v0.1.md card C3"
    )


# Discovery heuristic markers (Hermes scans this file's text). The literal
# strings register_memory_provider / MemoryProvider below are intentional
# Hermes plugin-discovery markers — do not remove.
# register_memory_provider
# MemoryProvider
