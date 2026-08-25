"""P3 — capitalized-phrase extraction guards; P4 — phrase normalization idempotence.

SKIP rationale (per card C5 instructions): the capitalized-phrase extractor and
phrase-normalization function belong to the graph_extractor port, which is
v0.2 scope per docs/plans/v0.1.md §7 and is not present in the surfaces this
card tests (store.py / ingest.py / provider.py). Marking SKIP with reason
instead of inventing test targets.

The nearest in-scope surrogate, ``HybridAgeMemoryProvider._extract_topics``
(capitalized-phrase harvesting into the rolling session-topic window), does
exist and is exercised in test_p5_p6_module_deps.py::test_topic_window_guards.
"""
import pytest

pytest.skip(
    "P3/P4: extractor + normalization ship with v0.2 graph_extractor "
    "(plan §7); not present in v0.1 ported surface",
    allow_module_level=True,
)
