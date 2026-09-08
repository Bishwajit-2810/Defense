# Dashboard — React 19 + Vite + Tailwind

The operator UI for the smart layer: posts and their per-comment analysis, live
pipeline trace, warnings, reports, agents, and a chat tab. It is a **read-mostly
client of the API** — every number on screen comes from `/v1/...`, nothing is
computed here that the pipeline could not also report. The writes it does make
are the deliberate ones: starting, stopping, resuming and deleting analysis jobs,
generating a report, and the runtime config toggles.

> **Comprehensive documentation:** See [DASHBOARD_UI.md](../docs/DASHBOARD_UI.md) for
> all 11 tabs, component architecture, SSE streaming, and authentication flow.
> See [SYSTEM_MONITOR.md](../docs/SYSTEM_MONITOR.md) for the hardware telemetry drawer
> and observability stack.

This replaced the vanilla HTML/CSS/JS dashboard the design docs specify; that one
is preserved at [`../dashboard_legacy/`](../dashboard_legacy/). Where a design doc
still says "plain HTML/CSS/JS, no build step", this directory is the current
answer.

## Run it

The normal path is the whole stack in one command, which starts this dev server
for you:

```bash
cd .. && uv run run_all.py --with-agents      # dashboard on http://127.0.0.1:8080
```

Standalone, against an API that is already up:

```bash
npm install
npm run dev        # Vite dev server, HMR
npm run build      # production bundle into dist/
npm run preview    # serve the built bundle
```

The API base URL is a **hardcoded constant**, `API_BASE` in
[`src/utils/api.js`](src/utils/api.js) (`http://127.0.0.1:8001`) — there is no Vite
proxy and no env var. Point it elsewhere by editing that line; see
[`../run.md`](../docs/run.md) for the ports.

## Tests

```bash
npm test -- --run  # vitest + @testing-library/react (jsdom) — `npm test` alone WATCHES
npm run test:e2e   # Playwright; starts/reuses the Vite dev server on :5173 itself
npm run lint       # oxlint — silent with exit 0; ANY output is a failure
npm run build      # not optional before pushing, but it does NOT resolve JSX identifiers
                   # (5 JS chunks + 1 CSS by design — vite.config.js splits the vendor libs)
```

