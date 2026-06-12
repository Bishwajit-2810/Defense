-- Defense platform database initialization

-- pgvector extension — provides the `vector` type used for semantic search.
-- Replaces the standalone Qdrant service: embeddings live alongside the
-- canonical result in analysis_results.embedding.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS posts (
    id                VARCHAR PRIMARY KEY,
    campaign_id       VARCHAR,
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
    result          JSONB NOT NULL,
    embedding       vector(768),   -- pgvector: semantic-search vector (was Qdrant)
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
    type        VARCHAR,
    status      VARCHAR DEFAULT 'pending',
    selector    JSONB,
    options     JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    error       TEXT
);

CREATE TABLE IF NOT EXISTS llm_cache (
    id           SERIAL PRIMARY KEY,
    cache_key    VARCHAR UNIQUE,
    backend      VARCHAR,
    model        VARCHAR,
    task         VARCHAR,
    content_hash VARCHAR,
    response     JSONB,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tenant_policies (
    tenant_id      VARCHAR PRIMARY KEY,
    llm_backend    VARCHAR DEFAULT 'local',
    privacy_locked BOOLEAN DEFAULT FALSE,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_posts_campaign_id      ON posts (campaign_id);
CREATE INDEX IF NOT EXISTS idx_posts_content_hash     ON posts (content_hash);
CREATE INDEX IF NOT EXISTS idx_posts_status           ON posts (status);
CREATE INDEX IF NOT EXISTS idx_comments_post_id       ON comments (post_id);
CREATE INDEX IF NOT EXISTS idx_analysis_campaign_id   ON analysis_results (campaign_id);
CREATE INDEX IF NOT EXISTS idx_analysis_created_at    ON analysis_results (created_at);
-- Approximate nearest-neighbour index for cosine similarity (pgvector / semantic search).
CREATE INDEX IF NOT EXISTS idx_analysis_embedding_hnsw
    ON analysis_results USING hnsw (embedding vector_cosine_ops);
