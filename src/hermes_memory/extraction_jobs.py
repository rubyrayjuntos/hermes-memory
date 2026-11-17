"""Id-only extraction jobs for one stored turn (spec §5.2)."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .entities import normalize_name
from .extract_nouns import extract_nouns
from .reception import extract_assertions
from .turn_filter import segment_turn

RULES_VERSION = "3"
MODEL_VERSION = "1"

_ACKNOWLEDGEMENT_RE = re.compile(
    r"^(?:ok(?:ay)?|thanks?(?:\s+you)?|got\s+it|understood|sounds\s+good|"
    r"great|sure|yes|no|right|agreed|noted)[.! ]*$",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(r"^\s*(?:what|why|how|who|when|where|which|can|could|"
                          r"would|will|do|does|did|is|are|should)\b", re.IGNORECASE)
_CODE_OR_TOOL_OUTPUT_RE = re.compile(
    r"```|^\s*(?:\$|>>>|Traceback \(most recent call last\):|"
    r"(?:INFO|DEBUG|WARNING|ERROR):|\{[\s\"']|^\|)",
    re.IGNORECASE | re.MULTILINE,
)


def job_specs(span_id: int) -> tuple[tuple[str, str, dict], ...]:
    span = int(span_id)
    return (
        ("extract_rules", f"{span}:rules:{RULES_VERSION}",
         {"span_id": span, "extractor": "rules", "version": RULES_VERSION}),
    )


def should_enqueue_model(text: str, recent_texts=()) -> bool:
    """Return whether a user turn may add useful model-only extraction.

    The rules extractor is deliberately narrow; model work is reserved for
    unstructured facts or entity mentions the rules did not cover.
    """
    raw = text or ""
    authored = "".join(
        raw[seg.start:seg.end] for seg in segment_turn(raw) if seg.kind == "authored"
    )
    value = authored.strip()
    if not value or _ACKNOWLEDGEMENT_RE.fullmatch(value):
        return False
    if value.endswith("?") or _QUESTION_RE.match(value):
        return False
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if _CODE_OR_TOOL_OUTPUT_RE.search(value) or (
        lines and sum(line.startswith(("$", ">", "Traceback", "INFO:", "DEBUG:",
                                      "WARNING:", "ERROR:")) for line in lines) / len(lines) > 0.5
    ):
        return False

    normalized = " ".join(re.sub(r"[^\w\s]", " ", value.casefold()).split())
    if len(normalized) >= 24:
        for previous in recent_texts:
            prior = " ".join(re.sub(r"[^\w\s]", " ", str(previous).casefold()).split())
            if prior and (normalized == prior or SequenceMatcher(None, normalized, prior).ratio() >= 0.94):
                return False

    claims = extract_assertions(value)
    if not claims:
        return True

    covered = {
        normalize_name(str(claim.get(key) or ""))
        for claim in claims
        for key in ("subject", "object")
    }
    return any(normalize_name(mention.label) not in covered for mention in extract_nouns(value))
