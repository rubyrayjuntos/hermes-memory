"""Ground model-extractor JSON against the turn and context (spec §4.1, §5.2 step 2).

Pure. No network. Invalid shapes return [].
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from .entities import is_abstained, is_first_person, normalize_name


def _corpus_norms(turn: str, context: Sequence[str]) -> list[str]:
    return [normalize_name(turn), *(normalize_name(t) for t in context)]


def _occurs(norm: str, corpus: Sequence[str]) -> bool:
    if not norm:
        return False
    needle = f" {norm} "
    return any(needle in f" {hay} " for hay in corpus)


def _keep_surface(surface: str) -> bool:
    if is_first_person(surface):
        return True
    return not is_abstained(surface)


def grounded_claims(raw: dict, turn: str, context: Sequence[str], verbs: set[str]) -> list[dict]:
    """Keep claims whose verb is allowed and whose names appear in the turn or context.

    Output items match ``record_rule_claims``: ``subject``, ``verb``, ``object``,
    ``polarity``. First person is not abstained.
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
    corpus = _corpus_norms(turn, context)
    out: list[dict] = []
    for item in claims:
        if not isinstance(item, Mapping):
            return []
        subject, verb, obj = item.get("subject"), item.get("verb"), item.get("object")
        if not isinstance(subject, str) or not isinstance(verb, str) or not isinstance(obj, str):
            return []
        if verb not in verbs:
            continue
        if not _keep_surface(subject) or not _keep_surface(obj):
            continue
        subj_n, obj_n = normalize_name(subject), normalize_name(obj)
        if not _occurs(subj_n, corpus) or not _occurs(obj_n, corpus):
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
