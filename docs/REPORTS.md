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

## 2. Generation is synchronous, and that is a deliberate scale choice

Despite `POST /v1/reports` reading as "request an async generation", the corpus is
aggregated with **a few SQL queries** and the report completes immediately rather
than waiting on a queue consumer. At MVP scale that is cheap and the latency is
better; at production scale this is the component to move behind the bus.

## 3. The numbers come from SQL, not from a model

`_generate_report_content` aggregates `analysis_results`, tenant-scoped and
optionally campaign-scoped:

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
