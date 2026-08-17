-- Defense platform database initialization

-- pgvector extension — provides the `vector` type used for semantic search.
-- Replaces the standalone Qdrant service: embeddings live alongside the
-- canonical result in analysis_results.embedding.
CREATE EXTENSION IF NOT EXISTS vector;

-- pg_trgm — backs the lexical arm of hybrid retrieval. The corpus is code-mixed
-- Bangla / English / Banglish, where exact entity names, transliterations and
-- hashtags are what a multilingual sentence encoder blurs and what a lexical
-- match nails. Trigrams rather than a stemmed tsvector because Postgres ships
-- no Bengali text-search configuration; 'simple' + trigram is honest about that.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

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
    -- Which model produced `embedding`, and at what width. Change EMBEDDING_MODEL
    -- without these and old rows stay in the OLD vector space: still comparable
    -- by cosine distance, silently meaningless, with nothing to detect it by —
    -- `fit_dim()` will even truncate or pad a mismatched model into the column
    -- with a single warning. 'stub:sha256' is the sentinel for a hash vector,
    -- which is a different fact from "unknown" (NULL).
    embedding_model VARCHAR,
    embedding_dim   INTEGER,
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

-- Per-comment vectors.
--
-- The single biggest structural gap this schema used to have: one vector per
-- post, built from the CAPTION, while the signal in this corpus lives in the
-- comment threads. "Where are people angry about fuel prices?" could only ever
-- match caption text — the anger was in rows carrying no vector at all.
--
-- Separate table rather than a column on `comments` for three reasons: comments
-- are ingested before anything is embedded, so the vector's lifecycle is not the
-- row's; near-duplicates point at a representative instead of storing their own
-- copy (`represented_by`); and an HNSW index over a table that also serves
-- `get_thread` reads would pay for the index on every ingestion write.
CREATE TABLE IF NOT EXISTS comment_embeddings (
    comment_id        VARCHAR NOT NULL,
    post_id           VARCHAR NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    campaign_id       VARCHAR,
    tenant_id         VARCHAR NOT NULL DEFAULT 'default',
    embedding         vector(768),
    embedding_is_stub BOOLEAN DEFAULT FALSE,
    embedding_model   VARCHAR,
    embedding_dim     INTEGER,
    -- Set when this comment was NOT embedded in its own right: it normalises to
    -- the same text as another comment under the same post, so it shares that
    -- one's vector. Recorded rather than implied, for the same reason
    -- `label_source: "propagated"` is recorded on a copied sentiment label.
    represented_by    VARCHAR,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (post_id, comment_id)
);

-- Chunk vectors.
--
-- One vector per post averages a long caption into mush: a 5,463-character post
-- argues three things and gets the centroid of all three, close to none of them.
-- A chunk gets its own vector and its own citable span.
--
-- Every post has at least one row here — a short caption is a single chunk whose
-- text is the whole caption, so its vector is identical to the post-level one.
-- That matters: chunk retrieval never needs a fallback path for short posts, and
-- "no chunk matched" always means the post did not match, never that it was
-- never chunked.
CREATE TABLE IF NOT EXISTS post_chunks (
    post_id           VARCHAR NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    chunk_idx         INTEGER NOT NULL,
    campaign_id       VARCHAR,
    tenant_id         VARCHAR NOT NULL DEFAULT 'default',
    text              TEXT NOT NULL,
    char_start        INTEGER,
    embedding         vector(768),
    embedding_is_stub BOOLEAN DEFAULT FALSE,
    embedding_model   VARCHAR,
    embedding_dim     INTEGER,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (post_id, chunk_idx)
);

-- Persisted cluster centroids and their LLM-written labels.
--
-- Two problems, one table. `get_clusters` recomputes k-means on every call, so
-- "the themes" are re-derived — and can silently change — between one report and
-- the next, which makes "is this narrative growing?" unanswerable. And the
-- narrative agent's prompt asks for a cluster NAME that the tool has never
-- returned, so the agent invents one every run; the prompt is patched to ask for
-- `representative_summary` instead, but a real label is the actual fix.
--
-- `centroid` is what makes a label survive recomputation: a fresh cluster is
-- matched to a stored one by cosine distance between centroids rather than by
-- cluster_id, which k-means assigns arbitrarily on each run.
CREATE TABLE IF NOT EXISTS cluster_labels (
    id            BIGSERIAL PRIMARY KEY,
    campaign_id   VARCHAR,
    tenant_id     VARCHAR NOT NULL DEFAULT 'default',
    label         VARCHAR NOT NULL,
    centroid      vector(768) NOT NULL,
    size          INTEGER,
    -- Which model wrote the label, and over which vector space the centroid
    -- lives. A centroid from a different embedding model is not comparable to
    -- this one, so matching must be able to exclude it.
    labeled_by      VARCHAR,
    embedding_model VARCHAR,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
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
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS embedding_model VARCHAR;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS embedding_dim INTEGER;
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

-- ---------------------------------------------------------------------------
-- Hybrid retrieval: the lexical arm (RAG_STATE_AND_ROADMAP §3.3)
-- ---------------------------------------------------------------------------
-- `to_tsvector(regconfig, text)` is IMMUTABLE in its two-argument form, so this
-- can be an expression index; the one-argument form is only STABLE and cannot.
-- 'simple' is deliberate, not a placeholder: it does no stemming and no
-- stop-wording, which is the correct behaviour for a corpus Postgres has no
-- language configuration for.
CREATE INDEX IF NOT EXISTS idx_analysis_fts_simple
    ON analysis_results
 USING gin (to_tsvector('simple',
            coalesce(result->>'post_summary', '') || ' ' ||
            coalesce(result->>'post_text', '')));

-- Trigram index for substring/typo matching on the caption — the arm that
-- catches a transliterated name spelled three different ways.
CREATE INDEX IF NOT EXISTS idx_analysis_post_text_trgm
    ON analysis_results
 USING gin ((result->>'post_text') gin_trgm_ops);

-- Comment vectors: HNSW for kNN, plus the scoping columns every query filters on.
CREATE INDEX IF NOT EXISTS idx_comment_emb_hnsw
    ON comment_embeddings USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_comment_emb_tenant   ON comment_embeddings (tenant_id);
CREATE INDEX IF NOT EXISTS idx_comment_emb_campaign ON comment_embeddings (campaign_id);
CREATE INDEX IF NOT EXISTS idx_comment_emb_post     ON comment_embeddings (post_id);
-- Comment text lives in `comments`, so the lexical arm of comment search reads
-- from there.
CREATE INDEX IF NOT EXISTS idx_comments_text_trgm
    ON comments USING gin (text gin_trgm_ops);

-- Chunk vectors: same treatment as the other two vector tables.
CREATE INDEX IF NOT EXISTS idx_post_chunks_hnsw
    ON post_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_post_chunks_tenant   ON post_chunks (tenant_id);
CREATE INDEX IF NOT EXISTS idx_post_chunks_campaign ON post_chunks (campaign_id);

CREATE INDEX IF NOT EXISTS idx_cluster_labels_scope
    ON cluster_labels (tenant_id, campaign_id);

