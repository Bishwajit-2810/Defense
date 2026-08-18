# Dashboard — React 19 + Vite + Tailwind

The operator UI for the smart layer: posts and their per-comment analysis, live
pipeline trace, warnings, reports, agents, and a chat tab. It is a **read-mostly
client of the API** — every number on screen comes from `/v1/...`, nothing is
computed here that the pipeline could not also report.

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
- **Charts state their provenance.** `provenance` / `method_breakdown` /
  `stage2_selection` are rendered beside the counts they describe, because in stub
  mode most labels are not model output and a chart that does not say so is a
  claim the data does not support.
