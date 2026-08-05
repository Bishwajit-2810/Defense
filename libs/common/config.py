"""Centralised configuration for the defense system.

All settings are read from environment variables or a .env file.
Use get_settings() everywhere instead of instantiating Settings directly
so that the singleton cache is respected.

The JWT helpers at the bottom are deliberately NOT part of ``Settings``:
``Settings`` requires the infrastructure URLs (database, redis, clickhouse,
minio), so anything that imports it needs a fully-configured environment. The
token issuer and the token verifier must agree on the secret in every process,
including ones that have no database — so they read it through
``get_jwt_secret()`` instead.
"""

import functools
import hashlib
import os

from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# JWT secret — one source of truth
# ---------------------------------------------------------------------------
# The secret used to have three independent declarations (auth.py, deps.py, and
# the Settings field below), each captured at module import, and nothing checked
# that the issuer and the verifier agreed. An issuer/verifier mismatch presents
# as "login succeeds, then every subsequent call 401s", which is impossible to
# diagnose from the outside. It is now read here, per call, by both.

JWT_DEV_DEFAULT_SECRET = "change-me"
JWT_ALGORITHM = "HS256"

# Values that ship in the repo and therefore prove nothing about who holds a
# token. `.env` and `deploy/.env` both carry "change-me-in-production", which is
# a placeholder in every sense that matters even though it is not the literal
# default — treat it as one.
_PLACEHOLDER_SECRETS = frozenset({
    JWT_DEV_DEFAULT_SECRET,
    "change-me-in-production",
    "demo",
    "secret",
})

# Environments in which the placeholder secret is tolerated. Anything else must
# set JWT_SECRET explicitly or the process refuses to start.
_DEV_ENVIRONMENTS = frozenset({"dev", "development", "local", "test", "ci"})


def app_env() -> str:
    """The deployment environment name (``APP_ENV``), lowercased. Defaults to dev."""
    return (os.environ.get("APP_ENV") or "dev").strip().lower()


def get_jwt_secret() -> str:
    """The JWT signing/verifying secret.

    Read fresh from the environment on every call rather than captured at
    import, so the secret can be rotated without restarting the process — and
    so a test can set it without re-importing half the API.
    """
    return os.environ.get("JWT_SECRET") or JWT_DEV_DEFAULT_SECRET


def jwt_secret_is_default() -> bool:
    """True when a repo-shipped placeholder secret is still in use.

    Anyone who has read the repository can then sign a valid token.
    """
    return get_jwt_secret() in _PLACEHOLDER_SECRETS


def jwt_secret_fingerprint() -> str:
    """A short, non-reversible fingerprint of the secret, safe to log.

    Logging this at boot in every process that issues or verifies tokens makes
    an issuer/verifier mismatch visible in one line instead of presenting as a
    mystery 401 after a successful login.
    """
    return hashlib.sha256(get_jwt_secret().encode("utf-8")).hexdigest()[:12]


def require_jwt_secret() -> str:
    """Return the secret, refusing the placeholder outside a dev environment.

    Fails closed: a deployment that forgot to set JWT_SECRET should not start
    while silently accepting tokens that anyone on the internet can sign.
    """
    secret = get_jwt_secret()
    if jwt_secret_is_default() and app_env() not in _DEV_ENVIRONMENTS:
        raise RuntimeError(
            "JWT_SECRET is still a placeholder value that ships in this "
            f"repository, but APP_ENV={app_env()!r} is not a development "
            "environment. Set JWT_SECRET to a strong random value, or set "
            "APP_ENV=dev."
        )
    return secret


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
    # Kept for completeness, but the auth path must use get_jwt_secret()
    # below — see the module docstring for why this field is not the source
    # of truth.
    jwt_secret: str = JWT_DEV_DEFAULT_SECRET

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
