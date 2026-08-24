"""
hermes_memory.graph — AGE knowledge graph operations.

Wraps the cypher() calls, vertex creation, edge creation, and bridge table
management used by the hybrid-age memory provider.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("hybrid_age.graph")


class GraphClient:
    """Minimal AGE graph client: vertices, edges, bridge table."""

    def __init__(self, dsn: str, graph_name: str = "hermes_knowledge"):
        self.dsn = dsn
        self.graph_name = graph_name
        self.conn: psycopg.Connection | None = None

    def connect(self) -> None:
        self.conn = psycopg.connect(self.dsn)
        self.conn.autocommit = False

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None

    def ensure_label(self, label: str, is_vertex: bool = True) -> None:
        kind = "vlabel" if is_vertex else "elabel"
        func = "create_vlabel" if is_vertex else "create_elabel"
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    f"SELECT {func}('{self.graph_name}', '{label}')"
                )
                self.conn.commit()
        except Exception as e:
            if "already exists" not in str(e):
                raise
            self.conn.rollback()

    def create_vertex(self, label: str, properties: Dict) -> Optional[int]:
        name = properties.get("name", "")
        if not name:
            return None
        safe = str(name).replace("\\", "\\\\").replace("'", "\\'")
        sp = f"sp_v_{label.lower()}_{abs(hash(name)) % 10000}"
        try:
            with self.conn.cursor() as cur:
                cur.execute(f"SAVEPOINT {sp}")
                cur.execute(f"""
                    SELECT * FROM cypher('{self.graph_name}', $$
                        MERGE (v:{label} {{name: '{safe}'}})
                        RETURN v
                    $$) AS (v agtype)
                """)
                result = cur.fetchone()
                cur.execute(f"RELEASE SAVEPOINT {sp}")
                self.conn.commit()
                if result:
                    v_str = result[0].split("::")[0]
                    v_data = json.loads(v_str)
                    return v_data.get("id")
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
            logger.warning("Failed to merge vertex %s/%s", label, name)
            return None
        return None

    def create_edge(self, label: str, from_id: int, to_id: int,
                    properties: Dict = None) -> bool:
        props_json = _props_to_age(properties or {})
        sp = f"sp_e_{label.lower()}_{abs(hash((label, from_id, to_id))) % 100000}"
        try:
            with self.conn.cursor() as cur:
                cur.execute(f"SAVEPOINT {sp}")
                cur.execute(f"""
                    SELECT * FROM cypher('{self.graph_name}', $$
                        MATCH (a) WHERE id(a) = {from_id}
                        MATCH (b) WHERE id(b) = {to_id}
                        CREATE (a)-[e:{label} {props_json}]->(b)
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
            logger.warning("Failed to create edge %s", label)
            return False

    def merge_edge(self, label: str, from_id: int, to_id: int,
                   properties: Dict = None) -> bool:
        props_json = _props_to_age(properties or {})
        sp = f"sp_me_{abs(hash((label, from_id, to_id))) % 100000}"
        try:
            with self.conn.cursor() as cur:
                cur.execute(f"SAVEPOINT {sp}")
                cur.execute(f"""
                    SELECT * FROM cypher('{self.graph_name}', $$
                        MATCH (a) WHERE id(a) = {from_id}
                        MATCH (b) WHERE id(b) = {to_id}
                        MERGE (a)-[e:{label}]->(b)
                        RETURN e
                    $$) AS (e agtype)
                """)
                cur.fetchall()
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

    def get_existing_names(self, label: str) -> Set[str]:
        names: Set[str] = set()
        try:
            with self.conn.cursor() as cur:
                cur.execute(f"""
                    SELECT * FROM cypher('{self.graph_name}', $$
                        MATCH (v:{label})
                        RETURN v
                    $$) AS (v agtype)
                """)
                for row in cur.fetchall():
                    v_str = row[0]
                    if v_str.endswith("::vertex"):
                        v_str = v_str[:-len("::vertex")]
                    try:
                        v_data = json.loads(v_str)
                        props = v_data.get("properties") or {}
                        name = props.get("name")
                        if name:
                            names.add(name)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            self.conn.rollback()
        return names

    def create_bridge(self, chunk_id: str, source: str, vertex_id: int) -> None:
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


def _props_to_age(properties: Dict) -> str:
    parts = []
    for k, v in properties.items():
        safe_v = str(v).replace("'", "\\'")
        parts.append(f"{k}: '{safe_v}'")
    return "{" + ", ".join(parts) + "}"
