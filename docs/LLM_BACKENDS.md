# LLM Backends — Roles, Pluggable Serving, Policy, and Cost Telemetry

> **Scope.** The one client every LLM call in this system goes through: the seven
> **roles**, the two **backends** (`local` ⇄ `groq`) and how they are switched at
> runtime, tenant privacy locking, the circuit breaker and retry policy, and the
> usage counters `GET /v1/usage` reads.
>
> Code: [`libs/llm/client.py`](../src/defense/libs/llm/client.py) (roles, model
> resolution, continuation, streaming),
> [`policy.py`](../src/defense/libs/llm/policy.py) (tenant locking),
> [`circuit.py`](../src/defense/libs/llm/circuit.py) (breaker),
> [`usage.py`](../src/defense/libs/llm/usage.py) (counters),
> [`api/routers/config.py`](../src/defense/services/api/routers/config.py) (the
> runtime toggle). Model *selection* rationale is [models.md](models.md); costs
> are [cost_estimation.md](cost_estimation.md).

---

## 1. Two backends, one OpenAI-compatible API

| Backend | Serving | Trade |
| ------- | ------- | ----- |
| **`local`** (default) | Self-hosted vLLM in production, Ollama for local dev | No per-token bill, no data egress. Its marginal token cost genuinely **is** zero. |
| **`groq`** | Groq Cloud API | Fastest inference, zero GPU ops, per-token cost |

Both speak an OpenAI-compatible API, so switching is a config change rather than
a code change. Resolution order, per call:

```
request.backend ("local"|"groq")
  > Redis override  config:llm_backend      (the dashboard LLM chip, run_all.py --groq/--ollama)
  > LLM_BACKEND env
  > "local"
```

`PUT /v1/config/llm` only sets or clears the **Redis override** — it never mutates
the environment, so a worker restart always returns to the deployed default. Send
`backend: "auto"` (or omit it) on a request to follow the toggle; send `"local"`
or `"groq"` to force one for that call.

Flipping the **global** switch is restricted to `admin` / `owner` / `operator`
roles, because selecting `groq` routes every tenant's analysis off-box.

## 2. Seven roles — the cost story is in this table

A role is *what the call is for*; the model each resolves to is configuration.

| Role | Used by | Wants | `local` code default | `groq` code default |
| ---- | ------- | ----- | -------------------- | ------------------- |
| `stage1` | Stage-1 NLP over caption + comments | One fast, small model carrying high-volume work | `qwen2.5:7b` | `llama-3.3-70b-versatile` |
| `stage2` | Post-type, insight, comment stance | Cheap and **constrained** — picks from a fixed vocabulary | `qwen2.5:7b` | `llama-3.3-70b-versatile` |
| `summary` | Post summary + comment-thread summary | **Fluent** — writes Bangla prose | `qwen2.5:7b` | `llama-3.3-70b-versatile` |
| `agent` | The nine agents | Long context, tool calling | `llama3.1:8b` | `llama-3.3-70b-versatile` |
| `llm_a` | Legacy fast lane (reports) | Fast | `qwen2.5:7b` | `llama-3.1-8b-instant` |
| `llm_b` | Grounded reports, cluster summaries | Quality | `llama3.1:8b` | `llama-3.3-70b-versatile` |
| `vlm` | Image-grounded summaries | Vision | `qwen3-vl:4b` | `meta-llama/llama-4-scout-17b-16e-instruct` |

Override any of them with `{ROLE}_LOCAL_MODEL` / `{ROLE}_GROQ_MODEL`. **The
repository's shipped `.env` overrides two of these**, and both overrides matter:

| Role | Resolved on this checkout | Why |
| ---- | ------------------------- | --- |
| `stage1` local | `gemma3:4b` | The measured 16% routing rate is against *this* model ([ROUTER.md](ROUTER.md) §7) |
| `agent` local | `llama3.1:8b-16k` | A **derived Ollama tag** setting `num_ctx 16384` |

