"""Centralised configuration for the defense system."""

import functools
import hashlib
import os
import secrets
from enum import Enum

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

    # Stage 2
    llm_backend_key: str = RedisKeys.LLM_BACKEND.value
    llm_backend: str = "local"
    groq_api_key: str = ""
    local_llm_base_url: str = "http://localhost:8000/v1"

    llm_a_local_model: str = "Qwen/Qwen2.5-7B-Instruct"
    llm_a_groq_model: str = "llama-3.1-8b-instant"
    llm_b_local_model: str = "Qwen/Qwen2.5-32B-Instruct"
    llm_b_groq_model: str = "llama-3.3-70b-versatile"
    vlm_local_model: str = "Qwen/Qwen2.5-VL-7B-Instruct"
    vlm_groq_model: str = "meta-llama/llama-4-scout-17b-16e-instruct"
    
    stage2_batch_size: int = 10
    stage2_max_retries: int = 3
    
    # Pre-processing
    filter_emoji_only: bool = True
    
    # Which comments get the context-aware LLM stance pass.
    #   "all"      — every comment that has text to read (the default). Gives
    #                the UI three comparable verdicts (LLM / XLM-R / DistilBERT)
    #                on every comment.
    #   "escalate" — only where the cheap voters disagree, have nothing to say,
    #                or a watchlist entity is mentioned. Measured at ~20% of
    #                comments on the corpus post, at the cost of an empty LLM
    #                row on the rest.
    comment_llm_mode: str = "all"
    # Identical text (after normalisation) reuses its twin's LLM verdict: the
    # prompt would be character-for-character the same. Set false to force a
    # separate call for every comment.
    comment_dedup_propagate: bool = True

    # Stage 2 Parallel Classifiers — the two cheap voters in the ensemble.
    # Both are skipped in MODEL_STUB_MODE (no weights are downloaded) and a
    # failed load is recorded once, never faked as a neutral verdict.
    stage2_classifiers_enabled: bool = True
    # Where the two small classifiers run: "auto" tries the GPU and falls back
    # to CPU, "cpu" skips the GPU entirely, "cuda" insists on it. The GPU is
    # normally already hosting the LLM — on a 4 GB card serving qwen2.5:7b there
    # is ~285 MB left, which fits one classifier and not two.
    stage2_classifier_device: str = "auto"
    stage2_classifier_1: str = "tabularisai/multilingual-sentiment-analysis"
    stage2_classifier_2: str = "lxyuan/distilbert-base-multilingual-cased-sentiments-student"
    
    # Assembler
    assembler_batch_size: int = 50
    assembler_max_retries: int = 3
    assembler_flush_interval: int = 5

    # MCP
    agents_service_url: str = "http://localhost:8001"
    ingest_mcp_url: str = "http://ingest-mcp:8102"
    retrieval_mcp_url: str = "http://retrieval-mcp:8101"
    analytics_mcp_url: str = "http://analytics-mcp:8100"
    analytics_mcp_stub: bool = True
    
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
    stage1_local_model: str = "gemma3:4b"
    stage2_local_model: str = "qwen2.5:7b"
    summary_local_model: str = "qwen2.5:7b"
    llm_a_local_model: str = "qwen2.5:7b"
    llm_b_local_model: str = "qwen2.5:7b"
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

    stage1_groq_model: str = "llama-3.1-8b-instant"
    stage2_groq_model: str = "llama-3.3-70b-versatile"
    summary_groq_model: str = "llama-3.3-70b-versatile"
    llm_a_groq_model: str = "llama-3.3-70b-versatile"
    llm_b_groq_model: str = "llama-3.1-8b-instant"
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
