# Defense Project MCP Servers

## 1. Overview
The Defense platform exposes its data stores to AI agents via 3 FastMCP servers implementing the Model Context Protocol (MCP). Each server runs as an independent process, exposing typed tools over Streamable HTTP transport. The agent orchestrator (`runner.py`) connects to them via an MCP client.

| Server | Port | Backing Store | Source |
|---|---|---|---|
| `analytics-mcp` | 8110 | ClickHouse + Postgres | `src/defense/mcp_servers/analytics_mcp/server.py` |
| `retrieval-mcp` | 8101 | PostgreSQL + pgvector | `src/defense/mcp_servers/retrieval_mcp/server.py` |
| `ingest-mcp` | 8102 | Redis Streams | `src/defense/mcp_servers/ingest_mcp/server.py` |

All servers:
- Use FastMCP framework with streamable-HTTP transport
- Mount MCP endpoint at `/mcp`
- Expose `/health` endpoint for liveness checks
- Support stub mode for development without real data stores
- Are ClusterIP-only in Kubernetes (never externally exposed)
- Use structlog JSON logging

### Architecture

```mermaid
%%{init: {'theme':'base','themeVariables':{'fontFamily':'ui-sans-serif, system-ui, -apple-system, Roboto, Helvetica, Arial, sans-serif','fontSize':'14px','primaryColor':'#2f4468','primaryTextColor':'#eef2f8','primaryBorderColor':'#5b7bb5','secondaryColor':'#14564f','secondaryTextColor':'#eef2f8','secondaryBorderColor':'#2c9d8f','tertiaryColor':'#3d2f63','tertiaryTextColor':'#eef2f8','tertiaryBorderColor':'#8b6fd4','mainBkg':'#2f4468','nodeBorder':'#5b7bb5','nodeTextColor':'#eef2f8','lineColor':'#8fa1bd','textColor':'#eef2f8','titleColor':'#c9d6ea','clusterBkg':'#161e2e','clusterBorder':'#3f5573','edgeLabelBackground':'#1b2434','background':'transparent'}, 'flowchart':{'curve':'basis','padding':14,'nodeSpacing':45,'rankSpacing':55,'useMaxWidth':true}}}%%
graph TD
    Agent["Agent orchestrator<br/><small>services/agents/runner.py</small>"]
    Client["MCP client<br/><small>services/agents/mcp_client.py</small>"]

    Agent --> Client

    subgraph servers["FastMCP servers — 18 tools"]
        Analytics["analytics-mcp :8110<br/><small>6 tools</small>"]
        Retrieval["retrieval-mcp :8101<br/><small>9 tools</small>"]
        Ingest["ingest-mcp :8102<br/><small>3 tools</small>"]
    end

    Client -- "Streamable HTTP" --> Analytics
    Client -- "Streamable HTTP" --> Retrieval
    Client -- "Streamable HTTP" --> Ingest

    subgraph stores["Data stores"]
        ClickHouse[("ClickHouse<br/><small>analysis_events · comment_sentiments</small>")]
        Postgres[("PostgreSQL + pgvector<br/><small>analysis_results · comment_embeddings</small>")]
        Redis[("Redis streams<br/><small>ingestion:queue</small>")]
    end

    Analytics --> ClickHouse
    Analytics --> Postgres
    Retrieval --> Postgres
    Ingest --> Redis

    classDef entry fill:#3d2f63,stroke:#8b6fd4,stroke-width:1.5px,color:#eef2f8
    classDef svc fill:#2f4468,stroke:#5b7bb5,stroke-width:1.5px,color:#eef2f8
    classDef store fill:#14564f,stroke:#2c9d8f,stroke-width:1.5px,color:#eef2f8
    classDef tool fill:#1b2434,stroke:#5b7bb5,stroke-width:1px,color:#c9d6ea
    classDef obs fill:#5a3410,stroke:#c9772e,stroke-width:1.5px,color:#f6e6d5

    class Agent entry
    class Client,Analytics,Retrieval,Ingest svc
    class ClickHouse,Postgres,Redis store
```

