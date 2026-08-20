# Dashboard — React 19 + Vite + Tailwind

The operator UI for the smart layer: posts and their per-comment analysis, live
pipeline trace, warnings, reports, agents, and a chat tab. It is a **read-mostly
client of the API** — every number on screen comes from `/v1/...`, nothing is
computed here that the pipeline could not also report. The writes it does make
are the deliberate ones: starting, stopping, resuming and deleting analysis jobs,
generating a report, and the runtime config toggles.

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
[`../run.md`](../run.md) for the ports.

## Tests

```bash
npm test           # vitest + @testing-library/react (jsdom)
npm run test:e2e   # Playwright, needs the stack running
npm run lint       # oxlint
```

## What to keep in mind when editing

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
- **`STALE_AFTER_MS` mirrors `_STALE_JOB_SECONDS`** in
  `routers/analysis.py`. It decides both the **stalled** badge and whether Resume
  is worth pressing, and the API applies the same threshold when it accepts or
  refuses a resume — so if one moves, move both, or the tab will offer an action
  the API rejects. The badge exists because a power-cut job's row reads `running`
  forever, which is the most misleading thing this table can display.
- **Action outcomes and stream progress are two different lines.** `notice` holds
  what the last stop/resume/delete did; `liveProgress` belongs to the SSE stream
  and is rewritten by every frame. They were one field until resuming a job —
  which subscribes to its stream immediately — wiped its own summary before it
  could be read.
- **Charts state their provenance.** `provenance` / `method_breakdown` /
  `stage2_selection` are rendered beside the counts they describe, because in stub
  mode most labels are not model output and a chart that does not say so is a
  claim the data does not support.
