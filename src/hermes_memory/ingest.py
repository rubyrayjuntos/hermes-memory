"""hermes_memory.ingest — documentation/code indexing pipeline (card C4).

Ports the core of the production doc_indexer (~/.hermes/scripts/doc_indexer.py)
onto the C3 package layers:

    walk -> SHA-256 hash -> state diff (skip unchanged / flag drifted /
    detect deleted) -> chunk (no character loss) -> embed (768-dim invariant)
    -> doc_chunks + memory_entries rows -> memory_chunk_nodes bridges
    -> AGE MERGE (:File {path}), (:Module {name}), (:Dependency {name}),
       (:Imports) edges.

Store rules honored (plan §3.3): SAVEPOINT-wrapped Cypher, MERGE on minimal
unique keys ({path} / {name}), batched MERGEs (>=50 statements per txn),
vertex ids stringified at this boundary.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import asyncpg

from .config import HybridAgeConfig, load_config
from .embed import Embedder, vec_to_literal
from .store import validate_graph_name

logger = logging.getLogger("hybrid_age.ingest")

AGENT_IDENTITY = "librarian"
MAX_CHUNK_CHARS = 4000          # well under the ~6-7k Ollama rejection point
MAX_FILE_BYTES = 2_000_000

# .gitignore-lite
IGNORED_DIRS = {
    ".git", "node_modules", "__pycache__", "dist", "build", ".next",
    "venv", ".venv", "site-packages", ".mypy_cache", ".pytest_cache",
    "target", "vendor", ".tox", "htmlcov", ".ruff_cache", ".cache",
}
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".tar", ".gz",
    ".whl", ".so", ".dylib", ".dll", ".exe", ".class", ".jar", ".pyc",
    ".bin", ".db", ".sqlite", ".parquet", ".pkl", ".woff", ".woff2",
}

EXTENSIONS = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScriptReact",
    ".js": "JavaScript",
    ".jsx": "JavaScriptReact",
    ".md": "Documentation",
    ".sql": "SQL",
    ".yaml": "Config",
    ".yml": "Config",
    ".json": "Config",
    ".txt": "Text",
    ".toml": "Config",
}


# ---------------------------------------------------------------------------
# Pure helpers (property-test surface P5/P6/P7)
# ---------------------------------------------------------------------------

def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


_HEXISH = re.compile(r"^[0-9a-f]{8,}$", re.I)
_ALLNUM = re.compile(r"^\d{6,}$")
_UUIDISH = re.compile(
    r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$", re.I
)

MODULE_SKIP_DIRS = {
    "src", "lib", "app", ".", "..", "__pycache__", "node_modules",
    "dist", "build", "target", ".git", ".venv", "venv", "site-packages",
    "cache", ".cache", "tmp", ".tmp", "artifacts", "mlruns",
    "mlflow-artifacts", "checkpoints", "logs", "output", "outputs",
    "tests", "test", "fixtures",
}


def _is_module_name(name: str) -> bool:
    """False for storage artifacts masquerading as module names."""
    if not name:
        return False
    if name.lower() in MODULE_SKIP_DIRS:
        return False
    if _UUIDISH.match(name) or _HEXISH.match(name) or _ALLNUM.match(name):
        return False
    return True


def module_for(rel_path: str) -> Optional[str]:
    """Derive a logical Module name from a path: src/auth/service.ts -> auth."""
    parts = Path(rel_path).parts
    for seg in reversed(parts[:-1]):
        if _is_module_name(seg):
            return seg
    return None


PY_IMPORT = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.M)
TS_IMPORT = re.compile(r"""^\s*(?:import|export)[^'"]*from\s+['"]([^'"]+)['"]""", re.M)
TS_REQUIRE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+\.(?:py|ts|tsx|js|jsx|md|sql))\)")


def extract_dependencies(content: str, lang: str) -> List[str]:
    """Normalized, sorted, deduplicated dependency targets for [:IMPORTS]."""
    deps: Set[str] = set()
    if lang == "Python":
        for m in PY_IMPORT.finditer(content):
            target = m.group(1) or m.group(2)
            if target:
                deps.add(target.split(".")[0] if not target.startswith(".") else target)
    elif lang.startswith(("TypeScript", "JavaScript")):
        for m in TS_IMPORT.finditer(content):
            deps.add(m.group(1))
        for m in TS_REQUIRE.finditer(content):
            deps.add(m.group(1))
    elif lang == "Documentation":
        for m in MD_LINK.finditer(content):
            deps.add(m.group(1))
    return sorted(d.strip() for d in deps if d and len(d.strip()) > 1)


