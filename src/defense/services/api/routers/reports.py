"""Reports endpoints — generate, list, and retrieve analysis reports."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone

# Repo root on path so `libs.llm` imports when the API runs from services/api.

from html import escape as html_escape
import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from defense.services.api.deps import get_current_user, get_db, get_redis, rate_limit
from defense.services.api.models import ReportRequest, ReportResponse

log = structlog.get_logger(__name__)

try:
    from weasyprint import HTML
except Exception:
    HTML = None

router = APIRouter(prefix="/v1/reports", tags=["reports"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_report_row(db: AsyncSession, report_id: str, *, tenant_id: str = "default") -> dict | None:
    """Fetch a report job row by ID, scoped to *tenant_id*; returns None when not found."""
    row = (
        await db.execute(
            text(
                """
                SELECT id, selector, options, status, created_at, updated_at, error
                FROM jobs
                WHERE id = :id AND type = 'report'
                  AND (tenant_id = :tid OR selector->>'tenant_id' = :tid)
                """
            ),
            {"id": report_id, "tid": tenant_id},
        )
    ).mappings().first()
    return dict(row) if row else None


def _row_to_report(row: dict) -> ReportResponse:
    selector = row.get("selector") or {}
    options = row.get("options") or {}
    if isinstance(selector, str):
        try:
            selector = json.loads(selector)
        except Exception:
            selector = {}
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except Exception:
            options = {}
    content: dict = options.get("result") or {} if isinstance(options, dict) else {}
    report_id = str(row.get("id") or row.get("report_id") or "")
    return ReportResponse(
        id=report_id,
        report_id=report_id,
        campaign_id=selector.get("campaign_id", "") if isinstance(selector, dict) else "",
        type=options.get("type") if isinstance(options, dict) else None,
        title=options.get("title") if isinstance(options, dict) else None,
        status=row.get("status", "done"),
        error=row.get("error"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        download_url=options.get("download_url") if isinstance(options, dict) else None,
        period=content.get("period"),
        summary=content.get("summary"),
        summary_source=content.get("summary_source"),
        clusters=content.get("clusters"),
        # Read back what `create_report` paid for. Omitting these two was §13.1:
        # the summaries were generated, stored in `options.result`, and then
        # never read by any of the three report endpoints.
        embedding_clusters=content.get("embedding_clusters"),
        embedding_clusters_are_stub=bool(content.get("embedding_clusters_are_stub")),
        metrics=content.get("metrics"),
    )


async def _generate_report_content(db: AsyncSession, campaign_id: str, tenant_id: str = "default") -> dict:
    """Aggregate stored analysis results into report content.

    Synchronous-at-request-time generation: the corpus is aggregated with a few
    SQL queries (cheap at MVP scale), so reports complete immediately instead
    of waiting on a queue consumer.
    """
    scoped = campaign_id not in ("", "all")
    where = "WHERE ar.tenant_id = :tid" + (" AND ar.campaign_id = :cid" if scoped else "")
    params: dict = {"tid": tenant_id, "cid": campaign_id} if scoped else {"tid": tenant_id}

    # --- Headline metrics -----------------------------------------------
    head = (
        await db.execute(
            text(
                f"""
                SELECT count(*)                          AS total_posts,
                       min(ar.created_at)                AS first_at,
                       max(ar.created_at)                AS last_at
                FROM analysis_results ar {where}
                """
            ),
            params,
        )
    ).mappings().first()

    sentiments = (
        await db.execute(
            text(
                f"""
                SELECT ar.result->>'overall_sentiment' AS s, count(*) AS n
                FROM analysis_results ar {where}
                GROUP BY 1 ORDER BY 2 DESC
                """
            ),
            params,
        )
    ).mappings().all()

    languages = (
        await db.execute(
            text(
                f"""
                SELECT ar.result->>'language' AS lang, count(*) AS n
                FROM analysis_results ar {where}
                GROUP BY 1 ORDER BY 2 DESC LIMIT 10
                """
            ),
            params,
        )
    ).mappings().all()

    # --- Topic clusters: count + dominant sentiment per topic -------------
    topic_filter = "WHERE ar.tenant_id = :tid" + (" AND ar.campaign_id = :cid" if scoped else "")
    topic_rows = (
        await db.execute(
            text(
                f"""
                SELECT t.topic                            AS topic,
                       ar.result->>'overall_sentiment'    AS s,
                       count(*)                           AS n
                FROM analysis_results ar,
                     jsonb_array_elements_text(
                         CASE
                             WHEN jsonb_typeof(ar.result->'topics') = 'array' THEN ar.result->'topics'
                             ELSE '[]'::jsonb
                         END
                     ) AS t(topic)
                {topic_filter}
                GROUP BY 1, 2
                """
            ),
            params,
        )
    ).mappings().all()

    by_topic: dict[str, dict] = {}
    for r in topic_rows:
        entry = by_topic.setdefault(r["topic"], {"size": 0, "sentiments": {}})
        entry["size"] += r["n"]
        entry["sentiments"][r["s"] or "neutral"] = r["n"]
    clusters = [
        {
            "cluster_id": f"topic-{i}",
            "label": topic,
            "size": data["size"],
            "top_sentiment": max(data["sentiments"], key=data["sentiments"].get),
        }
        for i, (topic, data) in enumerate(
            sorted(by_topic.items(), key=lambda kv: kv[1]["size"], reverse=True)[:8]
        )
    ]

    # --- Platform distribution -----------------------------------------
    platforms = (
        await db.execute(
            text(
                f"""
                SELECT ar.result->>'platform' AS p, count(*) AS n
                FROM analysis_results ar {where}
                GROUP BY 1 ORDER BY 2 DESC LIMIT 10
                """
            ),
            params,
        )
    ).mappings().all()
    platform_map = {r["p"] or "Unknown": r["n"] for r in platforms if r["p"]}

    # --- Watchlist Warnings & Threat Details ---------------------------
    warning_rows = (
        await db.execute(
            text(
                f"""
                SELECT ar.post_id,
                       ar.result->>'overall_sentiment' AS s,
                       ar.result->>'watchlist_alert_reason' AS r,
                       ar.result->>'post_summary' AS summ,
                       ar.result->>'platform' AS p,
                       ar.result->>'toxicity_score' AS tox,
                       ar.result->>'hate_speech_score' AS hate
                FROM analysis_results ar
                {where} AND (
                    ar.result->>'watchlist_alert' = 'true'
                    OR ar.result->>'watchlist_alert' = '1'
                    OR ar.result @> '{{\"watchlist_alert\": true}}'::jsonb
                )
                ORDER BY ar.created_at DESC LIMIT 15
                """
            ),
            params,
        )
    ).mappings().all()

    warnings_list = []
    warning_reasons_map: dict[str, int] = {}
    for r in warning_rows:
        reason = r["r"] or "Watchlist trigger"
        warning_reasons_map[reason] = warning_reasons_map.get(reason, 0) + 1
        tox_val = float(r["tox"]) if r["tox"] is not None else 0.0
        hate_val = float(r["hate"]) if r["hate"] is not None else 0.0
        warnings_list.append({
            "post_id": str(r["post_id"]),
            "sentiment": r["s"] or "neutral",
            "reason": reason,
            "summary": r["summ"] or "Flagged content requires analyst inspection.",
            "platform": r["p"] or "Unknown",
            "toxicity": round(tox_val, 3),
            "hate_speech": round(hate_val, 3),
        })

    # --- Overall Toxicity & Risk Stats ---------------------------------
    tox_stats = (
        await db.execute(
            text(
                f"""
                SELECT avg(COALESCE(NULLIF(ar.result->>'toxicity_score', '')::float, 0)) AS avg_tox,
                       avg(COALESCE(NULLIF(ar.result->>'hate_speech_score', '')::float, 0)) AS avg_hate,
                       count(CASE WHEN (ar.result->>'watchlist_alert')::boolean = true OR ar.result @> '{{\"watchlist_alert\": true}}'::jsonb THEN 1 END) AS warn_count
                FROM analysis_results ar {where}
                """
            ),
            params,
        )
    ).mappings().first() or {}

    total = head["total_posts"] or 0
    sent_map = {r["s"] or "neutral": r["n"] for r in sentiments}
    lang_map = {r["lang"] or "und": r["n"] for r in languages}
    period = None
    if head["first_at"] and head["last_at"]:
        period = f"{head['first_at']:%Y-%m-%d} → {head['last_at']:%Y-%m-%d}"

    scope_label = f"campaign {campaign_id}" if scoped else "all campaigns"
    sent_text = ", ".join(f"{k}: {v}" for k, v in sent_map.items()) or "no data"
    top_topics = ", ".join(c["label"] for c in clusters[:5]) or "none"
    warn_count = tox_stats.get("warn_count") or len(warnings_list)
    summary = (
        f"Analyzed {total} posts across {scope_label}"
        + (f" ({period})" if period else "")
        + f". Sentiment breakdown — {sent_text}. Watchlist alerts: {warn_count}. Top topics: {top_topics}."
    )

    return {
        "period": period,
        "summary": summary,
        "clusters": clusters,
        "warnings": warnings_list,
        "metrics": {
            "total_posts": total,
            "sentiment_breakdown": sent_map,
            "languages": lang_map,
            "platforms": platform_map,
            "warning_count": warn_count,
            "avg_toxicity": round(float(tox_stats.get("avg_tox") or 0.0), 3),
            "avg_hate_speech": round(float(tox_stats.get("avg_hate") or 0.0), 3),
            "warning_reasons": warning_reasons_map,
        },
    }


async def _llm_narrative(
    content: dict,
    campaign_id: str,
    report_type: str | None,
    backend_override: str | None,
) -> str | None:
    """Ask LLM-B for a grounded executive summary of the aggregated metrics.

    Returns None on any failure (no LLM backend reachable, timeout, …) so the
    caller can keep the SQL-aggregate summary instead.
    """
    try:
        from defense.libs.llm.client import LLMClient  # noqa: PLC0415 — lazy: only grounded reports need it

        prompt = (
            "You are an analyst writing the executive summary of a social-media "
            f"monitoring report (type: {report_type or 'trend'}, scope: "
            f"{'campaign ' + campaign_id if campaign_id not in ('', 'all') else 'all campaigns'}).\n"
            "Below are the aggregated metrics computed from the analyzed posts. "
            "Write 3-5 factual, neutral sentences: overall volume, dominant "
            "sentiment and what drives it, the main topic clusters, and any "
            "notable language or engagement pattern. No markdown, no bullet "
            "points, no preamble.\n\n"
            f"Metrics JSON:\n{json.dumps(content, ensure_ascii=False, default=str)}"
        )
        response = await asyncio.wait_for(
            LLMClient().chat(
                role="llm_b",
                messages=[{"role": "user", "content": prompt}],
                backend_override=backend_override,
                max_tokens=400,
                temperature=0.2,
                usage_task="report_narrative",
            ),
            timeout=6.0,
        )
        narrative = (response.get("content") or "").strip()
        return narrative or None
    except Exception as exc:
        log.warning("report_llm_summary_failed", error=str(exc))
        return None


# ---------------------------------------------------------------------------
# Embedding cluster summarization (architecture §5 — the LLM cost lever)
# ---------------------------------------------------------------------------

# Fallback threshold to prevent a runaway query if the cluster has an absurd
# number of comments (this dataset doesn't, but an unbounded read is fragile).
from defense.libs.common.config import get_settings
_CLUSTER_SAMPLE_CAP = get_settings().report_cluster_sample_cap


async def _embedding_clusters(
    db: AsyncSession,
    campaign_id: str,
    backend_override: str | None,
    tenant_id: str = "default",
) -> tuple[list[dict], bool]:
    """Cluster post embeddings (pgvector → k-means) and summarize one slice per
    cluster with a single LLM-B call each — N posts, ~k LLM calls (§5).

    Returns ``(clusters, any_vector_was_a_stub)``. The second element exists
    because these summaries cost real LLM calls and a stub vector is not
    semantic: clustering hash vectors groups posts arbitrarily, so the summaries
    describe nothing. That has to travel to the reader rather than being
    inferable only from a log line (§13.1 / §13.2).

    Clusters are [] when there are too few embeddings or clustering is
    unavailable; individual summaries degrade to None (not an error) if the LLM
    is unreachable.
    """
    try:
        from defense.libs.clustering import cluster_embeddings, parse_pgvector  # noqa: PLC0415
    except Exception as exc:  # numpy missing, etc.
        log.warning("clustering_unavailable", error=str(exc))
        return [], False

    scoped = campaign_id not in ("", "all")
    where = "WHERE ar.embedding IS NOT NULL AND ar.tenant_id = :tid"
    params: dict = {"lim": _CLUSTER_SAMPLE_CAP, "tid": tenant_id}
    if scoped:
        where += " AND ar.campaign_id = :cid"
        params["cid"] = campaign_id

    rows = (
        await db.execute(
            text(
                f"""
                SELECT ar.post_id                          AS post_id,
                       ar.embedding::text                  AS emb,
                       ar.result->>'post_summary'          AS summary,
                       ar.result->>'overall_sentiment'     AS sentiment,
                       COALESCE(ar.embedding_is_stub, FALSE) AS is_stub
                FROM analysis_results ar
                {where}
                ORDER BY ar.created_at DESC
                LIMIT :lim
                """
            ),
            params,
        )
    ).mappings().all()

    vectors: list[list[float]] = []
    meta: list[dict] = []
    stub_vectors = 0
    for r in rows:
        vec = parse_pgvector(r["emb"])
        if vec:
            vectors.append(vec)
            if r["is_stub"]:
                stub_vectors += 1
            meta.append({
                "post_id": r["post_id"],
                "summary": r["summary"],
                "sentiment": r["sentiment"] or "neutral",
            })

    if len(vectors) < 2:
        return [], bool(stub_vectors)

    # A hash stub is "not semantic", so clustering a table of them groups noise
    # and every per-cluster LLM call summarises an arbitrary set of posts. That
    # has to reach the reader, not just this log line — the caller puts it on the
    # response as `embedding_clusters_are_stub` (§13.2 / §5.9).
    if stub_vectors:
        log.warning(
            "embedding_clusters_over_stub_vectors",
            stub_vectors=stub_vectors,
            total_vectors=len(vectors),
            detail="clusters group hash noise; summaries are not meaningful",
        )

    cr = cluster_embeddings(vectors)

    # One LLM-B call per cluster over a representative slice. Reuse one client;
    # if it can't be built, clusters are still returned (summaries just None).
    llm = None
    try:
        from defense.libs.llm.client import LLMClient  # noqa: PLC0415
        llm = LLMClient()
    except Exception:
        llm = None

    tasks = []
    cluster_metas = []
    for ci in range(cr.k):
        member_idx = cr.members[ci] if ci < len(cr.members) else []
        if not member_idx:
            continue
        rep_idx = cr.representative_indices[ci]
        # dominant sentiment in the cluster
        sent_counts: dict[str, int] = {}
        for mi in member_idx:
            s = meta[mi]["sentiment"]
            sent_counts[s] = sent_counts.get(s, 0) + 1
        top_sentiment = max(sent_counts, key=sent_counts.get)

        # representative + a few member summaries as grounding for the LLM
        sample = [meta[mi]["summary"] for mi in ([rep_idx] + member_idx[:5]) if meta[mi]["summary"]]
        cluster_metas.append({
            "ci": ci,
            "top_sentiment": top_sentiment,
            "rep_idx": rep_idx,
            "size": cr.sizes[ci],
        })
        tasks.append(_summarize_cluster(llm, sample, top_sentiment, backend_override))

    summaries = await asyncio.gather(*tasks, return_exceptions=True)

    clusters: list[dict] = []
    for cm, summ in zip(cluster_metas, summaries):
        ci = cm["ci"]
        summary_str = summ if isinstance(summ, str) else None
        clusters.append({
            "cluster_id": f"emb-{ci}",
            "size": cm["size"],
            "top_sentiment": cm["top_sentiment"],
            "representative_post_id": meta[cm["rep_idx"]]["post_id"] if cm["rep_idx"] >= 0 else None,
            "summary": summary_str,
        })

    clusters.sort(key=lambda c: c["size"], reverse=True)
    return clusters, bool(stub_vectors)


async def _summarize_cluster(
    llm,
    sample_summaries: list[str],
    top_sentiment: str,
    backend_override: str | None,
) -> str | None:
    """One LLM-B call summarizing a cluster from representative post summaries."""
    if llm is None or not sample_summaries:
        return None
    try:
        prompt = (
            "These are post summaries from one cluster of similar social-media "
            f"posts (dominant sentiment: {top_sentiment}). In ONE neutral "
            "sentence, state the common theme. No preamble.\n\n"
            + "\n".join(f"- {s}" for s in sample_summaries[:6])
        )
        resp = await asyncio.wait_for(
            llm.chat(
                role="llm_b",
                messages=[{"role": "user", "content": prompt}],
                backend_override=backend_override,
                max_tokens=120,
                temperature=0.2,
                usage_task="report_cluster_summary",
            ),
            timeout=6.0,
        )
        return (resp.get("content") or "").strip() or None
    except Exception as exc:
        log.warning("cluster_summary_failed", error=str(exc))
        return None


# ---------------------------------------------------------------------------
# GET /v1/reports
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=list[ReportResponse],
    summary="List reports for the current user",
)
async def list_reports(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> list[ReportResponse]:
    """Return all report jobs ordered by creation time (newest first)."""
    tenant_id = current_user.get("tenant_id", "default")
    rows = (
        await db.execute(
            text(
                """
                SELECT id, selector, options, status, created_at, updated_at, error
                FROM jobs
                WHERE type = 'report' AND (tenant_id = :tid OR selector->>'tenant_id' = :tid)
                ORDER BY created_at DESC
                LIMIT 100
                """
            ),
            {"tid": tenant_id},
        )
    ).mappings().all()

    return [_row_to_report(dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# POST /v1/reports
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ReportResponse,
    summary="Request an async report generation",
    dependencies=[Depends(rate_limit)],
)
async def create_report(
    body: ReportRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> ReportResponse:
    """Enqueue report generation for *campaign_id*.

    The report is generated asynchronously by the report worker.  Poll
    ``GET /v1/reports/{id}`` to check for completion.
    """
    report_id = str(uuid.uuid4())
    now = datetime.now(tz=timezone.utc)
    tenant_id = current_user.get("tenant_id", "default")

    # Corpus-wide reports (the dashboard) omit campaign_id → default to "all".
    campaign_id = body.campaign_id or "all"

    selector = {
        "campaign_id": campaign_id,
        "filters": body.filters.model_dump(exclude_none=True) if body.filters else {},
        "tenant_id": tenant_id,
    }
    options = {
        "title": body.title,
        "type": body.type,
        "include_sentiment": body.include_sentiment,
        "include_engagement": body.include_engagement,
        "include_topics": body.include_topics,
        "requested_by": current_user.get("sub"),
        "tenant_id": tenant_id,
        **(body.options or {}),
    }

    # Generate the report content inline — the aggregation is a handful of SQL
    # queries, so reports complete at request time (no queue consumer needed).
    try:
        content = await _generate_report_content(db, campaign_id, tenant_id=tenant_id)
        content["summary_source"] = "aggregate"

        # Grounded reports (default) get an LLM-B narrative on top of the
        # SQL aggregates; the aggregate one-liner is kept as a fallback and
        # for audit. Honour the runtime backend override the dashboard sets.
        if bool((body.options or {}).get("grounded", True)):
            backend_override = None
            try:
                raw_override = await redis.get("config:llm_backend")
                if raw_override in ("local", "groq"):
                    backend_override = raw_override
            except Exception:
                pass
            narrative = await _llm_narrative(content, campaign_id, body.type, backend_override)
            if narrative:
                content["aggregate_summary"] = content["summary"]
                content["summary"] = narrative
                content["summary_source"] = "llm"

            # Embedding clusters + per-cluster LLM summaries (§5 cost lever):
            # ~k LLM calls for the whole corpus, not one per post. Best-effort —
            # never fail report creation if clustering/LLM is unavailable.
            try:
                emb_clusters, emb_are_stub = await _embedding_clusters(
                    db, campaign_id, backend_override, tenant_id=tenant_id
                )
                content["embedding_clusters"] = emb_clusters
                content["embedding_clusters_are_stub"] = emb_are_stub
            except Exception as exc:
                log.warning("embedding_clusters_failed", error=str(exc))
                content["embedding_clusters"] = []
                content["embedding_clusters_are_stub"] = False

        options["result"] = content
        await db.execute(
            text(
                """
                INSERT INTO jobs (id, tenant_id, type, status, selector, options, created_at, updated_at)
                VALUES (:id, :tenant_id, :type, :status, CAST(:selector AS jsonb), CAST(:options AS jsonb), :created_at, :updated_at)
                """
            ),
            {
                "id": report_id,
                "tenant_id": tenant_id,
                "type": "report",
                "status": "done",
                "selector": json.dumps(selector, default=str),
                "options": json.dumps(options, default=str),
                "created_at": now,
                "updated_at": now,
            },
        )
    except Exception as exc:
        log.error("report_create_failed", report_id=report_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate report",
        ) from exc

    log.info("report_generated", report_id=report_id, campaign_id=campaign_id)
    return ReportResponse(
        id=report_id,
        report_id=report_id,
        campaign_id=campaign_id,
        type=body.type,
        title=body.title,
        status="done",
        created_at=now,
        updated_at=now,
        period=content.get("period"),
        summary=content.get("summary"),
        summary_source=content.get("summary_source"),
        clusters=content.get("clusters"),
        embedding_clusters=content.get("embedding_clusters"),
        embedding_clusters_are_stub=bool(content.get("embedding_clusters_are_stub")),
        metrics=content.get("metrics"),
    )


# ---------------------------------------------------------------------------
# GET /v1/reports/export_latest
# ---------------------------------------------------------------------------


@router.get(
    "/export_latest",
    summary="Generate and immediately download an analysis report for recent posts",
)
@router.get(
    "/export_latest/export",
    include_in_schema=False,
)
async def export_latest_report(
    campaign_id: str = Query("all", description="Campaign ID or 'all'"),
    type: str = Query("mass_reaction", description="Report type"),
    format: str = Query("pdf", description="pdf or html"),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
):
    """Generate a fresh grounded mass reaction report and stream it directly for download."""
    tenant_id = current_user.get("tenant_id", "default")
    recent_row = None
    try:
        recent_row = (
            await db.execute(
                text(
                    """
                    SELECT id FROM jobs
                    WHERE type = 'report' AND status = 'done'
                      AND (tenant_id = :tid OR selector->>'tenant_id' = :tid)
                      AND COALESCE(selector->>'campaign_id', 'all') = :cid
                      AND created_at >= NOW() - INTERVAL '5 minutes'
                    ORDER BY created_at DESC LIMIT 1
                    """
                ),
                {"tid": tenant_id, "cid": campaign_id},
            )
        ).mappings().first()
    except Exception:
        recent_row = None

    rep_id = (recent_row.get("id") or recent_row.get("report_id")) if recent_row else None
    if rep_id:
        return await export_report(rep_id, format=format, db=db, current_user=current_user)

    req = ReportRequest(
        campaign_id=campaign_id,
        type=type,
        title=f"Mass Reaction Analysis ({campaign_id})",
        options={"grounded": True},
    )
    rep_resp = await create_report(req, Request({"type": "http"}), db=db, redis=redis, current_user=current_user)
    return await export_report(rep_resp.report_id, format=format, db=db, current_user=current_user)


# ---------------------------------------------------------------------------
# GET /v1/reports/{id}
# ---------------------------------------------------------------------------


@router.get(
    "/{report_id}",
    response_model=ReportResponse,
    summary="Get a report by ID",
)
async def get_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> ReportResponse:
    """Return the current status and metadata for *report_id*."""
    tenant_id = current_user.get("tenant_id", "default")
    row = await _get_report_row(db, report_id, tenant_id=tenant_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report '{report_id}' not found",
        )
    return _row_to_report(row)


def _report_to_html(report_data: dict) -> str:
    title = html_escape(str(report_data.get("title") or report_data.get("type") or "Mass Reaction & Intelligence Report"))
    report_id = html_escape(str(report_data.get("id") or report_data.get("report_id") or "N/A"))
    campaign_id = html_escape(str(report_data.get("campaign_id") or "all"))
    created_at = report_data.get("created_at") or datetime.now(tz=timezone.utc).isoformat()
    if isinstance(created_at, datetime):
        date_str = created_at.strftime("%Y-%m-%d %H:%M UTC")
    else:
        date_str = str(created_at)[:19].replace("T", " ")

    summary = html_escape(str(report_data.get("summary") or "No executive summary available."))
    summary_source = html_escape(str(report_data.get("summary_source") or "aggregate"))
    period = html_escape(str(report_data.get("period") or "All available history"))

    metrics = report_data.get("metrics") or {}
    total_posts = metrics.get("total_posts") or 0
    sent_map = metrics.get("sentiment_breakdown") or {}
    platform_map = metrics.get("platforms") or {}
    lang_map = metrics.get("languages") or {}

    warning_count = metrics.get("warning_count") or 0
    avg_tox = float(metrics.get("avg_toxicity") or 0.0)
    avg_hate = float(metrics.get("avg_hate_speech") or 0.0)
    warning_reasons = metrics.get("warning_reasons") or {}

    warnings = report_data.get("warnings") or []
    clusters = report_data.get("clusters") or []
    emb_clusters = report_data.get("embedding_clusters") or []
    emb_stub = bool(report_data.get("embedding_clusters_are_stub"))

    dom_sent = "neutral"
    if sent_map:
        dom_sent = max(sent_map, key=sent_map.get)

    def sent_badge_class(s):
        s_lower = str(s).lower()
        if s_lower == "positive": return "badge-pos"
        if s_lower == "negative": return "badge-neg"
        if s_lower == "mixed": return "badge-mixed"
        return "badge-neutral"

    # --- Sentiment Rows & Multi-segment Bar ---
    sent_rows_html = ""
    pos_pct = round(((sent_map.get("positive") or 0) / total_posts) * 100, 1) if total_posts > 0 else 0
    neu_pct = round(((sent_map.get("neutral") or 0) / total_posts) * 100, 1) if total_posts > 0 else 0
    neg_pct = round(((sent_map.get("negative") or 0) / total_posts) * 100, 1) if total_posts > 0 else 0
    mix_pct = round(((sent_map.get("mixed") or 0) / total_posts) * 100, 1) if total_posts > 0 else 0

    for s_label, count in sent_map.items():
        pct_val = round((count / total_posts) * 100, 1) if total_posts > 0 else 0
        s_esc = html_escape(str(s_label))
        sent_rows_html += f"""
        <tr>
            <td><span class="badge {sent_badge_class(s_label)}">{s_esc}</span></td>
            <td><strong>{count}</strong></td>
            <td>
                <div class="bar-container">
                    <div class="bar bar-{s_esc.lower()}" style="width: {pct_val}%;"></div>
                </div>
                <span class="pct-label">{pct_val}%</span>
            </td>
        </tr>
        """

    # --- Platform Rows ---
    platform_rows_html = ""
    for p_name, p_count in platform_map.items():
        p_pct = round((p_count / total_posts) * 100, 1) if total_posts > 0 else 0
        p_esc = html_escape(str(p_name))
        platform_rows_html += f"""
        <tr>
            <td><strong>{p_esc}</strong></td>
            <td>{p_count}</td>
            <td>
                <div class="bar-container">
                    <div class="bar bar-platform" style="width: {p_pct}%;"></div>
                </div>
                <span class="pct-label">{p_pct}%</span>
            </td>
        </tr>
        """

    # --- Topic Rows ---
    topic_rows_html = ""
    for c in clusters:
        c_label = html_escape(str(c.get("label") or "Topic"))
        c_size = c.get("size") or 0
        c_sent = html_escape(str(c.get("top_sentiment") or "neutral"))
        c_pct = round((c_size / total_posts) * 100, 1) if total_posts > 0 else 0
        topic_rows_html += f"""
        <tr>
            <td><strong>{c_label}</strong></td>
            <td>{c_size} posts ({c_pct}%)</td>
            <td><span class="badge {sent_badge_class(c_sent)}">{c_sent}</span></td>
        </tr>
        """

    # --- Detailed Watchlist Warning Table Rows ---
    warning_table_html = ""
    for w in warnings:
        w_id = html_escape(str(w.get("post_id") or "Unknown"))
        w_reason = html_escape(str(w.get("reason") or "Watchlist trigger"))
        w_sent = html_escape(str(w.get("sentiment") or "neutral"))
        w_summ = html_escape(str(w.get("summary") or "Content flagged for review."))
        w_plat = html_escape(str(w.get("platform") or "Unknown"))
        w_tox = w.get("toxicity") or 0.0
        w_hate = w.get("hate_speech") or 0.0

        tox_badge = f'<span class="badge badge-neg">Tox: {w_tox:.2f}</span>' if w_tox > 0.4 else f'<span class="badge badge-neutral">Tox: {w_tox:.2f}</span>'
        warning_table_html += f"""
        <tr>
            <td><code class="code-id">{w_id[:12]}</code></td>
            <td><span class="platform-pill">{w_plat}</span></td>
            <td><strong class="warn-reason">{w_reason}</strong></td>
            <td><span class="badge {sent_badge_class(w_sent)}">{w_sent}</span> {tox_badge}</td>
            <td class="cell-summary">{w_summ}</td>
        </tr>
        """

    # --- Embedding Cluster Cards ---
    emb_cards_html = ""
    for idx, ec in enumerate(emb_clusters):
        cid = html_escape(str(ec.get("cluster_id") or f"cluster-{idx}"))
        csize = ec.get("size") or 0
        csent = html_escape(str(ec.get("top_sentiment") or "neutral"))
        csumm = html_escape(str(ec.get("summary") or "Theme analysis available."))
        emb_cards_html += f"""
        <div class="emb-card">
            <div class="emb-card-header">
                <span class="emb-title">Cluster {cid} ({csize} posts)</span>
                <span class="badge {sent_badge_class(csent)}">{csent}</span>
            </div>
            <p class="emb-summary">{csumm}</p>
        </div>
        """

    warn_rate_pct = round((warning_count / total_posts) * 100, 1) if total_posts > 0 else 0
    tox_pct = min(round(avg_tox * 100, 1), 100)
    risk_level = "Low" if avg_tox < 0.25 else "Moderate" if avg_tox < 0.5 else "Elevated" if avg_tox < 0.75 else "Severe"
    risk_badge_cls = "badge-pos" if risk_level == "Low" else "badge-mixed" if risk_level in ("Moderate", "Elevated") else "badge-neg"

    warn_card_cls = "card-warn" if warning_count > 0 else ""
    warn_card_style = "color: #be123c;" if warning_count > 0 else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
    @page {{
        size: A4;
        margin: 14mm 12mm;
    }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        color: #1e293b;
        background: #ffffff;
        line-height: 1.5;
        font-size: 12px;
        margin: 0;
        padding: 0;
    }}
    .header {{
        border-bottom: 2px solid #6366f1;
        padding-bottom: 10px;
        margin-bottom: 16px;
    }}
    .header h1 {{
        font-size: 20px;
        margin: 0 0 4px 0;
        color: #0f172a;
        font-weight: 700;
    }}
    .meta-line {{
        font-size: 10px;
        color: #64748b;
    }}
    .summary-box {{
        background: #f8fafc;
        border: 1px solid #cbd5e1;
        border-left: 4px solid #6366f1;
        border-radius: 6px;
        padding: 12px 14px;
        margin-bottom: 18px;
    }}
    .summary-box h2 {{
        font-size: 11px;
        margin: 0 0 6px 0;
        color: #4338ca;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }}
    .summary-text {{
        font-size: 12px;
        color: #334155;
        margin: 0;
        line-height: 1.5;
    }}
    .cards-grid {{
        display: table;
        width: 100%;
        margin-bottom: 18px;
    }}
    .card-cell {{
        display: table-cell;
        width: 25%;
        padding-right: 8px;
        vertical-align: top;
    }}
    .card-cell:last-child {{
        padding-right: 0;
    }}
    .card {{
        background: #f1f5f9;
        border: 1px solid #e2e8f0;
        border-radius: 6px;
        padding: 10px 8px;
        text-align: center;
    }}
    .card-warn {{
        background: #fff1f2;
        border-color: #fecdd3;
    }}
    .card-num {{
        font-size: 18px;
        font-weight: 700;
        color: #0f172a;
    }}
    .card-label {{
        font-size: 9px;
        color: #64748b;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-top: 2px;
    }}
    .section-title {{
        font-size: 13px;
        font-weight: 700;
        color: #0f172a;
        margin: 16px 0 8px 0;
        border-bottom: 1.5px solid #e2e8f0;
        padding-bottom: 4px;
    }}
    .multi-progress-container {{
        background: #f1f5f9;
        border-radius: 6px;
        padding: 10px 12px;
        margin-bottom: 16px;
        border: 1px solid #e2e8f0;
    }}
    .multi-progress-title {{
        font-size: 10px;
        text-transform: uppercase;
        font-weight: 600;
        color: #475569;
        margin-bottom: 6px;
    }}
    .stacked-bar {{
        height: 16px;
        width: 100%;
        background: #e2e8f0;
        border-radius: 8px;
        overflow: hidden;
        display: flex;
        margin-bottom: 8px;
    }}
    .seg-pos {{ background: #10b981; height: 100%; }}
    .seg-neu {{ background: #64748b; height: 100%; }}
    .seg-neg {{ background: #ef4444; height: 100%; }}
    .seg-mix {{ background: #f59e0b; height: 100%; }}
    .legend-row {{
        font-size: 10px;
        color: #475569;
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
    }}
    .legend-item {{
        display: inline-flex;
        align-items: center;
        margin-right: 12px;
    }}
    .dot {{
        width: 8px;
        height: 8px;
        border-radius: 50%;
        display: inline-block;
        margin-right: 4px;
    }}
    .dot-pos {{ background: #10b981; }}
    .dot-neu {{ background: #64748b; }}
    .dot-neg {{ background: #ef4444; }}
    .dot-mix {{ background: #f59e0b; }}
    table {{
        width: 100%;
        border-collapse: collapse;
        margin-bottom: 14px;
    }}
    th {{
        text-align: left;
        font-size: 10px;
        text-transform: uppercase;
        color: #64748b;
        padding: 6px 8px;
        border-bottom: 1.5px solid #cbd5e1;
        background: #f8fafc;
    }}
    td {{
        padding: 6px 8px;
        border-bottom: 1px solid #f1f5f9;
        vertical-align: middle;
        font-size: 11px;
    }}
    .bar-container {{
        background: #e2e8f0;
        height: 7px;
        border-radius: 4px;
        display: inline-block;
        width: 100px;
        overflow: hidden;
        vertical-align: middle;
        margin-right: 6px;
    }}
    .bar {{
        height: 100%;
        border-radius: 4px;
    }}
    .bar-positive {{ background: #10b981; }}
    .bar-negative {{ background: #ef4444; }}
    .bar-neutral {{ background: #64748b; }}
    .bar-mixed {{ background: #f59e0b; }}
    .bar-platform {{ background: #6366f1; }}
    .bar-tox {{ background: #f43f5e; }}
    .pct-label {{
        font-size: 10px;
        color: #475569;
    }}
    .badge {{
        display: inline-block;
        padding: 2px 7px;
        border-radius: 10px;
        font-size: 9px;
        font-weight: 600;
        text-transform: uppercase;
    }}
    .badge-pos {{ background: #d1fae5; color: #047857; }}
    .badge-neg {{ background: #fee2e2; color: #b91c1c; }}
    .badge-neutral {{ background: #f1f5f9; color: #475569; }}
    .badge-mixed {{ background: #fef3c7; color: #b45309; }}
    .warn-banner {{
        background: #fff1f2;
        border: 1px solid #fecdd3;
        border-left: 4px solid #e11d48;
        border-radius: 6px;
        padding: 10px 12px;
        margin-bottom: 14px;
    }}
    .warn-banner h3 {{
        font-size: 11px;
        margin: 0 0 4px 0;
        color: #be123c;
        text-transform: uppercase;
    }}
    .warn-reason {{
        color: #be123c;
    }}
    .code-id {{
        font-family: monospace;
        font-size: 10px;
        background: #f1f5f9;
        padding: 2px 4px;
        border-radius: 3px;
    }}
    .platform-pill {{
        font-size: 9px;
        background: #e0e7ff;
        color: #4338ca;
        padding: 1px 6px;
        border-radius: 4px;
        font-weight: 600;
    }}
    .cell-summary {{
        color: #475569;
        font-size: 10px;
    }}
    .emb-card {{
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 6px;
        padding: 8px 10px;
        margin-bottom: 6px;
    }}
    .emb-card-header {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 2px;
    }}
    .emb-title {{
        font-weight: 600;
        color: #334155;
    }}
    .emb-summary {{
        margin: 0;
        color: #475569;
        font-size: 11px;
    }}
    .two-col {{
        display: table;
        width: 100%;
        margin-bottom: 14px;
    }}
    .col-left {{
        display: table-cell;
        width: 50%;
        padding-right: 10px;
        vertical-align: top;
    }}
    .col-right {{
        display: table-cell;
        width: 50%;
        padding-left: 10px;
        vertical-align: top;
    }}
    .footer {{
        margin-top: 20px;
        padding-top: 8px;
        border-top: 1px solid #e2e8f0;
        font-size: 9px;
        color: #94a3b8;
    }}
</style>
</head>
<body>

<div class="header">
    <h1>{title}</h1>
    <div class="meta-line">
        Report ID: {report_id} &bull; Campaign: {campaign_id} &bull; Generated: {date_str} &bull; Engine: Stage 1/2 LLM ({summary_source})
    </div>
</div>

<div class="summary-box">
    <h2>Executive Summary — Mass Reaction Analysis</h2>
    <p class="summary-text">{summary}</p>
</div>

<div class="cards-grid">
    <div class="card-cell">
        <div class="card">
            <div class="card-num">{total_posts}</div>
            <div class="card-label">Posts Analyzed</div>
        </div>
    </div>
    <div class="card-cell">
        <div class="card">
            <div class="card-num"><span class="badge {sent_badge_class(dom_sent)}">{html_escape(str(dom_sent))}</span></div>
            <div class="card-label">Dominant Sentiment</div>
        </div>
    </div>
    <div class="card-cell">
        <div class="card {warn_card_cls}">
            <div class="card-num" style="{warn_card_style}">{warning_count} <span style="font-size: 11px;">({warn_rate_pct}%)</span></div>
            <div class="card-label">Watchlist Alerts</div>
        </div>
    </div>
    <div class="card-cell">
        <div class="card">
            <div class="card-num"><span class="badge {risk_badge_cls}">{risk_level} ({avg_tox:.2f})</span></div>
            <div class="card-label">Toxicity & Threat Index</div>
        </div>
    </div>
</div>

<!-- Sentiment Stacked Progress Chart -->
<div class="multi-progress-container">
    <div class="multi-progress-title">Sentiment & Public Reaction Proportions</div>
    <div class="stacked-bar">
        <div class="seg-pos" style="width: {pos_pct}%;" title="Positive {pos_pct}%"></div>
        <div class="seg-neu" style="width: {neu_pct}%;" title="Neutral {neu_pct}%"></div>
        <div class="seg-neg" style="width: {neg_pct}%;" title="Negative {neg_pct}%"></div>
        <div class="seg-mix" style="width: {mix_pct}%;" title="Mixed {mix_pct}%"></div>
    </div>
    <div class="legend-row">
        <span class="legend-item"><span class="dot dot-pos"></span> Positive: <strong>{sent_map.get('positive', 0)}</strong> ({pos_pct}%)</span>
        <span class="legend-item"><span class="dot dot-neu"></span> Neutral: <strong>{sent_map.get('neutral', 0)}</strong> ({neu_pct}%)</span>
        <span class="legend-item"><span class="dot dot-neg"></span> Negative: <strong>{sent_map.get('negative', 0)}</strong> ({neg_pct}%)</span>
        <span class="legend-item"><span class="dot dot-mix"></span> Mixed: <strong>{sent_map.get('mixed', 0)}</strong> ({mix_pct}%)</span>
    </div>
</div>

<!-- 2-Column Analytics Layout: Platforms & Threat Gauge -->
<div class="two-col">
    <div class="col-left">
        <div class="section-title">Platform Distribution</div>
        <table>
            <thead>
                <tr>
                    <th>Platform</th>
                    <th>Count</th>
                    <th>Share</th>
                </tr>
            </thead>
            <tbody>
                {platform_rows_html if platform_rows_html else '<tr><td colSpan="3">All Platforms</td></tr>'}
            </tbody>
        </table>
    </div>
    <div class="col-right">
        <div class="section-title">Threat & Risk Exposure Index</div>
        <table>
            <thead>
                <tr>
                    <th>Metric</th>
                    <th>Score</th>
                    <th>Risk Gauge</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td><strong>Avg Toxicity Score</strong></td>
                    <td>{avg_tox:.3f}</td>
                    <td>
                        <div class="bar-container">
                            <div class="bar bar-tox" style="width: {tox_pct}%;"></div>
                        </div>
                        <span class="pct-label">{tox_pct}%</span>
                    </td>
                </tr>
                <tr>
                    <td><strong>Avg Hate Speech Score</strong></td>
                    <td>{avg_hate:.3f}</td>
                    <td>
                        <div class="bar-container">
                            <div class="bar bar-negative" style="width: {min(round(avg_hate * 100, 1), 100)}%;"></div>
                        </div>
                        <span class="pct-label">{min(round(avg_hate * 100, 1), 100)}%</span>
                    </td>
                </tr>
            </tbody>
        </table>
    </div>
</div>

{f'''
<!-- Watchlist Warnings & Detailed Alerts Section -->
<div class="warn-banner">
    <h3>Watchlist Warnings & Risk Intelligence ({warning_count} Total Alerts)</h3>
    <p style="margin: 0; font-size: 11px; color: #9f1239;">
        Analyst inspection required: {warning_count} post(s) triggered system watchlist warnings due to elevated toxicity, hate speech, or watchlist keyword triggers.
    </p>
</div>

<div class="section-title">Detailed Watchlist Warnings & Flagged Posts</div>
<table>
    <thead>
        <tr>
            <th>Post ID</th>
            <th>Platform</th>
            <th>Warning Reason</th>
            <th>Sentiment / Toxicity</th>
            <th>Summary / Content Preview</th>
        </tr>
    </thead>
    <tbody>
        {warning_table_html}
    </tbody>
</table>
''' if warning_table_html else ''}

{f'''
<div class="section-title">Key Topic Clusters</div>
<table>
    <thead>
        <tr>
            <th>Topic</th>
            <th>Volume</th>
            <th>Dominant Sentiment</th>
        </tr>
    </thead>
    <tbody>
        {topic_rows_html}
    </tbody>
</table>
''' if topic_rows_html else ''}

{f'''
<div class="section-title">Semantic Clusters & LLM Summaries {"(Stub Vectors)" if emb_stub else ""}</div>
<div>
    {emb_cards_html}
</div>
''' if emb_cards_html else ''}

<div class="footer">
    Defense Social Media Monitoring & Threat Intelligence &bull; Period: {period} &bull; Confidential
</div>

</body>
</html>"""


