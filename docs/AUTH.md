# Auth — Identity, Tokens, API Keys, Tenants, Rate Limits

> **Scope.** Who is calling, what they may do, and how a browser that cannot set
> headers still authenticates a stream. Signup and token issuance, the JWT
> policy, API keys, tenant scoping and the privacy lock, SSE tickets, and
> per-identity rate limiting.
>
> Code: [`api/routers/auth.py`](../src/defense/services/api/routers/auth.py)
> (endpoints), [`api/deps.py`](../src/defense/services/api/deps.py)
> (`get_current_user`, `rate_limit`, tenant policy),
> [`libs/auth.py`](../src/defense/libs/auth.py) (tokens, tickets, key hashing),
> [`libs/common/config.py`](../src/defense/libs/common/config.py) (the secret
> policy), [`libs/ratelimit.py`](../src/defense/libs/ratelimit.py).
> Threat-model context: [architecture.md](architecture.md) §9.

---

## 1. Endpoints

| Endpoint | Auth | Does |
| -------- | ---- | ---- |
| `GET /v1/auth/config` | **none** | What the login screen may offer — whether signup and any-login are enabled |
| `POST /v1/auth/signup` | none | Register a user, return a Bearer JWT |
| `POST /v1/auth/token` | credentials | Obtain a Bearer JWT |
| `POST /v1/auth/refresh` | Bearer | Exchange a valid token for a fresh one (sliding session) |
| `GET /v1/auth/me` | Bearer | Who am I — lets a client tell *"no token"* from *"expired token"* |
| `POST /v1/auth/sse-ticket` | Bearer | Mint a single-use, short-lived stream ticket |
| `GET /v1/auth/verify` | — | Validate a raw token without establishing a session |

`GET /v1/auth/config` is unauthenticated on purpose: the login screen has to know
which deployment it is looking at *before* anyone has a credential.

**Signup is what makes the rest reachable without a psql session.** The dashboard
could verify a password but nothing could *create* one, so every deployment either
seeded the `users` table by hand or lived on the `ALLOW_ANY_LOGIN` dev path.

## 2. The JWT policy

HS256, correct `datetime` claims, expiry enforced by `python-jose`. The **core was
always sound**; what was broken sat around it, and each fix is worth knowing:

- **The login endpoint issued a signed 24-hour token to *anybody*.** Now
  credentials are checked, and the permissive path is an explicit, env-gated dev
  mode.
- **There was no refresh / verify / revocation route**, so the dashboard could not
  distinguish "no token" from "expired token" and a leaked token lived its full
  lifetime. `/refresh`, `/me` and `/verify` close that.
- **`EventSource` cannot set headers**, so the credential travelled in a query
  parameter. Replaced by single-use tickets (§4).

**Claims are allow-listed.** Only `sub`, `tenant_id`, `role`, `exp`, `iat`,
`scope` are accepted from a token — a token cannot smuggle extra fields into the
principal.

**The secret is read fresh on every call**, from the environment first:

```python
os.environ["JWT_SECRET"] or settings.jwt_secret or JWT_DEV_DEFAULT_SECRET
```

`get_settings()` is `lru_cache`d, so reading only through it froze the secret at
first import — the issuer and the verifier could then sign and check with
different values for the rest of the process's life. Reading the environment
means **rotating `JWT_SECRET` invalidates issued tokens without a restart**.

**Placeholder secrets are blocked outside dev.** `change-me-in-production`,
`demo`, `secret` and the generated dev default are on a blocklist; outside
`dev`/`development`/`local`/`test`/`ci` (`APP_ENV`), `require_jwt_secret()`
raises rather than letting the service start on a secret that ships in this
repository. Startup logs `jwt_verifier_ready is_default_secret=… secret_fingerprint=…`
so which secret is in force is visible without printing it.

## 3. API keys and tenants

`get_current_user` accepts either a Bearer JWT or an API key. For a key,
`_principal_from_api_key` looks it up **by hash** in `api_keys`
(`active = TRUE`) — and **the tenant comes from the row, not from the client**.
That is the whole point of the table: a caller cannot name its own tenant.

Two failure behaviours worth knowing, because both were bugs:

- **An unknown key returns `None`, and the caller decides** whether that is a 401
  or (in dev) a fall-through to permissive MVP behaviour. The decision is not
  buried in the lookup.
- **A lookup failure rolls back the transaction.** On Postgres a failed statement
  aborts the whole transaction, and this is the *request's* session — the endpoint
  handler runs its own queries on it moments later. Without the rollback,
  swallowing the error only *looked* like graceful degradation: the request went
  on to die with `InFailedSQLTransactionError`, a 500 from whatever endpoint the
  caller was hitting, in the exact failure mode the branch exists to avoid. Only
  the first request per process showed it (a 30-second retry-suppression flag
  skips the lookup afterwards), which is what made it easy to miss.