**Twelve** unit-test files — one per page for `AnalysisJobs`, `Posts`, `Search`,
`Logs` and `Trace`, plus `App`, `MarkdownView`, `PostModal`,
`SystemMetricsChip`, `SystemMonitorDrawer`, `useSystemMetrics` and `sentiment` —
and two Playwright specs, both in `tests/dashboard.spec.ts` and both about this
dashboard. (The generated `example.spec.ts` scaffold that visited playwright.dev
is gone; a run is no longer half about someone else's site.) The backend suite
and the opt-in groups are in [`../testing.md`](../docs/testing.md).

**An e2e spec must not pin a product name.** Both specs asserted the `<title>`
and Welcome heading read *"Defense Analysis"* and had been failing since the app
was renamed **Selective Intelligence** — a red last stage that said nothing about
whether the dashboard works. Corrected 29 Aug 2026.

## What to keep in mind when editing

- **Keep `npm run lint` at zero, because it catches what the build cannot.** `vite build` does not resolve JSX identifiers: `PostModal` rendered
  `<AlertCircle>` without importing it, so the branch that *shows* a
  comment-loading error was itself a `ReferenceError`, and the build passed
  regardless. oxlint reports it as `react(jsx-no-undef)` — but it sat buried under
  63 warnings for unused imports, unused `catch (e)` bindings and
  computed-then-unrendered variables. It is at **0** now — the two
  `exhaustive-deps` warnings were fixed rather than suppressed (`useCallback` in
  `Trace.jsx` and `PostModal.jsx`), and oxlint does not honour an inline disable
  for that rule anyway, so a suppression comment would have been a lie.
- **A dependency array is evaluated during render.** Naming a `const` declared
  further down the component throws on the temporal dead zone — which is why
  `fetchPosts` sits above the effects that depend on it in `Posts.jsx`. The unit
  tests caught that immediately; the lint fix that introduced it did not.
- **A missing value and a neutral value must not render the same.** Several
  components exist in their current shape because of this: a labeller that did not
  vote shows `—` rather than vanishing, `label_voters: 0` renders as "not read"
  rather than "0% agreement", and a comment outside the router's analysed set is
  tagged `stage-1 only` instead of quietly showing one model's opinion as a
  consensus. If you add a field, decide what its *absence* looks like first.
- **`PostModal.jsx`'s `LABELLERS` list must stay in sync** with
  `Settings.stage2_classifier_names` and `ensemble.CHEAP_SOURCES` on the backend.
  A voter missing from that list still runs, still costs its forward pass, and is
  invisible — which is how five dead classifier slots went unnoticed for weeks.
  It holds **eight** entries: the seven model heads plus the LLM. Stage 1's
  `heuristic` is deliberately not one of them — it stopped voting on 17 Aug 2026,
  so a comment no model read shows `uncertain` / "not read" rather than a keyword
  verdict.
- **A control must not promise more than the pipeline can do.** The Jobs tab's
  **Stop** says "in-flight posts will finish, the rest are skipped", because that
  is the actual guarantee — cancellation is a flag each stage checks, so the post
  already inside a stage completes. **Delete** says the posts' analysis results
  are kept, because they are (`analysis_results` is keyed by post, not by job).
  **Resume** reports `30/300 already done, 270 re-queued` rather than just
  "resumed", which is the difference between a continuation and a no-op. If you
  reword these, keep the caveat.
- **One SSE stream per view, and close the one *this* effect opened.** An
  `async` connect function guarded by `if (ref.current) return` does not work:
  the guard runs before the first `await`, so StrictMode's double-mount passes it
  twice while the ref is still null, and the socket the ref does not keep is
  orphaned and never closed. In the Logs tab that doubled every line and the
  stream's 60-line backfill with it. The pattern that does work is a `cancelled`
  flag plus a local `es` closed in the cleanup — see `Logs.jsx`. Also dedupe: the
  log stream's backfill/live join repeats a line *by design*, so the client owns
  that, not the server.
- **A search hit is a whole post, so render it like one.** `/v1/search` returns
  the complete canonical result in `result` (the 200-char `snippet` is a list-view
  convenience, not the limit of what you may show), and that object is exactly
  what `PostModal` takes. Both the Search tab and the Posts tab's server-fallback
  rows pass it straight in — no second request for the analysis, only for the
  comment pages. The Search tab showed the truncated snippet and dropped `result`
  until 21 Aug 2026, which left a post found by id unreadable and unopenable.
- **The Posts search is two-tier, and the tiers mean different things.** It
  filters the loaded rows first (instant, no request), and only falls back to
  `/v1/search` when that finds nothing — because the table holds the latest 100
  posts, so a filter over "what happens to be loaded" would report *not found*
  for a post that exists. The empty state therefore has three branches, not one:
  nothing uploaded, nothing matched anywhere, and **no post has that id**
  (`id_lookup_missed` from the API). Collapsing them back into one line is how
  this page came to answer an id search with "Upload posts to get started".
- **`STALE_AFTER_MS` mirrors `_STALE_JOB_SECONDS`** in
  `routers/analysis.py`. It decides both the **stalled** badge and whether Resume
  is worth pressing, and the API applies the same threshold when it accepts or
  refuses a resume — so if one moves, move both, or the tab will offer an action
  the API rejects. The badge exists because a power-cut job's row reads `running`
  forever, which is the most misleading thing this table can display. Note the
  asymmetry the API has and this tab does not: since 29 Aug 2026 the API also
  consults the job's **stage frames**, because `jobs.updated_at` is written by the
  assembler alone and does not move while a single post sits in Stage 2. The badge
  can therefore say "stalled" on a job the API will refuse to resume — which is
  the safe direction, but do not add a second source here without matching it.
- **Do not render a number you do not have — and know which source is durable.**
  The Jobs tab's progress cell read
  `total > 0 ? completed/total : (isFinished ? 100 : 0)`, coercing a missing
  counter to `0` first. `total` and `completed` are two Redis keys with a 24 h
  TTL that expire **independently**, so a finished job routinely holds one
  without the other: two live jobs on 29 Aug 2026 rendered **0%** and **—**, and
  both had finished. The counters are ephemeral detail; `status` is a Postgres
  column and `done` *means* every post landed. So: both counters → a real ratio;
  otherwise terminal `done` → 100% marked `*` as derived; `cancelled`/`failed`
  never complete; running with no counters → **—**; a counter that exists and
  says zero is a measurement and still renders 0%. Same rule as "a missing value
  and a neutral value must not render the same", two rows up.
- **State that must survive a tab switch does not belong in `useState`.** `App`
  renders every page as `{activeTab === 'x' && <Page />}`, so leaving a tab
  *unmounts* it. Fine for a table that refetches; wrong for anything holding a
  live stream. The Trace tab kept its whole trace in `useState` and closed its
  `EventSource` on cleanup, so switching tabs came back to "Nothing traced yet"
  and abandoned a post mid-pipeline. It now lives in
  [`src/utils/traceSession.js`](src/utils/traceSession.js) — the module owns the
  session and the stream, and the page is a `useSyncExternalStore` view over it.
  Its tests unmount the page and assert the stream is still consuming.
- **Cross-tab navigation is an event, not a prop chain.** There is no router; the
  active tab is `useState` in `App.jsx`. A page handing work to another one
  dispatches `dashboard-navigate` with `{ tab }`. Data goes the other way: Posts
  writes the post id into the trace store *directly* before navigating, because
  Trace is not mounted yet and an event would have nowhere to land.
- **Action outcomes and stream progress are two different lines.** `notice` holds
  what the last stop/resume/delete did; `liveProgress` belongs to the SSE stream
  and is rewritten by every frame. They were one field until resuming a job —
  which subscribes to its stream immediately — wiped its own summary before it
  could be read.
- **Charts state their provenance.** `provenance` / `method_breakdown` /
  `stage2_selection` are rendered beside the counts they describe, because in stub
  mode most labels are not model output and a chart that does not say so is a
  claim the data does not support.