# ---------------------------------------------------------------------------
# GET /v1/reports/export_latest
# ---------------------------------------------------------------------------
# GET /v1/reports/{id}/export
# ---------------------------------------------------------------------------


@router.get(
    "/{report_id}/export",
    summary="Export a report as a downloadable PDF or HTML file",
)
async def export_report(
    report_id: str,
    format: str = Query("pdf", description="Export format: pdf, html, or json"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Return an analysis report as a formatted PDF or HTML file download."""
    if report_id == "export_latest":
        return await export_latest_report(campaign_id="all", format=format, db=db, redis=None, current_user=current_user)

    tenant_id = current_user.get("tenant_id", "default")
    row = await _get_report_row(db, report_id, tenant_id=tenant_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report '{report_id}' not found",
        )
    rep = _row_to_report(row)
    rep_dict = rep.model_dump()

    if format.lower() == "json":
        return rep_dict

    html_content = _report_to_html(rep_dict)

    if format.lower() == "pdf":
        if HTML is not None:
            try:
                pdf_bytes = await asyncio.to_thread(lambda: HTML(string=html_content).write_pdf())
                return Response(
                    content=pdf_bytes,
                    media_type="application/pdf",
                    headers={
                        "Content-Disposition": f"attachment; filename=\"analysis_report_{report_id[:8]}.pdf\"",
                        "X-Report-Id": report_id,
                    },
                )
            except Exception as exc:
                log.warning("report_pdf_generation_failed", error=str(exc))
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"PDF generation failed: {exc}",
                ) from exc
        else:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="PDF export is unavailable because WeasyPrint is not installed",
            )

    return Response(
        content=html_content,
        media_type="text/html",
        headers={
            "Content-Disposition": f"attachment; filename=\"analysis_report_{report_id[:8]}.html\"",
            "X-Report-Id": report_id,
        },
    )

