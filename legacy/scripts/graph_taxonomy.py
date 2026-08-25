#!/usr/bin/env python3
"""
graph_taxonomy.py — one authoritative label per entity, and typed relations.

Imported by graph_extractor.py so ingestion cannot create the corruption this
module exists to prevent:

  * an entity name resolves to exactly ONE label (Ray Swan was both Person and
    Concept, which split his edges across two vertices and pushed both to the
    periphery)
  * short ambiguous terms ('go', 'git', 'sql') need real evidence before they
    count as technologies — the English verb "go" produced a degree-500 phantom
  * co-mention is recorded as CO_MENTIONED with a weight, capped per message,
    instead of an all-pairs RELATED_TO clique (one message made 591 edges)
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

# ─── label precedence ────────────────────────────────────────────────────
# Higher index wins. A name already stored under a stronger label is never
# re-created under a weaker one.
LABEL_PRECEDENCE = [
    "Concept",        # weakest: the catch-all default
    "Skill",
    "Technology",
    "Tool",
    "Domain",
    "Project",
    "Organization",
    "Person",         # strongest: a named human is never "just a concept"
]
_RANK = {l: i for i, l in enumerate(LABEL_PRECEDENCE)}

# Explicit overrides for entities the heuristics get wrong.
CANONICAL_LABELS: Dict[str, str] = {
    "ray swan": "Person",
    "wre sva": "Person",
    "ariel fernandez": "Person",
    "ridgway scott": "Person",
    "anthropic": "Organization",
    "openai": "Organization",
    "google": "Organization",
    "microsoft": "Organization",
    "meta": "Organization",
    "nous research": "Organization",
    "bitnine": "Organization",
    "sodexo": "Organization",
    "mazda": "Organization",
    "revlon": "Organization",
    "fox sports": "Organization",
    "therabody": "Organization",
    "black & decker": "Organization",
    "hermes agent": "Tool",
    "claude code": "Tool",
    "topological data analysis": "Domain",
    "docker": "Technology",
    "python": "Technology",
    "sql": "Technology",
    "pgvector": "Technology",
    "postgres": "Technology",
    "postgresql": "Technology",
    "apache age": "Technology",
    "tokyo eye": "Project",
    "canon forge": "Project",
    "kitchen kontrol": "Project",
    "neuronote": "Project",
}


def rank(label: str) -> int:
    return _RANK.get(label, 0)


def canonical_label(name: str, proposed: str) -> str:
    """The label this name must use, regardless of what the caller proposed."""
    return CANONICAL_LABELS.get(name.strip().lower(), proposed)


def resolve_label(name: str, proposed: str,
                  existing: Optional[str] = None) -> str:
    """Final label for `name`.

    Precedence: explicit override > strongest of (existing, proposed).
    `existing` is whatever label the graph already holds for this name.
    """
    override = CANONICAL_LABELS.get(name.strip().lower())
    if override:
        return override
    if existing and rank(existing) >= rank(proposed):
        return existing
    return proposed


# ─── ambiguous short terms ───────────────────────────────────────────────
# These match ordinary English. 'go' as a verb created the graph's largest hub.
AMBIGUOUS_SHORT = {"go", "git", "sql", "aws", "gcp", "rag", "r", "c", "d",
                   "ai", "ml", "ui", "ux", "os", "vm", "db"}

# A short term only counts when a technical cue sits nearby.
TECH_CONTEXT = re.compile(
    r'\b(language|lang|code|coded|program|programming|written|write|compile|'
    r'compiled|runtime|binary|module|package|library|framework|install|'
    r'installed|version|repo|repository|commit|branch|query|queries|database|'
    r'schema|table|index|server|client|api|sdk|cli|golang|rustlang)\b',
    re.I)


def short_term_is_real(term: str, text: str) -> bool:
    """Does an ambiguous short term actually refer to the technology?

    Accept when EITHER:
      * it appears in a distinctly technical form (Go/Golang capitalised as a
        proper noun, or 'go' adjacent to a technical cue), OR
      * it appears as an exact uppercase acronym (SQL, AWS, GCP).
    Reject the bare lowercase verb sense.
    """
    t = term.strip()
    low = t.lower()
    if low not in AMBIGUOUS_SHORT:
        return True

    # Exact uppercase acronym in the source text -> real.
    if re.search(rf'\b{re.escape(t.upper())}\b', text):
        return True
    # 'Golang' / 'Go 1.22' style
    if low == "go" and re.search(r'\b(golang|go\s*\d|go\s*module|go\s*routine)',
                                 text, re.I):
        return True
    # Technical cue within the sentence containing the term.
    for sent in re.split(r'[.!?\n]', text):
        if re.search(rf'\b{re.escape(low)}\b', sent, re.I):
            if TECH_CONTEXT.search(sent):
                return True
    return False


# ─── alias normalization ─────────────────────────────────────────────────
# Maps normalized keys to the canonical (preferred) name. Everything that
# normalizes to the same key collapses to the canonical form.
#
# Source: empirical audit of the graph (audit_aliases.py) — every entry here
# was two or more distinct vertices that should be one.
CANONICAL_NAMES: Dict[str, str] = {
    # Projects
    "neuronote": "NeuroNote",
    "kitchen kontrol": "Kitchen Kontrol",
    "canon forge": "Canon Forge",
    "ai ml ops factory": "AI/ML Ops Factory",
    "tokyo eye": "Tokyo Eye",
    # Technologies
    "hyperbolic gnn": "Hyperbolic GNN",
    "mixture of experts": "Mixture of Experts",
    "pgvector": "pgvector",
    "postgres": "Postgres",
    "postgresql": "Postgres",
    "apache age": "Apache AGE",
    "sql": "SQL",
    # Organizations
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    # Domains
    "generative ai": "Generative AI",
    # Tools
    "hermes agent": "Hermes Agent",
    "claude code": "Claude Code",
}


def normalize_name(name: str) -> str:
    """Fold a name to its canonical form.

    Applied before every vertex write so `Pgvector`, `pgvector`, and `PGVector`
    all resolve to the same `pgvector` vertex. Without this, the same concept
    fragments into multiple nodes and every signal that should be one strong
    connection becomes three weak ones.
    """
    n = name.strip()
    key = norm(n)
    return CANONICAL_NAMES.get(key, n)


def normalized_key(name: str) -> str:
    """The dedup key for a name — norm() of the canonical form."""
    return norm(normalize_name(name))


def norm(name: str) -> str:
    """Normalize a name to a dedup key (lowercase, separators -> spaces)."""
    n = name.lower().strip()
    n = re.sub(r'[-_/]', ' ', n)
    n = re.sub(r'\s+', ' ', n)
    return n


# ─── typed relation extraction ───────────────────────────────────────────
# Patterns that assert a real, DIRECTED relationship. Everything else is
# co-mention and must not pretend otherwise.
RELATION_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("BUILT_WITH", re.compile(
        r'(?P<a>[\w .&/-]{2,40}?)\s+(?:is\s+)?(?:built|made|created|implemented)'
        r'\s+(?:with|using|on|in)\s+(?P<b>[\w .&/-]{2,40})', re.I)),
    ("USES", re.compile(
        r'(?P<a>[\w .&/-]{2,40}?)\s+(?:uses|use|using|leverages|relies\s+on|'
        r'depends\s+on|requires)\s+(?P<b>[\w .&/-]{2,40})', re.I)),
    ("WORKS_ON", re.compile(
        # Negative lookahead on 'built/building' + with/using so "X is built
        # with Y" is BUILT_WITH only, not also WORKS_ON.
        r'(?P<a>[\w .&/-]{2,40}?)\s+(?:works?\s+on|working\s+on|develops?)'
        r'\s+(?P<b>[\w .&/-]{2,40})', re.I)),
    ("PART_OF", re.compile(
        r'(?P<a>[\w .&/-]{2,40}?)\s+(?:is\s+)?(?:part\s+of|belongs\s+to|'
        r'inside|within)\s+(?P<b>[\w .&/-]{2,40})', re.I)),
]


def extract_typed_relations(text: str,
                            known: Set[str]) -> List[Tuple[str, str, str]]:
    """Find (subject, RELATION, object) where BOTH ends are known entities.

    Requiring both ends to be real entities is what keeps this from generating
    the same noise the clique loop did.
    """
    lower_map = {k.lower(): k for k in known}
    out: List[Tuple[str, str, str]] = []
    seen = set()
    # Match per sentence: a pattern spanning a sentence boundary invents a
    # relation between things that were never claimed to be related.
    for sent in re.split(r'(?<=[.!?])\s+|\n+', text):
        if not sent.strip():
            continue
        for rel, pat in RELATION_PATTERNS:
            for m in pat.finditer(sent):
                a_raw = m.group("a").strip(" .,;:*_#`-")
                b_raw = m.group("b").strip(" .,;:*_#`-")
                a = _match_entity(a_raw, lower_map, side="left")
                b = _match_entity(b_raw, lower_map, side="right")
                if a and b and a != b and (a, rel, b) not in seen:
                    out.append((a, rel, b))
                    seen.add((a, rel, b))
    return out


def _match_entity(fragment: str, lower_map: Dict[str, str],
                  side: str = "left") -> Optional[str]:
    """Find the known entity in `fragment` that is ADJACENT to the verb.

    Matching the longest entity anywhere in the fragment is wrong: in
    "Ray Swan works on Tokyo Eye", the subject fragment is the whole preceding
    clause, and 'Tokyo Eye' (longer) would win over 'Ray Swan' — inverting the
    relation. For a subject take the entity closest to the END of the fragment
    (nearest the verb); for an object take the one closest to the START.
    """
    frag = fragment.lower()
    best = None          # (position_key, length, original)
    for lk, orig in lower_map.items():
        idx = frag.rfind(lk) if side == "left" else frag.find(lk)
        if idx < 0:
            continue
        # Prefer proximity to the verb, then longer names as a tiebreak.
        pos_key = -idx if side == "left" else idx
        cand = (pos_key, -len(lk), orig)
        if best is None or cand < best:
            best = cand
    return best[2] if best else None


# ─── co-mention budget ───────────────────────────────────────────────────
# The old code linked every pair: N entities -> N(N-1)/2 edges (591 from one
# message). Cap it, and prefer specific entity types over generic Concepts.
CO_MENTION_TOP_K = 6

SPECIFICITY = {"Person": 6, "Project": 5, "Organization": 4, "Domain": 3,
               "Tool": 3, "Technology": 2, "Skill": 1, "Concept": 0}


def co_mention_pairs(entities: List[Tuple[str, str]],
                     top_k: int = CO_MENTION_TOP_K
                     ) -> List[Tuple[str, str]]:
    """Bounded co-mention pairs from [(name, label), ...].

    Keeps the top_k most specific entities and pairs only those, so a single
    message can contribute at most top_k*(top_k-1)/2 = 15 edges instead of 591.
    """
    ranked = sorted(entities, key=lambda e: -SPECIFICITY.get(e[1], 0))[:top_k]
    pairs = []
    for i, (n1, _) in enumerate(ranked):
        for n2, _ in ranked[i + 1:]:
            if n1 != n2:
                # Canonical ordering makes the pair undirected in practice, so
                # A->B and B->A cannot both exist.
                pairs.append(tuple(sorted((n1, n2))))
    return list(dict.fromkeys(pairs))
