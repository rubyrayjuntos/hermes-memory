# Hermes Persistent Memory & Prompt Context Injection Specification

## Technical Specification & Implementation Roadmap for Hybrid pgvector \+ Apache AGE Integration

# 1\. System Overview

This specification defines the architecture, pipeline, metric framework, and integration plan for introducing a durable, persistent memory layer into Hermes. The system utilizes a hybrid GraphRAG provider combining PostgreSQL with pgvector and Apache AGE (graph database). It operates as a dedicated MemoryProvider to prefetch relevant contextual memory and inject a fenced block directly into the prompt turn before model invocation without relying on interactive tool schemas.

# 2\. Architecture & Stack Requirements

## 2.1 Database & Extensions

* PostgreSQL 17 with pgvector and age extensions loaded.  
* Database search\_path configured to include ag\_catalog.  
* Apache AGE graph named hermes\_knowledge initialized with pre-declared node and edge labels.  
* Bridge table memory\_chunk\_nodes linking vector entries (memory\_entries) with graph vertex IDs.  
* **Graph Schema Migration & Evolution Strategy:** Versioned migration scripts manage graph label updates, vertex/edge property changes, and index maintenance alongside standard relational DDL. Schema evolution uses backward-compatible Cypher queries and dual-writing during transitional phases.  
* **Multi-Tenancy & Context Isolation:** User context isolation is strictly enforced at the database layer using Postgres Row-Level Security (RLS) on relational and vector tables, scoped by user\_id, combined with tenant-isolated subgraphs or filtered Cypher traversals in Apache AGE.  
* **RLS Policy Verification & Test Suite:** Multi-tenancy isolation includes automated integration tests verifying that RLS policies prevent cross-tenant vector leakage and sub-graph traversal across all read/write paths.

## 2.2 Embedding Provider Architecture

* **Primary Backend:** OpenAI text-embedding-3-small configured with native dimension truncation to 768 dimensions.  
* **Fallback Backend:** Local Ollama instance executing nomic-embed-text (768 dimensions).  
* **Dimension Invariant:** Strict 768-dimension requirement across all vector tables to enable seamless provider switching without schema modifications.

# 3\. Retrieval & Context Injection Pipeline

## 3.1 Read Path (Prefetch Execution)

* The prefetch() routine executes synchronously during the prompt processing lifecycle. It must be non-blocking to execution flow and safely catch all exceptions (returning an empty string on error or timeout).  
* Generate 768d vector representation of the current turn prompt using the active embedding client.  
* Perform pgvector Approximate Nearest Neighbor (ANN) search to obtain top-k seed nodes matching minimum similarity thresholds.  
* Execute 1 to 2 hop graph expansion via Apache AGE Cypher queries starting from resolved seed node IDs.  
* Rank merged candidates (vector chunks \+ graph triples) according to similarity and recency weighting.  
* Truncate candidate content to fit within the designated token budget (default 1200 tokens) and format into a fenced context string.  
* Replace native character-length approximations with exact tiktoken (cl100k\_base) token counting during prefetch candidate truncation to prevent prompt window overflow.

## 3.2 Write Path (Turn Synchronization)

* The sync\_turn call writes turn text and embeddings directly to conversations and memory\_entries.  
* Heavy entity extraction, relation extraction, and graph updates are decoupled from the interactive latency path and handled asynchronously via scheduled cron jobs.  
* **Stale-Data Policy & Queue Management:** The asynchronous background extraction pipeline enforces a maximum allowable lag of 300 seconds (5 minutes). Unprocessed turn tasks enter a durable queue with exponential backoff on failure and a maximum depth of 1,000 pending items before triggering alert telemetry. Stale entities pending graph sync remain search-addressable via vector fallback.  
* **Poison Pill & Dead-Letter Queue Handling:** Failed background extraction tasks exceeding 3 retry attempts are automatically moved to a dead-letter queue (DLQ) with alert notification, preventing poison pill turns from blocking queue execution.

# 4\. Metric Definitions

## 4.1 Retrieval-Hit Rate vs. Injection-Hit Rate

* **Retrieval-Hit Rate (Diagnostic Metric):** The fraction of golden-set queries for which expected memory chunks appear in the raw candidate pool (vector seeds \+ graph expansion) prior to scoring, ranking, and token truncation.  
* **Injection-Hit Rate (Product Metric):** The fraction of golden-set queries for which expected memory items (or faithful graph triples) successfully survive scoring, ranking, and token budget truncation, appearing in the final injected string returned by prefetch().

## 4.2 Injection Mean Reciprocal Rank (MRR)

Injection MRR calculates the reciprocal rank of the first valid expected memory item present in the final injected context block. Lines are evaluated top-to-bottom starting at rank 1\. If no expected item survives post-truncation, the reciprocal rank for that query is 0\.

# 5\. Benchmarking & Optimization Plan

## 5.1 Evaluation Suite Scope

* **Recall Evaluation:** Golden-set measurement assessing recall@k, Injection-Hit Rate, and Injection MRR.  
* **Latency Profiling:** Characterizing p50, p95, and p99 latency across cold/warm states and synthetic corpus sizes (1k, 10k, 100k rows).  
* **Throughput Benchmarking:** Measuring sustained turn ingestion rate and queue behavior during backlog bursts (256-deep backlog).  
* **Graph Ablation Experiment:** Executing benchmark passes with \--no-graph to isolate the net impact of Apache AGE graph expansion over pure vector retrieval.

