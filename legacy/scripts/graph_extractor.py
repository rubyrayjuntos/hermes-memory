#!/usr/bin/env python3
"""
graph_extractor.py — Extract entities from conversations and populate the AGE knowledge graph.

This is a foundation script that can be:
1. Run manually: python3 graph_extractor.py
2. Run via cronjob: hermes cron add --schedule "*/30 * * * *" --script ~/.hermes/scripts/graph_extractor.py
3. Called from a Hermes hook for real-time extraction

Extraction strategy (v1 — pattern-based, no LLM cost):
- Capitalized multi-word phrases → potential Person/Project/Organization
- Known technology terms → Technology vertices
- Email addresses → Person vertices
- GitHub/LinkedIn handles → Person vertices
- URLs → domain extraction

Future (v2 — LLM-assisted):
- Use auxiliary LLM to extract structured entities from conversation text
- Map relationships between extracted entities
- Merge duplicates via entity resolution
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# graph_taxonomy.py lives in the same directory as this script
sys.path.insert(0, str(Path(__file__).parent))
import graph_taxonomy as gt  # noqa: E402

import psycopg
from psycopg.rows import dict_row

# ─── Configuration ───────────────────────────────────────────────────────

DB_DSN = os.environ.get(
    "HERMES_MEMORY_DSN",
    "host=localhost port=5432 dbname=hermes user=hermes password=CHANGE-ME-via-HERMES_MEMORY_DSN",
)
GRAPH_NAME = os.environ.get("HERMES_MEMORY_GRAPH", "hermes_knowledge")

# Known technology terms to recognize
KNOWN_TECHNOLOGIES = {
    "postgres", "postgresql", "pgvector", "apache age", "neo4j", "mongodb",
    "redis", "kubernetes", "docker", "azure", "aws", "gcp",
    "python", "typescript", "javascript", "rust", "go", "sql",
    "pytorch", "tensorflow", "transformers", "langchain", "llamaindex",
    "vllm", "llama.cpp", "ollama", "openai", "anthropic",
    "hyperbolic gnn", "mixture of experts", "topological data analysis",
    "rag", "graphrag", "agentic ai", "generative ai",
    "hermes agent", "obsidian", "git", "github",
}

# Known project names (extend as needed)
KNOWN_PROJECTS = {
    "kitchen kontrol", "tokyo eye", "canon forge", "neuronote",
    "ai/ml ops factory", "ai/ml engineer academy",
}

# Known organizations
KNOWN_ORGANIZATIONS = {
    "sodexo", "nous research", "bitnine", "apache software foundation",
    "openai", "anthropic", "google", "microsoft",
}

# ─── Entity Extractors ───────────────────────────────────────────────────

# Words that must not START a phrase. A capitalized-run regex happily eats
# sentence and heading openers ("The Fix", "Why It", "What Happened"), which is
# how 31% of Concept vertices became prose fragments rather than concepts.
PHRASE_STOPWORDS = {
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


def extract_capitalized_phrases(text: str) -> List[Tuple[str, str]]:
    """Extract capitalized multi-word phrases → (phrase, label).

    Guards added after 31% of Concept vertices turned out to be prose
    fragments ('Working\\n\\nThe', 'Why It', 'The Fix'):
      * never match across a newline — headings ran into body text
      * reject phrases opening with a stopword
      * reject phrases where every word is a stopword
      * strip markdown/punctuation noise and normalise whitespace
    A phrase that is not a known technology/project/organisation is only kept as
    a Concept if it survives all of these.
    """
    # [^\S\n] = whitespace except newline, so a phrase cannot span lines.
    pattern = r'\b([A-Z][a-z]+(?:[^\S\n]+[A-Z][a-z]+){1,3})\b'
    matches = re.findall(pattern, text)

    results = []
    seen = set()
    for m in matches:
        phrase = re.sub(r'\s+', ' ', m).strip(" \t*_#`-—:;,.")
        if not phrase or phrase in seen:
            continue
        words = phrase.split()
        if len(words) < 2:
            continue

        lower = phrase.lower()
        if lower in KNOWN_TECHNOLOGIES:
            results.append((phrase, "Technology"))
            seen.add(phrase)
            continue
        if lower in KNOWN_PROJECTS:
            results.append((phrase, "Project"))
            seen.add(phrase)
            continue
        if lower in KNOWN_ORGANIZATIONS:
            results.append((phrase, "Organization"))
            seen.add(phrase)
            continue

        # Unknown -> Concept, but only if it reads like a real noun phrase.
        if words[0].lower() in PHRASE_STOPWORDS:
            continue
        if all(w.lower() in PHRASE_STOPWORDS for w in words):
            continue
        if len(phrase) > 45:
            continue
        results.append((phrase, "Concept"))
        seen.add(phrase)
    return results


def extract_emails(text: str) -> List[str]:
    """Extract email addresses."""
    pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    return re.findall(pattern, text)


def extract_urls(text: str) -> List[str]:
    """Extract URLs and return their domains."""
    pattern = r'https?://([^/\s]+)'
    matches = re.findall(pattern, text)
    return matches


def extract_known_terms(text: str) -> List[Tuple[str, str]]:
    """Extract known technology/project/org terms from text."""
    text_lower = text.lower()
    results = []
    for term in KNOWN_TECHNOLOGIES:
        if term in text_lower:
            # Get proper case version
            proper = term.title() if len(term) > 3 else term.upper()
            results.append((proper, "Technology"))
    for proj in KNOWN_PROJECTS:
        if proj in text_lower:
            proper = proj.title()
            results.append((proper, "Project"))
    for org in KNOWN_ORGANIZATIONS:
        if org in text_lower:
            proper = org.title()
            results.append((proper, "Organization"))
    return results


def extract_github_handles(text: str) -> List[str]:
    """Extract GitHub usernames."""
    pattern = r'github\.com/([A-Za-z0-9_-]+)'
    return re.findall(pattern, text)


def extract_linkedin_handles(text: str) -> List[str]:
    """Extract LinkedIn profile slugs."""
    pattern = r'linkedin\.com/in/([A-Za-z0-9_-]+)'
    return re.findall(pattern, text)


# ─── Graph Operations ────────────────────────────────────────────────────

class GraphExtractor:
    """Extract entities from messages and populate the AGE knowledge graph."""

    def __init__(self, dsn: str, graph_name: str = "hermes_knowledge"):
        self.dsn = dsn
        self.graph_name = graph_name
        self.conn = None

    def connect(self):
        self.conn = psycopg.connect(self.dsn)
        self.conn.autocommit = False

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def ensure_label(self, label: str, is_vertex: bool = True):
        """Ensure a label exists in the graph."""
        kind = "v" if is_vertex else "e"
        try:
            with self.conn.cursor() as cur:
                if is_vertex:
                    cur.execute(f"SELECT create_vlabel('{self.graph_name}', '{label}')")
                else:
                    cur.execute(f"SELECT create_elabel('{self.graph_name}', '{label}')")
                self.conn.commit()
        except Exception as e:
            if "already exists" not in str(e):
                raise
            self.conn.rollback()

    def get_existing_names(self, label: str) -> Set[str]:
        """Get all existing vertex names for a label.

        AGE's properties accessor returns agtype (not jsonb), so we
        parse the full vertex JSON to read properties.
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(f"""
                    SELECT * FROM cypher('{self.graph_name}', $$
                        MATCH (v:{label})
                        RETURN v
                    $$) AS (v agtype)
                """)
                names = set()
                for row in cur.fetchall():
                    v_str = row[0]
                    # Result format: {"id": ..., "label": ..., "properties": {...}}::vertex
                    if v_str.endswith("::vertex"):
                        v_str = v_str[:-len("::vertex")]
                    try:
                        v_data = json.loads(v_str)
                        props = v_data.get("properties", {})
                        name = props.get("name")
                        if name:
                            names.add(name)
                    except json.JSONDecodeError:
                        continue
                return names
        except Exception as e:
            self.conn.rollback()
            return set()

    def create_bridge(self, chunk_id: str, source: str, vertex_id: int):
        """Create a bridge table entry linking a chunk to a vertex."""
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO memory_chunk_nodes (chunk_id, source, vertex_id)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (chunk_id, source, vertex_id) DO NOTHING
                """, (chunk_id, source, vertex_id))
                self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            print(f"Warning: Failed to create bridge {chunk_id} -> {vertex_id}: {e}")

    def _age_savepoint(self, cur, name: str):
        """Create a savepoint for an isolated AGE operation."""
        cur.execute(f"SAVEPOINT {name}")

    def _age_rollback_to_savepoint(self, cur, name: str):
        """Rollback to a savepoint, isolating the failure."""
        cur.execute(f"ROLLBACK TO SAVEPOINT {name}")

    def _props_to_age(self, properties: Dict) -> str:
        """Convert a Python dict to AGE property syntax: {key: 'value', ...}."""
        parts = []
        for k, v in properties.items():
            # Escape single quotes in values
            safe_v = str(v).replace("'", "\\'")
            parts.append(f"{k}: '{safe_v}'")
        return "{" + ", ".join(parts) + "}"

    def create_vertex(self, label: str, properties: Dict) -> Optional[int]:
        """MERGE a vertex by name. Returns its vertex ID (new or existing).

        Two bugs were fixed here:
        1. This used CREATE with only a Python-side name check. That check reads
           a set built from a separate query, so anything that slipped between
           the read and the write produced a duplicate — the graph accumulated
           12 'Postgres' and 10 'Pgvector' vertices. MERGE is idempotent in the
           database, which is the only place idempotency can actually hold.
        2. It returned None when the name already existed, so callers could not
           tell "already present" from "failed" and could not build edges to an
           existing vertex. It now always returns the id.

        MERGE matches on {name: ...} ONLY. AGE does NOT support `ON CREATE SET`
        (syntax error at "ON"), and a plain `SET` would rewrite properties on
        every re-run, so extra properties are deliberately not written here —
        the name is the identity and that is what must be idempotent.
        """
        name = properties.get("name", "")
        if not name:
            return None

        safe_name = str(name).replace("\\", "\\\\").replace("'", "\\'")
        sp_name = f"sp_v_{label.lower()}_{abs(hash(name)) % 10000}"

        try:
            with self.conn.cursor() as cur:
                self._age_savepoint(cur, sp_name)
                cur.execute(f"""
                    SELECT * FROM cypher('{self.graph_name}', $$
                        MERGE (v:{label} {{name: '{safe_name}'}})
                        RETURN v
                    $$) AS (v agtype)
                """)
                result = cur.fetchone()
                cur.execute(f"RELEASE SAVEPOINT {sp_name}")
                self.conn.commit()
                if result:
                    vertex_data = json.loads(result[0].split("::")[0])
                    return vertex_data.get("id")
        except Exception as e:
            try:
                self.conn.rollback()
            except Exception:
                pass
            print(f"Warning: Failed to merge vertex {label}/{name}: {e}")
            return None
        return None

    def create_edge(self, label: str, from_id: int, to_id: int, properties: Dict = None):
        """Create an edge between two vertices."""
        props_json = self._props_to_age(properties or {})
        sp_name = f"sp_e_{label.lower()}_{abs(hash(from_id + to_id)) % 10000}"
        try:
            with self.conn.cursor() as cur:
                self._age_savepoint(cur, sp_name)
                cur.execute(f"""
                    SELECT * FROM cypher('{self.graph_name}', $$
                        MATCH (a) WHERE id(a) = {from_id}
                        MATCH (b) WHERE id(b) = {to_id}
                        CREATE (a)-[e:{label} {props_json}]->(b)
                        RETURN e
                    $$) AS (e agtype)
                """)
                cur.execute(f"RELEASE SAVEPOINT {sp_name}")
                self.conn.commit()
        except Exception as e:
            try:
                self.conn.rollback()
            except Exception:
                pass
            print(f"Warning: Failed to create edge {label}: {e}")

    def find_vertex_by_name(self, name: str) -> Optional[int]:
        """Find a vertex ID by name across all labels using Cypher."""
        labels = ["Person", "Project", "Technology", "Organization", "Concept", "Domain", "Skill", "Tool"]
        escaped_name = name.replace("'", "\\'")
        for label in labels:
            try:
                with self.conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT * FROM cypher('{self.graph_name}', $$
                            MATCH (v:{label})
                            WHERE v.properties->>'name' = '{escaped_name}'
                            RETURN id(v) AS vid
                            LIMIT 1
                        $$) AS (vid agtype)
                    """)
                    row = cur.fetchone()
                    if row:
                        # Parse agtype: "12345" -> int
                        return int(row[0].strip('"'))
            except Exception as e:
                self.conn.rollback()
                continue
        return None

    def get_recent_conversations(self, since_hours: int = 24) -> List[Dict]:
        """Get recent conversation turns from the database.

        NOTE: `interval '%s hours'` does NOT work. The placeholder sits inside a
        string literal, so psycopg never substitutes it and Postgres parses the
        malformed literal as 1 hour — silently returning almost nothing instead
        of raising. Multiply a literal interval instead.
        """
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT id, session_id, role, content, ts
                FROM conversations
                WHERE ts > now() - (%s * interval '1 hour')
                  AND content IS NOT NULL
                  AND length(content) > 20
                ORDER BY ts DESC
                LIMIT 1000
            """, (since_hours,))
            return list(cur.fetchall())

    def process_message(self, message: Dict):
        """Extract entities from a single message and add to graph.

        Rewritten to stop three forms of corruption at the source:
          * every name resolves to ONE canonical label (Ray Swan was both a
            Person and a Concept, splitting his edges across two vertices)
          * ambiguous short terms need real evidence ('go' the verb produced a
            degree-500 phantom Technology)
          * co-mention is bounded and typed, not an all-pairs RELATED_TO clique
            (one message previously generated 591 meaningless edges)
        """
        content = message.get("content", "")
        if not content or len(content) < 20:
            return

        extracted = []
        for phrase, label in extract_capitalized_phrases(content):
            extracted.append((phrase, label))
        for term, label in extract_known_terms(content):
            extracted.append((term, label))
        for email in extract_emails(content):
            extracted.append((email, "Person"))
        for handle in extract_github_handles(content):
            extracted.append((handle, "Person"))

        # ── resolve to one canonical label per name ────────────────────────
        resolved: Dict[str, str] = {}
        for name, label in extracted:
            if not name or len(name.strip()) < 2:
                continue
            name = name.strip()

            # Ambiguous short terms must earn their place.
            if not gt.short_term_is_real(name, content):
                continue

            # Fold to canonical form BEFORE any lookup or write, so variants
            # like pgvector/Pgvector/PGVector resolve to one vertex.
            name = gt.normalize_name(name)

            existing = self._existing_label(name)
            final = gt.resolve_label(name, label, existing)
            # Keep the strongest label if this name appears twice in one message.
            if name in resolved and gt.rank(resolved[name]) >= gt.rank(final):
                continue
            resolved[name] = final

        created: Dict[str, Tuple[int, str]] = {}
        for name, label in resolved.items():
            vid = self.create_vertex(label, {"name": name})
            if vid:
                created[name] = (vid, label)
                self.create_bridge(str(message["id"]), "conversation", vid)

        if not created:
            return

        # ── typed, directed relations from explicit statements ────────────
        known_names = set(created)
        for a, rel, b in gt.extract_typed_relations(content, known_names):
            if a in created and b in created:
                self.ensure_label(rel, is_vertex=False)
                self.merge_edge(rel, created[a][0], created[b][0],
                                {"source": "conversation",
                                 "message_id": str(message.get("id"))})

        # ── bounded co-mention, clearly marked as such ────────────────────
        pairs = gt.co_mention_pairs([(n, l) for n, (_, l) in created.items()])
        if pairs:
            self.ensure_label("CO_MENTIONED", is_vertex=False)
        for n1, n2 in pairs:
            if n1 in created and n2 in created:
                self.merge_edge("CO_MENTIONED", created[n1][0], created[n2][0],
                                {"source": "conversation"})

    def _existing_label(self, name: str) -> Optional[str]:
        """The strongest label this name already holds in the graph, if any.

        Consulted before every write so a name can never acquire a second label
        (the Ray Swan = Person + Concept split).
        """
        safe = str(name).replace("\\", "\\\\").replace("'", "\\'")
        best = None
        try:
            with self.conn.cursor() as cur:
                self._age_savepoint(cur, "sp_lbl")
                cur.execute(f"""
                    SELECT * FROM cypher('{self.graph_name}', $$
                        MATCH (v {{name: '{safe}'}})
                        RETURN label(v)
                    $$) AS (l agtype)
                """)
                for (raw,) in cur.fetchall():
                    lbl = str(raw).strip('"')
                    if best is None or gt.rank(lbl) > gt.rank(best):
                        best = lbl
                cur.execute("RELEASE SAVEPOINT sp_lbl")
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
            return None
        return best

    def merge_edge(self, label: str, from_id: int, to_id: int,
                   properties: Dict = None) -> bool:
        """MERGE an edge, incrementing `weight` instead of duplicating it.

        create_edge used CREATE, so reprocessing the same message re-created the
        same relationship: 'Tokyo Eye -> GO' existed 45 times and the graph held
        1,825 redundant copies. Weight now records repetition, which is the
        useful signal, without multiplying edges.
        """
        try:
            with self.conn.cursor() as cur:
                sp = f"sp_me_{abs(hash((label, from_id, to_id))) % 100000}"
                self._age_savepoint(cur, sp)
                cur.execute(f"""
                    SELECT * FROM cypher('{self.graph_name}', $$
                        MATCH (a) WHERE id(a) = {from_id}
                        MATCH (b) WHERE id(b) = {to_id}
                        MERGE (a)-[e:{label}]->(b)
                        RETURN e
                    $$) AS (e agtype)
                """)
                cur.fetchall()
                # Bump weight in a second statement: AGE rejects ON CREATE SET
                # and coalesce() inside MERGE, so read-modify-write it here.
                cur.execute(f"""
                    SELECT * FROM cypher('{self.graph_name}', $$
                        MATCH (a)-[e:{label}]->(b)
                        WHERE id(a) = {from_id} AND id(b) = {to_id}
                        SET e.weight = coalesce(e.weight, 0) + 1
                        RETURN e
                    $$) AS (e agtype)
                """)
                cur.fetchall()
                cur.execute(f"RELEASE SAVEPOINT {sp}")
                self.conn.commit()
                return True
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
            return False

    def run(self, since_hours: int = 24, dry_run: bool = False):
        """Run the extraction pipeline."""
        self.connect()
        try:
            # Ensure labels exist
            for label in ["Person", "Project", "Technology", "Organization", "Concept", "Domain", "Skill", "Tool"]:
                self.ensure_label(label, is_vertex=True)
            for label in ["RELATED_TO", "USES", "BUILT_WITH", "MENTIONS"]:
                self.ensure_label(label, is_vertex=False)

            # Get recent conversations
            conversations = self.get_recent_conversations(since_hours)
            print(f"Found {len(conversations)} conversations to process")

            if dry_run:
                for conv in conversations[:5]:
                    content = conv["content"][:100]
                    print(f"  [{conv['role']}] {content}...")
                return

            # Process conversations.
            # Count across EVERY label, not just Technology — the old counter
            # only sampled Technology, so new Concept/Project/Person/Skill/Tool/
            # Domain/Organization vertices were created but reported as 0.
            all_labels = ["Person", "Project", "Technology", "Organization",
                          "Concept", "Domain", "Skill", "Tool"]

            def snapshot():
                return {l: len(self.get_existing_names(l)) for l in all_labels}

            before = snapshot()
            for conv in conversations:
                self.process_message(conv)
            after = snapshot()

            per_label = {l: after[l] - before[l]
                         for l in all_labels if after[l] > before[l]}
            extracted_count = sum(per_label.values())
            if per_label:
                detail = ", ".join(f"{l} +{n}" for l, n in
                                   sorted(per_label.items(), key=lambda x: -x[1]))
                print(f"New entities by label: {detail}")

            print(f"Extracted {extracted_count} new entities from {len(conversations)} conversations")

        finally:
            self.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Extract entities from conversations into AGE graph")
    parser.add_argument("--hours", type=int, default=24, help="Process messages from last N hours")
    parser.add_argument("--dry-run", action="store_true", help="List messages without extracting")
    parser.add_argument("--dsn", default=DB_DSN, help="Postgres DSN")
    args = parser.parse_args()

    extractor = GraphExtractor(args.dsn, GRAPH_NAME)
    extractor.run(since_hours=args.hours, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
