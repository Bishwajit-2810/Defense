# Reports — Grounded Aggregates, Cluster Summaries, and Export

> **Scope.** The reporting surface: how a report's numbers are computed, where the
> LLM is allowed to write prose and where it is not, embedding-based topic
> clustering, and the export formats. Plus the honesty flag that travels with a
> report whose clusters were built on stub vectors.
>
> Code: [`api/routers/reports.py`](../src/defense/services/api/routers/reports.py),
> [`libs/clustering.py`](../src/defense/libs/clustering.py). The agent that
> *writes* narrative reports with tool access is the `reporter` agent —
> [AGENTS.md](AGENTS.md).

---

## 1. Endpoints

| Endpoint | Does |
| -------- | ---- |
| `GET /v1/reports` | List the caller's reports |
| `POST /v1/reports` | Request a report (rate-limited) |
| `GET /v1/reports/{id}` | Fetch one |
| `GET /v1/reports/{id}/export?format=pdf\|html\|json` | Download |
| `GET /v1/reports/export_latest` | Generate **and immediately download** a report for recent posts |
| `GET /v1/reports/export_latest/export` | The same, as a file response |

The dashboard's **Reports** tab is the client — [DASHBOARD_UI.md](DASHBOARD_UI.md).
The **Jobs** tab is a second one: its per-row *Download Report* calls
`export_latest?job_id=…` (§1.1).

## 1.1 Scoping — what a report actually covers

A report has **three** possible scopes, and they compose:

| Scope | Set by | Filter |
| ----- | ------ | ------ |
| Whole tenant corpus | the default (`campaign_id=all`, no post ids) | `ar.tenant_id = :tid` |
| One campaign | `campaign_id` | `+ ar.campaign_id = :cid` |
| An explicit set of posts | `post_ids` on `POST /v1/reports`, or `job_id` on `export_latest` | `+ ar.post_id = ANY(:pids)` |

Post scoping was added **29 Aug 2026**, and the reason is worth keeping. Campaign
used to be the only scope. Every job created by `POST /v1/analysis/run` from
explicit `post_ids` has **no campaign**, so the Jobs tab's per-row download fell
through to `campaign_id=all`: a job that analysed *one* post produced a report
over all fifty, under a filename that claimed otherwise, and nothing in the PDF
contradicted it — the scope was never printed.

Three things follow, all of which are load-bearing:

* **Every aggregate takes the same filter.** A single unscoped query is enough to
  report the whole corpus inside a document that claims to be about two posts.
  The topics aggregate had its own hand-rolled copy of the `WHERE` clause and was
  missed on the first pass; there is now one `where` string and the topic query
  uses it. `_embedding_clusters` takes the scope too, or the cluster summaries
  would be drawn from posts the report never mentions.
* **The 5-minute report cache is keyed on the scope, not just the campaign.**
  Reusing a corpus report generated a minute ago for a one-post request is the
  same bug wearing a different hat: the right filename over the wrong PDF.
* **`scope_label` travels with the document** ("all campaigns" / "campaign x" /
  "N selected post(s)") into the metrics block, into the LLM narrative prompt, and
  onto the rendered header as `Scope:`. A stored report from before this change
  has no label and falls back to its campaign.

Deduplication is not a concern here: Postgres upserts `analysis_results`
`ON CONFLICT (post_id)`, so it holds one row per post however many times that post
has been re-analysed. (The ClickHouse `analysis_events` table is append-only and
*does* need `LIMIT 1 BY post_id` — see [`tests/test_analytics_storage_contract.py`](../tests/test_analytics_storage_contract.py).)

```bash
# A report on exactly the posts one job analysed
curl -s -H "$KEY" "$API/v1/reports/export_latest?job_id=<job_id>&format=pdf" -o job.pdf

# Or name the posts yourself
curl -s -X POST $API/v1/reports -H "$KEY" -H "Content-Type: application/json" \
  -d '{"post_ids":["cmor32gy…"],"options":{"grounded":true}}'
```

An unknown `job_id` is a **404**, not a silent whole-corpus report. A job that
names neither a campaign nor post ids genuinely is corpus-wide, and reports as
such — which is then the truth rather than an accident.

## 2. Generation is synchronous, and that is a deliberate scale choice

Despite `POST /v1/reports` reading as "request an async generation", the corpus is
aggregated with **a few SQL queries** and the report completes immediately rather
than waiting on a queue consumer. At MVP scale that is cheap and the latency is
better; at production scale this is the component to move behind the bus.

## 3. The numbers come from SQL, not from a model

`_generate_report_content` aggregates `analysis_results`, tenant-scoped and
optionally campaign- or post-scoped (§1.1):