## 2. Analytics MCP Server (Port 8110)

### Environment Variables
- `ANALYTICS_MCP_STUB` — `true` to return synthetic data without real DB (dev mode)
- `CLICKHOUSE_HOST` — ClickHouse host (default: `clickhouse`)
- `CLICKHOUSE_PORT` — Native TCP port (default: `9000`)
- `CLICKHOUSE_DB` — Database name (default: `defense`)
- `CLICKHOUSE_USER` — (default: `default`)
- `CLICKHOUSE_PASSWORD` — (default: `""`)

### Tools (6 total)

#### `trend_query`
- **Purpose:** Aggregate sentiment and post volume over time buckets
- **Parameters:**
  - `campaign_id` (str, optional): Filter to campaign
  - `from_date` (str, optional): Start date YYYY-MM-DD
  - `to_date` (str, optional): End date YYYY-MM-DD
  - `granularity` (str): `day` | `week` | `month`
- **Returns:** List of dicts with `period`, `count`, `avg_sentiment`, `avg_toxicity`
- **Note:** This is the ONLY source for toxicity threshold data; `avg_toxicity` column is critical for the alerting agent

#### `sentiment_over_time`
- **Purpose:** Daily/weekly sentiment trajectory per campaign
- **Parameters:** Same as trend_query (campaign_id, from_date, to_date, granularity)
- **Returns:** List of dicts with `period`, `positive`, `negative`, `neutral`, `mixed` (POST COUNTS)
- **Note:** No toxicity field — returns sentiment category counts, not scores

#### `top_posts`
- **Purpose:** Rank posts by engagement, reaction volume, or toxicity
- **Parameters:**
  - `campaign_id` (str, optional)
  - `metric` (str): `total_reactions` | `comment_count` | `toxicity_score` | `hate_speech_score`
  - `limit` (int): Max results (default 10)
- **Returns:** List of post records with post_id, metrics, overall_sentiment
- **Note:** The ONLY tool that returns `post_id` values — agents must use this to cite specific posts

#### `reaction_mix`
- **Purpose:** Aggregate platform reactions (LIKE, LOVE, SAD, ANGRY, HAHA, WOW)
- **Parameters:** `campaign_id` (str, optional)
- **Returns:** Dict with reaction type counts and percentages

#### `watchlist_timeline`
- **Purpose:** Track watchlist entity mentions over time
- **Parameters:**
  - `campaign_id` (str, optional)
  - `from_date`, `to_date` (str, optional)
  - `granularity` (str): `day` | `week` | `month`
- **Returns:** Time series of watchlist hits per period

#### `agreement_stats`
- **Purpose:** Query ClickHouse `comment_sentiments` for ensemble agreement distributions
- **Parameters:** `campaign_id` (str, optional)
- **Returns:** Dict with `unanimous_share`, `abstained_share`, `single_voter_share`, `total_comments`, plus per-voter breakdown
- **Backed by:** ClickHouse `comment_sentiments` table

### ClickHouse Tables Queried
- `analysis_events` — Post-level analysis events with sentiment, toxicity, engagement metrics
- `comment_sentiments` — Per-comment ensemble voter results

### Stub Mode
When `ANALYTICS_MCP_STUB=true`:
- Returns synthetic deterministic data without DB connection
- Generates realistic time series, reaction mixes, and agreement stats
- Useful for development and testing without ClickHouse

## 3. Retrieval MCP Server (Port 8101)

### Environment Variables
- `DATABASE_URL` — PostgreSQL async connection string (default: `postgresql+asyncpg://defense:defense@localhost:5432/defense`)
- `RETRIEVAL_MCP_STUB` — `true` to skip vector search
- `MODEL_STUB_MODE` — `true`/`false` for query embedding generation
- `EMBEDDING_MODEL` — SentenceTransformer name (must match `analysis_results.embedding` column dimension, 768-dim)

