-- Defense platform database initialization

-- pgvector extension — provides the `vector` type used for semantic search.
-- Replaces the standalone Qdrant service: embeddings live alongside the
-- canonical result in analysis_results.embedding.
CREATE EXTENSION IF NOT EXISTS vector;

-- Tenant Isolation Migrations & Backfill Policy
-- Existing rows in posts, analysis_results, and jobs are backfilled to 'default' tenant.
-- Future rows acquire tenant_id explicitly from current_user / job envelope.

CREATE TABLE IF NOT EXISTS campaigns (
    id                VARCHAR PRIMARY KEY,
    tenant_id         VARCHAR NOT NULL DEFAULT 'default',
    name              VARCHAR,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS posts (
    id                VARCHAR PRIMARY KEY,
    campaign_id       VARCHAR,
    tenant_id         VARCHAR NOT NULL DEFAULT 'default',
    platform          VARCHAR,
    platform_post_id  VARCHAR,
    url               TEXT,
    media_type        VARCHAR,
    created_at        TIMESTAMPTZ,
    scraped_at        TIMESTAMPTZ,
    raw_payload       JSONB,
    content_hash      VARCHAR UNIQUE,
    status            VARCHAR DEFAULT 'pending',
    inserted_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS analysis_results (
    id              SERIAL PRIMARY KEY,
    post_id         VARCHAR REFERENCES posts(id),
    campaign_id     VARCHAR,
    tenant_id       VARCHAR NOT NULL DEFAULT 'default',
    result          JSONB NOT NULL,
    embedding       vector(768),   -- pgvector: semantic-search vector (was Qdrant)
    -- TRUE when `embedding` is the deterministic hash-seeded stub rather than a
    -- semantic vector (PROJECT_ASSESSMENT §5.9). kNN over stub rows returns
    -- arbitrary neighbours, and the row is otherwise indistinguishable from a
    -- real one — so search and report paths must be able to disclose it.
    embedding_is_stub BOOLEAN DEFAULT FALSE,
    schema_version  VARCHAR,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (post_id)
);

-- Flat comment rows (populated by the ingestion service from each post's
-- comment list; queried by retrieval-mcp get_thread / representative_comments).
CREATE TABLE IF NOT EXISTS comments (
    id          SERIAL PRIMARY KEY,
    comment_id  VARCHAR NOT NULL,            -- upstream comment id
    post_id     VARCHAR REFERENCES posts(id),
    text        TEXT,
    author      VARCHAR,
    likes       INTEGER DEFAULT 0,
    sentiment   VARCHAR,                     -- null at ingestion; NLP fills later
    created_at  TIMESTAMPTZ,
    UNIQUE (post_id, comment_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    id          VARCHAR PRIMARY KEY,
    tenant_id   VARCHAR NOT NULL DEFAULT 'default',
    type        VARCHAR,
    status      VARCHAR DEFAULT 'pending',
    selector    JSONB,
    options     JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    error       TEXT
);

-- There is deliberately no `llm_cache` table here.
--
-- The Stage-2 LLM response cache is Redis-only: keys of the form
-- `llm_cache:{backend}:{model}:{task}:{content_hash}` with a 7-day TTL, written
-- by services/workers/stage2_llm/cache.py. A Postgres table of the same name
-- used to be created here and read by GET /v1/usage, but nothing ever wrote it,
-- so it reported 0 tokens and 0 cache rows forever while the endpoint's docs
-- named it as a source. Dropped rather than left as a shape nothing fills.
--
-- Existing deployments can clean it up with:  DROP TABLE IF EXISTS llm_cache;

CREATE TABLE IF NOT EXISTS tenant_policies (
    tenant_id      VARCHAR PRIMARY KEY,
    llm_backend    VARCHAR DEFAULT 'local',
    privacy_locked BOOLEAN DEFAULT FALSE,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

-- API keys, stored as SHA-256 hashes (PROJECT_ASSESSMENT §5.6 / P1.1).
--
-- Previously ANY non-empty key authenticated and carried no tenant, so
-- check_llm_backend_policy resolved every API-key caller to tenant "default" —
-- a tenant with no policy row, i.e. no privacy lock. The privacy-locked-tenant
-- guarantee, which is the best design decision in the project, was therefore
-- unenforceable for the entire API-key surface.
--
-- The tenant now comes from THIS TABLE, never from a client-supplied token body.
-- Only the hash is stored: a leaked database does not yield usable credentials.
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash   VARCHAR PRIMARY KEY,          -- sha256 hex of the raw key
    tenant_id  VARCHAR NOT NULL,
    label      VARCHAR,                      -- human note: who holds this key
    role       VARCHAR DEFAULT 'user',
    active     BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_used  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys (tenant_id);

-- Demo key is no longer seeded automatically for security reasons.

-- Users, for the login endpoint that currently authenticates anybody
-- (PROJECT_ASSESSMENT §6.6 defect 3). Passwords are salted-hash only.
CREATE TABLE IF NOT EXISTS users (
    username      VARCHAR PRIMARY KEY,
    password_hash VARCHAR NOT NULL,          -- pbkdf2_sha256$iterations$salt$hash
    tenant_id     VARCHAR NOT NULL DEFAULT 'default',
    role          VARCHAR DEFAULT 'user',
    active        BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Chat history. A conversation belongs to one operator in one tenant; both are
-- carried on the row so a query can never accidentally cross either boundary.
CREATE TABLE IF NOT EXISTS chat_conversations (
    id         VARCHAR PRIMARY KEY,
    tenant_id  VARCHAR NOT NULL DEFAULT 'default',
    username   VARCHAR NOT NULL,
    title      VARCHAR NOT NULL DEFAULT 'New chat',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- One row per turn. `meta` holds the provenance of an assistant turn — which
-- agent answered, which MCP servers and tools it called, citations. Reopening a
-- conversation without it would show the answers and lose the evidence, which
-- is the half that makes an intelligence briefing checkable.
CREATE TABLE IF NOT EXISTS chat_messages (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id VARCHAR NOT NULL REFERENCES chat_conversations (id) ON DELETE CASCADE,
    role            VARCHAR NOT NULL,        -- 'user' | 'assistant'
    content         TEXT NOT NULL,
    meta            JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_conv_owner
    ON chat_conversations (tenant_id, username, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_msg_conversation
    ON chat_messages (conversation_id, id);

-- Idempotent schema updates for existing deployments
ALTER TABLE posts ADD COLUMN IF NOT EXISTS tenant_id VARCHAR NOT NULL DEFAULT 'default';
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS tenant_id VARCHAR NOT NULL DEFAULT 'default';
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS embedding_is_stub BOOLEAN DEFAULT FALSE;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tenant_id VARCHAR NOT NULL DEFAULT 'default';

-- Indexes
CREATE INDEX IF NOT EXISTS idx_campaigns_tenant_id   ON campaigns (tenant_id);
CREATE INDEX IF NOT EXISTS idx_posts_tenant_id          ON posts (tenant_id);
CREATE INDEX IF NOT EXISTS idx_posts_campaign_id      ON posts (campaign_id);
CREATE INDEX IF NOT EXISTS idx_posts_content_hash     ON posts (content_hash);
CREATE INDEX IF NOT EXISTS idx_posts_status           ON posts (status);
CREATE INDEX IF NOT EXISTS idx_comments_post_id       ON comments (post_id);
CREATE INDEX IF NOT EXISTS idx_analysis_tenant_id     ON analysis_results (tenant_id);
CREATE INDEX IF NOT EXISTS idx_analysis_campaign_id   ON analysis_results (campaign_id);
CREATE INDEX IF NOT EXISTS idx_analysis_created_at    ON analysis_results (created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_id         ON jobs (tenant_id);
-- Approximate nearest-neighbour index for cosine similarity (pgvector / semantic search).
CREATE INDEX IF NOT EXISTS idx_analysis_embedding_hnsw
    ON analysis_results USING hnsw (embedding vector_cosine_ops);