# 6\. Production Integration & Deployment Plan

## 6.1 Deployment Sequence

* **Pre-flight Environment Validation:** Verify fully-qualified DSN format in environment configurations and validate API credentials.  
* **Plugin Deployment:** Install plugin package to \~/.hermes/plugins/hybrid-age/ ensuring presence of mandatory discovery markers in \_\_init\_\_.py.  
* **Configuration Activation:** Set memory.provider: hybrid-age in active configuration files.  
* **Verification Gate:** Execute end-to-end verification harness, validate memory record insertion across turns, and verify prompt context inclusion.  
* **Cron Resumption:** Re-enable background pipeline jobs (graph extractor, document indexer, watchdog) once live writes are confirmed.

## 6.2 Fallback & Rollback Strategy

If memory prefetch or database connectivity encounters critical failure, resetting memory.provider: '' immediately reverts Hermes to its default built-in storage model without data loss in the PostgreSQL instance.

# 7\. Technical Artifacts & File Specifications

## 7.1 Database Schema Bridge (scripts/bridge.sql)

```sql
--- Memory chunk to graph vertex bridge table schema with referential integrity
CREATE TABLE IF NOT EXISTS memory_chunk_nodes (
    chunk_id UUID NOT NULL REFERENCES memory_entries(id) ON DELETE CASCADE,
    node_id BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chunk_id, node_id)
);

```

## 7.2 Provider Implementation Placeholder (src/hermes\_memory/provider.py)

```py
#import logging
import time
from typing import List, Dict, Any, Optional
import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger("hermes_memory.provider")

class HybridAgeMemoryProvider:
    def __init__(self, config: Any, embedder: Any):
        self.config = config
        self.embedder = embedder
        self.dsn = config.db.dsn
        self.graph_name = config.db.graph_name
        self.token_budget = config.token_budget

    def prefetch(self, query: str, user_id: Optional[str] = None) -> str:
        start_time = time.time()
        try:
            # 1. Embed query text (768-d vector)
            query_vec = self.embedder.embed_text(query)
            if not query_vec:
                return ""
            vec_str = "[" + ",".join(map(str, query_vec)) + "]"

            candidates: Dict[str, Dict[str, Any]] = {}
            seed_ids: List[str] = []

            with psycopg2.connect(self.dsn) as conn:
                conn.autocommit = True
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    # 2. Vector ANN seed lookup via pgvector
                    cur.execute("""
                        SELECT id, chunk_text, 1 - (embedding <=> %s::vector) AS sim_score, created_at
                        FROM memory_entries
                        WHERE 1 - (embedding <=> %s::vector) >= %s
                        ORDER BY sim_score DESC LIMIT %s;
                    """, (vec_str, vec_str, self.config.min_similarity, self.config.vector_k))
                    for row in cur.fetchall():
                        sid = str(row['id'])
                        seed_ids.append(sid)
                        candidates[row['chunk_text']] = {
                            'sim': float(row['sim_score']),
                            'recency': 1.0,  # Normalized recency factor
                            'graph': 0.0
                        }

                    # 3. Apache AGE Cypher multi-hop expansion with timeout protection
                    if seed_ids:
                        try:
                            cur.execute("SET statement_timeout = 200;")  # 200ms timeout guard
                            cur.execute("LOAD 'age'; SET search_path = ag_catalog, public;")
                            formatted_ids = ", ".join(f"'{sid}'" for sid in seed_ids)
                            cypher_q = f"""
                                SELECT * FROM cypher('{self.graph_name}', $$
                                MATCH (a)-[r1]->(b)-[r2]->(c)
                                WHERE r1.source_chunk IN [{formatted_ids}] AND r2.source_chunk IS NOT NULL
                                RETURN r2.source_chunk
                                $$) AS (chunk_id agtype);
                            """
                            cur.execute(cypher_q)
                            g_ids = [r['chunk_id'].strip('"') for r in cur.fetchall() if r['chunk_id']]
                            if g_ids:
                                cur.execute("SELECT id, chunk_text FROM memory_entries WHERE id::text = ANY(%s);", (g_ids,))
                                for r in cur.fetchall():
                                    txt = r['chunk_text']
                                    if txt in candidates:
                                        candidates[txt]['graph'] = 1.0
                                    else:
                                        candidates[txt] = {'sim': 0.3, 'recency': 0.5, 'graph': 1.0}
                        except Exception as ge:
                            logger.warning(f"Graph expansion timed out or failed; falling back to vector seeds: {ge}")

            # 4. Composite Scoring & Ranking (W_sim * Sim + W_recency * Rec + W_graph * Graph)
            w_sim, w_rec, w_graph = 0.4, 0.3, 0.3
            scored = []
            for txt, s in candidates.items():
                score = (w_sim * s['sim']) + (w_rec * s['recency']) + (w_graph * s['graph'])
                scored.append((score, txt))
            scored.sort(key=lambda x: x[0], reverse=True)

            # 5. Format & enforce token budget
            header = "<context_memory>\n"
            footer = "</context_memory>"
            curr_tokens = len(header) // 4
            lines = []
            for _, txt in scored:
                line_tokens = (len(txt) + 4) // 4
                if curr_tokens + line_tokens + (len(footer) // 4) > self.token_budget:
                    break
                lines.append(f"- {txt}")
                curr_tokens += line_tokens

            return header + "\n".join(lines) + "\n" + footer if lines else ""
        except Exception as e:
            logger.error(f"Prefetch failed unexpectedly: {e}")
            return ""

    def sync_turn(self, conversation_id: str, turn_data: dict) -> None:
        # Async write to vector store handled here
        pass

```