### Tools (9 total)

#### `semantic_search`
- **Purpose:** 3-arm hybrid retrieval with RRF fusion
- **Search Arms:**
  1. **Dense vector:** pgvector cosine similarity on `analysis_results.embedding` (768-dim)
  2. **Chunk vector:** pgvector cosine on `post_chunks.embedding` for sub-document retrieval
  3. **Lexical:** PostgreSQL full-text search (`to_tsvector`) + `pg_trgm` trigram similarity
- **Fusion:** Reciprocal Rank Fusion (RRF) combining all arms
- **Parameters:**
  - `query` (str): Search text (English/Bangla)
  - `campaign_id` (str, optional): Filter to campaign
  - `limit` (int): Max results (default 10)
  - `tenant_id` (str, optional): Tenant isolation
- **Returns:** List of post results with similarity scores, match types, summaries

#### `search_comments`
- **Purpose:** Semantic and lexical search over comment embeddings
- **Search Arms:** Comment vector search + comment text term matching + trigram similarity
- **Parameters:** `query`, `campaign_id`, `limit`, `tenant_id`
- **Returns:** List of comments with post_id, comment_id, text, sentiment, similarity score

#### `get_post`
- **Purpose:** Fetch complete canonical analysis JSON for a single post
- **Parameters:** `post_id` (str), `tenant_id` (str, optional)
- **Returns:** Full analysis result including sentiment, toxicity, summary, entities, topics, engagement

#### `get_thread`
- **Purpose:** Fetch post caption + complete comment list ordered by engagement
- **Parameters:** `post_id` (str), `limit` (int, optional), `tenant_id` (str, optional)
- **Returns:** Post caption + ordered comment list with text, sentiment, reactions

#### `representative_comments`
- **Purpose:** Fetch most-liked and representative comments for a post
- **Parameters:** `post_id` (str), `limit` (int, optional), `tenant_id` (str, optional)
- **Returns:** Top comments by reaction count

#### `get_clusters`
- **Purpose:** Run k-means/HDBSCAN clustering over post embedding vectors
- **Parameters:** `campaign_id` (str, optional), `n_clusters` (int, optional), `tenant_id` (str, optional)
- **Returns:** List of clusters with:
  - `label` — Human-reviewed name (if persisted)
  - `representative_summary` — Summary of central post
  - `posts_clustered` — Count of posts in cluster
  - `scan_truncated` — Whether the full corpus was clustered
  - `is_stub` — Whether vectors are deterministic hashes (groupings arbitrary)
  - `dominant_sentiment` — Most common sentiment in cluster

#### `stance_by_target`
- **Purpose:** Entity stance breakdown from `target_stances` column
- **Parameters:** `target_id` (str, optional — call with NO target_id first to discover available targets), `campaign_id` (str, optional)
- **Returns:** Per-target stance distribution: supportive %, opposing %, neutral %, post count

#### `stance_over_time`
- **Purpose:** Entity stance time series
- **Parameters:** `target_id` (str, optional), `campaign_id` (str, optional), `from_date`, `to_date`, `granularity`
- **Returns:** Time series of stance distributions per period

#### `coverage_stats`
- **Purpose:** Calculate analyzed vs total post/comment coverage and vector provenance
- **Parameters:** `campaign_id` (str, optional), `tenant_id` (str, optional)
- **Returns:** Dict with:
  - `total_posts`, `analyzed_posts`, `coverage_pct`
  - `total_comments`, `analyzed_comments`
  - `stub_embeddings`, `stub_embedding_share` — Deterministic hash vectors count
  - `embedding_models` — List of embedding model names in the index
  - `comment_vector_coverage` — Fraction of comments with vectors
  - `posts_over_comment_cap`, `comments_dropped_by_cap` — Retrieval blind spot metrics

