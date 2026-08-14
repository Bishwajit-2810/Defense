"""Reports endpoints — generate, list, and retrieve analysis reports."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone

# Repo root on path so `libs.llm` imports when the API runs from services/api.

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from defense.services.api.deps import get_current_user, get_db, get_redis, rate_limit
from defense.services.api.models import ReportRequest, ReportResponse

log = structlog.get_logger(__name__)

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
    selector: dict = row.get("selector") or {}
    options: dict = row.get("options") or {}
    content: dict = options.get("result") or {}
    return ReportResponse(
        id=row["id"],
        report_id=row["id"],
        campaign_id=selector.get("campaign_id", ""),
        type=options.get("type"),
        title=options.get("title"),
        status=row["status"],
        error=row.get("error"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        download_url=options.get("download_url"),
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
                     jsonb_array_elements_text(ar.result->'topics') AS t(topic)
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

    total = head["total_posts"] or 0
    sent_map = {r["s"] or "neutral": r["n"] for r in sentiments}
    lang_map = {r["lang"] or "und": r["n"] for r in languages}
    period = None
    if head["first_at"] and head["last_at"]:
        period = f"{head['first_at']:%Y-%m-%d} → {head['last_at']:%Y-%m-%d}"

    scope_label = f"campaign {campaign_id}" if scoped else "all campaigns"
    sent_text = ", ".join(f"{k}: {v}" for k, v in sent_map.items()) or "no data"
    top_topics = ", ".join(c["label"] for c in clusters[:5]) or "none"
    summary = (
        f"Analyzed {total} posts across {scope_label}"
        + (f" ({period})" if period else "")
        + f". Sentiment breakdown — {sent_text}. Top topics: {top_topics}."
    )

    return {
        "period": period,
        "summary": summary,
        "clusters": clusters,
        "metrics": {
            "total_posts": total,
            "sentiment_breakdown": sent_map,
            "languages": lang_map,
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
            timeout=30.0,
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

    clusters: list[dict] = []
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
        summary = await _summarize_cluster(llm, sample, top_sentiment, backend_override)

        clusters.append({
            "cluster_id": f"emb-{ci}",
            "size": cr.sizes[ci],
            "top_sentiment": top_sentiment,
            "representative_post_id": meta[rep_idx]["post_id"] if rep_idx >= 0 else None,
            "summary": summary,
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
            timeout=30.0,
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