## 7.3 Golden Set Dataset Schema (data/golden\_set.json)

```json
[
  {
    "id": "query_01",
    "query": "Sample evaluation prompt query",
    "expected_memory": "Expected factual string or triple content"
  }
]
```

# 8\. Configuration Reference

Default parameter baseline for provider setup:

| Parameter | Default Value | Description |
| :---- | :---- | :---- |
| embed\_provider | openai | Primary embedding backend (openai | ollama) |
| vector\_dimensions | 768 | Enforced vector dimension length |
| graph\_name | hermes\_knowledge | Apache AGE graph identifier |
| vector\_k | 12 | Top ANN seed candidate limit |
| min\_similarity | 0.55 | Similarity score threshold for vector seeds |
| token\_budget | 1200 | Maximum token limit for prefetch block |
| timeout\_ms | 200 (target) / 500 (hard) | Execution timeout prior to returning empty context statement\_timeout 200ms Configurable statement timeout guard for Cypher graph expansion queries  |

# 9\. Appendix A: Evaluation Golden Set Dataset (data/golden\_set.json)

The active golden set dataset used by the evaluation harness:

```json
[
  {
    "id": "q1_auth_fix",
    "query": "How did we fix the verify run auth failure?",
    "expected_memory": "update the repo .env to the new password and re-initialize the stack volume"
  },
  {
    "id": "q2_db_port",
    "query": "Which database port does production memory use?",
    "expected_memory": "Main hermes-postgres (host port 5440, db hermes)"
  },
  {
    "id": "q3_embed_model",
    "query": "What is our primary embedding model for Phase 2?",
    "expected_memory": "OpenAI text-embedding-3-small @ 768 dims"
  },
  {
    "id": "q4_embed_reason",
    "query": "Why did we drop the Ollama dependency?",
    "expected_memory": "Removes always-on Ollama dependency; negligible cost at personal scale"
  },
  {
    "id": "q5_rollback",
    "query": "How do I rollback the memory provider to SQLite?",
    "expected_memory": "Set memory.provider: '' in config.yaml"
  },
  {
    "id": "q6_plugin_discovery",
    "query": "What marker is required for Hermes plugin discovery?",
    "expected_memory": "init.py must literally contain register_memory_provider/MemoryProvider"
  },
  {
    "id": "q7_age_quirk",
    "query": "What is the workaround for AGE 1.6 missing ON CREATE SET?",
    "expected_memory": "MERGE-then-SET pattern"
  },
  {
    "id": "q8_gauntlet_defects",
    "query": "How many bugs did the gauntlet catch before the v0.1 release?",
    "expected_memory": "~14 defects caught before release across the gauntlet"
  },
  {
    "id": "q9_schema_dims",
    "query": "Do we need to migrate the schema for the new embeddings?",
    "expected_memory": "native dim truncation to 768 keeps schema unchanged"
  },
  {
    "id": "q10_deliberate_miss",
    "query": "What is the recipe for chocolate chip cookies?",
    "expected_memory": "DELIBERATE_MISS_EXPECTING_EMPTY"
  }
]
```

# 10\. Appendix B: Environment Variables Specification

Configuration settings for DSN connection strings and API credentials:  
Configuration settings for DSN connection strings, API credentials, and PgBouncer connection pooling:

```shell
# Fully qualified Postgres DSN for the hybrid-age provider:
HYBRID_AGE_DSN=postgres://hermes:<YOUR_PG_PASSWORD>@localhost:5440/hermes
# Phase 2 Embeddings: OpenAI API Key
OPENAI_API_KEY=sk-your-actual-api-key-here

# Connection Pooling (PgBouncer) & High-Availability Settings
PGBOUNCER_HOST=localhost
PGBOUNCER_PORT=6432
PGBOUNCER_POOL_MODE=transaction
PGBOUNCER_MAX_CLIENT_CONN=200
PGBOUNCER_DEFAULT_POOL_SIZE=25
# Note: Ensure prepared statements are disabled when running in transaction pooling mode.
```

```shell
# Fully qualified Postgres DSN for the hybrid-age provider:
HYBRID_AGE_DSN=postgres://hermes:<YOUR_PG_PASSWORD>@localhost:5440/hermes
# Phase 2 Embeddings: OpenAI API Key
OPENAI_API_KEY=sk-your-actual-api-key-here
```

# 11\. Appendix C: Memory Tuning Specification (Ranking & Budget)

Experimental methodology for tuning the ranking algorithm and token budget in the Hermes hybrid memory provider.

## 11.1 Part 1: Analyzing Ranking Weight Impact

