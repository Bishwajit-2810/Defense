# Chat — Free-Form Chatbot and Agent Handover

> **Scope.** The conversational surface: plain chat against the platform LLM,
> token streaming over SSE, automatic routing of a question to one of the nine
> agents when it needs corpus data, and persisted conversation history.
>
> Code: [`api/routers/chat.py`](../src/defense/services/api/routers/chat.py) (chat,
> stream, agent routing), [`chat_history.py`](../src/defense/services/api/routers/chat_history.py)
> (conversations). The agents themselves are [AGENTS.md](AGENTS.md); backend
> resolution is [LLM_BACKENDS.md](LLM_BACKENDS.md).

---

## 1. Endpoints

| Endpoint | Does |
| -------- | ---- |
| `POST /v1/chat` | One reply → `{reply, backend, model, usage}` |
| `POST /v1/chat/stream` | Server-Sent Events — token-by-token streaming |
| `POST /v1/chat/agent` | Decide **who answers**: plain chat, or one of the nine agents |
| `GET /v1/chat/models` | Which models the chat can use, per backend |
| `GET/POST /v1/chat/conversations` | List (newest first) / create |
| `GET/PATCH/DELETE /v1/chat/conversations/{id}` | Read with turns / rename / delete (turns cascade) |
| `POST /v1/chat/conversations/{id}/messages` | Append turns |
| `DELETE /v1/chat/conversations` | Clear all of the caller's history |

The dashboard's **Chat** tab is the client — [DASHBOARD_UI.md](DASHBOARD_UI.md).

## 2. Backend resolution

Per request, identical to the rest of the system:

```
request.backend ("local"|"groq")
  > Redis toggle  config:llm_backend   (dashboard LLM chip, run_all.py --groq/--ollama)
  > LLM_BACKEND env
  > "local"
```

Send `backend: "auto"` (or omit it) to follow the toggle; send `"local"` or
`"groq"` to force one for this call.

## 3. The privacy lock applies here too

A privacy-locked tenant **cannot force `groq`** from the chat box. The same
`check_llm_backend_policy` the analysis endpoints use runs on `/v1/chat` and
`/v1/chat/agent`. A chat surface is exactly where a privacy control gets
forgotten, which is why it is enforced at the same layer rather than reimplemented
here ([LLM_BACKENDS.md](LLM_BACKENDS.md) §3).

Chat spend lands in the **`interactive`** usage lane, deliberately outside
`pipeline_tokens` — so however much anyone uses the chatbot, the per-post cost
figure is unaffected, while the tokens still appear in the global totals and
per-model spend ([LLM_BACKENDS.md](LLM_BACKENDS.md) §4).

## 4. Who answers — `POST /v1/chat/agent`

Plain chat can reason but cannot *look*. A question about the corpus needs an
agent with MCP tools. This endpoint decides which, and returns one of two shapes:

| Response | Meaning |
| -------- | ------- |
| `{"mode": "chat", "agent": null, "reason": …}` | No corpus data needed — the caller should use `POST /v1/chat/stream` as normal |
| `{"mode": "agent", …}` | Merged with the agents-service response: either a finished run, or a `run_id` to poll at `GET /v1/agents/{run_id}` |

### The routing decision

`agent` in the request selects the policy:

| Value | Behaviour |
| ----- | --------- |
| `auto` (or omitted) | An LLM router classifies the message against the agent catalogue |
| a known agent name | Pinned by the operator |
| `none` / `chat` / `off` | Plain chat |
| anything else | **422** with the list of known agents |

The `auto` router is a small JSON-mode call (`max_tokens=200`) given the agent
catalogue — each agent's name and description — plus **two turns** of prior
context, which is enough to resolve a follow-up like *"and the other campaign?"*
without paying to classify the whole conversation. Recognised small talk
short-circuits to plain chat before any LLM call.

**Routing never raises.** A router that is down must degrade to *answering* the
message, not to failing it.

### Handover is reported, not hidden

If the previous turn was answered by a different agent, the response carries
`switched_from`. A mid-conversation handover is a fact about the answer, not a UI
detail: the operator asked one follow-up and a different analyst with a different
toolset picked it up. The response says which, and why.

### A missing agents service degrades

If the agents sidecar is unreachable the endpoint returns
`{"mode": "chat", "degraded": true, "reason": "agents service unreachable …"}`
rather than a 502. The agent layer is optional in this deployment, and a 502 would
make a missing sidecar look like a broken chat box.

## 5. Conversation history

Persisted in `chat_conversations` / `chat_messages` (Postgres).

**Ownership is `(tenant_id, username)` and it is applied in the `WHERE` clause of
every statement** — not checked after the fact. A conversation id is a guessable
opaque string, and "SELECT by id, then compare the owner" is one forgotten branch
away from serving someone else's chat.

**An assistant turn stores its `meta` verbatim**: which agent answered, which MCP
servers and tools it called, its citations. That is what makes a reopened
conversation *auditable* rather than merely readable — and it is the same record
that makes an ungrounded answer provable after the fact
([AGENTS.md](AGENTS.md)).

## 6. Rate limiting

`/v1/chat/agent` (and the other user-driven endpoints) run behind the
`rate_limit` dependency — Redis-backed, per caller. Chat is the one endpoint in
the system where an unbounded number of LLM calls is a *user* decision, so it is
also the one where a limit is load-bearing rather than defensive
([AUTH.md](AUTH.md) §5).

## 7. Evidence class

| Claim | State |
| ----- | ----- |
| Chat on both backends, switchable per request and globally | ✅ Measured |
| Token streaming over SSE | ✅ Measured |
| Privacy lock enforced on the chat path | ✅ Measured |
| Chat spend counted in the `interactive` lane, outside `pipeline_tokens` | ✅ Measured |
| Conversation ownership enforced in-query | ✅ Measured |
| Agent handover reported (`switched_from`) | ✅ Measured |
| Degradation to plain chat when the agents service is down | ✅ Measured |
| **Routing accuracy** — does `auto` pick the right agent? | 📋 **Unmeasured.** No labelled question→agent set exists; the router is an LLM classifier with no scorecard |
| Answer quality / groundedness of plain chat | 📋 **Unmeasured** — plain chat has no retrieval and therefore no citations to check |

Cross-references: [AGENTS.md](AGENTS.md) · [MCP_SERVERS.md](MCP_SERVERS.md) ·
[LLM_BACKENDS.md](LLM_BACKENDS.md) · [AUTH.md](AUTH.md) ·
[DASHBOARD_UI.md](DASHBOARD_UI.md) · [endpoints.md](endpoints.md).
