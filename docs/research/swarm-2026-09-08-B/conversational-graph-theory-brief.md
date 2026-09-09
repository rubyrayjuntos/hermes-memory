# Conversational Graph Theory Brief — Noun / Semantic Edge

> Path B — OC-MVP-3/4, swarm 2026-09-08-B. Grounded in `walk.py:76-91`, `V9__noun_passport_semantic_edge.sql`, `02_schema.sql:21`, `PANE_INTERACTION_SPEC.md`, `CORRECTIONS.md #3 #8 #9 #14`. Every V1 WILL was `grep -R` verified in `src/` — no DDL-only label promoted.

**Sources live:** `noun` (id SERIAL, label UNIQUE, type, ag_vertex_id BIGINT UNIQUE), `semantic_edge` (src_noun→tgt_noun, verb_type='mentions', magnitude 0-8, polarity -1/1, provenance_turns BIGINT[]), `conversations.id BIGSERIAL`, `drain_status`, `embed_model/dim` (trust_nomic_768). Walk is query-time `beam_score`.

---

## Executive Summary

1. **Your graph is adjacent-pairs, not a mesh:** `provider.py:580-581` builds `semantic_edge` from consecutive extracted nouns (`adjacent pairs only`, `CORRECTIONS.md #9`), not fully connected cliques. Degree is cheap; betweenness is sparse; community still matters but on paths.
2. **Leiden > Louvain, formally:** Louvain can produce disconnected/ badly-connected communities; Leiden guarantees well-connected (Nature 2019). For `semantic_edge.verb_type='mentions'` communities, Leiden is the correction.
3. **Centrality on co-mention is meaningful but weak until scale:** `File.inDegree` logic does not port — your `inDegree` is co-mention degree. PageRank on this graph is *entity importance* within a session, not blast radius. Keep degree for now; PageRank only when noun count >> 100s.
4. **Small-world holds for conversations:** Adjacent-pair paths give L ~ log n locally, but global coefficient is per-session, not global. 1-hop click (spec §2) is the correct budget; 2-hop render is MVP-out per spec.
5. **Walk is already GraphRAG-shaped:** `beam_score c = src_align*tgt_align, composite = 0.4*sim+0.4*c*prov+0.2*decay` is the only scorer (`CORRECTIONS #8`), not PPR. Do not add a stored `File.beamScore` (correction #14).

---

## 1. Centrality: What Measures What in the Co-Mention Graph

**Definition (cit.):** Centrality assigns importance to nodes by position in graph [Wikipedia Centrality](https://en.wikipedia.org/wiki/Centrality). Key variants: degree (neighbors count), betweenness (fraction of shortest paths through node), eigenvector (importance of neighbors), Katz (all paths decaying by length), PageRank (Google's eigenvector variant with damping) [Wikipedia PageRank](https://en.wikipedia.org/wiki/PageRank).

**Why for THIS graph:** Nodes are nouns, edges are adjacent co-mentions in a turn (`extract_nouns` order). `degree` = how many distinct neighbors this noun co-occurred with (across turns via `provenance_turns`). High degree nouns are topic anchors. But `betweenness` is low-variance here because paths are linear per-turn — a bridge noun connecting two sessions is rare and thus high-signal when it appears.

**Industry:** Same measures drive Neo4j GDS, NetworkX, KeyLines. For conversation graphs, degree + betweenness are standard entity-ranking; PageRank is overkill until graph is large (GraphRAG uses community detection, not PageRank, for summary [Microsoft GraphRAG](https://microsoft.github.io/graphrag/)).

**Concrete for us (SQL, not AGE):**
```sql
-- degree (cheap, live via count)
SELECT n.label, count(e.id) AS degree
FROM noun n LEFT JOIN semantic_edge e ON e.src_noun=n.id OR e.tgt_noun=n.id
GROUP BY n.id ORDER BY degree DESC LIMIT 20;

-- PageRank placeholder (compute on fly, don't store)
-- Use only when noun > 500: store nightly File.pageRank is WRONG graph — this is Noun.pageRank
```

**Theory→Data→Viz:** degree (`count(*)`) → node size `val = 1 + degree` already in `graph_view.py:_stamp_graph_degree` → viz size = degree today (keep until PageRank justified).

---

## 2. Community: Leiden vs Louvain, LPA, Infomap

**Definition:** Louvain greedily maximizes modularity [Louvain Method](https://en.wikipedia.org/wiki/Louvain_method); Leiden refines to guarantee well-connected communities and faster runtime [Nature Louvain→Leiden](https://www.nature.com/articles/s41598-019-41695-z), [Wikipedia Leiden](https://en.wikipedia.org/wiki/Leiden_algorithm).

**Why for THIS graph:** Adjacent-pair edges form linear chains per turn; Louvain will merge chains into communities that may be internally disconnected (a known failure). Leiden's refinement stage corrects this — important because `semantic_edge.magnitude` varies (0-8) and disconnected chains would otherwise share a community label.

**Industry:** GraphRAG (Microsoft) uses Leiden for community summaries; TigerGraph/Neo4j GDS default to Leiden post-2020.

**Concrete:**
```sql
-- Community is derived, not stored — compute batch:
-- Use python-louvain vs leidenalg on adjacency where weight=magnitude/8
-- Store only if needed: noun.leiden_id (INT), blocked until V9-gap closed
```

**Theory→Data→Viz:** `leiden_id` (if computed) → node color by community; cluster label in side sheet — but spec's funnel already segments by beam, so community is V2 (post-OC-MVP-3).

---

## 3. Spectral / Small-World / Scale-Free: Does It Apply?

**Definition:** Spectral gap via Laplacian eigenvalues measures connectivity; small-world = high clustering + short path length (Watts-Strogatz); scale-free = power-law degree distribution.

**For THIS graph:** Per-session small-world holds trivially (adjacent chain → clustering from repeated pairs across turns). Global graph is *sessions* as components — not one small-world. Scale-free false for few dozen nouns. So `GraphMeta.smallWorldCoeff` is not a useful stored field — the peer review's correctly.

**Industry:** Small-world validation matters for social nets, less for short conversation windows.

**Concrete:** Don't add `GraphMeta` store. Use `SELECT count(*) FROM noun` as LOD decision (gate at 500 nouns before community compute).

---

## 4. Traversals & Walks: Why Beam Score, Not PPR/Node2Vec

**Definition:** PageRank/PPR are eigenvector walks with damping [PageRank](https://en.wikipedia.org/wiki/PageRank); Node2Vec learns embeddings via biased random walks [arXiv 1607.00653](https://arxiv.org/abs/1607.00653). Our `walk.py:76-91` is bounded walk: `c = src_align*tgt_align`, `composite = 0.4*sim +0.4*c*prov +0.2*decay`, `score = composite * magnitude/8`.

**Why beam_score not PPR:** CORRECTIONS #2 explicitly: `beam_score c` is **not** PPR — it's cosine pole alignment. Retrieval's `prov_boost` and `decay` are conversation-aware; PPR would ignore provenance_turns and hop decay. GraphRAG pattern is hybrid `ANN → expand → beam` which matches our `hops=1` + beam, not random walk embeddings.

**Concrete (already live):**
```python
from src.hermes_memory.walk import beam_score
c, composite, score = beam_score(sim=0.71, src_align=0.9, tgt_align=0.4, prov_boost=1.0, decay=0.95, magnitude=6.0)
# score is what funnel shows as "kept after beam/budget"
```

**Theory→Data→Viz:** `beam_score` components (`w` magnitude, `c` cosine, `decay`, `score`, `hop`) → edge tooltip ONLY in search context (spec §1): “if NOT part of active search: ‘Score only available in context of a search’” — prevents inventing score for browsed edges.

---

## 5. Temporal / Versioned Chains: Not Here

No `Deprecates` — our temporal is `conversations.drain_status` (`complete`/`graph_degraded`/`NULL`) + `extract_nouns` order, not AGE versioning. Adjacent-pair order is the only temporal signal; `valid_to` filtering is proposed/deferred per spec §2 (“update this line the day bi-temporal lands”, CORRECTIONS #7 not bi-temporal).

---

## Theory→Data→Viz Mapping (B-real)

| Theory | Data field (live, grep-verified) | Viz |
|--------|----------------------------------|-----|
| Degree centrality | `count(semantic_edge where src/tgt = n.id)` | Node `val = 1+degree` size (already `_stamp_graph_degree`) |
| Betweenness | compute on fly via NetworkX (not stored) | Border thickness on hover if bridge |
| Leiden community | `noun.leiden_id` (deferred, not stored) | Color by community when noun>500 |
| beam_score | query-time `walk.py:beat` outputs `w,c,decay,score,hop` per edge | Edge tooltip: magnitude+decay+beam components ONLY in search |
| provenance_turns | `semantic_edge.provenance_turns BIGINT[]` → `conversations_by_ids` | Click panel: neighbor label + magnitude + turn excerpts (spec §2) |
| drain_status / embed_model | `conversations.drain_status`, `embed_model/dim` (NULL → trust_nomic_768) | Side sheet badge: `graph_degraded`, `unpassported`, `Embedding unstamped` |

## Top 5 Recommendations (ranked)

1. **Keep degree, defer PageRank:** Ship `1+degree` sizing; gate PageRank/leiden until noun scale proves need.
2. **Leiden over Louvain when clustering:** If community ships, use Leiden (well-connected guarantee) — cite Nature.
3. **Honor adjacent-pairs reality:** Any “fully connected co-mention” claim is wrong per CORRECTIONS #9 — your edges are paths, size LOD accordingly (1-hop only per spec MVP-out 2-hop).
4. **Beam_score is only scorer:** Enforce `test_score_contract.py:126` — no new weighted-sum outside walk.py (CORRECTIONS #8). Don't store composite on noun.
5. **Provenance is the UX moat:** Click must show `turn` text, not just `label↔label` — that's the spec's “here is why” vs “linked”.

## Open Questions

1. At what noun/edge count does PageRank/leiden add signal vs degree?
2. Should magnitude weight degree (weighted degree) for sizing?
3. Does provenance_boost=1.0 need tuning for multi-turn sessions?
4. When does global vs per-session community make sense?

## References

- Centrality: https://en.wikipedia.org/wiki/Centrality
- PageRank: https://en.wikipedia.org/wiki/PageRank
- Leiden algorithm: https://en.wikipedia.org/wiki/Leiden_algorithm
- Louvain Method: https://en.wikipedia.org/wiki/Louvain_method
- From Louvain to Leiden: https://www.nature.com/articles/s41598-019-41695-z
- Louvain paper: https://arxiv.org/abs/0803.0476
- Leiden guarantees: https://arxiv.org/abs/1810.08473
- Microsoft GraphRAG: https://microsoft.github.io/graphrag/
- Neo4j GDS community: https://neo4j.com/docs/graph-data-science/current/algorithms/community/
- CORRECTIONS.md #2 #8 #9 #14, walk.py:76-91, V9__noun_passport_semantic_edge.sql