The goal is to minimize the gap between Retrieval-hit rate and Injection-hit rate. The final rank of a candidate chunk/triple is calculated via a composite scoring function combining vector similarity (W\_sim), time decay (W\_recency), and graph importance (W\_graph).  
**Experimental Scenarios (Token Budget locked at 1200):**

| Profile | Wsim | Wrecency | Wgraph | Hypothesis |
| :---- | :---- | :---- | :---- | :---- |
| **Baseline (Vector Heavy)** | 0.8 | 0.1 | 0.1 | Traditional RAG behavior; graph triples likely rank low. |
| **Recency Heavy** | 0.3 | 0.6 | 0.1 | Best for highly conversational, state-dependent queries. |
| **Graph Heavy** | 0.3 | 0.1 | 0.6 | Prioritizes structural relationships; tests graph extraction. |
| **Balanced** | 0.4 | 0.3 | 0.3 | Recommended baseline; assumes equal signal value. |

## 11.2 Part 2: Exploring Token Budget Tuning

Goal: Find the optimal token cutoff maximizing Injection-hit rate without context window bloat. Context Efficiency is defined as Injection-Hit Rate divided by Token Budget in thousands.  
**Budget Scenarios:**

| Scenario | Budget | Expected Behavior |
| :---- | :---- | :---- |
| **Strict** | 500 tokens | High precision required; lower injection-hit rate. |
| **Standard** | 1200 tokens | Baseline balance between context size and recall. |
| **Generous** | 2000 tokens | Injection-hit rate plateaus toward Retrieval-hit rate. |
| **Unbounded** | Unlimited (9999) | Diagnostic mode where Injection-hit equals Retrieval-hit. |

# 12\. Appendix D: Evaluation Harness Source Code (hermes\_memory\_benchmarker.py)

Complete implementation of the benchmarking and metrics harness:

