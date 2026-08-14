from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from defense.libs.models.posts import Post, Comment, AnalysisResult

log = structlog.get_logger(__name__)

class PostRepository:
    """Repository for managing Posts, Comments, and AnalysisResults."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def upsert_post(self, normalized: Dict[str, Any], raw: Dict[str, Any], tenant_id: str = "default") -> str:
        """Upsert a post and its comments from the normalized payload."""
        post_id = normalized.get("post_id")
        if not post_id:
            raise ValueError("Post missing 'id'")

        tid = tenant_id or normalized.get("tenant_id") or raw.get("tenantId") or "default"

        # Prepare Post dict
        post_dict = {
            "id": post_id,
            "campaign_id": raw.get("campaignId"),
            "tenant_id": tid,
            "platform": normalized.get("platform"),
            "platform_post_id": normalized.get("platform_post_id"),
            "url": raw.get("url"),
            "media_type": normalized.get("media_type"),
            "raw_payload": raw,
            "content_hash": normalized.get("content_hash"),
            "status": "pending",
        }

        if raw.get("createdAt"):
            try:
                post_dict["created_at"] = datetime.fromisoformat(
                    raw["createdAt"].replace("Z", "+00:00")
                )
            except ValueError:
                pass

        if raw.get("scrapedAt"):
            try:
                post_dict["scraped_at"] = datetime.fromisoformat(
                    raw["scrapedAt"].replace("Z", "+00:00")
                )
            except ValueError:
                pass

        # PostgreSQL ON CONFLICT DO UPDATE
        stmt = insert(Post).values(post_dict)
        update_dict = {
            c.name: c
            for c in stmt.excluded
            if c.name not in ["id", "inserted_at"]
        }
        stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_dict)
        
        await self._session.execute(stmt)

        # Upsert comments if any
        comments = raw.get("comments") or []
        if comments:
            comment_dicts = []
            for c in comments:
                cid = c.get("id")
                if not cid:
                    continue
                cd = {
                    "post_id": post_id,
                    "comment_id": cid,
                    "text": c.get("text"),
                    "author": c.get("author", {}).get("username") if isinstance(c.get("author"), dict) else c.get("author"),
                    "likes": c.get("likes", 0),
                }
                if c.get("createdAt"):
                    try:
                        cd["created_at"] = datetime.fromisoformat(
                            c["createdAt"].replace("Z", "+00:00")
                        )
                    except ValueError:
                        pass
                comment_dicts.append(cd)

            if comment_dicts:
                c_stmt = insert(Comment).values(comment_dicts)
                c_update = {
                    col.name: col
                    for col in c_stmt.excluded
                    if col.name not in ["id", "post_id", "comment_id"]
                }
                c_stmt = c_stmt.on_conflict_do_update(
                    constraint="comments_post_id_comment_id_key",
                    set_=c_update
                )
                await self._session.execute(c_stmt)

        await self._session.commit()
        return post_id

    async def get_by_id(self, post_id: str) -> Optional[Post]:
        """Fetch a Post by ID."""
        result = await self._session.execute(select(Post).where(Post.id == post_id))
        return result.scalar_one_or_none()

    async def get_by_content_hash(self, content_hash: str) -> Optional[Post]:
        """Fetch a Post by content_hash."""
        result = await self._session.execute(select(Post).where(Post.content_hash == content_hash))
        return result.scalar_one_or_none()

    async def upsert_analysis(
        self, 
        post_id: str, 
        campaign_id: Optional[str], 
        result_json: Dict[str, Any], 
        embedding: List[float], 
        embedding_is_stub: bool, 
        schema_version: str,
        tenant_id: str = "default"
    ):
        """Upsert an AnalysisResult."""
        analysis_dict = {
            "post_id": post_id,
            "campaign_id": campaign_id,
            "tenant_id": tenant_id,
            "result": result_json,
            "embedding": embedding,
            "embedding_is_stub": embedding_is_stub,
            "schema_version": schema_version,
            "updated_at": datetime.now(timezone.utc)
        }

        stmt = insert(AnalysisResult).values(analysis_dict)
        update_dict = {
            c.name: c
            for c in stmt.excluded
            if c.name not in ["id", "post_id", "created_at"]
        }
        stmt = stmt.on_conflict_do_update(index_elements=["post_id"], set_=update_dict)
        
        await self._session.execute(stmt)

        # Mark post as done
        post_update = (
            update(Post)
            .where(Post.id == post_id)
            .values(status="done")
        )
        await self._session.execute(post_update)

        await self._session.commit()

