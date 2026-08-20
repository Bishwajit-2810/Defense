"""Centralised configuration for the defense system."""

import functools
import hashlib
import os
import secrets
from enum import Enum
from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict

JWT_DEV_DEFAULT_SECRET = secrets.token_hex(32)
JWT_ALGORITHM = "HS256"

_PLACEHOLDER_SECRETS = frozenset({
    JWT_DEV_DEFAULT_SECRET,
    "change-me-in-production",
    "demo",
    "secret",
})

_DEV_ENVIRONMENTS = frozenset({"dev", "development", "local", "test", "ci"})


def app_env() -> str:
    return (get_settings().app_env).strip().lower()


def apply_hf_offline_policy() -> bool:
    """Pin Hugging Face model loading to the local cache. Returns whether it did.

    Call this ONCE at worker start-up, before anything imports transformers.
    That ordering is the whole point: `transformers` reads `TRANSFORMERS_OFFLINE`
    at *import* time, so setting it later is silently ignored and the loader
    goes to the network anyway — which is how "MODEL_STUB_MODE downloads
    nothing" turned into a run that printed *"You are sending unauthenticated
    requests to the HF Hub"*. Every import of transformers in this tree is lazy,
    so a call at module top is early enough.

    Defaults to following MODEL_STUB_MODE — that mode already promises no
    downloads — and `HF_OFFLINE` overrides it either way. An explicit env var
    already in the environment always wins (`setdefault`).
    """
    settings = get_settings()
    want = settings.hf_offline if settings.hf_offline is not None else settings.model_stub_mode
    if want:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    # Telemetry is a network call too, and it is never wanted here.
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return bool(want)


def get_jwt_secret() -> str:
    """The signing secret, read fresh on every call.

    The environment wins over the cached Settings snapshot on purpose: rotating
    JWT_SECRET must invalidate previously-issued tokens without a restart, and
    `get_settings()` is lru_cached, so reading only through it froze the secret
    at first import — the issuer and the verifier could then be signing and
    checking with different values for the rest of the process's life.
    """
    return os.environ.get("JWT_SECRET") or get_settings().jwt_secret or JWT_DEV_DEFAULT_SECRET


def jwt_secret_is_default() -> bool:
    return get_jwt_secret() in _PLACEHOLDER_SECRETS


def jwt_secret_fingerprint() -> str:
    return hashlib.sha256(get_jwt_secret().encode("utf-8")).hexdigest()[:12]


def require_jwt_secret() -> str:
    secret = get_jwt_secret()
    if jwt_secret_is_default() and app_env() not in _DEV_ENVIRONMENTS:
        raise RuntimeError("JWT_SECRET is placeholder")
    return secret


class RedisKeys(str, Enum):
    SENTIMENT_MODEL = "config:sentiment_model"
    LLM_BACKEND = "config:llm_backend"
    # we can add more as we find them


