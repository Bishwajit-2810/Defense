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
