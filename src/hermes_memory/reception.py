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
    """Store seam for the reception stage: evidence writers, uptake, catch-up."""

    async def record_alias_equation(
        self, span_id: int, canon_surface: str, alias_surface: str
    ) -> bool: ...
    async def record_rule_claims(
        self, span_id: int, claims: Sequence[dict]
    ) -> list[int]: ...
    async def previous_turn(
        self, session_id: str, conv_id: int, *, role: str = "assistant"
    ) -> dict | None: ...
    async def write_uptake(
        self, prior_span_id: int, next_span_id: int, value: str
    ) -> None: ...
    async def record_repair(
        self, prior_span_id: int, repair_span_id: int, *, deictic: bool,
        new_claims: Sequence[dict] = (),
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


_TIME_TRAIL = re.compile(
    r"\s+(?:last week|now|anymore)\s*$",
    re.IGNORECASE,
)
_NEG_MARK = re.compile(r"\b(don'?t|do not|no longer)\b", re.IGNORECASE)
_NOT_ANYMORE = re.compile(r"\bnot\b.{0,80}\banymore\b", re.IGNORECASE)
_QUESTION_START = re.compile(
    r"^\s*(?:do|does|did|am|are|is|have|has|can|could|would|will|should)\b",
    re.IGNORECASE,
)
_HYPOTHETICAL = re.compile(r"^\s*if\b", re.IGNORECASE)
_REPORTED = re.compile(r"\b(?:said|says|told)\b", re.IGNORECASE)
_SENT_START = re.compile(r"[.!?…]\s*")
SELF_TEMPLATES: dict[str, tuple[re.Pattern, ...]] = {
    "lives_in": (
        re.compile(
            r"\bI\s+(?:(?:don't|do not|no longer)\s+)?live\s+in\s+(?P<obj>[^\n.]{1,120})",
            re.IGNORECASE,
        ),
        re.compile(r"\bI\s+moved\s+to\s+(?P<obj>[^\n.]{1,120})", re.IGNORECASE),
        re.compile(r"\bI\s+relocated\s+to\s+(?P<obj>[^\n.]{1,120})", re.IGNORECASE),
        re.compile(r"\bI(?:'m|\s+am)\s+back\s+in\s+(?P<obj>[^\n.]{1,120})", re.IGNORECASE),
        re.compile(
            r"\bI\s+left\s+\S.{0,40}?\s+for\s+(?P<obj>[^\n.]{1,120})",
            re.IGNORECASE,
        ),
    ),
    "based_in": (
        re.compile(r"\bI(?:'m|\s+am)\s+based\s+in\s+(?P<obj>[^\n.]{1,120})", re.IGNORECASE),
    ),
    "works_for": (
        re.compile(
            r"\bI\s+(?:(?:don't|do not|no longer)\s+)?work\s+(?:at|for)\s+(?P<obj>[^\n.]{1,120})",
            re.IGNORECASE,
        ),
        re.compile(r"\bI\s+joined\s+(?P<obj>[^\n.]{1,120})", re.IGNORECASE),
        re.compile(
            r"\bI(?:'m|\s+am)\s+(?:not\s+)?employed\s+by\s+(?P<obj>[^\n.]{1,120})",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bI\s+quit\b.{0,60}?\b(?:to\s+join|for)\s+(?P<obj>[^\n.]{1,120})",
            re.IGNORECASE,
        ),
    ),
    "role_is": (
        re.compile(r"\bMy\s+role\s+is\s+(?P<obj>[^\n.]{1,120})", re.IGNORECASE),
        re.compile(r"\bI(?:'m|\s+am)\s+an?\s+(?P<obj>[^\n.]{1,120})", re.IGNORECASE),
        re.compile(r"\bI\s+work\s+as\s+(?:an?\s+)?(?P<obj>[^\n.]{1,120})", re.IGNORECASE),
    ),
}


def _trim_self_object(raw: str) -> str:
    obj = (raw or "").strip().rstrip(".").rstrip("?!…").strip()
    obj = _TIME_TRAIL.sub("", obj).strip().rstrip(".").rstrip("?!…").strip()
    return obj


def _self_polarity(matched: str) -> str:
    if _NEG_MARK.search(matched) or _NOT_ANYMORE.search(matched):
        return "negative"
    return "positive"


def _abstain_context(text: str, start: int, end: int) -> bool:
    prev = 0
    for m in _SENT_START.finditer(text, 0, start):
        prev = m.end()
    nxt = len(text)
    n = _SENT_START.search(text, end)
    if n is not None:
        nxt = n.start()
    sent = text[prev:nxt]
    stripped = sent.strip()
    if stripped.endswith("?") or "?" in stripped[max(0, end - prev):]:
        return True
    if _QUESTION_START.match(stripped) or _HYPOTHETICAL.match(stripped):
        return True
    if _REPORTED.search(text[prev:start]):
        return True
    return False


def extract_self_assertions(text: str) -> list[dict]:
    """First-person functional claims. Conservative: abstain when evidence is thin."""
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for verb, patterns in SELF_TEMPLATES.items():
        for rx in patterns:
            for m in rx.finditer(text or ""):
                if _abstain_context(text or "", m.start(), m.end()):
                    continue
                obj = _trim_self_object(m.group("obj"))
                if not obj or len(obj.split()) > 6:
                    continue
                polarity = _self_polarity(m.group(0))
                key = (verb, obj.lower(), polarity)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "subject": "I",
                    "verb": verb,
                    "object": obj,
                    "polarity": polarity,
                })
    return out
