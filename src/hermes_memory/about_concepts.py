"""Legacy Turn->ABOUT->Concept linker (backfill / verify synthetics).

Live conversation writes use extract_nouns + the Session/Turn flower.
This module keeps the old cosine ABOUT path off the provider hot path.
"""
from __future__ import annotations

import datetime
import logging
import re
from typing import TYPE_CHECKING

from .session_kind import classify_session_kind
from .store_vec import _cosine_similarity

if TYPE_CHECKING:
    from .embed import Embedder
    from .store import Store

logger = logging.getLogger("hybrid_age")

# -- ABOUT linker helpers (Turn -> ABOUT -> Concept, real cosine) -----------
# Copied inline from ~/.hermes/scripts/graph_extractor.py — do not import that file.
# Pattern: multi-word capitalized phrases, whitespace except newline.
_ABOUT_PATTERN = r'\b([A-Z][a-z]+(?:[^\S\n]+[A-Z][a-z]+){1,3})\b'
_PHRASE_STOPWORDS = {
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
}


def _extract_concepts(text: str, max_concepts: int = 3) -> list[str]:
    """Extract 1-3 Concept names: 2-4 word Title Case only. No single-word fallback."""
    matches = re.findall(_ABOUT_PATTERN, text or "")
    results: list[str] = []
    seen: set[str] = set()
    for m in matches:
        phrase = re.sub(r"\s+", " ", m).strip(" \t*_#`-\u2014:;,.")
        if not phrase or phrase in seen:
            continue
        words = phrase.split()
        if len(words) < 2:
            continue
        if words[0].lower() in _PHRASE_STOPWORDS:
            continue
        if all(w.lower() in _PHRASE_STOPWORDS for w in words):
            continue
        if len(phrase) > 45:
            continue
        results.append(phrase)
        seen.add(phrase)
        if len(results) >= max_concepts:
            break
    if not results:
        return []
    return results[:max_concepts]


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s


SNAP_COSINE = 0.85
ONE_WORD_KEEP = frozenset({"zephyr", "atlas"})


def is_one_word_concept(name: str, keep: frozenset[str] = ONE_WORD_KEEP) -> bool:
    n = (name or "").strip()
    if not n or n.lower() in keep:
        return False
    return len(n.split()) == 1


C5_CONCEPT_NAMES = frozenset({"project zephyr", "atlas vault engine"})


def should_purge_concept(name: str) -> bool:
    n = (name or "").strip()
    if not n:
        return False
    if n.lower() in C5_CONCEPT_NAMES:
        return True
    return is_one_word_concept(n)