### Tenant scoping

`tenant_id` rides on the principal and is applied **in the `WHERE` clause** of
tenant-scoped queries — posts, analysis results, conversations, jobs, retrieval.
Chat history additionally scopes on `(tenant_id, username)`
([CHAT.md](CHAT.md) §5).

### The privacy lock

`tenant_policies` may mark a tenant `privacy_locked`, which pins it to the
`local` backend on **every** path: analysis enqueue (stamped into the job
envelope), resume (re-resolved, not trusted), chat, and agents. `groq` is
refused even when the request explicitly asks for it. Flipping the *global*
backend switch is additionally restricted to `admin`/`owner`/`operator` roles,
because selecting `groq` routes every tenant's analysis off-box. Details:
[LLM_BACKENDS.md](LLM_BACKENDS.md) §3.

## 4. SSE tickets — the fix for a credential in a URL

`EventSource` cannot send headers, so a streaming client needs *something* in the
URL. The old answer was the long-lived credential itself, which then sat in proxy
logs and browser history — and server-side a query parameter was treated as an
**API key**.

A ticket is the right something: **single-use, ~60 s (`SSE_TICKET_TTL`), bound to
the minting principal.** It leaks harmlessly.

```
POST /v1/auth/sse-ticket        (with your normal credential)
EventSource("…/stream?ticket=<ticket>")
```

Every SSE endpoint in the system takes tickets: analysis progress, pipeline,
logs, system telemetry.

## 5. Rate limiting

The `rate_limit` dependency enforces a per-identity budget per minute on
expensive endpoints — analysis runs, report generation, agent queries, chat.

- Identity is `sub` when authenticated; otherwise `X-Forwarded-For` →
  `X-Real-IP` → peer address, **in that order**, so that behind a reverse proxy
  all anonymous traffic does not share one bucket.
- Sets `X-RateLimit-Limit` / `X-RateLimit-Remaining`, and raises **429** with
  `Retry-After` when exceeded.
- Default `RATE_LIMIT_PER_MIN=600`; `RATE_LIMIT_ENABLED=false` disables it.

## 6. Configuration

| Env var | Default | Effect |
| ------- | ------- | ------ |
| `APP_ENV` | `dev` | Gates the placeholder-secret check; fallback for `ALLOW_ANY_LOGIN` / `ALLOW_SIGNUP` |
| `JWT_SECRET` | *(empty → generated dev secret)* | Signing secret, read fresh per call. Placeholders blocked outside dev |
| `JWT_EXPIRE_HOURS` | `12` | Token lifetime |
| `SSE_TICKET_TTL` | `60` | Stream-ticket lifetime, seconds |
| `ALLOW_ANY_LOGIN` | *(empty → follows `APP_ENV`)* | Accept any credentials — **already on in dev** |
| `ALLOW_SIGNUP` | *(empty → follows `APP_ENV`)* | Self-registration |
| `SIGNUP_TENANT_ID` | `default` | Tenant assigned to self-registered users |
| `RATE_LIMIT_ENABLED` | `true` | Master switch |
| `RATE_LIMIT_PER_MIN` | `600` | Per-identity budget |

The repository's `.env` ships `APP_ENV=dev`, `ALLOW_ANY_LOGIN=true`,
`ALLOW_SIGNUP=true` and the placeholder `JWT_SECRET` — a **development**
configuration, and the placeholder guard is exactly what stops it being deployed
as-is. Full list: [env.example.md](env.example.md).

## 7. Evidence class

| Claim | State |
| ----- | ----- |
| Credentials verified; permissive login is env-gated | ✅ Measured |
| Refresh / verify / `me` distinguish token states | ✅ Measured |
| Secret rotation invalidates tokens without restart | ✅ Measured |
| Placeholder secret refuses to start outside dev | ✅ Measured |
| JWT claims allow-listed | ✅ Measured |
| API-key tenant comes from the row | ✅ Measured |
| API-key lookup failure does not 500 the request | ✅ Measured (the rollback is asserted) |
| Single-use SSE tickets on every stream | ✅ Measured |
| Privacy lock holds on all four paths | ✅ Measured |
| Per-identity rate limiting with proxy-aware identity | ✅ Measured |
| **Penetration testing / external security review** | 📋 **Not done.** No adversarial audit has been run against a deployed instance; the findings above come from code review and regression tests |

Cross-references: [architecture.md](architecture.md) §9 ·
[api_design.md](api_design.md) · [LLM_BACKENDS.md](LLM_BACKENDS.md) ·
[CHAT.md](CHAT.md) · [DASHBOARD_UI.md](DASHBOARD_UI.md) (login flow) ·
[env.example.md](env.example.md).
