from datetime import datetime
from typing import Optional, List, Dict, Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    String,
    Text,
    DateTime,
    Boolean,
    Integer,
    ForeignKey,
    UniqueConstraint,
    func
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, default="default")
    name: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    campaign_id: Mapped[Optional[str]] = mapped_column(String)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, default="default")
    platform: Mapped[Optional[str]] = mapped_column(String)
    platform_post_id: Mapped[Optional[str]] = mapped_column(String)
    url: Mapped[Optional[str]] = mapped_column(Text)
    media_type: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    scraped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    raw_payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    content_hash: Mapped[Optional[str]] = mapped_column(String, unique=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    inserted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    comments: Mapped[List["Comment"]] = relationship("Comment", back_populates="post", cascade="all, delete-orphan")
    analysis: Mapped["AnalysisResult"] = relationship("AnalysisResult", back_populates="post", uselist=False, cascade="all, delete-orphan")


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    comment_id: Mapped[str] = mapped_column(String, nullable=False)
    post_id: Mapped[str] = mapped_column(String, ForeignKey("posts.id"))
    text: Mapped[Optional[str]] = mapped_column(Text)
    author: Mapped[Optional[str]] = mapped_column(String)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    sentiment: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("post_id", "comment_id", name="comments_post_id_comment_id_key"),
    )

    post: Mapped["Post"] = relationship("Post", back_populates="comments")


class AnalysisResult(Base):
    __tablename__ = "analysis_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[str] = mapped_column(String, ForeignKey("posts.id"), unique=True)
    campaign_id: Mapped[Optional[str]] = mapped_column(String)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, default="default")
    result: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False)
    embedding: Mapped[Any] = mapped_column(Vector(768))
    embedding_is_stub: Mapped[bool] = mapped_column(Boolean, default=False)
    # Which vector space this row lives in. Without these, changing
    # EMBEDDING_MODEL leaves old rows silently incomparable to new ones.
    embedding_model: Mapped[Optional[str]] = mapped_column(String)
    embedding_dim: Mapped[Optional[int]] = mapped_column(Integer)
    schema_version: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    post: Mapped["Post"] = relationship("Post", back_populates="analysis")


class PostChunk(Base):
    """One retrievable piece of a post's caption, with its own vector.

    Short posts get exactly one chunk holding the whole caption, so this table
    is a superset of the post-level vector rather than an alternative to it.
    """

    __tablename__ = "post_chunks"

    post_id: Mapped[str] = mapped_column(String, ForeignKey("posts.id"), primary_key=True)
    chunk_idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[Optional[str]] = mapped_column(String)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, default="default")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[Optional[int]] = mapped_column(Integer)
    embedding: Mapped[Any] = mapped_column(Vector(768))
    embedding_is_stub: Mapped[bool] = mapped_column(Boolean, default=False)
    embedding_model: Mapped[Optional[str]] = mapped_column(String)
    embedding_dim: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CommentEmbedding(Base):
    """One vector per comment — the retrievable form of the actual discourse.

    Keyed ``(post_id, comment_id)`` to match the ``comments`` unique constraint,
    so a re-analysis of a post replaces its vectors rather than duplicating
    them. ``represented_by`` is set on near-duplicates, which share the
    representative's vector instead of paying for their own.
    """

    __tablename__ = "comment_embeddings"

    post_id: Mapped[str] = mapped_column(String, ForeignKey("posts.id"), primary_key=True)
    comment_id: Mapped[str] = mapped_column(String, primary_key=True)
    campaign_id: Mapped[Optional[str]] = mapped_column(String)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, default="default")
    embedding: Mapped[Any] = mapped_column(Vector(768))
    embedding_is_stub: Mapped[bool] = mapped_column(Boolean, default=False)
    embedding_model: Mapped[Optional[str]] = mapped_column(String)
    embedding_dim: Mapped[Optional[int]] = mapped_column(Integer)
    represented_by: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


