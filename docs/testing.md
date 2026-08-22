# testing.md — how to run the tests

Three suites, three commands. Nothing here needs Docker, Ollama or a running API
**except** the two opt-in groups in §4 — a bare `uv run pytest` is safe on a
machine with a live dev stack, and that is enforced rather than assumed (§4.1).

```bash
# everything, from the repo root
uv run pytest -q                                   # backend      → 1377 passed, 3 skipped
cd dashboard && npm test -- --run                  # dashboard    → 62 passed
cd dashboard && npm run test:e2e                   # browser      → 4 passed
```

One line, if you want a single gate before pushing:

```bash
uv run pytest -q && (cd dashboard && npm test -- --run && npm run lint && npm run build && npm run test:e2e)
```

Counts are from **21 Aug 2026** (1,380 collected, 3 skipped by design). They are
here so a number that moves is visible; update them when you add tests.

### Reading the output

A clean run is **59 lines and exits 0, with no warnings from any of the four
commands** — no pytest warnings summary, `Found 0 warnings and 0 errors` from
oxlint, no Vite `(!)` advisories, and no `stderr` blocks. It was 208 lines with
63 lint warnings, 29 pytest warnings, two Vite advisories and a stack trace on
21 Aug 2026. Anything that appears now is new, and worth reading as a defect
rather than as weather.

Two notes on reading it anyway:

* **`npm run lint` should print `Found 0 warnings and 0 errors.`** It was **63**
  warnings on 21 Aug 2026 — unused imports, unused `catch (e)` bindings,
  computed-then-unrendered variables — and that noise is what made a clean run
  unreadable. Note that oxlint's help text ends "…consider handling this error",
  so `grep -i error` over lint output matches lines that are fine; read the count
  and the exit code, not the word. **Treat a new warning as a defect**: the
  cleanup found `PostModal` rendering `<AlertCircle>` without importing it (a
  `ReferenceError` on the one path that shows a comment-loading error), which the
  build cannot catch because it does not resolve JSX identifiers.
* **The build prints six chunks, not one.** `vite.config.js` splits the vendor
  libraries out (`charts` 179 kB, `react` 182 kB, `icons` 13 kB, app 199 kB),
  which is what cleared Vite's 500 kB advisory on the old single 573 kB bundle.
  Every chunk is still requested on first paint, so this is *not* lazy loading
  and changes nothing at runtime — it buys honest chunking and cache granularity.
  Deferring the charts until a tab draws one would need `React.lazy` + Suspense,
  which is a behaviour change and deliberately not part of a build-config fix.
* **A `stderr` block with a stack trace means a test is printing, not failing.**
  One test deliberately fails a request; it spies on `console.error` and asserts
  the component logged, instead of letting the trace dump into a green run. Do
  the same for any new error-path test.

**`pytest` should print no warnings summary at all.** It printed *29* until
21 Aug 2026, and 28 of those were our own code calling deprecated APIs — not
third-party noise:

| Source | Count | Fixed by |
| --- | --- | --- |
| `@app.on_event("startup"/"shutdown")` in both FastAPI apps | 6 | a `lifespan` context (`tests/test_app_lifespan.py` proves it still runs) |
| `redis.setex` in `agents/store.py` | 13 | `set(..., ex=…)` — the same command |
| `HTTP_422_UNPROCESSABLE_ENTITY` across four routers | 7 | `HTTP_422_UNPROCESSABLE_CONTENT` (same value, 422) |
| `testcontainers.postgres` / `.redis` imports in `conftest.py` | 2 | the `testcontainers.community.*` paths |
| sklearn HDBSCAN `copy` default changing in 1.10 | 1 | passing `copy=False` explicitly |

So treat a warnings summary as a to-do list, not as background noise: each line
names a call site of ours that a future release will remove. The `on_event`
migration is the one worth care — nothing in the suite used `TestClient` as a
context manager, so a lifespan that never fired would have passed every test and
served 503s in production. That is why `tests/test_app_lifespan.py` enters the
context and asserts the agents service's runner is actually wired.

