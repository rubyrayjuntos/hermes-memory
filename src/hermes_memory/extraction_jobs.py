"""Id-only extraction jobs for one stored turn (spec §5.2)."""
from __future__ import annotations

RULES_VERSION = "1"
MODEL_VERSION = "1"


def job_specs(span_id: int) -> tuple[tuple[str, str, dict], ...]:
    span = int(span_id)
    return (
        ("extract_rules", f"{span}:rules:{RULES_VERSION}",
         {"span_id": span, "extractor": "rules", "version": RULES_VERSION}),
        ("extract_model", f"{span}:model:{MODEL_VERSION}",
         {"span_id": span, "extractor": "model", "version": MODEL_VERSION}),
    )