def dep_kind(name: str) -> str:
    if name.startswith((".", "/", "@/")):
        return "internal"
    return "external"


def load_state(path: Path) -> Dict[str, str]:
    """Load {rel_path: sha256}; corrupt JSON yields {} (never raises)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def save_state(path: Path, state: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> List[str]:
    """Split text into chunks <= max_chars with NO character loss.

    Prefers paragraph boundaries (\\n\\n), then line boundaries; oversized
    single paragraphs are hard-split. ''.join(chunks) == text always.
    """
    if len(text) <= max_chars:
        return [text]

    # Split into atomic pieces (paragraphs, then lines, then hard splits) while
    # keeping every character; separators stay attached to the preceding piece.
    def split_oversized(piece: str, sep: str) -> List[str]:
        out: List[str] = []
        buf = ""
        for part in re.split(rf"(?<={sep})", piece):
            if not part:
                continue
            if len(buf) + len(part) <= max_chars:
                buf += part
            else:
                if buf:
                    out.append(buf)
                    buf = ""
                while len(part) > max_chars:      # single unit longer than cap
                    out.append(part[:max_chars])
                    part = part[max_chars:]
                buf = part
        if buf:
            out.append(buf)
        return out

    units: List[str] = [text]
    for sep in ("\n\n", "\n"):
        nxt: List[str] = []
        for u in units:
            if len(u) > max_chars:
                nxt.extend(split_oversized(u, sep))
            else:
                nxt.append(u)
        units = nxt
    return units


# ---------------------------------------------------------------------------
# Walk & diff
# ---------------------------------------------------------------------------

def walk_codebase(codebase: Path) -> List[Tuple[Path, str, str]]:
    """Yield (abs_path, rel_path, language) for indexable files."""
    out: List[Tuple[Path, str, str]] = []
    root_resolved = codebase.resolve()
    for root, dirs, files in os.walk(codebase):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext in BINARY_EXTENSIONS or ext not in EXTENSIONS:
                continue
            p = Path(root) / fn
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    continue
                # Symlink escape guard: skip files resolving outside the root.
                if os.path.islink(p) and not p.resolve().is_relative_to(root_resolved):
                    logger.debug("skipping symlink escape %s", p)
                    continue
            except OSError:
                continue
            out.append((p, p.relative_to(codebase).as_posix(), EXTENSIONS[ext]))
    return sorted(out, key=lambda t: t[1])


@dataclass
class DiffResult:
    changed: List[Tuple[Path, str, str, str]] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    current: Dict[str, str] = field(default_factory=dict)

    @property
    def deleted(self) -> List[str]:
        return self._deleted

    _deleted: List[str] = field(default_factory=list)


def diff_state(codebase: Path, prev: Dict[str, str]) -> DiffResult:
    result = DiffResult()
    seen: Set[str] = set()
    for path, rel, lang in walk_codebase(codebase):
        seen.add(rel)
        try:
            digest = file_hash(path)
        except OSError:
            continue
        result.current[rel] = digest
        if prev.get(rel) == digest:
            result.skipped.append(rel)
        else:
            result.changed.append((path, rel, lang, digest))
    result._deleted = sorted(set(prev) - seen)
    return result


# ---------------------------------------------------------------------------
# Ingestor
# ---------------------------------------------------------------------------

@dataclass
class IngestStats:
    indexed: int = 0
    skipped: int = 0
    drifted: int = 0
    deleted: int = 0
    chunks: int = 0
    entities: int = 0   # vertices merged (File+Module+Dependency)
    edges: int = 0
    bridges: int = 0
    embed_failures: int = 0
    errors: int = 0


def default_state_file(codebase: Optional[Path] = None) -> Path:
    """Per-codebase state file keyed by hash of the resolved absolute path.

    Multiple repos get distinct files (doc_ingest_state_{hash8}.json) so they
    never churn each other's saved hashes.
    """
    base = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "cache"
    if codebase is None:
        return base / "doc_ingest_state.json"
    key = hashlib.sha256(str(codebase.expanduser().resolve()).encode()).hexdigest()[:8]
    return base / f"doc_ingest_state_{key}.json"


class Ingestor:
    def __init__(self, config: Optional[HybridAgeConfig] = None,
                 state_file: Optional[Path] = None,
                 embedder: Optional[Any] = None):
        self.config = config or load_config()
        # Exact state_file wins (back-compat); otherwise per-codebase file is
        # resolved once the target codebase is known in run().
        self._state_override = (
            Path(state_file).expanduser() if state_file else None)
        self.state_file = self._state_override or default_state_file()
        self.embedder = embedder or Embedder(self.config.embed_url, self.config.embed_model,
                                             self.config.embed_dim)
        self.stats = IngestStats()

    async def run(self, codebase: Path) -> IngestStats:
        codebase = codebase.expanduser().resolve()
        if self._state_override is None:
            self.state_file = default_state_file(codebase)
        prev = load_state(self.state_file)
        diff = diff_state(codebase, prev)
        self.stats.skipped = len(diff.skipped)
        self.stats.deleted = len(diff.deleted)
        # Drifted = modified vs saved state; brand-new files (no prev entry)
        # are not drift.
        self.stats.drifted = sum(1 for _, rel, _, _ in diff.changed
                                 if prev.get(rel) is not None)

        conn = await asyncpg.connect(self.config.dsn)
        try:
            await conn.execute("LOAD 'age';")
            await conn.execute("SET search_path = ag_catalog, public;")
            # Backfill taxonomy for existing installs where ingest state caused
            # files with null doc_type/hash/indexed_at to be skipped forever
            # (Codex P1: reindex unchanged files when enriching metadata).
            # This repairs rows in-place so healthy:false heals on next run
            # without requiring file changes or state deletion.
            try:
                await conn.execute("""
                    UPDATE memory_entries
                       SET metadata = metadata
                         || jsonb_build_object('doc_type',
                              CASE
                                WHEN metadata->>'language' = 'Documentation' THEN 'architecture_decision'
                                WHEN metadata->>'language' IN ('SQL','Config') THEN 'standard_operating_procedure'
                                ELSE 'api_reference'
                              END)
                         || jsonb_build_object('hash', COALESCE(metadata->>'hash', md5(content)))
                         || jsonb_build_object('indexed_at', COALESCE(metadata->>'indexed_at', now()::text))
                         || jsonb_build_object('language', COALESCE(metadata->>'language','Text'))
                     WHERE metadata->>'file_path' IS NOT NULL
                       AND (metadata->>'doc_type' IS NULL OR metadata->>'hash' IS NULL OR metadata->>'indexed_at' IS NULL)
                """)
            except Exception:
                logger.debug("taxonomy backfill skipped", exc_info=True)
            if diff.deleted:
                await self._prune_deleted(conn, str(codebase), diff.deleted)
            # State must contain only successfully-indexed files so that a
            # failed file is retried on the next run: start from the skipped
            # (unchanged) files and add each file only after it indexes clean.
            current: Dict[str, str] = {rel: diff.current[rel]
                                       for rel in diff.skipped}
            for path, rel, lang, digest in diff.changed:
                try:
                    await self._index_file(conn, codebase, path, rel,
                                           lang, digest)
                    current[rel] = digest
                except Exception:
                    # Failed files stay out of saved state so the next run
                    # retries them; other files are still indexed & saved.
                    logger.warning("indexing failed for %s", rel, exc_info=True)
            save_state(self.state_file, current)
        finally:
            await conn.close()
        return self.stats

    # -- deletion -----------------------------------------------------------

    async def _prune_deleted(self, conn, codebase: str, gone: List[str]) -> None:
        from .store import age_str, cypher_call, savepoint_name
        for idx, fp in enumerate(gone):
            try:
                await conn.execute(
                    """
                    DELETE FROM memory_chunk_nodes b
                     USING memory_entries e
                     WHERE (b.chunk_id = e.id::text OR b.chunk_id = 'mem_' || e.id::text)
                       AND e.agent_identity = $1
                       AND e.metadata->>'file_path' = $2
                       AND e.metadata->>'codebase' = $3
                    """,
                    AGENT_IDENTITY, fp, codebase,
                )
                await conn.execute("DELETE FROM doc_chunks WHERE file_path = $1 AND source = $2",
                                   fp, codebase)
                await conn.execute(
                    """
                    DELETE FROM memory_entries
                     WHERE agent_identity = $1
                       AND metadata->>'file_path' = $2
                       AND metadata->>'codebase' = $3
                    """,
                    AGENT_IDENTITY, fp, codebase,
                )
                cy = (f"MATCH (f:File) WHERE f.path = {age_str(fp)} "
                      f"DETACH DELETE f RETURN 1")
                # DETACH DELETE wrapped in SAVEPOINT so AGE failure never poisons outer txn
                sp = savepoint_name("prune_del", idx)
                try:
                    await conn.execute(f"SAVEPOINT {sp}")
                except Exception:
                    # If SAVEPOINT unavailable (no txn), fallback to direct execute
                    try:
                        await conn.execute(
                            f"SELECT * FROM {cypher_call(self._graph(), cy)} AS (ok agtype)")
                    except Exception:
                        self.stats.errors += 1
                        logger.warning("prune cypher failed for %s", fp, exc_info=True)
                    else:
                        logger.info("pruned deleted file %s", fp)
                    continue
                try:
                    await conn.execute(
                        f"SELECT * FROM {cypher_call(self._graph(), cy)} AS (ok agtype)")
                    await conn.execute(f"RELEASE SAVEPOINT {sp}")
                except Exception:
                    try:
                        await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                    except Exception:
                        pass
                    try:
                        await conn.execute(f"RELEASE SAVEPOINT {sp}")
                    except Exception:
                        pass
                    self.stats.errors += 1
                    logger.warning("prune cypher failed for %s", fp, exc_info=True)
                else:
                    logger.info("pruned deleted file %s", fp)
            except Exception:
                self.stats.errors += 1
                logger.warning("prune failed for %s", fp, exc_info=True)

    def _graph(self) -> str:
        return validate_graph_name(self.config.graph)

    # -- per-file pipeline ----------------------------------------------------

    async def _index_file(self, conn, codebase: Path, path: Path,
                          rel: str, lang: str, digest: str) -> None:
        """Index one file. Raises on fatal failure so run() excludes it from
        saved state (the file is retried on the next run)."""
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            self.stats.errors += 1
            raise RuntimeError(f"unreadable file {rel}") from exc
        if not content.strip():
            return

        chunks = chunk_text(content)
        embeddings: List[Optional[List[float]]] = []
        for ch in chunks:
            embeddings.append(await self.embedder.embed_text(ch))
        embedded = [c for c, v in zip(chunks, embeddings) if v is not None]
        n_failed = len(chunks) - len(embedded)
        self.stats.embed_failures += n_failed
        if not embedded:
            return

        codebase_s = str(codebase)
        import datetime as _dt
        # Taxonomy per AGENTS.md: doc_type always required for file-backed rows.
        # Map language to doc_type; file ingest defaults to api_reference
        # (provider.py uses same default for file_path-backed writes).
        _doc_type = "api_reference"
        if lang == "Documentation":
            _doc_type = "architecture_decision"
        elif lang == "SQL":
            _doc_type = "standard_operating_procedure"
        elif lang in ("Config",):
            _doc_type = "standard_operating_procedure"
        meta_common = {
            "file_path": rel,
            "hash": digest,
            "language": lang,
            "codebase": codebase_s,
            "doc_type": _doc_type,
            "indexed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }

        # -- L2a: doc_chunks (all chunks; replace prior rows for this file) ----
        written_ids: List[str] = []   # enumerated chunk ids actually written
        try:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM doc_chunks WHERE source = $1 AND file_path = $2",
                    codebase_s, rel,
                )
                for i, (ch, vec) in enumerate(zip(chunks, embeddings)):
                    if vec is None:
                        continue
                    cid = f"{digest[:16]}:{i}"
                    await conn.execute(
                        """
                        INSERT INTO doc_chunks
                            (id, doc_hash, source, file_path, ordinal, content,
                             embedding, metadata)
                        VALUES ($1, $2, $3, $4, $5, $6, $7::vector, $8::jsonb)
                        ON CONFLICT (id) DO UPDATE SET
                            content = EXCLUDED.content,
                            embedding = EXCLUDED.embedding,
                            metadata = EXCLUDED.metadata
                        """,
                        cid, digest, codebase_s, rel, i, ch,
                        vec_to_literal(vec), json.dumps(meta_common),
                    )
                    written_ids.append(cid)
                    self.stats.chunks += 1
        except Exception:
            self.stats.errors += 1
            logger.warning("doc_chunks write failed for %s", rel, exc_info=True)

        # -- L2b: memory_entries (one row per file, keyed by file_path) --------
        mem_id: Optional[int] = None
        body = chunks[0][:MAX_CHUNK_CHARS]
        vec0 = next(v for v in embeddings if v is not None)
        try:
            row = await conn.fetchrow(
                """
                SELECT id FROM memory_entries
                 WHERE agent_identity = $1
                   AND metadata->>'file_path' = $2
                   AND metadata->>'codebase' = $3
                 ORDER BY id LIMIT 1
                """,
                AGENT_IDENTITY, rel, codebase_s,
            )
            if row:
                mem_id = row["id"]
                await conn.execute(
                    """
                    UPDATE memory_entries
                       SET content = $1, embedding = $2::vector,
                           metadata = $3::jsonb, updated_at = now()
                     WHERE id = $4
                    """,
                    body, vec_to_literal(vec0), json.dumps(meta_common), mem_id,
                )
            else:
                mem_id = await conn.fetchval(
                    """
                    INSERT INTO memory_entries
                        (agent_identity, target, content, embedding, metadata)
                    VALUES ($1, 'memory', $2, $3::vector, $4::jsonb)
                    RETURNING id
                    """,
                    AGENT_IDENTITY, body, vec_to_literal(vec0),
                    json.dumps(meta_common),
                )
        except Exception:
            self.stats.errors += 1
            logger.warning("memory_entries write failed for %s", rel, exc_info=True)

        # -- L3 graph: File / Module / Dependency + IMPORTS ---------------------
        from .store import age_str  # local import avoids cycle noise

        merge_items: List[Tuple[str, Dict[str, Any]]] = [("File", {"path": rel})]
        mod_name = module_for(rel)
        if mod_name:
            merge_items.append(("Module", {"name": mod_name}))
        deps = extract_dependencies(content, lang)[:40]
        for dep in deps:
            merge_items.append(("Dependency", {"name": dep, "kind": dep_kind(dep)}))

        vids = await self._merge_batched(conn, merge_items)
        file_vid = int(vids[0]) if vids and vids[0] else None
        mod_vid = int(vids[1]) if mod_name and vids[1:] and vids[1] else None
        dep_vids = [int(v) if v else None
                    for v in (vids[2:] if mod_name else vids[1:])]
        self.stats.entities += sum(1 for v in vids if v)

        edges = [("Imports", file_vid, dv) for dv in dep_vids if file_vid and dv]
        if edges:
            # Dynamic graph: IMPORTS edges carry vector radius + force + recency.
            edge_props = {}
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            for lab, src, dst in edges:
                edge_props[(lab, src, dst)] = {"weight": 1.0, "cosine": 0.80, "created_at": now_iso}
            self.stats.edges += await self._merge_edges(conn, edges, edge_props=edge_props)

        # -- Bridges ------------------------------------------------------------
        # Bridge the same enumerated chunk ids actually written to doc_chunks
        # so indices never shift when an embedding fails.
        # Canonical: memory_entries.id::text; legacy mem_ prefix kept as alias
        # for migration (see V8__bridge_canonical_alias.sql).
        if mem_id and file_vid:
            self.stats.bridges += await self._bridge(
                conn, str(mem_id), file_vid, source="memory_entry")
            # legacy alias for pre-V8 rows; bounded to one extra insert
            self.stats.bridges += await self._bridge(
                conn, f"mem_{mem_id}", file_vid, source="memory_entry")
        if mem_id and mod_vid:
            self.stats.bridges += await self._bridge(
                conn, str(mem_id), mod_vid, source="memory_entry")
            self.stats.bridges += await self._bridge(
                conn, f"mem_{mem_id}", mod_vid, source="memory_entry")
        for cid in written_ids:
            if not file_vid:
                break
            self.stats.bridges += await self._bridge(
                conn, cid, file_vid, source="doc_chunk")

        self.stats.indexed += 1
        if n_failed:
            logger.info("%s: %d/%d chunks embedded", rel, len(embedded), len(chunks))

    async def _merge_batched(self, conn, items) -> List[Optional[str]]:
        """SAVEPOINT-guarded, batched (>=50/txn when large) vertex MERGEs."""
        results: List[Optional[str]] = []
        graph = self._graph()
        from .store import age_props, age_str, check_label, cypher_call, savepoint_name
        items = list(items)
        for start in range(0, len(items), 50):
            batch = items[start:start + 50]
            try:
                async with conn.transaction():
                    for idx, (label, props) in enumerate(batch):
                        sp = savepoint_name("ing_v", idx)
                        await conn.execute(f"SAVEPOINT {sp}")
                        try:
                            key_prop = "path" if "path" in props else "name"
                            non_key = {k: v for k, v in props.items() if k != key_prop}
                            # AGE 1.6 has no ON CREATE/ON MATCH SET; MERGE on the
                            # minimal key, then plain SET for mutable properties.
                            sets = age_props(non_key)
                            cy = (
                                f"MERGE (v:{check_label(label)} "
                                f"{{{key_prop}: {age_str(props[key_prop])}}})\n"
                                + (f"SET v += {sets}\n" if sets != "{}" else "")
                                + f"RETURN id(v)"
                            )
                            row = await conn.fetchrow(
                                f"SELECT * FROM {cypher_call(graph, cy)} AS (id agtype)")
                            raw = str(row["id"]).strip('"') if row else None
                            results.append(str(int(raw)) if raw else None)
                            await conn.execute(f"RELEASE SAVEPOINT {sp}")
                        except Exception:
                            await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                            results.append(None)
                            self.stats.errors += 1
                            logger.debug("vertex MERGE failed %s", props, exc_info=True)
            except Exception:
                expected = start + len(batch)
                while len(results) < expected:
                    results.append(None)
                self.stats.errors += 1
        return results

    async def _merge_edges(self, conn, edges, edge_props=None) -> int:
        done = 0
        graph = self._graph()
        from .store import age_props, check_label, cypher_call, savepoint_name
        edges = list(edges)
        for start in range(0, len(edges), 50):
            batch = edges[start:start + 50]
            try:
                async with conn.transaction():
                    for idx, (label, src, dst) in enumerate(batch):
                        sp = savepoint_name("ing_e", idx)
                        await conn.execute(f"SAVEPOINT {sp}")
                        try:
                            props = (edge_props or {}).get((label, src, dst), {})
                            props_str = f" SET e += {age_props(props)}" if props else ""
                            cy = (
                                f"MATCH (a), (b) WHERE id(a) = {int(src)} "
                                f"AND id(b) = {int(dst)} "
                                f"MERGE (a)-[e:{check_label(label)}]->(b){props_str} RETURN id(e)"
                            )
                            row = await conn.fetchrow(
                                f"SELECT * FROM {cypher_call(graph, cy)} AS (id agtype)")
                            if row:
                                done += 1
                            await conn.execute(f"RELEASE SAVEPOINT {sp}")
                        except Exception:
                            await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                            self.stats.errors += 1
            except Exception:
                self.stats.errors += 1
        return done

    async def _bridge(self, conn, chunk_id: str, vertex_id: int,
                      source: str = "memory_entry") -> int:
        try:
            await conn.execute(
                """
                INSERT INTO memory_chunk_nodes (chunk_id, source, vertex_id, graph_name)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (chunk_id, source, vertex_id) DO NOTHING
                """,
                chunk_id, source, vertex_id, self.config.graph,
            )
            return 1
        except Exception:
            self.stats.errors += 1
            return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_summary(stats: IngestStats) -> None:
    rows = [
        ("indexed", stats.indexed), ("skipped", stats.skipped),
        ("drifted", stats.drifted), ("deleted", stats.deleted),
        ("chunks", stats.chunks), ("entities", stats.entities),
        ("edges", stats.edges), ("bridges", stats.bridges),
        ("embed_failures", stats.embed_failures), ("errors", stats.errors),
    ]
    width = max(len(k) for k, _ in rows)
    print("\n=== ingest summary ===")
    for k, v in rows:
        print(f"  {k:<{width}} : {v}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="hermes-memory-ingest",
        description="Index a codebase into pgvector + AGE hybrid memory",
    )
    ap.add_argument("path", nargs="?", default=os.getcwd(),
                    help="Codebase root to index (default: cwd)")
    ap.add_argument("--state-file", default=None,
                    help="Path to the hash-state JSON "
                         "(default: ~/.hermes/cache/doc_ingest_state.json)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    codebase = Path(args.path)
    if not codebase.is_dir():
        print(f"error: not a directory: {codebase}", file=sys.stderr)
        return 1

    state_file = Path(args.state_file).expanduser() if args.state_file else None
    ingestor = Ingestor(state_file=state_file)
    try:
        stats = asyncio.run(ingestor.run(codebase))
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"error: ingest failed: {exc}", file=sys.stderr)
        return 1
    _print_summary(stats)
    return 1 if stats.errors else 0


if __name__ == "__main__":
    sys.exit(main())
