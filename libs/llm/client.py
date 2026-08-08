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
from typing import Any, AsyncIterator, Optional, Type, TypeVar

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
from .usage import LANE_INTERACTIVE, track_usage

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Role → model-id maps
# ---------------------------------------------------------------------------

# Roles:
#   stage1 — the Stage-1 Fast-NLP model (sentiment/emotion/topic/intent/toxicity/
#            NER/keywords over caption + comments). One fast, small model carries
#            the high-volume per-post + per-comment work — see architecture.md §3.4.
#   stage2 — the Stage-2 CLASSIFICATION model (post-type, insight, context-aware
#            comment stance). Wants a cheap, constrained model.
#   summary — the Stage-2 SUMMARIZATION model (post summary + comment summary).
#            Wants a fluent one, which is a different requirement: classification
#            picks from a fixed vocabulary, summarization writes Bangla prose.
#            Splitting them is also the honest version of the cost story — the
#            expensive model is used for the one task that needs it, once per
#            post, rather than for every classification call.
#   llm_a / llm_b — the architectural LLM-A (fast) / LLM-B (quality) roles still
#            used by the agents + report layer (services/agents, reports.py).
#   vlm    — vision-language model for image-grounded summaries.
_ROLE_LOCAL_ENV: dict[str, str] = {
    "stage1": "STAGE1_LOCAL_MODEL",
    "stage2": "STAGE2_LOCAL_MODEL",
    "summary": "SUMMARY_LOCAL_MODEL",
    "llm_a": "LLM_A_LOCAL_MODEL",
    "llm_b": "LLM_B_LOCAL_MODEL",
    "vlm":   "VLM_LOCAL_MODEL",
}

_ROLE_GROQ_ENV: dict[str, str] = {
    "stage1": "STAGE1_GROQ_MODEL",
    "stage2": "STAGE2_GROQ_MODEL",
    "summary": "SUMMARY_GROQ_MODEL",
    "llm_a": "LLM_A_GROQ_MODEL",
    "llm_b": "LLM_B_GROQ_MODEL",
    "vlm":   "VLM_GROQ_MODEL",
}

# The default backend is `local` (LLM_BACKEND), so these Ollama model ids are the
# ids actually used out of the box for Stage 1 and Stage 2 — gemma3:4b (fast) for
# Stage 1, qwen2.5:7b (quality) for Stage 2. Override per role with the
# *_LOCAL_MODEL env vars; any model from `ollama list` works.
_ROLE_LOCAL_DEFAULT: dict[str, str] = {
    "stage1": "gemma3:4b",
    "stage2": "qwen2.5:7b",
    # `summary` defaults to the same model as `stage2` so nothing changes until
    # a bake-off picks a winner — see eval/bakeoff_summary.py, which scores the
    # candidates in `ollama list` (qwen2.5:7b, gemma4:26b, gemma4:31b) on real
    # Bangla posts. Point SUMMARY_LOCAL_MODEL at the winner and record the
    # numbers; "we chose it because it scored X" is defence material in a way
    # that "we chose it because it is bigger" is not.
    "summary": "qwen2.5:7b",
    "llm_a": "qwen2.5:7b",
    "llm_b": "qwen2.5:7b",
    "vlm":   "qwen3-vl:4b",
}

# Only consulted when LLM_BACKEND=groq (opt-in); local/Ollama is the default.
_ROLE_GROQ_DEFAULT: dict[str, str] = {
    "stage1": "llama-3.1-8b-instant",
    "stage2": "llama-3.3-70b-versatile",
    "summary": "llama-3.3-70b-versatile",
    "llm_a": "llama-3.1-8b-instant",
    "llm_b": "llama-3.3-70b-versatile",
    "vlm":   "meta-llama/llama-4-scout-17b-16e-instruct",
}

VALID_ROLES = frozenset(_ROLE_LOCAL_DEFAULT)


