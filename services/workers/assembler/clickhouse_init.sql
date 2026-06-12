CREATE TABLE IF NOT EXISTS analysis_events (
  post_id String,
  campaign_id String,
  platform LowCardinality(String),
  media_type LowCardinality(String),
  language LowCardinality(String),
  overall_sentiment LowCardinality(String),
  sentiment_score Float32,
  text_sentiment Nullable(String),
  image_sentiment Nullable(String),
  toxicity_score Float32,
  hate_speech_score Float32,
  comment_count Int32,
  stored_comments Int32,
  total_reactions Int64,
  coverage Float32,
  llm_used UInt8,
  llm_backend Nullable(String),
  topics Array(String),
  keywords Array(String),
  created_at DateTime,
  scraped_at DateTime,
  inserted_at DateTime DEFAULT now()
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (campaign_id, created_at, post_id);

CREATE TABLE IF NOT EXISTS comment_sentiments (
  comment_id String,
  post_id String,
  campaign_id String,
  platform LowCardinality(String),
  sentiment LowCardinality(String),
  sentiment_score Float32,
  emotion LowCardinality(String) DEFAULT 'neutral',
  method LowCardinality(String),
  likes Int32,
  author Nullable(String),
  inserted_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(inserted_at)
ORDER BY (post_id, comment_id);

-- Backfill the emotion column on tables created before per-comment emotion
-- labels existed (idempotent — no-op once the column is present).
ALTER TABLE comment_sentiments ADD COLUMN IF NOT EXISTS emotion LowCardinality(String) DEFAULT 'neutral' AFTER sentiment_score;

CREATE TABLE IF NOT EXISTS llm_usage (
  post_id String,
  campaign_id String,
  backend LowCardinality(String),
  model String,
  task String,
  prompt_tokens Int32,
  completion_tokens Int32,
  total_tokens Int32,
  latency_ms Int32,
  cache_hit UInt8,
  created_at DateTime DEFAULT now()
) ENGINE = MergeTree()
ORDER BY (created_at, backend, task);
