"""Ground model-extractor JSON against the turn (spec §4.1, §5.2 step 2).

Pure. No network. Invalid shapes return []. Context is prompt-only and is
never used as grounding evidence.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .entities import is_abstained, is_first_person, normalize_name

_FP_CUE = re.compile(
    r"\b(?:i|i'm|i've|i'd|i'll|me|my|mine|myself)\b",
    re.IGNORECASE,
)
_IMPLICIT_FP_VERBS = re.compile(
    r"^(?:moved|started|joined|left)\b",
    re.IGNORECASE,
)
_THIRD_PERSON_SUBJECT = re.compile(
    r"\b(?:she|he|they|it|the\s+\w+)\s+(?:moved|started|joined|left)\b",
    re.IGNORECASE,
)
_SELF_ALIAS = frozenset({"user", "the user", "me", "myself"})
# Spec 4 T4.2: assistant speaker only. "you/your" addresses the user and
# maps to "I" (self) for storage; the assistant's own "I/we/me" referring
# to itself must never become a user-self claim.
_ASSISTANT_SELF = frozenset({"i", "me", "my", "mine", "myself", "we", "us", "our", "ours"})
_ASSISTANT_YOU = frozenset({"you", "your", "yours"})


def _occurs(norm: str, corpus: Sequence[str]) -> bool:
    if not norm:
        return False
    needle = f" {norm} "
    return any(needle in f" {hay} " for hay in corpus)


def _keep_surface(surface: str) -> bool:
    if is_first_person(surface):
        return True
    return not is_abstained(surface)


def _has_first_person_cue(turn: str) -> bool:
    if not turn:
        return False
    # Explicit first-person pronouns
    if _FP_CUE.search(turn):
        return True
    # Implicit first-person verbs at sentence start (e.g., "moved to...", "Started at...")
    # but NOT if there's a third-person subject (e.g., "She moved...")
    if _IMPLICIT_FP_VERBS.search(turn.strip()) and not _THIRD_PERSON_SUBJECT.search(turn):
        return True
    return False


def _canonical_subject(subject: str, turn: str, speaker: str = "user") -> str | None:
    key = " ".join((subject or "").casefold().split())
    if speaker == "assistant":
        if key in _ASSISTANT_SELF:
            # The assistant talking about itself ("I think...", "we should...")
            # is never a claim about the user.
            return None
        if key in _ASSISTANT_YOU:
            return "I"
        return subject
    if is_first_person(subject) or key in _SELF_ALIAS:
        if not _has_first_person_cue(turn):
            return None
        return "I"
    return subject


def grounded_claims(raw: dict, turn: str, context: Sequence[str], verbs: set[str],
                    *, speaker: str = "user") -> list[dict]:
    """Keep claims whose verb is allowed and whose names appear in the turn.

    ``context`` is accepted for call-site compatibility but is not grounding
    evidence. First-person aliases map to ``I`` only when the turn has a cue.

    ``speaker`` selects the subject-remapping rules (Spec 4 T4.2): for
    ``"assistant"``, ``you/your`` maps to ``I`` (the user) while the
    assistant's own ``I/we/me`` is dropped -- it is never a self claim about
    the user. Evidence from this grounded claim must still be recorded with
    ``speaker='assistant'`` by the caller; this function does not decide
    confirmation status, only which claims survive grounding.
    """
    if not isinstance(raw, Mapping):
        return []
    claims = raw.get("claims")
    if not isinstance(claims, list):
        return []
    if not isinstance(turn, str):
        return []
    if not isinstance(context, Sequence) or isinstance(context, (str, bytes)):
        return []
    turn_corpus = [normalize_name(turn)]
    out: list[dict] = []
    for item in claims:
        if not isinstance(item, Mapping):
            return []
        subject, verb, obj = item.get("subject"), item.get("verb"), item.get("object")
        if not isinstance(subject, str) or not isinstance(verb, str) or not isinstance(obj, str):
            return []
        if verb not in verbs:
            continue
        canon = _canonical_subject(subject, turn, speaker)
        if canon is None:
            continue
        if canon == "I":
            subject = "I"
            subject_ok = True
        else:
            if not _keep_surface(subject):
                continue
            subject_ok = _occurs(normalize_name(subject), turn_corpus)
        if not _keep_surface(obj):
            continue
        if not subject_ok or not _occurs(normalize_name(obj), turn_corpus):
            continue
        polarity = item.get("polarity") or "positive"
        if polarity not in ("positive", "negative"):
            continue
        out.append({
            "subject": subject,
            "verb": verb,
            "object": obj,
            "polarity": polarity,
        })
    return out
