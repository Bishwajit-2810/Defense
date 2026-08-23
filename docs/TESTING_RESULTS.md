# Testing Results — Full-Chain Run

> **What this is.** The verbatim output of the one command that runs everything —
> the Python suite, then the dashboard's unit tests, lint, production build and
> end-to-end specs. Recorded so a reader can see the actual numbers rather than a
> claim about them.
>
> **How to run the suites individually, and what a green run does *not* prove**, is
> [testing.md](testing.md) — read that second point before quoting anything here.

---

## The command

```bash
uv run pytest -q && (cd dashboard && npm test -- --run && npm run lint && npm run build && npm run test:e2e)
```

The `&&` chain is the point: **every stage must pass for the next to run**, so a
completed run is a single pass/fail signal. `npm run test:e2e` starts its own Vite
dev server (`playwright.config.ts` → `webServer`), so nothing needs to be running
beforehand.

## Result

**PASS — chain exit code `0`.**

| Stage | Result | Duration |
| ----- | ------ | -------- |
| `uv run pytest -q` | **1,393 passed, 3 skipped** | 49.10 s |
| `npm test -- --run` (vitest) | **78 passed** across **12 files** | 2.57 s |
| `npm run lint` (oxlint) | **clean** — no output, exit 0 | <1 s |
| `npm run build` (vite) | **built** — 1,817 modules, 5 JS chunks + 1 CSS | 784 ms |
| `npm run test:e2e` (Playwright) | **2 passed** (chromium) | 2.3 s |

- **Run at:** 2026-08-23 13:17:27 → 13:18:27 (+06:00) — **60 s wall clock**
- **Environment:** Python 3.12.12 · uv 0.12.5 · Node 26.7.0 · npm 12.0.2 ·
  Linux 7.1.8-arch1-3
- **Configuration:** the repository's `.env` as committed — `MODEL_STUB_MODE` off
  with `EMBEDDING_STUB_MODE=false`, no ML extras beyond what is installed. No
  Docker stack and no Ollama were required.

### The 3 skips are deliberate

They are the two opt-in pytest markers, and a bare `pytest` is *supposed* to
exclude them:

| Marker | Why it is skipped |
| ------ | ----------------- |
| `e2e` | Needs a full Docker + Ollama stack, not just the venv |
| `destructive` | **Mutates or wipes live datastores.** Opt in with `RUN_DESTRUCTIVE_E2E=1` — a bare `pytest` must never be able to destroy a developer's dev environment |

Datastore-backed behaviour is still covered in the default run: `tests/conftest.py`
starts **throwaway** Postgres and Redis containers via testcontainers, so nothing
here reads or writes the compose stack.

## Verbatim output

```text
........................................................................ [  5%]
........................................................................ [ 10%]
........................................................................ [ 15%]
........................................................................ [ 20%]
........................................................................ [ 25%]
........................................................................ [ 30%]
........................................................................ [ 36%]
........................................................................ [ 41%]
.......................................ss............................... [ 46%]
........................................................................ [ 51%]
........................................................................ [ 56%]
........................................................................ [ 61%]
........................................................................ [ 67%]
........................................................................ [ 72%]
...............................................s........................ [ 77%]
........................................................................ [ 82%]
........................................................................ [ 87%]
........................................................................ [ 92%]
........................................................................ [ 97%]
............................                                             [100%]
1393 passed, 3 skipped in 49.10s
npm notice run dashboard@0.0.0 test
npm notice run vitest --run

 RUN  v4.1.10 /home/bk/code/defense/dashboard


 Test Files  12 passed (12)
      Tests  78 passed (78)
   Start at  13:18:20
   Duration  2.57s (transform 1.67s, setup 1.46s, import 4.12s, tests 4.10s, environment 12.86s)

npm notice run dashboard@0.0.0 lint
npm notice run oxlint
npm notice run dashboard@0.0.0 build
npm notice run vite build
vite v8.2.1 building client environment for production...

transforming...✓ 1817 modules transformed.
rendering chunks...
computing gzip size...
dist/index.html                             1.08 kB │ gzip:  0.52 kB
dist/assets/index-BdBCIROS.css             59.45 kB │ gzip: 10.20 kB
dist/assets/rolldown-runtime-CbXtAM7H.js    0.58 kB │ gzip:  0.36 kB
dist/assets/icons-x7_JM6QN.js              14.61 kB │ gzip:  5.37 kB
dist/assets/charts-DB-ZizL1.js            179.33 kB │ gzip: 61.92 kB
dist/assets/react-NtOV-0Rg.js             181.73 kB │ gzip: 57.15 kB
dist/assets/index-CoxbxR2Q.js             239.25 kB │ gzip: 53.44 kB

✓ built in 784ms
npm notice run dashboard@0.0.0 test:e2e
npm notice run playwright test
[WebServer] npm notice run dashboard@0.0.0 dev

[WebServer] npm notice run vite


Running 2 tests using 2 workers

[1/2] [chromium] › tests/dashboard.spec.ts:3:1 › has title
[2/2] [chromium] › tests/dashboard.spec.ts:8:1 › shows welcome screen elements
  2 passed (2.3s)
```