```py
iimport json
import time
import argparse
import statistics
import os
import sys
import uuid
from dataclasses import dataclass
from typing import List, Tuple
import logging

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    print("Error: psycopg2 is required. Run: pip install psycopg2-binary")
    exit(1)

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../src/hermes_memory')))
try:
    from config import load_config
    from embed import get_embedder
except ImportError:
    print("Error: Could not import config/embed from src/hermes_memory.")
    exit(1)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("hermes-bench")

@dataclass
class GoldenPair:
    id: str
    query: str
    expected_memory: str

@dataclass
class BenchConfig:
    name: str
    w_sim: float = 0.8
    w_recency: float = 0.1
    w_graph: float = 0.1
    max_tokens: int = 1200
    use_graph: bool = True
    min_sim: float = 0.55
    k: int = 12

@dataclass
class RunMetrics:
    config_name: str
    retrieval_hit_rate: float
    injection_hit_rate: float
    injection_mrr: float
    empty_block_rate: float
    p50_latency: float
    p95_latency: float
    p99_latency: float
    avg_tokens: float
    context_efficiency: float

class MetricsEvaluator:
    @staticmethod
    def normalize(text: str) -> str:
        return " ".join(text.lower().split())

    @staticmethod
    def is_match(expected: str, candidate_text: str) -> bool:
        norm_expected = MetricsEvaluator.normalize(expected)
        norm_candidate = MetricsEvaluator.normalize(candidate_text)
        return norm_expected in norm_candidate

    @staticmethod
    def calculate_mrr(expected: str, returned_block: str) -> float:
        if not returned_block.strip():
            return 0.0
        lines = returned_block.split('\n')
        rank = 1
        for line in lines:
            line = line.strip()
            if not line.startswith('-'):
                continue
            if MetricsEvaluator.is_match(expected, line):
                return 1.0 / rank
            rank += 1
        return 0.0

class ProviderAdapter:
    def __init__(self, dsn: str):
        self.dsn = dsn
        try:
            self.conn = psycopg2.connect(dsn)
            self.conn.autocommit = True
            with self.conn.cursor() as cur:
                cur.execute("LOAD 'age';")
                cur.execute("SET search_path = ag_catalog, \"$user\", public;")
        except Exception as e:
            logger.error(f"Failed to connect to Database: {e}")
            exit(1)
        self.plugin_config = load_config()
        self.embedder = get_embedder(self.plugin_config.embed)

    def debug_prefetch(self, query: str, config: BenchConfig) -> Tuple[str, List[str], int]:
        query_vec = self.embedder.embed_text(query)
        if not query_vec:
            return "", [], 0
        vec_str = "[" + ",".join(map(str, query_vec)) + "]"
        raw_candidates = []
        candidates_data = {}
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT id, chunk_text, 1 - (embedding <=> %s::vector) as sim_score
                FROM memory_entries
                WHERE 1 - (embedding <=> %s::vector) >= %s
                ORDER BY sim_score DESC
                LIMIT %s
            """, (vec_str, vec_str, config.min_sim, config.k))
            seed_ids = []
            for row in cur.fetchall():
                chunk_id, text, sim = row
                seed_ids.append(str(chunk_id))
                candidates_data[text] = {'sim': sim, 'graph': 0.0}
                if text not in raw_candidates:
                    raw_candidates.append(text)
            if config.use_graph and seed_ids:
                formatted_ids = ", ".join(f"'{sid}'" for sid in seed_ids)
                cypher_query = f"""
                    SELECT * FROM cypher('hermes_knowledge', $$
                    MATCH ()-[r1]->()-[r2]->()
                    WHERE r1.source_chunk IN [{formatted_ids}] AND r2.source_chunk IS NOT NULL
                    RETURN r2.source_chunk
                    $$) AS (chunk_id agtype);
                """
                try:
                    cur.execute(cypher_query)
                    graph_ids = [res[0].strip('"') for res in cur.fetchall() if res[0]]
                    if graph_ids:
                        cur.execute("""
                            SELECT id, chunk_text FROM memory_entries WHERE id::text = ANY(%s)
                        """, (graph_ids,))
                        for row in cur.fetchall():
                            _, text = row
                            if text not in candidates_data:
                                candidates_data[text] = {'sim': 0.3, 'graph': 1.0}
                                raw_candidates.append(text)
                            else:
                                candidates_data[text]['graph'] = 1.0
                except Exception:
                    pass
        scored_candidates = []
        for text, scores in candidates_data.items():
            final_score = (config.w_sim * scores['sim']) + (config.w_recency * 1.0) + (config.w_graph * scores['graph'])
            scored_candidates.append((final_score, text))
        scored_candidates.sort(key=lambda x: x[0], reverse=True)
        final_block = "Relevant memory context (hybrid vector + graph):\n"
        current_tokens = len(final_block) // 4
        for score, text in scored_candidates:
            line_tokens = len(text) // 4 + 2
            if current_tokens + line_tokens > config.max_tokens:
                break
            final_block += f"- {text}\n"
            current_tokens += line_tokens
        if current_tokens == len("Relevant memory context (hybrid vector + graph):\n") // 4:
            final_block = ""
        return final_block, raw_candidates, current_tokens

    def debug_sync_turn(self, chunk_text: str) -> float:
        start = time.perf_counter()
        query_vec = self.embedder.embed_text(chunk_text)
        if query_vec:
            vec_str = "[" + ",".join(map(str, query_vec)) + "]"
            with self.conn.cursor() as cur:
                try:
                    cur.execute("""
                        INSERT INTO memory_entries (id, chunk_text, embedding)
                        VALUES (%s, %s, %s::vector) ON CONFLICT DO NOTHING;
                    """, (str(uuid.uuid4()), chunk_text, vec_str))
                except Exception:
                    pass
        return (time.perf_counter() - start) * 1000

class Benchmarker:
    def __init__(self, golden_set: List[GoldenPair], dsn: str):
        self.golden_set = golden_set
        self.provider = ProviderAdapter(dsn)
        self.evaluator = MetricsEvaluator()

    def run_config(self, config: BenchConfig) -> RunMetrics:
        logger.info(f"Running config: {config.name} (Budget: {config.max_tokens}, Graph: {config.use_graph})")
        retrieval_hits, injection_hits, mrr_sum, empty_blocks = 0, 0, 0.0, 0
        latencies, tokens = [], []
        for pair in self.golden_set:
            start_time = time.perf_counter()
            final_block, raw_cands, toks = self.provider.debug_prefetch(pair.query, config)
            latency_ms = (time.perf_counter() - start_time) * 1000
            latencies.append(latency_ms)
            tokens.append(toks)
            if not final_block.strip(): empty_blocks += 1
            if any(self.evaluator.is_match(pair.expected_memory, c) for c in raw_cands): retrieval_hits += 1
            if self.evaluator.is_match(pair.expected_memory, final_block): injection_hits += 1
            mrr_sum += self.evaluator.calculate_mrr(pair.expected_memory, final_block)
        n = len(self.golden_set)
        latencies.sort()
        inj_rate = injection_hits / n
        budget_k = config.max_tokens / 1000.0
        efficiency = inj_rate / budget_k if budget_k > 0 else 0.0

        return RunMetrics(
            config_name=config.name, retrieval_hit_rate=retrieval_hits / n, injection_hit_rate=inj_rate,
            injection_mrr=mrr_sum / n, empty_block_rate=empty_blocks / n,
            p50_latency=latencies[int(n * 0.50)] if n > 0 else 0,
            p95_latency=latencies[int(n * 0.95)] if n > 0 else 0,
            p99_latency=latencies[int(n * 0.99)] if n > 0 else 0,
            avg_tokens=statistics.mean(tokens) if tokens else 0, context_efficiency=efficiency
        )

def print_markdown_report(metrics_list: List[RunMetrics], title: str):
    print(f"\n### {title}\n")
    print("| Configuration | Ret-Hit | Inj-Hit | MRR | Empty | p95 Latency | Avg Tokens | Efficiency |")
    print("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    for m in metrics_list:
        print(f"| **{m.config_name}** | {m.retrieval_hit_rate:.2f} | {m.injection_hit_rate:.2f} | "
              f"{m.injection_mrr:.2f} | {m.empty_block_rate:.2f} | {m.p95_latency:.1f}ms | "
              f"{m.avg_tokens:.0f} | {m.context_efficiency:.2f} |")
    print("\n")

def run_ablation(benchmarker: Benchmarker):
    base = BenchConfig("Hybrid (Graph ON)", use_graph=True)
    ablated = BenchConfig("Vector-only (Graph OFF)", use_graph=False)
    print_markdown_report([benchmarker.run_config(base), benchmarker.run_config(ablated)], "Ablation Study: Graph Expansion Impact")

def run_weight_optimization(benchmarker: Benchmarker):
    profiles = [
        BenchConfig("Baseline (Vector Heavy)", w_sim=0.8, w_recency=0.1, w_graph=0.1),
        BenchConfig("Recency Heavy", w_sim=0.3, w_recency=0.6, w_graph=0.1),
        BenchConfig("Graph Heavy", w_sim=0.3, w_recency=0.1, w_graph=0.6),
        BenchConfig("Balanced", w_sim=0.4, w_recency=0.3, w_graph=0.3),
    ]
    print_markdown_report([benchmarker.run_config(p) for p in profiles], "Ranking Weight Optimization")

def run_budget_optimization(benchmarker: Benchmarker):
    budgets = [500, 1000, 1200, 1500, 2000, 9999]
    results = [benchmarker.run_config(BenchConfig(f"Budget {b}" if b != 9999 else "Unbounded (9999)", max_tokens=b)) for b in budgets]
    print_markdown_report(results, "Token Budget Optimization")

def run_throughput_benchmark(benchmarker: Benchmarker, burst_size: int = 256):
    print(f"\n### Write-Path Throughput Benchmark\nSimulating a burst queue of {burst_size} pending turns...")
    latencies = []
    total_start = time.perf_counter()
    for i in range(burst_size):
        dummy_text = f"User said something interesting at turn {i} that needs to be durably stored."
        latency_ms = benchmarker.provider.debug_sync_turn(dummy_text)
        latencies.append(latency_ms)
        if (i + 1) % 50 == 0: print(f"  ... Drained {i + 1}/{burst_size} items")
    total_time = time.perf_counter() - total_start
    latencies.sort()
    print("\n**Throughput Results:**")
    print(f"- **Total Time:** {total_time:.2f} seconds")
    print(f"- **Sustained Rate:** {(burst_size / total_time):.1f} turns/sec")
    print(f"- **p50 Latency:** {latencies[int(burst_size * 0.50)]:.2f} ms")
    print(f"- **p95 Latency:** {latencies[int(burst_size * 0.95)]:.2f} ms")

def main():
    parser = argparse.ArgumentParser(description="Hermes Memory Benchmarker (Hybrid pgvector + AGE)")
    parser.add_argument("--golden-set", type=str, help="Path to golden set JSON file")
    parser.add_argument("--dsn", type=str, help="Postgres DSN override")
    parser.add_argument("--ablation", action="store_true", help="Run Graph vs Vector-only ablation")
    parser.add_argument("--optimize-weights", action="store_true", help="Run ranking weight optimization profiles")
    parser.add_argument("--optimize-budget", action="store_true", help="Run token budget scaling tests")
    parser.add_argument("--throughput", action="store_true", help="Run write-path throughput/queue burst benchmark")
    args = parser.parse_args()
    db_dsn = args.dsn or os.environ.get("HYBRID_AGE_DSN", "postgres://hermes:<YOUR_PG_PASSWORD>@localhost:5440/hermes")
    if args.golden_set:
        with open(args.golden_set, 'r') as f:
            golden_set = [GoldenPair(**item) for item in json.load(f)]
    else:
        logger.warning("No --golden-set provided. Exiting.")
        return
    benchmarker = Benchmarker(golden_set, db_dsn)
    ran_something = False
    if args.ablation: run_ablation(benchmarker); ran_something = True
    if args.optimize_weights: run_weight_optimization(benchmarker); ran_something = True
    if args.optimize_budget: run_budget_optimization(benchmarker); ran_something = True
    if args.throughput: run_throughput_benchmark(benchmarker); ran_something = True
    if not ran_something: run_ablation(benchmarker)

if __name__ == "__main__":
    main()

```