**Why `summary` is a separate role at all.** Classification picks from a fixed
vocabulary and wants a cheap constrained model; summarization writes prose and
wants a fluent one. Splitting them means the expensive model is spent on the one
task that needs it — **once per post** — rather than on every classification
call. It is also what makes the cost story honest, and it is why the response
cache had to start keying on the *resolved model id*
([STAGE2_LLM.md](STAGE2_LLM.md) §4).

**Why the `agent` role needs `-16k`.** `ollama serve` defaults to a 4,096-token
context and **silently discards** the overflow — so on any large tool result the
system prompt and the operator's question were the parts thrown away. Measured:
an 11k-token prompt evaluates **24** tokens on `llama3.1:8b` and all **11,045**
on the `-16k` tag. See [AGENTS.md](AGENTS.md).

## 3. Tenant policy — the privacy lock

```python
TenantPolicy(tenant_id, llm_backend="local", privacy_locked=False)
```

`enforce_policy(policy, requested_backend, default_backend)` returns the
effective backend and raises `PolicyViolationError` when a privacy-locked tenant
would be routed to `groq` — **including** when the request explicitly asks for it.

Privacy-locked tenants are pinned to `local` **on every path**, not just the
analysis endpoints:

- the API resolves each job's backend, applies the lock where the tenant is
  known, and **stamps the decision into the job envelope**, so a global toggle
  cannot route a locked tenant's content off-box after enqueue;
- `resume` re-resolves rather than trusting the stored `llm_backend`, because the
  policy may have been locked in the meantime ([JOBS.md](JOBS.md) §1);
- `/v1/chat` and `/v1/chat/stream` enforce the same policy as the analysis
  endpoints ([CHAT.md](CHAT.md) §3).

Policies live in the `tenant_policies` table.

## 4. Cost telemetry — counters at the client, not the call site

The usage counters used to live in the Stage-2 worker and were incremented there
and nowhere else, so **five other callers spent tokens that reached no counter** —
`/v1/chat`, `/v1/chat/stream`, the reports narrative and cluster summaries,
Stage-1's LLM path, and the agent runner — while `UsageResponse.total_tokens`
said *"Total tokens spent"* and `scope_note` said *"All figures are
system-wide."* They covered Stage 2.

So the counters moved **down** to `LLMClient`, the layer every one of those
callers already goes through. Tracking a call is now the default rather than
something each new call site must remember — which is the only version of this
that stays true, because the original defect happened precisely because five call
sites were added over time and none of their authors knew there was a counter.

### The dimensions

| Counter | Answers |
| ------- | ------- |
| `usage:tokens:{backend}:{model}` | `local`'s marginal cost is **zero** and Groq's per-model prices differ by more than an order of magnitude, so one blended rate is wrong for both — in opposite directions |
| `usage:tokens:lane:{lane}` | Which part of the system spent it |
| `usage:calls:task:{task}` | Which task |

Five lanes, and the split is the point:

| Lane | Contents |
| ---- | -------- |
| `post` | Once per post: summary / post_type / insight |
| `comment` | Many per post: comment stance, comment-thread summary |
| `stage1` | Stage 1's own LLM path — measured at **96% of all calls** in that mode, and none of it was counted before |
| `interactive` | Chat and reports — everything a human waits on |
| `agent` | Agent runs. Its own lane because the cost *shape* differs: a tool-calling loop re-sends a growing transcript every turn, so one agent question can cost many multiples of one chat turn |

`PIPELINE_LANES = (post, comment, stage1)` is what `pipeline_tokens` sums, so
**the per-post cost figure cannot be inflated by however much anyone used the
chatbot**, while chat and agent spend still lands in the global totals and the
per-model spend that `estimated_cost_usd` needs.

`GET /v1/usage` reports `tokens_by_backend_model`, `cost_by_backend_model`, the
lane split and `pipeline_tokens`, with `local` priced at zero.
`LLM_USAGE_TRACKING_DISABLED` turns writes off for offline evals.