### PostgreSQL Tables/Views Queried
- `analysis_results` — Main analysis results with 768-dim `embedding` vector column
- `post_chunks` — Sub-document chunks with embedding vectors
- `comments` — Comment text, sentiment, engagement
- `comment_embeddings` — Per-comment 768-dim vectors
- `cluster_labels` — Persisted human-reviewed cluster labels

### Stub Mode
When `RETRIEVAL_MCP_STUB=true`:
- `semantic_search` returns first N rows from `analysis_results` (no vector math)
- All other tools still hit PostgreSQL normally

## 4. Ingest MCP Server (Port 8102)

### Architecture Invariant
> **These tools trigger pulls from the upstream post-with-details API and write ONLY to our own database (Postgres + Redis queue). We NEVER write back to upstream.**

### Environment Variables
- `REDIS_URL` — Redis connection (default: `redis://localhost:6379/0`)

### Tools (3 total)

#### `pull_campaign`
- **Purpose:** Queue a campaign date-range pull from upstream API
- **Parameters:**
  - `campaign_id` (str): Campaign CUID
  - `from_date` (str): Start date YYYY-MM-DD
  - `to_date` (str): End date YYYY-MM-DD
  - `limit` (int): Max posts (1-1000, default 100)
- **Redis Stream:** `ingestion:queue`
- **Returns:** `{ job_id, status: 'queued', campaign_id, from_date, to_date, limit }`

#### `fetch_more_comments`
- **Purpose:** Queue deeper comment pull for an under-covered post
- **Parameters:**
  - `post_id` (str): Post CUID
  - `limit` (int): Max comments (1-500, default 100)
- **Redis Stream:** `ingestion:fetch_comments`
- **Returns:** `{ job_id, post_id, status: 'queued', limit }`

#### `refresh_post`
- **Purpose:** Queue re-fetch of single post from upstream
- **Parameters:** `post_id` (str): Post CUID
- **Redis Stream:** `ingestion:refresh`
- **Returns:** `{ job_id, post_id, status: 'queued' }`

All tools return immediately with `status: 'queued'` — actual upstream fetch is async by the ingestion worker.

## 5. Agent-to-Tool Mapping

| Tool | analyst | coverage | alerting | stance | comparator | toxicity | narrative | quality | reporter |
|---|---|---|---|---|---|---|---|---|---|
| trend_query | ✅ | | ✅ | | ✅ | ✅ | ✅ | | ✅ |
| sentiment_over_time | ✅ | | ✅ | | ✅ | | | | ✅ |
| top_posts | ✅ | ✅ | ✅ | | ✅ | ✅ | ✅ | ✅ | ✅ |
| reaction_mix | ✅ | | | | ✅ | | | | ✅ |
| semantic_search | ✅ | | | ✅ | ✅ | ✅ | ✅ | | ✅ |
| search_comments | ✅ | | | ✅ | | ✅ | ✅ | | |
| get_post | ✅ | ✅ | | ✅ | | | ✅ | ✅ | ✅ |
| get_thread | ✅ | | | ✅ | | ✅ | | | |
| representative_comments | ✅ | | | ✅ | | ✅ | | | ✅ |
| get_clusters | | | | | | | ✅ | | |
| stance_by_target | | | | ✅ | | | | | |
| stance_over_time | | | | ✅ | | | | | |
| coverage_stats | | | | | | | | ✅ | |
| agreement_stats | | | | | | | | ✅ | |
| fetch_more_comments | | ✅ | | | | | | | |
| watchlist_timeline | | | | | | | | | |

## 6. Security & Multi-Tenancy
- All tools propagate `tenant_id` for data isolation
- MCP servers are ClusterIP-only (never publicly exposed)
- Strict input validation on all parameters
- SQL injection prevention via parameterized queries
- Campaign/post ID validation before queries

## 7. Deployment

### Docker Compose
Each MCP server runs as a separate container:
```yaml
analytics-mcp:  # port 8110
retrieval-mcp:  # port 8101  
ingest-mcp:     # port 8102
```