## 12.1 Synthetic Load Generator (scripts/synthetic\_load\_generator.py)

Utility script for injecting synthetic records and graph relationships to test scaling behavior:

{% raw %}
```py
import os
import random
import argparse
import uuid
from typing import List, Tuple

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    print("Error: psycopg2 is required. Run: pip install psycopg2-binary")
    exit(1)

TECH_WORDS = ["AuthModule", "RateLimiter", "RedisCache", "PostgresDB", "UserSession", 
              "HermesCore", "GraphExtractor", "VectorStore", "Embedder", "APIWorker"]
ACTION_WORDS = ["DEPENDS_ON", "CONNECTS_TO", "MIGRATES", "READS_FROM", "WRITES_TO"]

def generate_random_vector(dim: int = 768) -> str:
    vec = [random.uniform(-1.0, 1.0) for _ in range(dim)]
    magnitude = sum(x**2 for x in vec) ** 0.5
    normalized = [x / magnitude for x in vec]
    return "[" + ",".join(f"{x:.6f}" for x in normalized) + "]"

def generate_chunk_text(entity1: str, action: str, entity2: str) -> str:
    templates = [
        f"In the latest architecture review, we decided that {entity1} {action} {entity2}.",
        f"User mentioned having issues when {entity1} {action} {entity2}.",
        f"Note: ensure {entity1} {action} {entity2} before deployment."
    ]
    return random.choice(templates)

def run_synthetic_load(dsn: str, target_size: int, batch_size: int = 500):
    print(f"Connecting to database to inject {target_size} records...")
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute("LOAD 'age';")
        cur.execute("SET search_path = ag_catalog, \"$user\", public;")
        inserted = 0
        while inserted < target_size:
            current_batch_size = min(batch_size, target_size - inserted)
            vector_records: List[Tuple[str, str, str]] = []
            graph_queries: List[str] = []
            for _ in range(current_batch_size):
                chunk_id = str(uuid.uuid4())
                e1 = random.choice(TECH_WORDS) + f"_{random.randint(1, target_size // 10 + 1)}"
                e2 = random.choice(TECH_WORDS) + f"_{random.randint(1, target_size // 10 + 1)}"
                action = random.choice(ACTION_WORDS)
                text = generate_chunk_text(e1, action, e2)
                vec_str = generate_random_vector(768)
                vector_records.append((chunk_id, text, vec_str))
                cypher = f"""
                SELECT * FROM cypher('hermes_knowledge', $$
                    MERGE (a:Entity {{name: '{e1}'}})
                    SET a.last_seen = timestamp()
                    MERGE (b:Entity {{name: '{e2}'}})
                    SET b.last_seen = timestamp()
                    MERGE (a)-[r:{action}]->(b)
                    SET r.source_chunk = '{chunk_id}'
                    RETURN a, b
                $$) AS (a agtype, b agtype);
                """
                graph_queries.append(cypher)
            insert_sql = """
                INSERT INTO memory_entries (id, chunk_text, embedding) 
                VALUES %s ON CONFLICT (id) DO NOTHING;
            """
            execute_values(cur, insert_sql, vector_records)
            for cq in graph_queries:
                cur.execute(cq)
            conn.commit()
            inserted += current_batch_size
            print(f"Progress: {inserted} / {target_size} records injected.")
        print("\n✅ Synthetic load complete!")
    except Exception as e:
        print(f"\n❌ Error during load generation: {e}")
        conn.rollback()
    finally:
        if conn:
            cur.close()
            conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate synthetic load for Hermes Postgres/AGE memory.")
    parser.add_argument("--size", type=int, choices=[1000, 10000, 100000], default=1000, help="Target size of synthetic corpus.")
    parser.add_argument("--dsn", type=str, help="Postgres DSN.")
    args = parser.parse_args()
    db_dsn = args.dsn or os.environ.get("HYBRID_AGE_DSN", "postgres://hermes:<YOUR_PG_PASSWORD>@localhost:5440/hermes")
    if db_dsn == "<YOUR_PG_PASSWORD>":
        print("❌ Error: HYBRID_AGE_DSN is set to a bare password, not a valid DSN string.")
        exit(1)
    run_synthetic_load(db_dsn, args.size)
```
{% endraw %}