There is deliberately **no `llm_usage` table** in ClickHouse — one existed and
nothing ever wrote or read it ([ASSEMBLER.md](ASSEMBLER.md) §3).

## 5. Reliability

**Retry.** `tenacity` with exponential backoff on transient API errors.

**Circuit breaker, per backend.** A repeatedly-failing endpoint (a Groq 5xx storm,
a local vLLM that is down) is taken out of rotation for a cooldown instead of
being hammered on every request. Three states: `closed` (calls flow, consecutive
failures counted) → `open` (short-circuited until the cooldown elapses) →
`half_open` (one trial call; success closes, failure re-opens). Default threshold
5 failures, 30 s cooldown. The clock is injectable, so the state machine is
unit-testable without sleeping.

**Groq → local failover.** A `groq` failure logs
`llm_groq_failed_falling_back_to_local` and retries on `local`, which is why
`GROQ_API_KEY` matters even in a nominally local deployment.

**Degenerate-JSON detection.** `_is_degenerate_json` catches a reply that is
well-formed JSON carrying no content — a real failure mode of small local models
that would otherwise persist as an empty-but-valid result.

**Truncation continuation.** `finish_reason == "length"` triggers auto-continuation
up to `LLM_MAX_CONTINUATIONS`, trimmed to the last complete sentence and flagged;
truncated replies are never cached ([STAGE2_LLM.md](STAGE2_LLM.md) §2).

## 6. Configuration

| Env var | Default | Effect |
| ------- | ------- | ------ |
| `LLM_BACKEND` | `local` | Default backend; any call may override per request |
| `LOCAL_LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible endpoint — the `/v1` suffix is required |
| `LOCAL_LLM_API_KEY` | `ollama` | Ollama ignores it; the OpenAI SDK requires non-empty |
| `GROQ_API_KEY` | — | Used when `LLM_BACKEND=groq` **and** on automatic failover |
| `{STAGE1,STAGE2,SUMMARY,AGENT,LLM_A,LLM_B,VLM}_LOCAL_MODEL` | see §2 | Per-role local model |
| `{…}_GROQ_MODEL` | see §2 | Per-role Groq model |
| `LLM_MAX_CONTINUATIONS` | `2` | Auto-continuation budget |
| `LLM_CACHE_DISABLED` | `false` | Bypass the Stage-2 response cache |
| `LLM_USAGE_TRACKING_DISABLED` | `false` | Stop writing usage counters |
| `OLLAMA_CONTEXT_LENGTH` | — | Read by `ollama serve`, **not** by this app. Prefer a derived `-16k` tag |

Full list with failure modes: [env.example.md](env.example.md).

## 7. Evidence class

| Claim | State |
| ----- | ----- |
| Runtime `local` ⇄ `groq` switching, per request and globally | ✅ Measured |
| Privacy lock holds on analysis, resume, chat and agent paths | ✅ Measured |
| Every LLM caller reaches the usage counters | ✅ Measured — counters live in the client |
| Per-backend/model pricing, `local` at zero | ✅ Measured |
| `pipeline_tokens` isolates per-post spend from chat/agent | ✅ Measured |
| 4k-context truncation on the bare `llama3.1:8b` tag | ✅ **Measured** — 24 of 11,045 prompt tokens evaluated |
| Circuit breaker state machine | ✅ Unit-tested with an injected clock |
| Groq → local failover | 🟡 Works, unmeasured — no failover has been staged under load |
| `local` ⇄ `groq` **quality parity** | 📋 **Unmeasured** — the parity run in [evaluation.md](evaluation.md) §4 needs gold labels |

Cross-references: [models.md](models.md) (which model, and why) ·
[STAGE2_LLM.md](STAGE2_LLM.md) · [AGENTS.md](AGENTS.md) · [CHAT.md](CHAT.md) ·
[REPORTS.md](REPORTS.md) · [cost_estimation.md](cost_estimation.md) ·
[AUTH.md](AUTH.md) (tenants).
