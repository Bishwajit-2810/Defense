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

class Post(Base):
    __tablename__ = "posts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    campaign_id: Mapped[Optional[str]] = mapped_column(String)
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
    result: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False)
    embedding: Mapped[Any] = mapped_column(Vector(768))
    embedding_is_stub: Mapped[bool] = mapped_column(Boolean, default=False)
    schema_version: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    post: Mapped["Post"] = relationship("Post", back_populates="analysis")