## 12.2 Configuration Module (src/hermes\_memory/config.py)

Configuration loader and data classes for memory provider settings:

```py
import os
from dataclasses import dataclass
import logging

logger = logging.getLogger("hermes-memory-config")

@dataclass
class EmbedConfig:
    provider: str
    model: str
    dimensions: int
    openai_api_key: str
    ollama_url: str

@dataclass
class DBConfig:
    dsn: str
    graph_name: str

@dataclass
class MemoryConfig:
    embed: EmbedConfig
    db: DBConfig
    token_budget: int = 1200

def load_config() -> MemoryConfig:
    dsn = os.environ.get("HYBRID_AGE_DSN", "postgres://hermes:password@localhost:5440/hermes")
    graph_name = os.environ.get("HYBRID_AGE_GRAPH", "hermes_knowledge")
    provider = os.environ.get("HERMES_EMBED_PROVIDER", "openai").lower()
    if provider == "openai":
        model = os.environ.get("HERMES_EMBED_MODEL", "text-embedding-3-small")
    else:
        model = os.environ.get("HERMES_EMBED_MODEL", "nomic-embed-text")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/embeddings")
    dimensions = 768
    if provider == "openai" and not api_key:
        logger.warning("OPENAI_API_KEY is not set. OpenAI embeddings will fail.")
    return MemoryConfig(
        embed=EmbedConfig(
            provider=provider,
            model=model,
            dimensions=dimensions,
            openai_api_key=api_key,
            ollama_url=ollama_url
        ),
        db=DBConfig(dsn=dsn, graph_name=graph_name)
    )
```

## 12.3 Embeddings Client (src/hermes\_memory/embed.py)

Embedding abstraction layer supporting primary OpenAI and fallback Ollama backends:

