"""Mention-order noun extractor for conversation ingest (spec §6)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .provider import _is_noise

# Copied from legacy/scripts/graph_taxonomy.py — do not import legacy/.
CANONICAL_NAMES: dict[str, str] = {
    "neuronote": "NeuroNote",
    "kitchen kontrol": "Kitchen Kontrol",
    "canon forge": "Canon Forge",
    "ai ml ops factory": "AI/ML Ops Factory",
    "tokyo eye": "Tokyo Eye",
    "hyperbolic gnn": "Hyperbolic GNN",
    "mixture of experts": "Mixture of Experts",
    "pgvector": "pgvector",
    "postgres": "Postgres",
    "postgresql": "Postgres",
    "apache age": "Apache AGE",
    "sql": "SQL",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "generative ai": "Generative AI",
    "hermes agent": "Hermes Agent",
    "claude code": "Claude Code",
}

SCHEMA_DENYLIST = frozenset({
    "id", "src", "tgt", "conf", "noun_id", "chunk_id", "session_id",
    "turn_id", "vertex_id", "e_src_vec", "e_tgt_vec", "provenance_turns",
    "magnitude", "polarity", "verb_type",
})

VERIFY_SYNTHETICS = frozenset({"project zephyr", "atlas vault engine"})
_VERIFY_SYNTHETIC_TOKENS = [s.split() for s in VERIFY_SYNTHETICS]

AMBIGUOUS_SHORT = frozenset({
    "go", "git", "sql", "aws", "gcp", "rag", "r", "c", "d",
    "ai", "ml", "ui", "ux", "os", "vm", "db",
})

PHRASE_STOPWORDS = frozenset({
    "the", "a", "an", "and", "but", "or", "if", "then", "else", "when",
    "while", "why", "what", "how", "who", "which", "that", "this", "these",
    "those", "there", "here", "it", "its", "is", "are", "was", "were", "be",
    "been", "being", "do", "does", "did", "actually", "unless", "no", "not",
    "yes", "also", "just", "only", "very", "much", "more", "most", "some",
    "any", "each", "every", "both", "either", "neither", "for", "from",
    "with", "without", "into", "onto", "about", "after", "before", "during",
    "since", "until", "because", "so", "such", "than", "too", "now", "next",
    "first", "second", "third", "last", "final", "one", "two", "three",
    "note", "warning", "result", "results", "step", "steps", "example",
    "summary", "verdict", "fix", "fixed", "broken", "working", "current",
    "via", "see", "please", "talks", "talk", "to",
    "in", "of", "on", "at", "by", "as", "up", "out", "off", "over",
    "appear", "appears", "appeared", "today", "tomorrow", "yesterday",
    "production", "release", "deployed", "remember", "uses", "use", "used",
    "storage", "layer", "turn", "user", "verify", "synthetic",
})

TRAILING_GLUE = frozenset({
    "appear", "appears", "appeared", "in", "of", "on", "at", "to", "for",
    "from", "with", "by", "as", "up", "out", "off", "over", "today",
    "tomorrow", "yesterday", "now", "then", "production", "release",
})

TECH_CONTEXT = re.compile(
    r"\b(language|lang|code|coded|program|programming|written|write|compile|"
    r"compiled|runtime|binary|module|package|library|framework|install|"
    r"installed|version|repo|repository|commit|branch|query|queries|database|"
    r"schema|table|index|server|client|api|sdk|cli|golang|rustlang)\b",
    re.I,
)

BACKTICK_RE = re.compile(r"`([^`\n]+)`")
IDENTIFIER_RE = re.compile(
    r"\b(?:"
    r"[A-Z][a-z0-9]*(?:[A-Z][a-z0-9]*)+"
    r"|[a-z][a-z0-9]*(?:_[a-z0-9]+)+"
    r"|[a-z][a-z0-9]*(?:-[a-z0-9]+)+"
    r"|[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z][A-Za-z0-9]*)+"
    r"|[A-Za-z]+\d+"
    r")\b",
)
ACRONYM_RE = re.compile(r"\b[A-Z]{2,}\b")
TITLE_MULTIWORD_RE = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b",
)
WORD_RE = re.compile(r"\b[\w.-]+\b")

CONF_BACKTICK = 0.90
CONF_ALIAS = 0.85
CONF_IDENTIFIER = 0.75
CONF_MULTIWORD = 0.70
CONF_ACRONYM = 0.60
CONF_FLOOR = 0.3
MAX_NOUNS = 5


@dataclass(frozen=True)
class NounMention:
    label: str
    type: str | None
    conf: float
    mention_index: int


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


def _norm_key(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"[-_/]", " ", n)
    n = re.sub(r"\s+", " ", n)
    return n


def short_term_is_real(term: str, text: str) -> bool:
    """Ambiguous short terms need technical evidence (legacy graph_taxonomy)."""
    t = term.strip()
    low = t.lower()
    if low not in AMBIGUOUS_SHORT:
        return True
    if re.search(rf"\b{re.escape(t.upper())}\b", text):
        return True
    if low == "go" and re.search(
        r"\b(golang|go\s*\d|go\s*module|go\s*routine)", text, re.I
    ):
        return True
    for sent in re.split(r"[.!?\n]", text):
        if re.search(rf"\b{re.escape(low)}\b", sent, re.I):
            if TECH_CONTEXT.search(sent):
                return True
    return False


def _is_identifier_shape(surface: str) -> bool:
    return bool(IDENTIFIER_RE.fullmatch(surface.strip()))


def _is_bare_english_word(surface: str) -> bool:
    s = surface.strip()
    if not s or " " in s:
        return False
    if _is_identifier_shape(s):
        return False
    if s.isupper() and len(s) >= 2:
        return False
    return s.isalpha() and s.islower()


def _collapse_ws(surface: str) -> str:
    return re.sub(r"\s+", " ", surface.strip())


def _resolve_label(
    surface: str,
    existing_by_slug: dict[str, str],
) -> tuple[str, float, bool]:
    """Return (label, conf_bonus_type, is_alias_or_existing)."""
    key = _norm_key(surface)
    if key in CANONICAL_NAMES:
        return CANONICAL_NAMES[key], CONF_ALIAS, True
    slug = _slug(surface)
    if slug in existing_by_slug:
        return existing_by_slug[slug], CONF_ALIAS, True
    return _collapse_ws(surface), CONF_IDENTIFIER, False


def _denylisted(surface: str) -> bool:
    slug = _slug(surface)
    low = surface.strip().lower()
    return slug in SCHEMA_DENYLIST or low in SCHEMA_DENYLIST


def _is_verify_synthetic_fragment(surface: str) -> bool:
    """True when tokens are a contiguous subspan of a C5 verify synthetic name."""
    cand = _norm_key(surface).split()
    if not cand:
        return False
    cn = len(cand)
    for syn in _VERIFY_SYNTHETIC_TOKENS:
        sn = len(syn)
        if cn > sn:
            continue
        for i in range(sn - cn + 1):
            if syn[i : i + cn] == cand:
                return True
    return False


def _valid_multiword_words(words: list[str]) -> bool:
    if words[0].lower() in PHRASE_STOPWORDS:
        return False
    if all(w.lower() in PHRASE_STOPWORDS for w in words):
        return False
    if any(w.lower() in PHRASE_STOPWORDS for w in words):
        return False
    if words[-1].lower() in TRAILING_GLUE:
        return False
    if any(_is_identifier_shape(w) for w in words):
        return False
    return True


def _multiword_windows(text: str) -> list[tuple[str, int]]:
    """2–4 token lowercase name phrases (first-mint); not Title Case spans."""
    tokens = [(m.group(0), m.start()) for m in WORD_RE.finditer(text)]
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    n = len(tokens)
    for i in range(n):
        for length in range(2, 5):
            if i + length > n:
                break
            chunk = tokens[i : i + length]
            words = [c[0] for c in chunk]
            if not all(re.fullmatch(r"[a-z][a-z0-9-]*", w) for w in words):
                continue
            if not _valid_multiword_words(words):
                continue
            phrase = " ".join(words)
            key = phrase.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append((phrase, chunk[0][1]))
    return out


def _collect_candidates(text: str) -> list[tuple[str, int, str]]:
    """(surface, char_offset, kind) in mention order."""
    out: list[tuple[str, int, str]] = []
    seen_span: set[tuple[int, int]] = set()

    def add(surface: str, start: int, kind: str) -> None:
        surface = surface.strip(" \t*_#`-\u2014:;,.")
        if not surface:
            return
        end = start + len(surface)
        span = (start, end)
        if span in seen_span:
            return
        seen_span.add(span)
        out.append((surface, start, kind))

    for m in BACKTICK_RE.finditer(text):
        add(m.group(1), m.start(1), "backtick")

    for m in IDENTIFIER_RE.finditer(text):
        add(m.group(0), m.start(), "identifier")

    for m in ACRONYM_RE.finditer(text):
        add(m.group(0), m.start(), "acronym")

    for phrase, start in _multiword_windows(text):
        add(phrase, start, "multiword")

    for m in TITLE_MULTIWORD_RE.finditer(text):
        phrase = m.group(1)
        words = phrase.split()
        if not _valid_multiword_words(words):
            continue
        add(phrase, m.start(1), "multiword")

    out.sort(key=lambda x: x[1])
    return out


def _conf_for(kind: str, is_alias: bool) -> float:
    if is_alias:
        return CONF_ALIAS
    return {
        "backtick": CONF_BACKTICK,
        "identifier": CONF_IDENTIFIER,
        "multiword": CONF_MULTIWORD,
        "acronym": CONF_ACRONYM,
    }.get(kind, CONF_MULTIWORD)


def extract_nouns(
    text: str,
    *,
    existing_labels: Sequence[str] = (),
    synthetic_session: bool = False,
) -> list[NounMention]:
    if not text:
        return []

    existing_by_slug = {_slug(lbl): lbl for lbl in existing_labels if lbl}
    candidates = _collect_candidates(text)
    if _is_noise(text) and not any(
        kind in ("backtick", "identifier") for _, _, kind in candidates
    ):
        return []

    by_slug: dict[str, NounMention] = {}
    mention_index = 0

    for surface, _offset, kind in candidates:
        if len(surface) > 45:
            continue
        if len(surface.split()) > 4:
            continue
        if _denylisted(surface):
            continue

        words = surface.split()
        if len(words) == 1:
            if _is_bare_english_word(surface):
                continue
            low = surface.lower()
            if low in AMBIGUOUS_SHORT and not short_term_is_real(surface, text):
                continue
            if kind == "acronym" and not short_term_is_real(surface, text):
                continue

        label, _base_conf, is_alias = _resolve_label(surface, existing_by_slug)

        if synthetic_session and _is_verify_synthetic_fragment(surface):
            continue

        if not is_alias and kind == "multiword":
            label = _collapse_ws(surface)

        conf = _conf_for(kind, is_alias)
        if conf < CONF_FLOOR:
            continue

        slug = _slug(label)
        if not slug:
            continue

        if slug not in by_slug:
            by_slug[slug] = NounMention(
                label=label,
                type=None,
                conf=conf,
                mention_index=mention_index,
            )
            mention_index += 1
        else:
            prev = by_slug[slug]
            if conf > prev.conf:
                by_slug[slug] = NounMention(
                    label=prev.label,
                    type=prev.type,
                    conf=conf,
                    mention_index=prev.mention_index,
                )

    kept = sorted(by_slug.values(), key=lambda n: (-n.conf, n.mention_index))[:MAX_NOUNS]
    kept.sort(key=lambda n: n.mention_index)
    return kept
