"""Pydantic models for all API request/response bodies."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TokenRequest(BaseModel):
    username: str = Field(..., description="Username for authentication")
    password: str = Field(..., description="Password for authentication")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


#: Usernames are an identity other tenants' operators read in audit logs, so keep
#: them boring: no whitespace, no unicode look-alikes, no empty string.
_USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")

#: Deliberately low, and enforced in one place shared by the model and the
#: ``/v1/auth/config`` advertisement the dashboard renders — a rule the UI states
#: but the server does not apply is worse than no rule.
MIN_PASSWORD_LENGTH = 8


class SignupRequest(BaseModel):
    """Self-service registration.

    Note what is *absent*: ``tenant_id`` and ``role``. A client that could name
    its own tenant could read another tenant's corpus, and one that could name
    its own role would grant itself admin — so both are assigned server-side.
    """

    username: str = Field(
        ...,
        description="3-64 chars, lowercase letters/digits/._- , starting alphanumeric",
    )
    password: str = Field(..., description=f"At least {MIN_PASSWORD_LENGTH} characters")

    @field_validator("username")
    @classmethod
    def _valid_username(cls, v: str) -> str:
        # Case-fold before validating AND before storing: `users.username` is the
        # primary key, so without this "Alice" and "alice" are two accounts and
        # whoever registers second silently gets the other's login prompt.
        v = (v or "").strip().lower()
        if not _USERNAME_PATTERN.match(v):
            raise ValueError(
                "username must be 3-64 characters of lowercase letters, digits, "
                "'.', '_' or '-', and start with a letter or digit"
            )
        return v

    @field_validator("password")
    @classmethod
    def _valid_password(cls, v: str) -> str:
        if len(v or "") < MIN_PASSWORD_LENGTH:
            raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        return v


class SignupResponse(TokenResponse):
    """A token, plus the identity the server actually assigned.

    The token is returned so the dashboard does not have to immediately re-post
    the password to ``/v1/auth/token``; the echoed fields exist so the UI shows
    the tenant and role it *was given* rather than the ones it asked for.
    """

    username: str
    tenant_id: str
    role: str


# ---------------------------------------------------------------------------
# Jobs / shared
# ---------------------------------------------------------------------------


class JobResponse(BaseModel):
    job_id: str
    status: str = "pending"
    status_url: str


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


class IngestSyncRequest(BaseModel):
    campaign_id: str = Field(..., description="Campaign identifier to pull posts for")
    posted_from: datetime = Field(..., description="Start of the date range (inclusive)")
    posted_to: datetime = Field(..., description="End of the date range (inclusive)")

    @field_validator("posted_to")
    @classmethod
    def to_after_from(cls, v: datetime, info: Any) -> datetime:
        from_val = info.data.get("posted_from")
        if from_val and v < from_val:
            raise ValueError("posted_to must be >= posted_from")
        return v


class UploadRequest(BaseModel):
    """Accept either an inline list of posts or an S3 object URI."""

    posts: Optional[List[Dict[str, Any]]] = Field(
        None, description="Inline batch of PostWithDetails objects", max_length=1000
    )
    object_uri: Optional[str] = Field(
        None, description="s3://bucket/key pointing to a JSON file of posts"
    )
    options: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Per-request task options carried with each post into the pipeline "
            "(want_summary, summary_lang, llm_backend, …)"
        ),
    )

    @field_validator("object_uri")
    @classmethod
    def validate_s3_uri(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.startswith("s3://"):
            raise ValueError("object_uri must start with s3://")
        return v

    def model_post_init(self, __context: Any) -> None:
        if self.posts is None and self.object_uri is None:
            raise ValueError("Either 'posts' or 'object_uri' must be provided")


class UploadJobResponse(BaseModel):
    job_id: str
    status: str = "pending"
    count: Optional[int] = None
    status_url: str


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


class AnalysisFilter(BaseModel):
    campaign_id: Optional[str] = None
    platform: Optional[str] = None
    posted_from: Optional[datetime] = None
    posted_to: Optional[datetime] = None
    min_sentiment_score: Optional[float] = None
    max_sentiment_score: Optional[float] = None


class AnalysisRunRequest(BaseModel):
    post_ids: Optional[List[str]] = Field(
        None, description="Explicit list of post IDs to analyse"
    )
    campaign_id: Optional[str] = Field(
        None, description="Analyse all posts for this campaign"
    )
    filter: Optional[AnalysisFilter] = Field(
        None, description="Additional filters applied when campaign_id is set"
    )
    options: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Per-request task options forwarded to the router/Stage-2 "
            "(want_summary, want_insight, target_lang, llm_backend, …)"
        ),
    )


class AnalysisRunResponse(BaseModel):
    analysis_id: str
    status: str = "queued"
    # Observed share of posts the router sent to Stage 2 (stats:llm_routed /
    # stats:total_processed). None — not a design target dressed up as an
    # estimate — until the router has actually routed something.
    estimated_llm_share: float | None = None
    status_url: str


class EngagementResult(BaseModel):
    comment_count: int = 0
    stored_comments: int = 0
    total_reactions: int = 0
    share_count: int = 0


class CommentAnalysisResult(BaseModel):
    analyzed: int
    coverage: float  # clamped to 1.0 — see coverage_anomaly
    coverage_label: Optional[str] = None  # server-rendered coverage string
    # Set when the stored comment rows exceed the platform's reported
    # commentCount (5 posts in the corpus, up to 112 stored against 42
    # reported). An upstream inconsistency, surfaced rather than absorbed.
    coverage_anomaly: Optional[Dict[str, Any]] = None
    summary: Optional[str] = None  # LLM-written natural-language mood of the comments
    summary_source: Optional[str] = None  # "llm" | None
    sentiment_breakdown: Dict[str, int] = {}          # all comments
    # Written comments only — emoji-only reactions excluded. Charting the two
    # side by side separates "what people said" from "how the crowd reacted"
    # instead of blending 17% emoji reactions into one indistinguishable bar.
    sentiment_breakdown_substantive: Dict[str, int] = {}
    reaction_only: int = 0                       # emoji-only comment count
    # Per-watchlist-entity stance rollup (stance_targets.md). A SEPARATE
    # measurement from sentiment_breakdown: a comment can be positive in tone
    # while opposing a listed entity. Never sum or merge the two. Entities
    # nobody mentioned are absent rather than zero-filled.
    target_stances: Dict[str, Any] = {}
    # Per-comment emotion counts (schema v1.1). NOTE: emotion is the free
    # emoji+lexicon heuristic for every comment at Stage 1 — see each comment's
    # `emotion_method`. Only comments Stage 2 re-labelled carry a model emotion.
    emotion_breakdown: Dict[str, int] = {}
    method_breakdown: Dict[str, int] = {}
    # Where the labels above came from: {total, inferred, heuristic,
    # inferred_share, by_method}. Surfaced alongside the breakdowns so a
    # sentiment chart can state its own provenance the way it states coverage —
    # in the shipped configuration most comment labels are NOT model output.
    provenance: Dict[str, Any] = {}
    themes: List[str] = []
    top_keywords: List[str] = []
    representative_comments: List[Any] = []
    # Per-comment sentiment for every embedded comment. Only populated on the
    # single-result detail path; stripped from list responses to bound payload.
    comments: List[Any] = []


class ConfidenceResult(BaseModel):
    overall: float
    sentiment: float
    language: float
    topics: float


class ProcessingResult(BaseModel):
    """Run provenance — *what actually produced this result*.

    This model was a whitelist of six fields, and Pydantic silently discarded
    every other key in `processing`. Combined with the same pattern in the
    assembler, that meant **eleven** provenance fields — including `stub_mode`,
    `nlp_engine`, `degraded_components` and `role_models` — were computed by the
    pipeline, persisted to Postgres, and then dropped on the way out of the API.
    Consumers could not read them, so the dashboard rendered a confident
    "nothing degraded" on every run.

    `extra="allow"` is deliberate: a provenance model whose job is to answer
    "what ran?" must not be the thing that decides which answers are permitted.
    A new field added upstream now surfaces automatically instead of vanishing
    (and `tests/test_provenance_survives.py` asserts the chain end to end).
    """

    model_config = ConfigDict(extra="allow")

    stage1_ms: Optional[float] = None
    stage2_ms: Optional[float] = None
    llm_used: bool = False
    llm_backend: Optional[str] = None
    llm_model: Optional[str] = None
    schema_version: str = "1.0"

    # --- Stage-1 provenance (forwarded by the assembler) ---
    nlp_engine: Optional[str] = None            # stub | models | llm — INTENDED path
    stub_mode: Optional[bool] = None            # deterministic hashes, not model output
    degraded_components: Optional[List[str]] = None  # what actually fell back (§9.10)
    llm_role: Optional[str] = None
    unit: Optional[str] = None
    model_versions: Optional[Dict[str, Any]] = None
    vision_used: Optional[bool] = None
    vision_produced_signal: Optional[bool] = None   # only this licenses an image claim
    vision_model: Optional[str] = None
    vision_status: Optional[str] = None

    # --- Stage-2 provenance ---
    role_models: Optional[Dict[str, Any]] = None    # {role: resolved model id}


class AnalysisResultResponse(BaseModel):
    """Wraps the canonical output schema (output_schema.json)."""

    id: int
    post_id: str
    campaign_id: str
    platform: str
    platform_post_id: str
    media_type: str
    language: str
    post_text: Optional[str] = None  # original post caption/text
    post_type: Optional[str] = None
    post_summary: Optional[str] = None
    post_summary_lang: Optional[str] = None
    post_summary_source: Optional[str] = None  # "vlm" (image-grounded) | "llm" | null
    post_summary_grounding: Optional[str] = None  # e.g. "caption+ocr+image"
    # True when the summary hit the model's token ceiling even after
    # auto-continuation and was trimmed to its last complete sentence (§6.1).
    post_summary_truncated: Optional[bool] = None
    # Which detector produced `language` — fastText or the script heuristic.
    # `language_confidence` cannot be interpreted without it.
    language_method: Optional[str] = None
    watchlist_alert: Optional[bool] = None
    overall_sentiment: Optional[str] = None
    sentiment_score: Optional[float] = None
    # Per-component sentiments, each its own {label, score} (caption / image).
    # null for null-caption (text) / text-only (image) posts respectively.
    text_sentiment: Optional[Dict[str, Any]] = None
    image_sentiment: Optional[Dict[str, Any]] = None
    baseline_sentiment: Optional[float] = None
    emotion: Optional[Dict[str, Any]] = None
    intents: List[str] = []
    topics: List[str] = []
    # One-line Stage-2 insight (null when Stage 2 was skipped). `intents` and
    # `topics` above also carry Stage-2's refinements; all three used to be
    # dropped by the assembler before reaching this response.
    insight: Optional[str] = None
    entities: List[Dict[str, Any]] = []
    brand_mentions: List[str] = []
    keywords: List[str] = []
    toxicity_score: Optional[float] = None
    hate_speech_score: Optional[float] = None
    engagement: EngagementResult
    reaction_breakdown: Optional[Dict[str, Any]] = None
    image_analysis: Optional[Dict[str, Any]] = None
    comment_analysis: CommentAnalysisResult
    confidence: ConfidenceResult
    processing: ProcessingResult
    created_at: datetime
    scraped_at: datetime

    model_config = {"from_attributes": True}


class AnalysisDetailResponse(BaseModel):
    analysis_id: str
    status: str
    campaign_id: Optional[str] = None
    post_ids: List[str] = []
    results: Optional[List[AnalysisResultResponse]] = None
    created_at: Optional[datetime] = None
    progress: Optional[Dict[str, Any]] = Field(
        None, description="Per-job progress: {total, completed, failed}"
    )


# ---------------------------------------------------------------------------
# Overview (corpus-level aggregates — all computed server-side)
# ---------------------------------------------------------------------------


class LabelCount(BaseModel):
    """A label and its count, used for distribution lists."""

    label: str
    count: int


class LlmPanel(BaseModel):
    total_posts: int = 0
    posts_with_llm: int = 0
    posts_with_summaries: int = 0
    backends_seen: List[LabelCount] = []


class CorpusCoverage(BaseModel):
    """Comment coverage across the whole result set, not per post.

    Per-post coverage has a median of 26.7% and is what the dashboard shows;
    the aggregate — every stored comment against every reported `commentCount`
    — is a much smaller number and it is the one that bounds what a
    thread-level sentiment claim can support. It previously appeared nowhere.
    """

    analyzed: int = 0
    reported: int = 0
    coverage: float = 0.0          # analyzed / reported, corpus-wide
    posts_with_anomaly: int = 0    # posts storing more comments than reported


class OverviewResponse(BaseModel):
    """Everything the Overview tab renders — fully aggregated server-side so the
    dashboard only fetches and displays it (no client-side counting)."""

    total_posts: int = 0
    campaign_id: Optional[str] = None
    corpus_coverage: CorpusCoverage = CorpusCoverage()
    # Sentiment is a fixed taxonomy so the donut always has stable keys.
    sentiment_distribution: Dict[str, int] = Field(
        default_factory=lambda: {"positive": 0, "negative": 0, "neutral": 0, "mixed": 0}
    )
    language_distribution: List[LabelCount] = []
    top_topics: List[LabelCount] = []
    # Post-level dominant emotion across the corpus.
    emotion_distribution: List[LabelCount] = []
    # Per-comment emotion summed across all posts (from comment_analysis.emotion_breakdown).
    comment_emotion_distribution: List[LabelCount] = []
    llm_panel: LlmPanel = LlmPanel()


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


class ReportRequest(BaseModel):
    # campaign_id is optional: the dashboard generates corpus-wide reports with no
    # campaign scope. When omitted it defaults to "all" (every campaign).
    campaign_id: Optional[str] = Field(None, description="Campaign to scope the report to")
    type: Optional[str] = Field(None, description="Report type, e.g. 'trend' (free-form)")
    title: Optional[str] = None
    include_sentiment: bool = True
    include_engagement: bool = True
    include_topics: bool = True
    filters: Optional[AnalysisFilter] = None
    options: Optional[Dict[str, Any]] = None


class ReportResponse(BaseModel):
    id: str
    report_id: Optional[str] = None  # mirrors `id` (the dashboard reads report_id)
    campaign_id: str
    type: Optional[str] = None
    title: Optional[str] = None
    status: str = "pending"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    download_url: Optional[str] = None
    # Generated content (populated when status == "done"; rendered by the dashboard)
    period: Optional[str] = None
    summary: Optional[str] = None
    summary_source: Optional[str] = None  # "llm" (grounded narrative) | "aggregate"
    clusters: Optional[List[Dict[str, Any]]] = None
    # Embedding clusters — the LLM cost lever (architecture.md §5): one LLM-B
    # call per cluster instead of one per post. Distinct from `clusters` above,
    # which is the zero-LLM SQL topic aggregate.
    #
    # This field's absence WAS the bug: `_embedding_clusters` ran, paid for up to
    # MAX_CLUSTERS summaries, wrote them into the job row — and FastAPI's
    # response_model stripped them, because nothing declared them here. The same
    # shape as §11.1's discarded Stage-2 `insight`, in the feature the cost
    # argument is named after (PROJECT_ASSESSMENT §13.1).
    embedding_clusters: Optional[List[Dict[str, Any]]] = None
    # True when the vectors those clusters were computed from are hash stubs, so
    # a reader is never invited to treat a summary of noise as a finding (§13.2).
    embedding_clusters_are_stub: bool = False
    metrics: Optional[Dict[str, Any]] = None
    # Error message when status == "failed" — selected from the DB but previously
    # stripped by response_model because it was not declared here (§P7.15).
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class SearchResult(BaseModel):
    post_id: str
    campaign_id: Optional[str] = None
    score: float = 1.0
    snippet: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    # True when this row's stored vector (or the query's) is the deterministic
    # hash stub rather than a semantic embedding. The `score` is then a distance
    # between two random unit vectors — it looks exactly as plausible as a real
    # one, so a UI must not render this as a semantic match (§5.9).
    embedding_is_stub: bool = False


class SearchResponse(BaseModel):
    query: str
    semantic: bool
    total: int
    results: List[SearchResult]
