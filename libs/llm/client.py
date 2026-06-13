"""
Backend-agnostic LLM/VLM client.

Usage:
    client = LLMClient()
    response = await client.chat(
        role="llm_a",                   # or "llm_b", "vlm"
        messages=[{"role":"user","content":"hello"}],
        backend_override=None,           # or "local" / "groq"
        tenant_policy=None,             # TenantPolicy instance
        response_format={"type":"json_object"},  # optional
    )
"""

import json
import os
import time
from typing import Any, Optional, Type, TypeVar

import openai
from loguru import logger as log
from openai import AsyncOpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .circuit import CircuitBreaker
from .policy import DEFAULT_POLICY, PolicyViolationError, TenantPolicy, enforce_policy

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Role → model-id maps
# ---------------------------------------------------------------------------

_ROLE_LOCAL_ENV: dict[str, str] = {
    "llm_a": "LLM_A_LOCAL_MODEL",
    "llm_b": "LLM_B_LOCAL_MODEL",
    "vlm":   "VLM_LOCAL_MODEL",
}

_ROLE_GROQ_ENV: dict[str, str] = {
    "llm_a": "LLM_A_GROQ_MODEL",
    "llm_b": "LLM_B_GROQ_MODEL",
    "vlm":   "VLM_GROQ_MODEL",
}

# Defaults target a local Ollama (OpenAI-compatible) server. Override per role
# with the LLM_*_LOCAL_MODEL env vars; any model from `ollama list` works.
_ROLE_LOCAL_DEFAULT: dict[str, str] = {
    "llm_a": "qwen2.5:7b",
    "llm_b": "qwen2.5:7b",
    "vlm":   "qwen3-vl:4b",
}

_ROLE_GROQ_DEFAULT: dict[str, str] = {
    "llm_a": "llama-3.1-8b-instant",
    "llm_b": "llama-3.3-70b-versatile",
    "vlm":   "meta-llama/llama-4-scout-17b-16e-instruct",
}

VALID_ROLES = frozenset(_ROLE_LOCAL_DEFAULT)


