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
  -- Per-type reaction counts, for the reaction-mix aggregate. These used to
  -- live on a `reaction_events` table that analytics_mcp queried but that no
  -- migration ever created and no writer ever populated, so `get_reaction_mix`
  -- raised in any non-stub deployment. Folding them onto the post-level row
  -- keeps the mix on the same dedup path as every other aggregate.
  like_count Int64 DEFAULT 0,
  love_count Int64 DEFAULT 0,
  haha_count Int64 DEFAULT 0,
  wow_count Int64 DEFAULT 0,
  sad_count Int64 DEFAULT 0,
  angry_count Int64 DEFAULT 0,
  care_count Int64 DEFAULT 0,
  -- See the ALTERs below for why these two are here.
  watchlist_alert UInt8 DEFAULT 0,
  label_agreement Float32 DEFAULT 0,
  created_at DateTime,
  scraped_at DateTime,
  inserted_at DateTime DEFAULT now()
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (campaign_id, created_at, post_id);

-- Backfill the reaction columns on tables created before they existed
-- (idempotent — no-ops once present). Additive only: no engine or ORDER BY
-- change, so this needs no table rebuild on an existing deployment.
ALTER TABLE analysis_events ADD COLUMN IF NOT EXISTS like_count  Int64 DEFAULT 0 AFTER keywords;
ALTER TABLE analysis_events ADD COLUMN IF NOT EXISTS love_count  Int64 DEFAULT 0 AFTER like_count;
ALTER TABLE analysis_events ADD COLUMN IF NOT EXISTS haha_count  Int64 DEFAULT 0 AFTER love_count;
ALTER TABLE analysis_events ADD COLUMN IF NOT EXISTS wow_count   Int64 DEFAULT 0 AFTER haha_count;
ALTER TABLE analysis_events ADD COLUMN IF NOT EXISTS sad_count   Int64 DEFAULT 0 AFTER wow_count;
ALTER TABLE analysis_events ADD COLUMN IF NOT EXISTS angry_count Int64 DEFAULT 0 AFTER sad_count;
ALTER TABLE analysis_events ADD COLUMN IF NOT EXISTS care_count  Int64 DEFAULT 0 AFTER angry_count;
-- Watchlist alerting over time, and the post's mean label agreement. A watchlist
-- alert that lives only inside one JSON document cannot answer "is hostility
-- toward X rising this week?" — which is the actual monitoring question.
ALTER TABLE analysis_events ADD COLUMN IF NOT EXISTS watchlist_alert UInt8 DEFAULT 0 AFTER keywords;
ALTER TABLE analysis_events ADD COLUMN IF NOT EXISTS label_agreement Float32 DEFAULT 0 AFTER watchlist_alert;

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
  -- Ensemble provenance: how much the independent labellers agreed on this
  -- comment (1.0 = unanimous), and whether the label was computed for this
  -- comment or copied from a near-duplicate's representative. Together they
  -- make "which labels need a human?" a query instead of a guess.
  label_agreement Float32 DEFAULT 0,
  label_source LowCardinality(String) DEFAULT '',
  inserted_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(inserted_at)
ORDER BY (post_id, comment_id);

-- Backfill the emotion column on tables created before per-comment emotion
-- labels existed (idempotent — no-op once the column is present).
ALTER TABLE comment_sentiments ADD COLUMN IF NOT EXISTS emotion LowCardinality(String) DEFAULT 'neutral' AFTER sentiment_score;
ALTER TABLE comment_sentiments ADD COLUMN IF NOT EXISTS label_agreement Float32 DEFAULT 0 AFTER author;
ALTER TABLE comment_sentiments ADD COLUMN IF NOT EXISTS label_source LowCardinality(String) DEFAULT '' AFTER label_agreement;

-- There is deliberately no `llm_usage` table here.
--
-- One used to be created (post_id, campaign_id, backend, model, task,
-- prompt/completion/total_tokens, latency_ms, cache_hit) and **nothing ever
-- wrote or read it** — grep the tree: the only other mention was a prose line in
-- PROJECT_ASSESSMENT.md. That is the exact defect §11.3 removed the Postgres
-- `llm_cache` table for, one store over, and leaving it standing would have kept
-- a plausible-looking schema around for a reader to be pointed at later.
--
-- Per-(backend, model, task) LLM spend is dimensioned in Redis instead —
-- `usage:tokens:{backend}:{model}`, `usage:tokens:lane:{lane}`,
-- `usage:calls:task:{task}`, written by stage2_llm/worker._track_usage and read
-- by GET /v1/usage (§5.8). That is where the cost breakdown is actually
-- answered, and unlike this table it has a writer.
--
-- Existing deployments can clean it up with:  DROP TABLE IF EXISTS llm_usage;
