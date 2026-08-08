import pytest
from defense.contracts.events import PostIngested
from defense.libs.repos.posts import PostRepository

@pytest.mark.asyncio
async def test_ingestion_e2e(db_session, redis_client):
    """
    Golden path E2E test verifying that a Post is upserted to Postgres,
    and a PostIngested event is correctly emitted to Redis pipeline:events stream.
    """
    repo = PostRepository(db_session)
    
    post_payload = {
        "post_id": "test_post_1",
        "campaign_id": "test_campaign",
        "platform": "twitter",
        "text": "This is a test post for E2E",
        "author": "tester",
        "timestamp": "2023-01-01T00:00:00Z",
        "url": "https://twitter.com/tester/status/test_post_1"
    }
    
    raw_payload = {
        "campaignId": "test_campaign",
        "url": "https://twitter.com/tester/status/test_post_1",
        "createdAt": "2023-01-01T00:00:00Z"
    }
    
    # 1. Upsert Post
    await repo.upsert_post(post_payload, raw_payload)
    await db_session.commit()
    
    # Verify DB persistence
    from defense.libs.models.posts import Post
    from sqlalchemy import select
    result = await db_session.execute(select(Post).where(Post.id == "test_post_1"))
    saved_post = result.scalar_one_or_none()
    
    assert saved_post is not None
    assert saved_post.platform == "twitter"
    assert saved_post.campaign_id == "test_campaign"
    
    # 2. Emit Domain Event
    from defense.contracts.events import emit_pipeline_event
    
    event = PostIngested(
        post_id="test_post_1",
        job_id="job_123",
        hash="testhash123",
        platform="twitter"
    )
    await emit_pipeline_event(redis_client, event)
    
    # Verify Stream persistence
    stream_messages = await redis_client.xrange("pipeline:events", min="-", max="+")
    assert len(stream_messages) == 1
    
    msg_id, fields = stream_messages[0]
    event_json = fields[b"event"].decode()
    
    import json
    event_data = json.loads(event_json)
    
    assert event_data["type"] == "PostIngested"
    assert event_data["post_id"] == "test_post_1"
    assert event_data["job_id"] == "job_123"