class AboutConceptLinker:
    async def _about_existing_concepts(
        self,
        store: Store,
        embedder: Embedder,
        turn_vec: list[float] | None,
        scored: list[tuple[str, float]],
        max_extra: int = 4,
        min_cos: float = SNAP_COSINE,
    ) -> list[tuple[str, float]]:
        """Hook this turn to Concepts already in the graph (cross-session hubs)."""
        if not turn_vec:
            return scored
        names = self._concept_names
        if names is None:
            try:
                names = await store.fetch_concept_names()
            except Exception:
                logger.debug("fetch_concept_names failed", exc_info=True)
                return scored
            self._concept_names = names
        slug_map = {_slug(n): n for n in names if n}
        resolved: list[tuple[str, float]] = []
        seen_slug: set[str] = set()
        for name, cos in scored:
            canon = slug_map.get(_slug(name), name)
            key = _slug(canon)
            if not key or key in seen_slug:
                continue
            if is_one_word_concept(canon):
                continue
            seen_slug.add(key)
            resolved.append((canon, cos))
        scored = resolved
        already = {n for n, _ in scored}
        extra: list[tuple[str, float]] = []
        skip_hubs = {"Project Zephyr", "Atlas Vault Engine"}
        for name in names[:40]:
            name = (name or "").strip()
            if not name or name in already or name in skip_hubs:
                continue
            if is_one_word_concept(name):
                continue
            vec = self._concept_emb.get(name)
            if vec is None:
                try:
                    vec = await embedder.embed_text(name)
                except Exception:
                    vec = None
                if vec:
                    self._concept_emb[name] = vec
            cos = _cosine_similarity(turn_vec, vec)
            if cos is None or cos < min_cos:
                continue
            extra.append((name, float(cos)))
            already.add(name)
            if len(extra) >= max_extra:
                break
        return scored + extra

    async def _link_turn_concepts(
        self, store: Store, embedder: Embedder, conv_id: int,
        session_id: str, content: str, turn_vec: list[float] | None,
    ) -> None:
        """Create Turn->ABOUT->Concept edges with real cosine + bridge."""
        concepts = _extract_concepts(content)
        scored: list[tuple[str, float]] = []
        for concept in concepts:
            try:
                cvec = await embedder.embed_text(concept)
            except Exception:
                cvec = None
            cos = _cosine_similarity(turn_vec, cvec)
            if cos is None:
                continue
            cos = max(-1.0, min(1.0, float(cos)))
            if cos < SNAP_COSINE:
                continue
            scored.append((concept, cos))
        scored = await self._about_existing_concepts(
            store, embedder, turn_vec, scored, min_cos=SNAP_COSINE,
        )
        # Always MERGE the Turn vertex + bridge, even with zero ABOUT edges.
        # Title-case extraction missing used to skip the vertex entirely, so
        # live chats never appeared on the graph (only C5 verify synthetics).
        try:
            await store.ensure_about_labels()
        except Exception:
            logger.debug("ensure_about_labels failed", exc_info=True)
        turn_content = (content or "")[:200]
        turn_props = {"name": f"turn_{conv_id}", "session_id": session_id, "turn_id": int(conv_id), "content": turn_content, "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        try:
            vids_turn = await store.merge_vertices_batched([("Turn", turn_props)])
            turn_vid_str = vids_turn[0] if vids_turn else None
            if not turn_vid_str:
                return
            turn_vid = int(turn_vid_str)
        except Exception:
            logger.debug("Turn vertex merge failed", exc_info=True)
            return
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        edges: list[tuple[str, int, int]] = []
        edge_props: dict[tuple[str, int, int], dict] = {}
        # Session chain: Turn -IN_SESSION-> Session, prev -NEXT-> Turn
        if session_id:
            try:
                sess_vids = await store.merge_vertices_batched(
                    [("Session", {
                        "name": session_id,
                        "kind": classify_session_kind(session_id),
                        "created_at": now_iso,
                    })]
                )
                sess_vid = int(sess_vids[0]) if sess_vids and sess_vids[0] else None
                if sess_vid:
                    edges.append(("IN_SESSION", turn_vid, sess_vid))
                    edge_props[("IN_SESSION", turn_vid, sess_vid)] = {
                        "weight": 1.0, "cosine": 1.0, "created_at": now_iso,
                    }
            except Exception:
                logger.debug("Session vertex merge failed", exc_info=True)
            try:
                prev_id = await store.previous_conversation_id(session_id, conv_id)
                if prev_id:
                    prev_vids = await store.merge_vertices_batched(
                        [("Turn", {"name": f"turn_{prev_id}"})]
                    )
                    prev_vid = int(prev_vids[0]) if prev_vids and prev_vids[0] else None
                    if prev_vid:
                        edges.append(("NEXT", prev_vid, turn_vid))
                        edge_props[("NEXT", prev_vid, turn_vid)] = {
                            "weight": 1.0, "cosine": 1.0, "created_at": now_iso,
                        }
            except Exception:
                logger.debug("NEXT edge failed", exc_info=True)
        if scored:
            concept_items = [("Concept", {"name": c, "created_at": now_iso}) for c, _ in scored]
            try:
                concept_vids = await store.merge_vertices_batched(concept_items)
            except Exception:
                logger.debug("Concept vertex merge failed", exc_info=True)
                concept_vids = []
            for (concept, cos), cvid_str in zip(scored, concept_vids or []):
                if not cvid_str:
                    continue
                try:
                    cvid = int(cvid_str)
                except ValueError:
                    continue
                edges.append(("ABOUT", turn_vid, cvid))
                edge_props[("ABOUT", turn_vid, cvid)] = {
                    "weight": 1.0, "cosine": float(cos), "created_at": now_iso,
                }
        if edges:
            try:
                await store.merge_edges_batched(edges, edge_props=edge_props)
            except Exception:
                logger.debug("turn edge merge failed", exc_info=True)
        try:
            await store.bridge_turn(conv_id, turn_vid)
        except Exception:
            logger.debug("bridge_turn failed", exc_info=True)