---

## 1. Backend — `pytest`

```bash
uv run pytest -q                      # the whole suite, ~50 s
uv run pytest                         # same, with per-test names
uv run pytest tests/test_job_lifecycle.py -q          # one file
uv run pytest -q -k "cancel or resume"                # by name substring
uv run pytest -q -x --ff                              # stop at the first failure, failed-first
uv run pytest -q -rs                                  # …and list why anything skipped
```

`uv run` is the reliable spelling: it resolves the project venv, and the suite
needs the `dev` extra (`pytest-asyncio` especially — without it the async tests
are silently *not collected* rather than failed). `.venv/bin/python -m pytest -q`
is equivalent if the venv is already synced.

**No setup step.** `pyproject.toml` supplies everything:

| Setting | Why it exists |
| --- | --- |
| `testpaths = ["tests"]` | Specifies the test directories to discover tests from. |
| `pythonpath = [".", "src", "src/defense"]` | The workers use bare imports (`from libs.… import`) because each is launched with its own package root, while the installed package is `defense.…`. Tests import **both** spellings, so both roots must be importable or a whole class of contract test silently stops collecting. |

And `tests/conftest.py` forces the environment the suite assumes:

* **`HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE`** — no test may reach Hugging Face
  for weights. Several files used to spend 25–200 s each waiting on a download
  for a model whose *absence* was the assertion. `DEFENSE_TEST_ALLOW_DOWNLOADS=1`
  opts out.
* **`STAGE1_LLM=false`** — a bare `ModelRegistry()` otherwise tries to reach
  Ollama and burns the full retry budget (65 s) for one assertion about a stub.
* **`LOG_TO_REDIS=0`** — the Redis log sink writes `logs:recent` / `logs:live`,
  which is what the dashboard's **Logs** tab reads. Without this, `pytest`
  published its own output into the operator's log view, where the agent suites'
  deliberately fabricated fixtures (`post_id='1234567890abcdef'`, a table of
  "#12345 Economic Growth") appeared as real agent runs at real timestamps. Set
  `LOG_TO_REDIS=1` to opt back in.
* **`.env` is ignored** for the session — `Settings` reads it by default, so
  config assertions had silently become assertions about whatever the developer's
  local file contained (the JWT tests were checking a real deployment secret).

## 2. Dashboard unit tests — `vitest` + jsdom

```bash
cd dashboard
npm test -- --run          # once (CI shape)
npm test                   # watch mode
npm test -- --run src/pages/Posts.test.jsx        # one file
npm run lint               # oxlint — warnings only today, no errors
npm run build              # vite build; catches what a unit test cannot
```

`npm test` alone starts a **watcher** and does not exit — pass `-- --run` in any
script or CI step. Tests live beside the code (`src/**/*.{test,spec}.{js,jsx,ts,tsx}`);
`dashboard/tests/` is Playwright's and is excluded, because vitest was picking
those up and failing them with "Playwright Test did not expect test() to be
called here".

Nine files today: `App`, `MarkdownView`, `sentiment`, `PostModal`, and one per
page for `AnalysisJobs`, `Posts`, `Search`, `Logs` and `Trace`.

**A flaky test is worse than no test.** If a dashboard test fails in the full run
but passes on its own, suspect a race in the *component*, not just the test —
that is how the Trace tab's preselect bug was found: a default selection computed
from a stale `selectedPostId` could overwrite a choice the operator made in the
same tick. Re-run the single file 8×, and the whole suite right after `pytest`
(the machine is busiest then, which is when the original flake appeared).

## 3. Browser tests — Playwright

```bash
cd dashboard && npm run test:e2e              # headless chromium
npm run test:e2e -- --reporter=list           # per-test lines instead of the HTML report
npm run test:e2e -- --headed                  # watch it drive
npx playwright show-report                    # open the last HTML report
```