class Settings(BaseSettings):
    # Infrastructure
    database_url: str = ""
    redis_url: str = "redis://localhost:6379"
    clickhouse_url: str = "clickhouse://localhost:9000/defense"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "defense"

    # Bus
    bus_backend: str = "redis"
    kafka_bootstrap_servers: str = "localhost:9092"

    # API / Frontend
    cors_origins: str = "*"
    rate_limit_enabled: bool = True
    rate_limit_per_min: int = 600
    public_base_url: str = "http://localhost:8001"

    # Workers shared
    model_stub_mode: bool = True
    # Load HF checkpoints from the local cache only, never the network.
    # None = follow MODEL_STUB_MODE (which already promises no downloads).
    # Set HF_OFFLINE=false to allow fetching even in stub mode.
    hf_offline: bool | None = None
    redis_block_ms: int = 2000
    
    # Ingestion
    ingestion_max_retries: int = 3
    near_dup_dedup: str = "false"
    near_dup_threshold: float = 0.95
    
    # Stage 1
    stage1_batch_size: int = 1
    stage1_max_retries: int = 3
    stage1_ocr_sentiment: bool = False
    sentiment_model_config_key: str = RedisKeys.SENTIMENT_MODEL.value
    image_fetch_timeout: float = 10.0
    tesseract_lang: str = "ben+eng"
    comment_laugh_sentiment: str = "negative"
    nlp_stage1_consumer: str = ""
    
    # Router
    router_batch_size: int = 10
    router_max_retries: int = 3
    router_confidence_threshold: float = 0.8
    router_post_type_confidence_threshold: float = 0.8
    router_toxicity_threshold: float = 0.7
    router_long_text_chars: int = 1000
    # Does "a summary is wanted" alone justify Stage 2? No: Stage 1 writes a
    # summary for every post. Leaving this true makes rule 3 fire on every
    # request and the gate stops gating.
    router_summary_routes: bool = False
    #: How many comments per post Stage 2 analyses, ranked by reaction count
    #: (``likes``) descending. The router picks them ONCE and marks them, so every
    #: Stage-2 voter — the HF heads, the LLM stance pass, the dedup cache — reads
    #: the same set and the UI's side-by-side comparison has no holes.
    #:
    #: **0 (the default) = no cap: every comment with text is analysed.** That is
    #: the deliberate choice — a capped run leaves the tail of the thread with no
    #: model verdict at all (Stage 1's keyword label does not vote), so
    #: `sentiment_breakdown` fills up with `uncertain` and the post-level coverage
    #: label stops matching the per-comment table.
    #:
    #: Set a positive value only as a speed/cost guard, and quote it when you do:
    #: at 7 heads it is ~0.92 s/comment of CPU plus ceil(N/COMMENT_STANCE_BATCH)
    #: LLM calls, so a 2,857-comment thread is ~44 min of classifier time and ~115
    #: stance batches. `ensemble.not_analysed` reports what a cap dropped.
    #:
    #: Only comments with text ever compete: emoji-only reactions and bare links
    #: get no model call, so a highly-liked ❤️ must not consume a slot — and with
    #: no cap they are still the one group that ends up `uncertain`, because there
    #: is nothing in them for a model to read.
    router_comment_top_n: int = 0
    #: Minimum word tokens for a comment to be eligible for Stage-2 analysis.
    #: 0 = any comment with text; >2 (e.g. 3) = ignores short 1-2 word comments and pure emoji.
    router_comment_min_words: int = 0

    # Stage 2
    llm_backend_key: str = RedisKeys.LLM_BACKEND.value
    # NOTE: llm_backend / groq_api_key / local_llm_base_url and the llm_a/llm_b/
    # vlm model names used to be declared here AND again in the "Pipeline Model"
    # block below, with different values (vLLM-style HF repo ids here, ollama
    # tags there). A repeated field is not an error in pydantic — the LAST
    # definition simply wins — so this block was dead text that read like
    # configuration: editing `local_llm_base_url` here moved nothing, and the
    # port it named (8000) was not the one in use (11434). Declared once now,
    # in the block below.

    stage2_batch_size: int = 10
    stage2_max_retries: int = 3
    
    # Pre-processing
    filter_emoji_only: bool = True
    
    # Which comments get the context-aware LLM stance pass.
    #   "all"      — every comment that has text to read (the default). Gives
    #                the UI the LLM's verdict beside all seven cheap heads on
    #                every comment.
    #   "escalate" — only where the cheap voters disagree, have nothing to say,
    #                or a watchlist entity is mentioned. The ~20% figure was
    #                measured with TWO cheap voters; with seven, any single
    #                dissenter escalates, so re-measure before assuming it still
    #                saves anything.
    comment_llm_mode: str = "all"
    # Identical text (after normalisation) reuses its twin's LLM verdict: the
    # prompt would be character-for-character the same. Set false to force a
    # separate call for every comment.
    comment_dedup_propagate: bool = True

    # Stage 2 Parallel Classifiers — the cheap voters in the ensemble.
    # Skipped in MODEL_STUB_MODE unless the weights are already cached, and a
    # failed load is recorded once, never faked as a neutral verdict.
    stage2_classifiers_enabled: bool = True
    # Where the small classifiers run: "auto" tries the GPU and falls back to
    # CPU, "cpu" skips the GPU entirely, "cuda" insists on it. The GPU is
    # normally already hosting the LLM — on a 4 GB card serving qwen2.5:7b there
    # is ~285 MB left, which fits one classifier and not seven. "cpu" is the
    # right setting on this box; see .env.
    stage2_classifier_device: str = "auto"

    # The roster, as (voter name, checkpoint) pairs. Every name here MUST also
    # appear in ensemble.CHEAP_SOURCES or the vote is collected and then ignored
    # by the escalation gate — tests/test_ensemble.py asserts that.
    #
    # A checkpoint earns a slot only if it is a *sentiment* head whose labels
    # survive worker._map_sentiment_label. Four of the five original entries
    # failed that bar and had never once voted: csebuetnlp/banglabert and
    # sagorsarker/bangla-bert-base are base encoders (ElectraForPreTraining /
    # BertForMaskedLM) — `pipeline("sentiment-analysis")` bolts a randomly
    # initialised head on them and emits LABEL_0/LABEL_1, which maps to None —
    # and l3cube-pune/bengali-sentiment-bert plus
    # mrm8488/distilmbert-fine-tuned-bengali-sentiment do not exist on the Hub
    # at all. A slot that cannot vote is worse than an empty one: it reads as
    # coverage in the roster and contributes nothing to agreement.
    #
    # A slot also needs a tokenizer this environment can actually build. The
    # obvious pick for slot 3, cardiffnlp/twitter-xlm-roberta-base-sentiment,
    # ships only `sentencepiece.bpe.model` — no `tokenizer.json` — so
    # transformers 5 tries to convert the slow tokenizer and raises
    # "`tiktoken` is required". Its `-multilingual` sibling is the same model
    # family with a fast tokenizer in the repo, and costs no new dependency.
    #
    # Measured on 300 real corpus comments (CPU, 16 cores), all seven loaded:
    #
    #   head                    pos  neg  neu  conf  agrees w/ majority  ms/comment
    #   xlmr                    27%  43%  30%  0.54        52%              241
    #   distilbert              54%  45%   0%  0.50        62%              207
    #   twitter_xlmr            33%  34%  33%  0.67        56%              326
    #   banglabert              29%  41%  30%  0.84        61%              282
    #   bengali_sentiment_bert  31%  51%  18%  0.76        70%              277
    #   mbert                   26%  51%  22%  0.36        55%              320
    #   modernbert              81%  19%   1%  0.69        40%              860
    #
    # and what the roster does to the ensemble:
    #
    #   roster                       uncertain  unanimous  escalate%
    #   2 (xlmr+distilbert)             49.7%      50.3%      49.7%
    #   7 (all)                         50.7%       9.3%      90.7%
    #   6 (drop modernbert)             32.0%      16.0%      84.0%
    #   3 (banglabert, bengali, twitter) 5.0%      42.0%      58.0%
    #
    # Read those with two caveats. First, none of it is accuracy — the gold set
    # is unlabelled, so this measures agreement, and seven heads agreeing can be
    # seven heads wrong together. Second, `uncertain` falls with fewer voters
    # partly by arithmetic: 2-of-3 clears DEFAULT_MIN_AGREEMENT, 4-of-7 does not.
    #
    # What survives both caveats is `modernbert`: 81% positive on a visibly
    # mixed thread, agreeing with the room 40% of the time, for 47% of the
    # roster's total CPU. Dropping it alone takes abstentions from 51% to 32%.
    #
    # ALL SEVEN STAY (decided 17 Aug 2026), so every comment gets all eight
    # verdicts — the seven heads plus the LLM. Do not thin the roster to reduce
    # abstentions: a comment eight models split on is genuinely contested, and
    # removing the dissenter manufactures agreement rather than measuring it. The
    # honest levers on that 51% are DEFAULT_MIN_AGREEMENT (`libs/ensemble.py` —
    # 4-of-8 is a plurality the threshold currently rejects) and, eventually, a
    # gold set that says which heads are actually right. `modernbert`'s skew is a
    # reason to WEIGHT it, not to silence it.
    #
    # Setting a slot to "" still drops that voter — it is the switch for a
    # deliberate, quoted experiment, not a default.
    stage2_classifier_1: str = "tabularisai/multilingual-sentiment-analysis"
    stage2_classifier_2: str = "lxyuan/distilbert-base-multilingual-cased-sentiments-student"
    stage2_classifier_3: str = "cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual"
    stage2_classifier_4: str = "ADn-001/banglabert-sentnob-sentiment"
    stage2_classifier_5: str = "ahs95/banglabert-sentiment-analysis"
    stage2_classifier_6: str = "nlptown/bert-base-multilingual-uncased-sentiment"
    stage2_classifier_7: str = "clapAI/modernBERT-base-multilingual-sentiment"

    #: Voter name per slot. A ClassVar, so pydantic does not expose it as a
    #: settable field: the names are the keys in `parallel_labels`, and renaming
    #: one from the environment would silently orphan stored rows, the dashboard
    #: column that reads them, and ensemble.CHEAP_SOURCES. The *checkpoint* in
    #: each slot is configurable; which voter it answers as is not.
    stage2_classifier_names: ClassVar[tuple[str, ...]] = (
        "xlmr",                    # 1  DistilBERT-multilingual, 5-class
        "distilbert",              # 2  DistilBERT-multilingual student, 3-class
        "twitter_xlmr",            # 3  XLM-R trained on social media, 3-class
        "banglabert",              # 4  BanglaBERT/Electra on SentNoB (Bangla social)
        "bengali_sentiment_bert",  # 5  BanglaBERT/Electra, 5-class
        "mbert",                   # 6  multilingual BERT, 1-5 stars
        "modernbert",              # 7  ModernBERT-base multilingual, 3-class
    )

    @property
    def stage2_classifier_roster(self) -> list[tuple[str, str]]:
        """[(voter name, checkpoint)] for every slot that has a checkpoint."""
        slots = [
            self.stage2_classifier_1, self.stage2_classifier_2,
            self.stage2_classifier_3, self.stage2_classifier_4,
            self.stage2_classifier_5, self.stage2_classifier_6,
            self.stage2_classifier_7,
        ]
        return [
            (name, model.strip())
            for name, model in zip(self.stage2_classifier_names, slots)
            if (model or "").strip()
        ]


    # Assembler
    assembler_batch_size: int = 50
    assembler_max_retries: int = 3
    assembler_flush_interval: int = 5

    # MCP
    # agents_service_url and analytics_mcp_stub are declared further down (see
    # the note in the Stage 2 block): the later definition is the one pydantic
    # keeps, and these two read `http://localhost:8001` / `True` here while the
    # values actually in force are `http://agents:8010` / `False`.
    ingest_mcp_url: str = "http://localhost:8102"
    retrieval_mcp_url: str = "http://localhost:8101"
    analytics_mcp_url: str = "http://localhost:8110"

    # Misc
    log_level: str = "INFO"
    log_to_redis: bool = True
    log_redis_max: int = 3000
    log_redis_ttl: int = 86400
    app_env: str = "dev"
    jwt_secret: str = ""
    jwt_expire_hours: int = 12
    sse_ticket_ttl: int = 60
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = ""
    upstream_api_url: str = ""
    upstream_api_key: str = ""
    embedding_dim: int = 768
    embedding_model: str = "paraphrase-multilingual-mpnet-base-v2"
    # Real embeddings WITHOUT the rest of the Stage-1 model suite.
    #
    # MODEL_STUB_MODE is one switch over seven models: sentiment, emotion,
    # toxicity, CLIP/SigLIP, NER, KeyBERT and the sentence encoder. Retrieval
    # needs exactly the last one, and the others land on a GPU that is normally
    # already hosting the LLM — the stage2_classifier_device comment above puts
    # the free VRAM on a 4 GB card at ~285 MB. Tying "make search meaningful" to
    # "load the vision stack" made the first change cost the second.
    #
    # None = follow MODEL_STUB_MODE (so this is a no-op unless set). Setting it
    # false also needs HF_OFFLINE=false, since HF offline policy follows
    # MODEL_STUB_MODE too and would otherwise block the download.
    embedding_stub_mode: bool | None = None
    #: Device for the sentence encoder: "cpu", "cuda", "cuda:1", or "" to let
    #: sentence-transformers choose (CUDA when present).
    #:
    #: Worth setting to "cpu" on this hardware. The GPU is a 4 GB card that
    #: normally already hosts the LLM, so loading the encoder on CUDA fails with
    #: `CUDA out of memory` depending on what ollama is doing — and the fallback
    #: from a failed load is HASH vectors, i.e. retrieval silently degrades to
    #: ranking noise, transiently. On CPU the same encoder costs 99 ms per query
    #: and produces identical vectors. `_get_model` retries on CPU automatically;
    #: this makes it the first choice rather than the recovery path.
    embedding_device: str = ""
    llm_max_continuations: int = 2
    local_llm_api_key: str = "ollama"
    llm_backend: str = "local"
    groq_api_key: str = ""
    local_llm_base_url: str = "http://localhost:11434/v1"
    llm_cb_failure_threshold: int = 5
    llm_cb_cooldown_seconds: float = 30.0
    llm_usage_tracking_disabled: bool = False
    analytics_mcp_port: int = 8100
    ingest_mcp_port: int = 8102
    retrieval_mcp_port: int = 8101
    retrieval_mcp_stub: bool = False

    # Model overrides
    # Pipeline Model (Stage 1 & Stage 2): Same unified model
    stage1_local_model: str = "qwen2.5:7b"
    stage2_local_model: str = "qwen2.5:7b"
    summary_local_model: str = "qwen2.5:7b"

    # Agent Model (Dedicated 3rd model for Agentic RAG & reasoning)
    #
    # `-16k` is a derived tag over plain llama3.1:8b that sets `num_ctx 16384`
    # (config/Modelfile.llama31-16k). `ollama serve` defaults to a 4096-token
    # window and silently DISCARDS anything longer, oldest messages first — so a
    # large tool result pushed the system prompt and the operator's question out
    # of context and the agent answered from the tail of a JSON payload. Measured
    # on this machine: an 11k-token prompt evaluates 24 tokens on `llama3.1:8b`
    # and all 11,045 on `llama3.1:8b-16k`.
    #
    # Setting OLLAMA_CONTEXT_LENGTH on the ollama service is the better fix — it
    # covers the pipeline models too — but a PARAMETER in the model wins over it,
    # so retag or drop the `-16k` suffix here if you go that route.
    agent_local_model: str = "llama3.1:8b-16k"
    agent_groq_model: str = "llama-3.3-70b-versatile"

    # Backward-compatible role models
    llm_a_local_model: str = "qwen2.5:7b"
    llm_b_local_model: str = "llama3.1:8b"
    vlm_local_model: str = "qwen3-vl:4b"

    fasttext_lang_model: str = "/models/fasttext/lid.176.bin"
    sentiment_model: str = "cardiffnlp/twitter-xlm-roberta-base-sentiment"
    banglishbert_sentiment_model: str | None = None
    banglabert_sentiment_model: str | None = None
    mbert_sentiment_model: str | None = None
    emotion_model: str = "j-hartmann/emotion-english-distilroberta-base"
    toxicity_model: str = "unitary/toxic-bert"
    clip_model: str = "google/siglip-base-patch16-224"
    ner_model: str = "gliner"

    # Groq Fallbacks for each tier
    stage1_groq_model: str = "llama-3.3-70b-versatile"
    stage2_groq_model: str = "llama-3.3-70b-versatile"
    summary_groq_model: str = "llama-3.3-70b-versatile"
    llm_a_groq_model: str = "llama-3.1-8b-instant"
    llm_b_groq_model: str = "llama-3.3-70b-versatile"
    vlm_groq_model: str = "meta-llama/llama-4-scout-17b-16e-instruct"

    stage1_llm: bool = True
    # Should Stage 1 ALSO LLM-label the comments?
    #
    # Off by default, because Stage 2 now labels every post's comments with a
    # bigger model plus two classifiers — so this pass re-does the same work
    # with the weaker one and its verdict survives only as a single vote that
    # the others usually outweigh. It is also the pipeline's slowest step:
    # measured on the live run, one 25-comment batch took 120 s and came back
    # with invalid JSON (16 labels salvaged out of 25).
    #
    # Turn it on when running WITHOUT the Stage-2 ensemble
    # (COMMENT_STANCE=false / STAGE2_CLASSIFIERS_ENABLED=false), where it is
    # the only thing giving comments a model-quality label.
    stage1_llm_comments: bool = False
    stage1_llm_comment_max: int = 0
    stage1_llm_batch: int = 25
    stage1_llm_concurrency: int = 3
    stage1_llm_batch_retries: int = 1
    
    topic_proto_floor: float = 0.28
    intent_proto_floor: float = 0.30
    proto_margin: float = 0.05
    post_type_proto_floor: float = 0.28
    
    router_consumer: str | None = None
    stage2_consumer: str | None = None
    
    cluster_algo: str = "kmeans"
    
    ingestion_stream: str = "ingestion:queue"
    ingestion_group: str = "ingestion-workers"
    nlp_stage1_stream: str = "nlp:stage1:queue"
    nlp_stage1_group: str = "stage1-nlp-group"
    router_stream: str = "router:queue"
    router_group: str = "router-workers"
    stage2_llm_stream: str = "llm:stage2:queue"
    stage2_llm_group: str = "stage2-llm-workers"
    assembler_stream: str = "assembler:queue"
    assembler_group: str = "assembler-group"
    
    analytics_mcp_stub: bool = False
    
    allow_unknown_api_keys: bool | None = None
    
    agents_service_url: str = "http://agents:8010"
    agent_sync_timeout: float = 28.0

    # Stage 2 specific overrides
    summary_role: str = "summary"
    comment_stance: bool = True
    comment_stance_role: str = "stage2"
    comment_stance_batch: int = 25
    comment_stance_max_per_post: int = 0
    comment_stance_concurrency: int = 3
    comment_stance_batch_retries: int = 1
    comment_summary: bool = True
    summary_max_tokens: int = 1024
    comment_summary_max_tokens: int = 640
    insight_max_tokens: int = 768
    post_type_max_tokens: int = 128
    stance_targets_file: str = "config/stance_targets.yml"
    vlm_summary: bool = True
    llm_cache_disabled: bool = False

    # Auth and API
    allow_any_login: str = ""
    allow_signup: str = ""
    signup_tenant_id: str = "default"
    report_cluster_sample_cap: int = 1500
    embedding_allow_stub: bool = True

    # -- Retrieval (RAG_STATE_AND_ROADMAP §3.2 - §3.7) ----------------------
    # Hybrid retrieval: run the pgvector kNN and a lexical scan, then fuse with
    # reciprocal rank fusion. On this corpus (code-mixed Bangla / English /
    # Banglish) the lexical arm carries exact entity names, transliterations and
    # hashtags — precisely what a multilingual sentence encoder blurs. It is
    # also the ONLY arm that means anything while MODEL_STUB_MODE=true.
    retrieval_hybrid: bool = True
    #: The 60 in `score(d) = Σ 1/(k + rank_i(d))`. Standard RRF constant.
    retrieval_rrf_k: int = 60
    #: Over-fetch factor per arm before fusion/reranking: ask for k * this,
    #: return k. 4 puts a limit=10 request at 40 candidates, matching §3.4's
    #: "retrieve 30-50, rerank down to 8".
    retrieval_candidate_multiplier: int = 4
    #: Cross-encoder reranking of the fused candidates. Off by default: it needs
    #: the `ml` extra and costs a second model load, and it is pointless while
    #: the first-stage ranking is hash noise.
    retrieval_rerank: bool = False
    retrieval_rerank_model: str = "BAAI/bge-reranker-v2-m3"
    #: How many posts get_clusters may pull vectors for. Was a hardcoded 100,
    #: which turned "corpus themes" into "themes of the 100 newest posts" with
    #: nothing saying so (§3.6).
    retrieval_cluster_scan_cap: int = 2000
    #: Chunk long captions and give each piece its own vector (§3.5). A short
    #: post is one chunk holding the whole caption, so this is a no-op below the
    #: threshold — the cost is paid only where a single vector was averaging
    #: several arguments together.
    retrieval_chunks: bool = True
    #: Prefer chunk vectors over the post-level vector in semantic_search. Split
    #: from the write switch so the two can be measured independently: chunks can
    #: be written and NOT searched, which is what a before/after eval needs.
    retrieval_chunk_search: bool = True
    #: Embed comment text as well as captions (§3.2). The signal in this corpus
    #: lives in the threads; without this a question about what people are angry
    #: about can only ever match caption text.
    comment_embeddings_enabled: bool = True
    #: Per-post ceiling on comments embedded, after near-duplicate grouping.
    #: 0 disables the cap.
    comment_embedding_max_per_post: int = 1000

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

    def get_async_database_url(self) -> str:
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url
        
    def get_groq_price_override(self, model: str) -> float | None:
        env_key = f"GROQ_PRICE_PER_1K_{model.replace('/', '_').replace('-', '_').replace('.', '_').upper()}"
        val = os.environ.get(env_key)
        if val:
            try:
                return float(val)
            except ValueError:
                pass
        return None


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