```py
import json
import urllib.request
import urllib.error
import logging
from typing import List, Optional
from config import load_config, EmbedConfig

logger = logging.getLogger("hermes-memory-embed")

class Embedder:
    def embed_text(self, text: str) -> Optional[List[float]]:
        raise NotImplementedError

class OpenAIEmbedder(Embedder):
    def __init__(self, config: EmbedConfig):
        self.api_key = config.openai_api_key
        self.model = config.model
        self.dimensions = config.dimensions
        self.endpoint = "https://api.openai.com/v1/embeddings"

    def embed_text(self, text: str) -> Optional[List[float]]:
        if not self.api_key:
            logger.error("OpenAI API key missing. Cannot generate embeddings.")
            return None
        payload = json.dumps({
            "model": self.model,
            "input": text,
            "dimensions": self.dimensions
        }).encode('utf-8')
        req = urllib.request.Request(self.endpoint, data=payload)
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                result = json.loads(response.read().decode('utf-8'))
                vector = result['data'][0]['embedding']
                if len(vector) != self.dimensions:
                    logger.error(f"Dimension mismatch! Expected {self.dimensions}, got {len(vector)}")
                    return None
                return vector
        except urllib.error.URLError as e:
            logger.error(f"OpenAI Embedding failed: {e}")
            return None

class OllamaEmbedder(Embedder):
    def __init__(self, config: EmbedConfig):
        self.url = config.ollama_url
        self.model = config.model
        self.dimensions = config.dimensions

    def embed_text(self, text: str) -> Optional[List[float]]:
        payload = json.dumps({"model": self.model, "prompt": text}).encode('utf-8')
        req = urllib.request.Request(self.url, data=payload)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode('utf-8'))
                vector = result.get('embedding')
                if not vector:
                    logger.error("Ollama returned empty embedding.")
                    return None
                if len(vector) != self.dimensions:
                    logger.error(f"Ollama dimension mismatch! Expected {self.dimensions}, got {len(vector)}")
                    return None
                return vector
        except urllib.error.URLError as e:
            logger.error(f"Ollama Embedding failed: {e}")
            return None

def get_embedder(config: EmbedConfig) -> Embedder:
    if config.provider == "openai":
        logger.info(f"Using OpenAI embedder ({config.model} @ {config.dimensions}d)")
        return OpenAIEmbedder(config)
    elif config.provider == "ollama":
        logger.info(f"Using Ollama embedder ({config.model} @ {config.dimensions}d)")
        return OllamaEmbedder(config)
    else:
        logger.error(f"Unknown embed provider: {config.provider}. Falling back to Ollama.")
        return OllamaEmbedder(config)
```

            p95\_latency=latencies\[int(n \* 0.95)\] if n \> 0 else 0,

# 13\. Telemetry, Logging, & Alerting Strategy

To ensure operational observability and maintain system health across vector and graph search paths, Hermes memory provider enforces structured logging, mandatory tracing context, and critical operational alerts.

## 13.1 Structured Logging & Tracing Context

All log records emitted by the provider, background extraction worker, and evaluation harnesses must emit structured JSON payloads under dedicated namespaces. Every request lifecycle must carry propagating correlation IDs to link prompt execution turns directly to vector and Cypher query operations.

* **Logging Namespaces:** Loggers are segmented into hermes\_memory.provider, hermes\_memory.embed, hermes\_memory.graph, and hermes\_memory.worker.  
* **Mandatory Tracing Fields:** Every telemetry event and log statement must capture trace\_id, turn\_id, user\_id, and provider\_id.

## 13.2 Health Metrics & Alert Thresholds

Prometheus-compatible metrics monitor queue backpressure, execution latency breaches, and prefetch degradation:

| Metric Name | Alert Threshold / Condition | Severity & Action |
| :---- | :---- | :---- |
| hermes\_queue\_depth | \> 1,000 pending items or lag \> 300s | P2 Warning: Scale extraction workers |
| hermes\_prefetch\_latency\_ms | p95 \> 200ms or p99 \> 500ms | P1 Critical: Trigger graph circuit breaker |
| hermes\_empty\_context\_rate | \> 15% over a 5-minute window | P2 Warning: Inspect embedding client health |

# 14\. CI/CD & Verification Pipeline

Automated deployment gates prevent retrieval regressions and maintain accuracy before changes reach production environments.

## 14.1 Golden Set Regression Protections

The version-controlled evaluation dataset (data/golden\_set.json) serves as the primary verification baseline. Every pull request modifying database schemas, query logic, or ranking algorithms executes automated regression passes using hermes\_memory\_benchmarker.py.

## 14.2 Automated Deployment Gates

Release promotion requires passing non-negotiable verification gates in the CI workflow:

* **Injection-Hit Rate Floor:** Injection-hit rate must remain \>= 0.85 across the standard golden set.  
* **MRR Threshold:** Injection MRR must not degrade by more than 0.05 compared to the baseline commit.  
* **Latency Gate:** Prefetch p95 latency must remain below 200ms on synthetic test corpora (10k rows).

# 15\. Disaster Recovery (DR) & Data Retention

Data lifecycle policies and emergency operational controls safeguard system stability and compliance.

## 15.1 Retention & Cleanup Policies

Raw conversational memory chunks stored in memory\_entries are subject to a default retention window of 180 days unless flagged for permanent storage. Scheduled background vacuum and purge jobs purge expired vector entries along with cascading deletes on graph bridge entries in memory\_chunk\_nodes.

## 15.2 Emergency Break-Glass Rollback

In the event of database cluster failure, severe latency degradation, or graph corruption, operators can trigger an immediate rollback without restarting application instances:

* **Disable Prefetch:** Update configuration setting to memory.provider: '' or export HERMES\_MEMORY\_DISABLE=true.  
* **Fallback Operation:** Hermes instantly falls back to built-in local memory storage while underlying PostgreSQL and Apache AGE components are restored or repaired.

            p99\_latency=latencies\[int(n \* 0.99)\] if n \> 0 else 0,  