Playwright starts the Vite dev server itself (`webServer` in
`playwright.config.ts`) and **reuses one already on :5173**, so it exercises
whatever is running rather than a stale bundle. Chromium only; the other browser
projects are commented out.

> E2E browser tests are located in `dashboard/tests/dashboard.spec.ts` (page title, login screen).

## 4. The opt-in groups — what a plain run deliberately skips

`uv run pytest -q -rs` lists them. Three skips, all by design:

### 4.1 Destructive end-to-end — `RUN_DESTRUCTIVE_E2E=1`

```bash
RUN_DESTRUCTIVE_E2E=1 uv run pytest tests/test_pipeline_e2e.py -q
```

**This wipes your datastores.** It runs `run_all.py --reset`, which FLUSHALLs
Redis and truncates Postgres and ClickHouse, and it needs the full Docker +
Ollama stack. It is gated because a plain `pytest` used to destroy the
developer's live dev environment — and it was 85% of the
suite's wall clock. Only run it against a stack you are willing to lose.

### 4.2 Live router — `DEFENSE_LIVE_ROUTER=1`

```bash
DEFENSE_LIVE_ROUTER=1 uv run pytest tests/test_chat_routing_corpus.py -q
# → 85 passed in 130 s   (vs 83 passed, 2 skipped in ~2 s without the flag)
```

Two tests that put the chat router in front of a **real** local LLM, so they cost
about two minutes of Ollama time — which is why they are not in the default run,
not because they are unreliable. The gate is `DEFENSE_LIVE_ROUTER=1` **and** a
reachable LLM: the probe is a 1.5 s socket connect to `LOCAL_LLM_BASE_URL`, so
setting the flag on a machine with nothing on :11434 skips quickly rather than
hanging or failing.

### 4.3 Markers

```bash
uv run pytest --markers                   # what is declared
uv run pytest -q -m "not e2e"             # exclude the stack-dependent group
```

`e2e` means "needs a full Docker + Ollama stack, not just the venv";
`destructive` means "mutates or wipes live datastores; opt-in only".

## 5. What the suites do and do not prove

* **Datastore-backed behaviour is covered by testcontainers**, not by your dev
  stack: `tests/conftest.py` can start throwaway Postgres and Redis containers.
  Nothing in a default run reads or writes the compose stack.
* **There is still no measured accuracy for any labeller.** 1,370 passing tests
  are correctness and contract tests. `eval/gold/comments_gold_300.json` holds
  300 stratified rows and **0 are adjudicated**, deliberately — labels seeded
  from a model in this repo would measure agreement with itself. See
  [evaluation.md](evaluation.md) and PROJECT_ASSESSMENT §7.2. A green suite is
  not an accuracy claim.
* **The contract tests grep the source.** Several assert things like "every
  worker checks the job stop flag before doing the work" or "the dashboard only
  reads fields the API returns". They fail on a rename, which is the point —
  but it also means a passing suite tells you the *wiring* holds, not that a
  model is good.

## 6. Before you push

```bash
uv run pytest -q                     # 1377 passed, 3 skipped, and NO warnings summary
cd dashboard
npm test -- --run                    # 62 passed
npm run lint                         # must read "Found 0 warnings and 0 errors"
npm run build                        # six chunks, no (!) advisory
npm run build                        # must succeed — the unit tests do not compile the app
npm run test:e2e                     # 4 passed
```

`npm run build` earns its place, but note what it does **not** catch: it does not
resolve JSX identifiers. `PostModal` rendered `<AlertCircle>` without importing
it — a `ReferenceError` on the one path that exists to show the operator a
comment-loading error — and the build passed the whole time. oxlint reports that
as `react(jsx-no-undef)`, and it only became visible once the 63 warnings stopped
burying it. Lint is not cosmetic here.