def _retryable(func):
    """Decorator that applies tenacity retry logic to an async method."""
    return retry(
        retry=retry_if_exception_type(
            (openai.RateLimitError, openai.APIConnectionError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )(func)


class LLMClient:
    """
    Single entry-point for LLM / VLM inference.

    Two underlying AsyncOpenAI clients are created at construction time:
    one pointing at the local vLLM endpoint and one at Groq Cloud.
    """

    def __init__(self) -> None:
        self._default_backend: str = os.environ.get("LLM_BACKEND", "local")

        # Local vLLM client (no auth required by default, but honour a key if set)
        local_base_url: str = os.environ.get(
            "LOCAL_LLM_BASE_URL", "http://localhost:11434/v1"
        )
        local_api_key: str = os.environ.get("LOCAL_LLM_API_KEY", "ollama")
        self._local_client = AsyncOpenAI(
            base_url=local_base_url,
            api_key=local_api_key,
        )

        # Groq Cloud client
        groq_api_key: str = os.environ.get("GROQ_API_KEY", "")
        self._groq_client = AsyncOpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=groq_api_key or "missing-groq-api-key",
        )

        # Per-role model overrides from env vars
        self._local_models: dict[str, str] = {
            role: os.environ.get(_ROLE_LOCAL_ENV[role], _ROLE_LOCAL_DEFAULT[role])
            for role in VALID_ROLES
        }
        self._groq_models: dict[str, str] = {
            role: os.environ.get(_ROLE_GROQ_ENV[role], _ROLE_GROQ_DEFAULT[role])
            for role in VALID_ROLES
        }

        # Per-backend circuit breakers (§8): take a flapping backend out of
        # rotation for a cooldown instead of hammering it every request.
        cb_threshold = int(os.environ.get("LLM_CB_FAILURE_THRESHOLD", "5"))
        cb_cooldown = float(os.environ.get("LLM_CB_COOLDOWN_SECONDS", "30"))
        self._breakers: dict[str, CircuitBreaker] = {
            "local": CircuitBreaker("local", cb_threshold, cb_cooldown),
            "groq": CircuitBreaker("groq", cb_threshold, cb_cooldown),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_model(self, role: str, backend: str) -> str:
        if role not in VALID_ROLES:
            raise ValueError(
                f"Unknown role '{role}'. Valid roles: {sorted(VALID_ROLES)}"
            )
        if backend == "local":
            return self._local_models[role]
        if backend == "groq":
            return self._groq_models[role]
        raise ValueError(f"Unknown backend '{backend}'. Use 'local' or 'groq'.")

    def _get_client(self, backend: str) -> AsyncOpenAI:
        if backend == "local":
            return self._local_client
        if backend == "groq":
            return self._groq_client
        raise ValueError(f"Unknown backend '{backend}'.")

    @_retryable
    async def _call_api(
        self,
        client: AsyncOpenAI,
        model: str,
        messages: list[dict[str, Any]],
        response_format: Optional[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format

        return await client.chat.completions.create(**kwargs)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def chat(
        self,
        role: str,
        messages: list[dict[str, Any]],
        backend_override: Optional[str] = None,
        tenant_policy: Optional[TenantPolicy] = None,
        response_format: Optional[dict[str, Any]] = None,
        max_tokens: int = 2048,
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        """
        Send a chat request and return a normalised response dict.

        Returns
        -------
        {
            "content": str,
            "usage": {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int},
            "model": str,
            "backend": str,
        }

        Raises
        ------
        PolicyViolationError
            When the tenant is privacy-locked and the resolved backend is "groq".
        ValueError
            For unknown role or backend strings.
        openai.RateLimitError / openai.APIConnectionError
            Re-raised after 3 retries are exhausted.
        """
        effective_backend = enforce_policy(
            tenant_policy=tenant_policy,
            requested_backend=backend_override,
            default_backend=self._default_backend,
        )

        model_id = self._resolve_model(role, effective_backend)

        # Preemptive failover: if Groq's circuit is open (recent repeated
        # failures), don't even try it — route straight to local, which is
        # always available and privacy-safe (§8).
        if effective_backend == "groq" and not self._breakers["groq"].allow():
            log.warning("llm_groq_circuit_open_preempting_local role={}", role)
            effective_backend = "local"
            model_id = self._resolve_model(role, "local")

        t0 = time.perf_counter()
        try:
            completion = await self._call_api(
                client=self._get_client(effective_backend),
                model=model_id,
                messages=messages,
                response_format=response_format,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            self._breakers[effective_backend].record_success()
        except Exception as exc:
            self._breakers[effective_backend].record_failure()
            # A Groq failure (bad/expired key, connection, rate limit) must never
            # take down analysis — fall back to the local Ollama backend, which is
            # always available and privacy-safe. Local failures still propagate.
            if effective_backend == "groq":
                log.warning(
                    "llm_groq_failed_falling_back_to_local role={} model={} error={}",
                    role, model_id, exc,
                )
                effective_backend = "local"
                model_id = self._resolve_model(role, "local")
                completion = await self._call_api(
                    client=self._get_client("local"),
                    model=model_id,
                    messages=messages,
                    response_format=response_format,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                self._breakers["local"].record_success()
            else:
                log.error(
                    "llm_call_failed backend={} role={} model={} error={}",
                    effective_backend, role, model_id, exc,
                )
                raise

        choice = completion.choices[0]
        usage = completion.usage
        total_tokens = usage.total_tokens if usage else 0
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        log.info(
            "llm_call backend={} role={} model={} tokens={} latency_ms={}",
            effective_backend, role, completion.model, total_tokens, latency_ms,
        )

        return {
            "content": choice.message.content or "",
            "usage": {
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
                "total_tokens": total_tokens,
            },
            "model": completion.model,
            "backend": effective_backend,
        }

    async def chat_structured(
        self,
        role: str,
        messages: list[dict[str, Any]],
        schema_class: Type[T],
        backend_override: Optional[str] = None,
        tenant_policy: Optional[TenantPolicy] = None,
        max_tokens: int = 2048,
        temperature: float = 0.1,
    ) -> T:
        """
        Like chat(), but forces JSON output and parses it into schema_class.

        Parameters
        ----------
        schema_class:
            A Pydantic model class.  The raw JSON string returned by the LLM
            is validated and returned as an instance of schema_class.

        Returns
        -------
        An instance of schema_class.

        Raises
        ------
        pydantic.ValidationError
            If the model output cannot be parsed/validated against schema_class.
        json.JSONDecodeError
            If the model returns malformed JSON.
        """
        response = await self.chat(
            role=role,
            messages=messages,
            backend_override=backend_override,
            tenant_policy=tenant_policy,
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            temperature=temperature,
        )

        raw_json = response["content"]
        data = json.loads(raw_json)
        # schema_class is expected to be a Pydantic BaseModel subclass
        return schema_class.model_validate(data)