| Section | Query |
| ------- | ----- |
| Headline metrics | `count(*)`, first and last `created_at` |
| Sentiment distribution | `GROUP BY result->>'overall_sentiment'` |
| Language distribution | `GROUP BY result->>'language'`, top 10 |
| Topic clusters | Count and dominant sentiment per topic |

**This ordering is the point.** Every number in a report is a SQL aggregate over
stored results. The LLM is invited afterwards, to *describe* those numbers — never
to produce them. That is what makes a report checkable: a claim in the narrative
can be diffed against the metrics block in the same document.

## 4. Where the LLM is used

### The executive narrative

`_llm_narrative` asks the `llm_b` role for **3–5 factual, neutral sentences**
about volume, dominant sentiment and what drives it, the main topic clusters, and
any notable language or engagement pattern — with the aggregated metrics JSON in
the prompt, no markdown, no preamble, `temperature=0.2`, `max_tokens=400`, and a
**6-second timeout**.

**It returns `None` on any failure** — no backend reachable, a timeout, a refusal
— and the caller keeps the SQL-aggregate summary instead. A report is never
blocked by, and never silently degraded by, an unavailable model.

### Cluster summaries

`_embedding_clusters` pulls post embeddings from pgvector, k-means clusters them
([`libs/clustering.py`](../src/defense/libs/clustering.py)), and summarises **one
slice per cluster with a single `llm_b` call each** — so N posts cost ~k LLM
calls, not N. That is the same cost lever the pipeline's router applies at the
post level, one tier up.

Degradation is graded rather than fatal:

| Situation | Behaviour |
| --------- | --------- |
| `numpy`/clustering unavailable | `clusters: []`, logged |
| Too few embeddings | `clusters: []` |
| LLM unreachable | Individual summaries become `None` — **not** an error |
| Cluster has an absurd number of comments | Bounded by `REPORT_CLUSTER_SAMPLE_CAP` |

### The stub-vector honesty flag

`_embedding_clusters` returns `(clusters, any_vector_was_a_stub)`, and that second
element travels **into the report**.

It exists because these summaries cost real LLM calls and **a stub vector is not
semantic**: clustering hash vectors groups posts arbitrarily, so the summaries
describe nothing. The reader has to be told that in the document, not left to
infer it from a log line — a plausible-sounding cluster summary over arbitrary
groupings is exactly the kind of output that survives review.

## 5. Export

| Format | Notes |
| ------ | ----- |
| `json` | The raw report document |
| `html` | `_report_to_html`, with every interpolated value HTML-escaped |
| `pdf` | Rendered from the HTML by **WeasyPrint**, which is an **optional** import — the module tolerates its absence rather than failing to load |

## 6. Ownership and limits

Reports are scoped by `tenant_id` on every read. Generation runs behind the
`rate_limit` dependency, because a report costs up to `k + 1` LLM calls and is
user-triggered ([AUTH.md](AUTH.md) §5). Report spend lands in the
**`interactive`** usage lane, outside `pipeline_tokens`
([LLM_BACKENDS.md](LLM_BACKENDS.md) §4).

## 7. Known gap

`embedding_clusters` is written into the report's **`options` column only** —
there is no dedicated column or table for it, so a query cannot reach a report's
clusters without parsing JSON out of `options`. This is recorded in
[PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §13.1 and is still open. It is a
separate issue from the analysis job's `options`, which was being written as a
literal `{}` and is now fixed ([JOBS.md](JOBS.md) §1).

## 8. Evidence class

| Claim | State |
| ----- | ----- |
| Every report number is a SQL aggregate over stored results | ✅ Measured |
| Every aggregate carries the report's scope (no unscoped query) | ✅ Measured — [`tests/test_report_scope.py`](../tests/test_report_scope.py) asserts it per query |
| `job_id` resolves to that job's posts, and an unknown job is a 404 | ✅ Measured |
| The rendered PDF states its own scope | ✅ Measured |
| Narrative failure degrades to the SQL summary | ✅ Measured |
| Cluster summarisation costs ~k calls, not N | ✅ Measured |
| Stub-vector flag reaches the report document | ✅ Measured |
| HTML escaping on export | ✅ Measured |
| PDF export | 🟡 Works when WeasyPrint is installed; optional by design |
| **Narrative faithfulness** — does the prose match the metrics? | 📋 **Unmeasured.** No LLM-judge or human rubric has been run; the plan is [evaluation.md](evaluation.md) §3 |
| Cluster quality (are the clusters meaningful?) | 📋 **Unmeasured** — no labelled topic structure exists |

Cross-references: [AGENTS.md](AGENTS.md) (the `reporter` agent) ·
[SEARCH.md](SEARCH.md) · [LLM_BACKENDS.md](LLM_BACKENDS.md) ·
[api_design.md](api_design.md) · [endpoints.md](endpoints.md) ·
[evaluation.md](evaluation.md) §3 and §5.