## Reading the build output

The five JS chunks are **by design**, not accidental fragmentation.
`vite.config.js` sets `manualChunks` to split the vendor libraries out of the app
bundle:

| Chunk | Raw | gzip | Contents |
| ----- | ---: | ---: | -------- |
| `index-*.js` | 239.25 kB | 53.44 kB | The dashboard's own code |
| `react-*.js` | 181.73 kB | 57.15 kB | React + React-DOM |
| `charts-*.js` | 179.33 kB | 61.92 kB | chart.js + react-chartjs-2 |
| `icons-*.js` | 14.61 kB | 5.37 kB | lucide-react |
| `rolldown-runtime-*.js` | 0.58 kB | 0.36 kB | Vite/Rolldown runtime |
| `index-*.css` | 59.45 kB | 10.20 kB | Tailwind output |

The single bundle was 573 kB and tripped Vite's 500 kB advisory on every build.
Splitting changes nothing about *what* the browser loads — every chunk is still
requested on first paint — so it carries no runtime risk. What it buys is honest
chunking (the fix the advisory actually asks for, rather than raising
`chunkSizeWarningLimit` to hide it) and cache granularity: chart.js and React
change on dependency bumps, the app chunk changes on every deploy, and they no
longer invalidate each other. **No `(!)` size advisory in the output above** is
therefore part of the pass.

Deferring the charts until a tab that draws one is opened would be the bigger win,
but that needs `React.lazy` + Suspense boundaries — a runtime behaviour change,
deliberately not bundled into a build-config fix.

## What this run does *not* prove

The most important section, and the reason [testing.md](testing.md) §5 exists.

- **There is no measured accuracy for any labeller.** 1,393 passing tests are
  **correctness and contract** tests. `eval/gold/comments_gold_300.json` holds 300
  stratified rows and **0 are adjudicated** — deliberately, because labels seeded
  from a model in this repo would measure agreement with itself. A green suite is
  not an accuracy claim. See [evaluation.md](evaluation.md) §8.
- **The one exception is retrieval**, and it is not in this run: recall@k / MRR@k /
  nDCG@k come from `python -m eval.score_retrieval`, reported in
  [evaluation.md](evaluation.md) §8.4.
- **Several contract tests grep the source.** They assert things like "every worker
  checks the job stop flag before doing the work" or "the dashboard only reads
  fields the API returns". They fail on a rename, which is the point — but it means
  a passing suite tells you the **wiring** holds, not that a model is good.
- **`npm run build` does not resolve JSX identifiers.** `PostModal` once rendered
  `<AlertCircle>` without importing it — a `ReferenceError` on the one path that
  exists to show the operator a comment-loading error — and the build passed the
  whole time. oxlint reports that as `react(jsx-no-undef)`, which is why lint is in
  this chain and is not cosmetic.
- **No real-mode ML numbers.** With no ML extras installed, real-mode components
  degrade to heuristics and say so via `processing.degraded_components`. **No
  latency or accuracy figure from such a run is quotable** —
  [FEATURES.md](FEATURES.md) §13.
- **Nothing here exercises the image path.** It cannot: the corpus's 69 `photoUrls`
  are relative object-storage keys and the objects are not in MinIO, so
  `vision_status` is never `ok` ([STAGE1_NLP.md](STAGE1_NLP.md) §6).
- **No load or scale testing.** Horizontal scaling is code-complete and
  **unbenchmarked**; no multi-replica run has been timed
  ([PIPELINE.md](PIPELINE.md) §6).

## Reproducing this

```bash
uv sync                      # core deps only — the chain needs no extras
uv run pytest -q
cd dashboard && npm install
npm test -- --run && npm run lint && npm run build && npm run test:e2e
```

Expected differences on another machine: wall-clock times, and the content-hash
suffixes in the asset filenames (`index-BdBCIROS.css` and friends) which change
whenever the sources do. The **counts** should match exactly on this commit; if
`pytest` collects a different number, the tree has changed.

---

## Related documents

- [testing.md](testing.md) — the three suites, their markers, and the pre-push checklist
- [evaluation.md](evaluation.md) — the measurement plan this run does not satisfy, and §8.4's measured retrieval numbers
- [FEATURES.md](FEATURES.md) — the evidence class of every capability
- [TECH_STACK.md](TECH_STACK.md) — the tooling versions above, and why each is pinned