### Kubernetes
Manifest: `deploy/k8s/mcp-services.yaml`
- 3 Deployments (one per server)
- ClusterIP Services
- Resource limits configured per server
- Liveness/readiness probes on `/health`

### Local Development
Started by `run_all.py --with-agents`:
- Each server started as a subprocess
- Stub mode environment variables configured automatically
- Health checks verified before agent orchestrator starts

## 8. Stub vs Real Mode

| Server | Stub Env Var | Behavior in Stub Mode |
|---|---|---|
| analytics-mcp | `ANALYTICS_MCP_STUB=true` | Returns synthetic data, no ClickHouse needed |
| retrieval-mcp | `RETRIEVAL_MCP_STUB=true` | Skips vector search, returns first N rows; other tools still hit Postgres |
| ingest-mcp | N/A | Always needs Redis (enqueues jobs) |

## Tool Invocation Flow

```mermaid
%%{init: {'theme':'base','themeVariables':{'fontFamily':'ui-sans-serif, system-ui, -apple-system, Roboto, Helvetica, Arial, sans-serif','fontSize':'14px','primaryColor':'#2f4468','primaryTextColor':'#eef2f8','primaryBorderColor':'#5b7bb5','lineColor':'#8fa1bd','textColor':'#eef2f8','actorBkg':'#2f4468','actorBorder':'#5b7bb5','actorTextColor':'#eef2f8','actorLineColor':'#8fa1bd','signalColor':'#a9bcd8','signalTextColor':'#c9d6ea','labelBoxBkgColor':'#3d2f63','labelBoxBorderColor':'#8b6fd4','labelTextColor':'#eef2f8','loopTextColor':'#c9d6ea','activationBkgColor':'#14564f','activationBorderColor':'#2c9d8f','noteBkgColor':'#3f3312','noteBorderColor':'#c99a2e','noteTextColor':'#f5ead1','sequenceNumberColor':'#0d1117','background':'transparent'}, 'sequence':{'useMaxWidth':true,'mirrorActors':false,'boxMargin':12,'messageAlign':'center','actorFontFamily':'ui-sans-serif, system-ui, -apple-system, Roboto, Helvetica, Arial, sans-serif','messageFontFamily':'ui-sans-serif, system-ui, -apple-system, Roboto, Helvetica, Arial, sans-serif','noteFontFamily':'ui-sans-serif, system-ui, -apple-system, Roboto, Helvetica, Arial, sans-serif','actorFontSize':14,'messageFontSize':13,'noteFontSize':13}}}%%
sequenceDiagram
    autonumber
    participant Agent as Agent orchestrator
    participant MCP_Client as MCP client
    participant Server as FastMCP server
    participant DB as Data store

    Agent->>MCP_Client: Call tool (e.g. top_posts)
    MCP_Client->>Server: Streamable HTTP request
    Server->>Server: Validate parameters + scope to tenant
    alt Parameters valid
        Server->>DB: Execute query / action
        DB-->>Server: Rows
        Server-->>MCP_Client: Result payload
    else Placeholder or unscoped id
        Server-->>MCP_Client: ValueError — "not a post id"
        Note over Server,MCP_Client: Refusing beats returning zero rows:<br/>an empty result teaches the model<br/>the corpus is empty.
    end
    MCP_Client-->>Agent: Tool result string
```

---

## Related documents

- [AGENTS.md](AGENTS.md) — the nine agents, their per-agent tool allowlists and budgets
- [SEARCH.md](SEARCH.md) — the retrieval arms behind `semantic_search` / `search_comments`, and the measured recall@k table
- [RAG_STATE_AND_ROADMAP.md](RAG_STATE_AND_ROADMAP.md) — what the retrieval layer stores, and the audit sections
- [ASSEMBLER.md](ASSEMBLER.md) §3 — the ClickHouse tables `analytics-mcp` queries, and why the reaction columns live on the post row
- [LLM_BACKENDS.md](LLM_BACKENDS.md) — the `agent` role these tools are called from
