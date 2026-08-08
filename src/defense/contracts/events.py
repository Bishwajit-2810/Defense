import json
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional, Union
from pydantic import BaseModel, Field

class BaseEvent(BaseModel):
    post_id: str
    job_id: Optional[str] = None
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class PostIngested(BaseEvent):
    type: Literal["PostIngested"] = "PostIngested"
    hash: str
    platform: str

class PostAnalyzed(BaseEvent):
    type: Literal["PostAnalyzed"] = "PostAnalyzed"
    stage1_result: Dict[str, Any]

class PostRouted(BaseEvent):
    type: Literal["PostRouted"] = "PostRouted"
    use_llm: bool
    reasons: list[str]

class PostEnriched(BaseEvent):
    type: Literal["PostEnriched"] = "PostEnriched"
    stage2_result: Dict[str, Any]

class ResultAssembled(BaseEvent):
    type: Literal["ResultAssembled"] = "ResultAssembled"
    valid: bool

PipelineEvent = Union[PostIngested, PostAnalyzed, PostRouted, PostEnriched, ResultAssembled]

async def emit_pipeline_event(redis: Any, event: PipelineEvent):
    """Append a strongly-typed domain event to the global pipeline:events stream."""
    try:
        payload = event.model_dump(mode="json")
        body = json.dumps(payload, ensure_ascii=False)
        await redis.xadd("pipeline:events", {"event": body}, maxlen=100000, approximate=True)
    except Exception:
        # Progress and events should not fail the pipeline
        pass
