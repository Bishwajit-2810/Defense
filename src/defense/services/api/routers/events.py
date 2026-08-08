from typing import Any
import json
from fastapi import APIRouter, Depends, Query
import redis.asyncio as aioredis
from defense.services.api.deps import get_current_user, get_redis

router = APIRouter(prefix="/v1/events", tags=["events"])

@router.get("", summary="Fetch recent pipeline events from the event stream")
async def get_pipeline_events(
    limit: int = Query(200, ge=1, le=1000, description="Most recent N events"),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Returns the most recent events from the global `pipeline:events` stream."""
    try:
        # Read from the end of the stream, reversed (newest first), up to `limit`
        raw_events = await redis.xrevrange("pipeline:events", max="+", min="-", count=limit)
    except Exception:
        return {"events": [], "error": "failed to read stream"}

    events = []
    for message_id, fields in raw_events:
        try:
            event_data = json.loads(fields.get(b"event", b"{}").decode())
            events.append({
                "stream_id": message_id.decode(),
                "event": event_data
            })
        except Exception:
            continue
            
    return {"events": events}
