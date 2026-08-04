"""Pydantic models for all API request/response bodies."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TokenRequest(BaseModel):
    username: str = Field(..., description="Username for authentication")
    password: str = Field(..., description="Password for authentication")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


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
        None, description="Inline batch of PostWithDetails objects"
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
    comment_count: int
    stored_comments: int
    total_reactions: int
    share_count: int


class CommentAnalysisResult(BaseModel):
    analyzed: int
    coverage: float
    coverage_label: Optional[str] = None  # server-rendered coverage string
    summary: Optional[str] = None  # LLM-written natural-language mood of the comments
    summary_source: Optional[str] = None  # "llm" | None
    sentiment_breakdown: Dict[str, int]
    emotion_breakdown: Dict[str, int] = {}  # per-comment emotion counts (schema v1.1)
    method_breakdown: Dict[str, int] = {}
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
    stage1_ms: Optional[float] = None
    stage2_ms: Optional[float] = None
    llm_used: bool = False
    llm_backend: Optional[str] = None
    llm_model: Optional[str] = None
    schema_version: str = "1.0"


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
    overall_sentiment: str
    sentiment_score: float
    # Per-component sentiments, each its own {label, score} (caption / image).
    # null for null-caption (text) / text-only (image) posts respectively.
    text_sentiment: Optional[Dict[str, Any]] = None
    image_sentiment: Optional[Dict[str, Any]] = None
    baseline_sentiment: Optional[float] = None
    emotion: Optional[Dict[str, Any]] = None
    intents: List[str] = []
    topics: List[str] = []
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


class OverviewResponse(BaseModel):
    """Everything the Overview tab renders — fully aggregated server-side so the
    dashboard only fetches and displays it (no client-side counting)."""

    total_posts: int = 0
    campaign_id: Optional[str] = None
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
    metrics: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class SearchResult(BaseModel):
    post_id: str
    campaign_id: Optional[str] = None
    score: float = 1.0
    snippet: Optional[str] = None
    result: Optional[Dict[str, Any]] = None


class SearchResponse(BaseModel):
    query: str
    semantic: bool
    total: int
    results: List[SearchResult]
