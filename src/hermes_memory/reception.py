"""Reception-side pure functions: aliases, repair classification, assertions.

No I/O, no store access — unit-testable in isolation. Conservative by design:
abstain (unknown / no rows) whenever evidence is thin. Missing structure is
allowed; fake structure is not.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol


class ReceptionStore(Protocol):
    """Minimal store seam for the reception stage (aliases/uptake/claims)."""

    async def insert_alias(
        self, surface_norm: str, canon_id: str, source: str
    ) -> bool: ...
    async def insert_claims(
        self, span_id: int, claims: Sequence[dict]
    ) -> list[int]: ...
    async def previous_turn(
        self, session_id: str, conv_id: int, *, role: str = "assistant"
    ) -> dict | None: ...
    async def write_uptake(
        self, prior_span_id: int, next_span_id: int, value: str
    ) -> None: ...
    async def retract_on_repair(
        self, prior_span_id: int, next_span_id: int,
        next_text: str, new_claims: Sequence[dict],
    ) -> int: ...
    async def stamp_claims_status(self, conv_id: int, status: str) -> None: ...
    async def fetch_pending_reception(self, limit: int) -> list[dict]: ...

# Tight repair cues. Deliberately NOT \\bno\\b: it fires on "no problem",
# "I know", "innovation". A leading "no" (the correction position) counts.
_REPAIR_RES = [
    re.compile(r"(?i)\bi meant\b"),
    re.compile(r"(?i)\bthat's wrong\b"),
    re.compile(r"(?i)\bthat'?s not (right|correct|what i)\b"),
    re.compile(r"(?i)\bnot that\b"),
    re.compile(r"(?i)\byou'?re wrong\b"),
    re.compile(r"(?i)\bincorrect\b"),
    # Leading "no" counts only in correction position: bare "No." or "No, ...".
    # "no problem" / "I know" must not fire (the \bno\b hazard).
    re.compile(r"^\s*no\b(?=\s*[,.:;]|$)", re.IGNORECASE),
]

# "don't" fires ONLY on manner/correction uses ("don't keep saying X",
# "don't frame it that way"). Bare "don't worry" / "don't know" / "doesn't
# matter" abstain — otherwise every casual negative stamps repaired and the
# retractor eats the prior span (the don't-cannon hazard).
_MANNER_RES = [
    re.compile(
        r"(?i)\bdon'?t\b.{0,48}\b(say|saying|said|calls?|calling|"
        r"frame|framing|phras\w*|describ\w*|word\w*|tone|manner)\b"
    ),
]

# Deictic repair with no new assertion: "no, not that" points at the prior
# span without stating a replacement claim.
_DEICTIC_RES = [
    re.compile(r"(?i)\bnot that\b"),
    re.compile(r"(?i)\bnot (it|what i meant)\b"),
]

# Explicit alias equations only. Embedding similarity must never mint these.
_ALIAS_RES = [
    re.compile(r"(.{2,60}?)\s*,?\s+also called\s+(.{2,60})", re.IGNORECASE),
    re.compile(r"(.{2,60}?)\s+\(aka\s+(.{2,60}?)\)", re.IGNORECASE),
    re.compile(r"(.{2,60}?)\s+aka\s+(.{2,60})", re.IGNORECASE),
]

# Claim subjects must be name-like. Pronouns, deictics, and generic process
# nouns ("Pipeline is green" from pasted logs) never name an entity — minting
# them repeats the Title-Case noun bug at smaller blast radius.
_STOP_FIRST = frozenset({
    "it", "they", "he", "she", "we", "you", "i",
    "this", "that", "these", "those", "the", "a", "an",
    "here", "there", "what", "which", "who", "how", "when", "where", "why",
    "pipeline", "want", "got", "per", "one", "all", "both", "with", "from",
})

# A negated object ("is not a guidance bus") must never become a positive
# claim. Negation is span-only until explicit polarity extraction lands;
# corrections travel through the repair-cue path instead.
_NEGATION = re.compile(r"^(not|no|never|neither)\b", re.IGNORECASE)
_ASSERTION_RES = [
    ("is", re.compile(r"\b([A-Z][\w\-]+(?:\s+[A-Z][\w\-]+){0,3})\s+is\s+([^.\n]{2,120})")),
    ("uses", re.compile(r"\b([A-Z][\w\-]+(?:\s+[A-Z][\w\-]+){0,3})\s+uses\s+([^.\n]{2,120})")),
]


def normalize_surface(text: str) -> str:
    """Deterministic normalizer: lower, collapse separators, strip punctuation."""
    s = (text or "").lower().strip()
    s = re.sub(r"[\s_\-]+", " ", s)
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def extract_alias_equations(text: str) -> list[tuple[str, str]]:
    """Explicit 'X also called Y' / 'X aka Y' equations → [(surface_norm, canon_id)].

    canon_id is the normalized primary name. Empty evidence → []. Never called
    on embedding similarity output.
    """
    out: list[tuple[str, str]] = []
    for rx in _ALIAS_RES:
        for m in rx.finditer(text or ""):
            surface = normalize_surface(m.group(2))
            canon = normalize_surface(m.group(1))
            if surface and canon and surface != canon:
                out.append((surface, canon))
    return list(dict.fromkeys(out))


def classify_repair(prior_text: str, next_text: str) -> str:
    """v1 uptake verdict for an assistant->user pair: 'repaired' | 'unknown'.

    Only the repaired state fires pre-gold (tight cues). accepted/used/abandoned
    need labeled thresholds — until then they stay unknown (no bonus, no penalty).
    """
    nxt = next_text or ""
    for rx in _REPAIR_RES:
        if rx.search(nxt):
            return "repaired"
    for rx in _MANNER_RES:
        if rx.search(nxt):
            return "repaired"
    return "unknown"


def is_deictic_repair(next_text: str) -> bool:
    """Repair pointing at the prior span with no replacement assertion."""
    return any(rx.search(next_text or "") for rx in _DEICTIC_RES)


def extract_assertions(text: str) -> list[dict]:
    """Boringly explicit '<Entity> is|uses <object>' claims from a user span.

    Returns [{subject, verb, object, polarity, act}]. Anything else → [] (the
    span is stored, unstructured). Never invents a verb the sentence lacks.
    """
    out: list[dict] = []
    for verb, rx in _ASSERTION_RES:
        for m in rx.finditer(text or ""):
            subject = m.group(1).strip()
            obj = m.group(2).strip().rstrip(".")
            if not subject or not obj:
                continue
            if subject.split()[0].lower() in _STOP_FIRST:
                continue
            if _NEGATION.match(obj):
                continue
            out.append({
                    "subject": subject,
                    "verb": verb,
                    "object": obj,
                    "polarity": "positive",
                    "act": "assert",
                })
    return out
