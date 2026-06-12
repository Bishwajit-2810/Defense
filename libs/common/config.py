"""Centralised configuration for the defense system.

All settings are read from environment variables or a .env file.
Use get_settings() everywhere instead of instantiating Settings directly
so that the singleton cache is respected.
"""

import functools

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ------------------------------------------------------------------ #
    # Infrastructure                                                       #
    # ------------------------------------------------------------------ #
    database_url: str
    redis_url: str
    clickhouse_url: str
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str = "defense"

    # Semantic-search vector dimension (pgvector column in analysis_results).
    # Must match the analysis_results.embedding column and the Stage-1 embedder.
    embedding_dim: int = 768

    # ------------------------------------------------------------------ #
    # LLM / VLM backend selection                                          #
    # ------------------------------------------------------------------ #
    llm_backend: str = "local"  # "local" | "groq" — docs promise local-by-default (no egress)

    groq_api_key: str = ""
    local_llm_base_url: str = "http://localhost:8000/v1"

    # Fast / small model (LLM-A)
    llm_a_local_model: str = "Qwen/Qwen2.5-7B-Instruct"
    llm_a_groq_model: str = "llama-3.1-8b-instant"

    # Large / capable model (LLM-B)
    llm_b_local_model: str = "Qwen/Qwen2.5-32B-Instruct"
    llm_b_groq_model: str = "llama-3.3-70b-versatile"

    # Vision-language model (VLM)
    vlm_local_model: str = "Qwen/Qwen2.5-VL-7B-Instruct"
    vlm_groq_model: str = "meta-llama/llama-4-scout-17b-16e-instruct"

    # ------------------------------------------------------------------ #
    # Security / Auth                                                      #
    # ------------------------------------------------------------------ #
    jwt_secret: str = "change-me"

    # ------------------------------------------------------------------ #
    # Observability                                                        #
    # ------------------------------------------------------------------ #
    log_level: str = "INFO"

    # ------------------------------------------------------------------ #
    # Upstream data source                                                 #
    # ------------------------------------------------------------------ #
    upstream_api_url: str = ""
    upstream_api_key: str = ""

    # ------------------------------------------------------------------ #
    # Pydantic-settings configuration                                      #
    # ------------------------------------------------------------------ #
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton.

    The first call reads from environment variables / .env; subsequent calls
    return the cached instance without re-reading the environment.
    """
    return Settings()