def _is_degenerate_json(content: Optional[str]) -> bool:
    """True when a JSON-mode reply is well-formed but carries no content.

    Grammar-constrained decoding lets a weak model bail out with `{}` / `[]` /
    `null` instead of failing outright, which is indistinguishable from "the
    model had nothing to say" unless we check for it here.
    """
    s = (content or "").strip()
    if not s:
        return True
    try:
        parsed = json.loads(s)
    except ValueError:
        return False  # unparseable — the caller's own parser/fallback handles it
    return parsed is None or (isinstance(parsed, (dict, list)) and not parsed)


def _max_continuations() -> int:
    """How many times chat() may re-ask to recover a length-truncated answer.

    Read per call (not captured at import) so a test or an eval run can change
    the budget without re-importing the module.
    """
    try:
        return max(0, int(os.environ.get("LLM_MAX_CONTINUATIONS", "2")))
    except ValueError:
        return 2


# Appended as a user turn after the model's partial answer when continuing a
# reply that stopped at the token ceiling. Deliberately language-neutral: the
# truncation this recovers is language-correlated (Bangla costs far more tokens
# per character than English), so the instruction must not nudge the model into
# switching language mid-summary.
_CONTINUE_INSTRUCTION = (
    "Your previous message was cut off because it reached the length limit. "
    "Continue from exactly where it stopped, in the same language and style. "
    "Do not repeat any text you have already written and do not restate the task."
)


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
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        # Tool definitions. Their absence here is why the agent runner reached
        # past this class into the raw AsyncOpenAI client — and lost the circuit
        # breaker, the Groq→local failover, truncation recovery and usage
        # tracking on the way (PROJECT_ASSESSMENT §13.6).
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

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
        model: Optional[str] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        usage_redis: Any = None,
        usage_lane: str = LANE_INTERACTIVE,
        usage_task: Optional[str] = None,
        track: bool = True,
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
            "finish_reason": str,   # "stop" | "length" | ... as reported by the backend
            "truncated": bool,      # True when the reply STILL ends at the token ceiling
            "continuations": int,   # how many follow-up calls were spent recovering it
            "tool_calls": list,     # OpenAI tool_calls, normalised; [] when none
        }

        Pass ``tools`` (OpenAI function-tool definitions) to enable tool calling.
        A reply carrying tool calls is returned as-is: continuation and the
        JSON-degeneracy retry are both skipped for it, because the model stopped
        to call a tool rather than because it ran out of room.

        **Every successful call is recorded** in the Redis usage counters
        `GET /v1/usage` reads, dimensioned by ``usage_lane`` and ``usage_task``.
        Tracking here rather than at each call site is deliberate: it lived in
        the Stage-2 worker, so the five other callers in the system spent tokens
        that reached no counter while the endpoint reported its figures as
        system-wide (PROJECT_ASSESSMENT §13.4). Pass ``track=False`` for calls
        that must not count (offline evals); pass ``usage_redis`` to reuse a
        connection you already hold.

        A completion that stopped because it hit ``max_tokens`` used to be
        returned exactly like a completed one, so no caller could tell a finished
        summary from half of one. Free-text replies (``response_format is None``)
        are now auto-continued up to ``LLM_MAX_CONTINUATIONS`` times; whatever
        state the answer ends in is reported in ``truncated`` so the caller can
        flag it and refuse to cache it.

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

        # An explicit `model` overrides the role→model default for the chosen
        # backend. On any failover to local below we drop the override and use the
        # local role default, since a backend-specific model id won't exist there.
        model_id = model or self._resolve_model(role, effective_backend)

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
                tools=tools,
                tool_choice=tool_choice,
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
                # Record the local outcome on the local breaker either way. Only
                # the success used to be recorded, so a local backend that failed
                # *as the Groq fallback* never counted toward its own failure
                # threshold — the local circuit could not open along the one path
                # that hits it hardest (every Groq failure re-fires at local), and
                # `chat_stream` had the same gap until it was fixed there.
                try:
                    completion = await self._call_api(
                        client=self._get_client("local"),
                        model=model_id,
                        messages=messages,
                        response_format=response_format,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        tools=tools,
                        tool_choice=tool_choice,
                    )
                except Exception as local_exc:
                    self._breakers["local"].record_failure()
                    log.error(
                        "llm_local_fallback_failed role={} model={} error={}",
                        role, model_id, local_exc,
                    )
                    raise
                self._breakers["local"].record_success()
            else:
                log.error(
                    "llm_call_failed backend={} role={} model={} error={}",
                    effective_backend, role, model_id, exc,
                )
                raise

        choice = completion.choices[0]

        # Usage accumulates across the degeneracy retry and every continuation,
        # so the cost counters see the true token spend of the recovered answer
        # rather than only its last part. Seeded here, BEFORE the retry below can
        # rebind `completion` — accumulating afterwards silently dropped the
        # degenerate call's tokens from every usage counter.
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        def _accumulate(usage_obj) -> None:
            if not usage_obj:
                return
            totals["prompt_tokens"] += usage_obj.prompt_tokens or 0
            totals["completion_tokens"] += usage_obj.completion_tokens or 0
            totals["total_tokens"] += usage_obj.total_tokens or 0

        _accumulate(completion.usage)

        # JSON-mode degeneracy retry: Ollama turns `response_format=json_object`
        # into grammar-constrained decoding, and small models (gemma3:4b) satisfy
        # that grammar with the *empty* object `{}` — 2 completion tokens, a
        # syntactically valid answer carrying no analysis. Every caller then reads
        # its own defaults back out as if the model had judged them (a post scored
        # "neutral 0.00" that is plainly negative). Retry once unconstrained; the
        # same model/prompt then answers properly and callers' parsers already
        # tolerate ```json fences.
        if response_format is not None and _is_degenerate_json(choice.message.content):
            log.warning(
                "llm_json_mode_degenerate_retrying_unconstrained backend={} role={} model={}",
                effective_backend, role, model_id,
            )
            try:
                completion = await self._call_api(
                    client=self._get_client(effective_backend),
                    model=model_id,
                    messages=messages,
                    response_format=None,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                self._breakers[effective_backend].record_success()
            except Exception as exc:
                self._breakers[effective_backend].record_failure()
                if effective_backend == "groq":
                    log.warning(
                        "llm_json_retry_groq_failed_falling_back_to_local role={} model={} error={}",
                        role, model_id, exc,
                    )
                    effective_backend = "local"
                    model_id = self._resolve_model(role, "local")
                    try:
                        completion = await self._call_api(
                            client=self._get_client("local"),
                            model=model_id,
                            messages=messages,
                            response_format=None,
                            max_tokens=max_tokens,
                            temperature=temperature,
                        )
                    except Exception as local_exc:
                        self._breakers["local"].record_failure()
                        log.error(
                            "llm_json_retry_local_fallback_failed role={} model={} error={}",
                            role, model_id, local_exc,
                        )
                        raise
                    self._breakers["local"].record_success()
                else:
                    log.error(
                        "llm_json_retry_failed backend={} role={} model={} error={}",
                        effective_backend, role, model_id, exc,
                    )
                    raise
            choice = completion.choices[0]
            _accumulate(completion.usage)

        content = choice.message.content or ""
        finish_reason = getattr(choice, "finish_reason", None) or "stop"

        # Tool calls, normalised into plain serialisable dicts so callers never
        # touch the SDK's objects. This normalisation used to live in the agent
        # runner, which is the only reason that module reached past this class
        # (§13.6).
        tool_calls_out: list[dict[str, Any]] = []
        for tc in (getattr(choice.message, "tool_calls", None) or []):
            tool_calls_out.append({
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            })

        # Auto-continuation: a reply that stopped at the token ceiling is half an
        # answer, and nothing downstream could previously tell. Re-ask with the
        # partial text as context and concatenate. Only for free-text — a
        # JSON-mode reply cannot be continued token-wise into valid JSON, so
        # those are reported as truncated and left to the caller's parser.
        #
        # Never continue a reply that carries tool calls: the model stopped to
        # invoke a tool, and asking it to "continue" would produce prose the
        # caller's tool loop has no slot for.
        continuations = 0
        if response_format is None and finish_reason == "length" and not tool_calls_out:
            budget = _max_continuations()
            while finish_reason == "length" and continuations < budget:
                continuations += 1
                log.warning(
                    "llm_truncated_continuing backend={} role={} model={} attempt={}/{}",
                    effective_backend, role, model_id, continuations, budget,
                )
                cont = await self._call_api(
                    client=self._get_client(effective_backend),
                    model=model_id,
                    messages=[
                        *messages,
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": _CONTINUE_INSTRUCTION},
                    ],
                    response_format=None,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                cont_choice = cont.choices[0]
                _accumulate(cont.usage)
                extra = (cont_choice.message.content or "").strip()
                finish_reason = getattr(cont_choice, "finish_reason", None) or "stop"
                if not extra:
                    break
                # Join without swallowing a word boundary; the model resumes
                # mid-sentence as often as it resumes at one.
                content = content.rstrip() + ("" if content.rstrip().endswith("-") else " ") + extra

        truncated = finish_reason == "length"
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        log.info(
            "llm_call backend={} role={} model={} tokens={} latency_ms={} "
            "finish_reason={} truncated={} continuations={}",
            effective_backend, role, completion.model, totals["total_tokens"], latency_ms,
            finish_reason, truncated, continuations,
        )
        if truncated:
            log.warning(
                "llm_response_truncated backend={} role={} model={} max_tokens={} chars={}",
                effective_backend, role, model_id, max_tokens, len(content),
            )

        result = {
            "content": content,
            "usage": totals,
            "model": completion.model,
            "backend": effective_backend,
            "finish_reason": finish_reason,
            "truncated": truncated,
            "continuations": continuations,
            "tool_calls": tool_calls_out,
        }

        # Count it. `track_usage` swallows its own failures — a cost counter must
        # never be the reason a call fails — so this is safe on the success path.
        if track:
            await track_usage(
                usage_redis,
                result,
                lane=usage_lane,
                task=usage_task or role,
            )

        return result

    async def chat_stream(
        self,
        role: str,
        messages: list[dict[str, Any]],
        backend_override: Optional[str] = None,
        tenant_policy: Optional[TenantPolicy] = None,
        max_tokens: int = 1024,
        temperature: float = 0.3,
        model: Optional[str] = None,
        usage_redis: Any = None,
        usage_lane: str = LANE_INTERACTIVE,
        usage_task: Optional[str] = None,
        track: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a chat completion token-by-token (for the chatbot endpoint).

        Backend resolution and tenant-policy enforcement match ``chat()``. Yields
        a sequence of event dicts:

            {"type": "meta",  "backend": str, "model": str}          # once, first
            {"type": "delta", "content": str}                         # zero or more
            {"type": "done",  "backend": str, "model": str, "usage": {...}}  # last
            {"type": "error", "error": str}                           # instead of done, on failure

        Failover to local happens only when Groq fails BEFORE the first token —
        once bytes are on the wire a mid-stream failure just ends with an error
        event (we can't cleanly restart a partially-streamed answer).
        """
        effective_backend = enforce_policy(
            tenant_policy=tenant_policy,
            requested_backend=backend_override,
            default_backend=self._default_backend,
        )
        # An explicit `model` overrides the role→model default; dropped on any
        # failover to local (a backend-specific model id won't exist there).
        model_id = model or self._resolve_model(role, effective_backend)

        # Preemptive failover: Groq circuit open → go straight to local.
        if effective_backend == "groq" and not self._breakers["groq"].allow():
            log.warning("llm_stream_groq_circuit_open_preempting_local role={}", role)
            effective_backend = "local"
            model_id = self._resolve_model(role, "local")

        async def _open(backend: str, model: str):
            kwargs = dict(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=True,
            )
            # Ollama (local backend) may reject stream_options, so only request
            # streaming usage for remote backends. Without this, streamed
            # responses report zero token usage and cost tracking under-counts.
            if backend != "local":
                kwargs["stream_options"] = {"include_usage": True}
            return await self._get_client(backend).chat.completions.create(**kwargs)

        t0 = time.perf_counter()
        try:
            stream = await _open(effective_backend, model_id)
        except Exception as exc:
            self._breakers[effective_backend].record_failure()
            if effective_backend == "groq":
                log.warning(
                    "llm_stream_groq_open_failed_falling_back_to_local model={} error={}",
                    model_id, exc,
                )
                effective_backend = "local"
                model_id = self._resolve_model(role, "local")
                # A failing fallback must end the stream with an `error` event
                # like every other failure path. Letting it propagate raises out
                # of the async generator instead, which reaches the SSE bridge as
                # an unhandled exception mid-response rather than as a frame the
                # client can render.
                try:
                    stream = await _open(effective_backend, model_id)
                except Exception as local_exc:
                    self._breakers["local"].record_failure()
                    log.error(
                        "llm_stream_local_fallback_failed model={} error={}",
                        model_id, local_exc,
                    )
                    yield {"type": "error", "error": str(local_exc)}
                    return
            else:
                log.error("llm_stream_open_failed backend={} error={}", effective_backend, exc)
                yield {"type": "error", "error": str(exc)}
                return

        yield {"type": "meta", "backend": effective_backend, "model": model_id}

        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        try:
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens or 0,
                        "completion_tokens": chunk.usage.completion_tokens or 0,
                        "total_tokens": chunk.usage.total_tokens or 0,
                    }
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    yield {"type": "delta", "content": delta}
            self._breakers[effective_backend].record_success()
        except Exception as exc:
            self._breakers[effective_backend].record_failure()
            log.error("llm_stream_failed backend={} model={} error={}", effective_backend, model_id, exc)
            yield {"type": "error", "error": str(exc)}
            return

        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        log.info(
            "llm_stream backend={} role={} model={} tokens={} latency_ms={}",
            effective_backend, role, model_id, usage["total_tokens"], latency_ms,
        )
        # Count the stream too. A streamed chatbot turn costs exactly what a
        # non-streamed one costs, and counting only the latter would rebuild
        # §13.4's blind spot on the endpoint the dashboard actually uses.
        if track:
            await track_usage(
                usage_redis,
                {"usage": usage, "backend": effective_backend, "model": model_id},
                lane=usage_lane,
                task=usage_task or role,
            )
        yield {"type": "done", "backend": effective_backend, "model": model_id, "usage": usage}

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

    # ------------------------------------------------------------------
    # Model discovery (for the chat model picker)
    # ------------------------------------------------------------------

    def default_model(self, role: str, backend: str) -> str:
        """The role→model default for a backend (public wrapper)."""
        return self._resolve_model(role, backend)

    async def list_models(self, backend: str) -> list[str]:
        """Best-effort list of model ids the backend can serve.

        Queries the backend's OpenAI-compatible ``/v1/models`` (Ollama returns
        every pulled model; Groq returns its hosted catalogue). Returns ``[]`` if
        the backend is unreachable or unauthenticated — the caller can still fall
        back to the configured default.
        """
        try:
            resp = await self._get_client(backend).models.list()
        except Exception as exc:
            log.warning("llm_list_models_failed backend={} error={}", backend, exc)
            return []
        ids = {getattr(m, "id", None) for m in getattr(resp, "data", []) or []}
        return sorted(i for i in ids if i)
