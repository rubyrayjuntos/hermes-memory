"""Which surfaces may name an entity (graph layer spec §4.1).

Pure. Pronouns, demonstratives, indefinites and generic nouns never create
or match an entity; first-person singular resolves to the owner's self
entity. Anything the normalizer reduces to nothing abstains.
"""
from __future__ import annotations

import re
from typing import Literal

from .reception import normalize_surface

SELF_NAME = "(self)"
SELF_NORM = ""

FIRST_PERSON = frozenset({"i", "me", "my", "mine", "myself"})
PRONOUNS = frozenset({
    "he", "him", "his", "himself", "she", "her", "hers", "herself",
    "they", "them", "their", "theirs", "themselves", "it", "its", "itself",
    "we", "us", "our", "ours", "ourselves", "you", "your", "yours",
    "yourself", "yourselves",
})
DEMONSTRATIVES = frozenset({"this", "that", "these", "those"})
INDEFINITES = frozenset({
    "someone", "somebody", "something", "anyone", "anybody", "anything",
    "everyone", "everybody", "everything", "nobody", "nothing",
})
ABSTAIN_SURFACES = PRONOUNS | DEMONSTRATIVES | INDEFINITES
GENERIC_HEADS = frozenset({
    "project", "app", "application", "repo", "repository", "team", "company",
    "thing", "user", "system", "tool", "service", "code", "stuff", "one",
})
DETERMINERS = frozenset({
    "the", "a", "an", "this", "that", "these", "those", "my", "our", "your",
    "his", "her", "their", "its",
})
STOPWORDS = frozenset({
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "was", "were", "be", "do", "does", "did", "where", "what", "who", "which",
    "when", "why", "how", "with", "at", "by", "from",
})

_NAME_RE = re.compile(r"[A-Z][\w\-]*(?:\s+[A-Z][\w\-]*){0,3}")


def normalize_name(surface: str) -> str:
    return normalize_surface(surface)


def is_first_person(surface: str) -> bool:
    return normalize_name(surface) in FIRST_PERSON


def is_abstained(surface: str) -> bool:
    norm = normalize_name(surface)
    if not norm:
        return True
    if norm in FIRST_PERSON:
        return False
    if norm in ABSTAIN_SURFACES:
        return True
    words = norm.split()
    if all(w in STOPWORDS or w in ABSTAIN_SURFACES for w in words):
        return True
    return len(words) == 2 and words[0] in DETERMINERS and words[1] in GENERIC_HEADS


def classify_object(surface: str) -> Literal["entity", "literal", "abstain"]:
    raw = (surface or "").strip()
    if is_abstained(raw):
        return "abstain"
    if is_first_person(raw) or _NAME_RE.fullmatch(raw):
        return "entity"
    return "literal"
