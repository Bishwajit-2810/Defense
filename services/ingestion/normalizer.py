"""
Ingestion normalizer — transforms a raw upstream PostWithDetails payload into
the canonical normalized dict that is stored in Postgres and forwarded to the
NLP pipeline.

Golden rules enforced here:
  1. Platform is always derived from the URL (never hardcoded).
  2. baseline_sentiment and baseline_viral_potential are kept exactly as
     received from upstream — never overwritten.
  3. comment_sentiment is intentionally null; NLP stage will compute it.
  4. No write-back to the upstream system.
  5. Content-hash dedup key is computed here and used by the service layer.
"""

from __future__ import annotations

import sys
import os

# Ensure libs directory is importable when this module is loaded directly.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from libs.common import (
    content_hash,
    normalize_text,
    platform_from_url,
    compute_coverage,
)
from libs.common.utils import reaction_breakdown_to_dict
from libs.schemas import assert_valid_input


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize_engagement(raw_eng: dict) -> dict:
    """
    Translate upstream camelCase engagement keys to snake_case.

    Upstream keys:
        commentCount, storedCommentRows, totalReactions, shareCount,
        reach, saves, impressions, storedReactionRows

    Normalized keys follow the output schema:
        comment_count, stored_comments, total_reactions, share_count,
        reach, saves, impressions, stored_reaction_rows
    """
    if not raw_eng:
        return {}
    return {
        "comment_count":       raw_eng.get("commentCount", 0),
        "stored_comments":     raw_eng.get("storedCommentRows", 0),
        "total_reactions":     raw_eng.get("totalReactions", 0),
        "share_count":         raw_eng.get("shareCount", 0),
        "reach":               raw_eng.get("reach", 0),
        "saves":               raw_eng.get("saves", 0),
        "impressions":         raw_eng.get("impressions", 0),
        "stored_reaction_rows": raw_eng.get("storedReactionRows", 0),
    }


def _normalize_comment(raw_comment: dict) -> dict:
    """
    Normalize a single comment object from upstream.

    Fields mapped:
        id                  → id
        text                → text  (normalize_text)
        likes               → likes
        category            → category
        parentId            → parent_id
        postedAt            → posted_at
        authorUrl           → author_url
        authorUsername      → author_username
        platformCommentId   → platform_comment_id
        replyCount          → reply_count
        sentiment           → sentiment  (kept null — rule #3; upstream sends
                                          null anyway, NLP will fill it later)
    """
    return {
        "id":                   raw_comment.get("id"),
        "text":                 normalize_text(raw_comment.get("text") or ""),
        "likes":                raw_comment.get("likes", 0),
        "category":             raw_comment.get("category"),
        "parent_id":            raw_comment.get("parentId"),
        "posted_at":            raw_comment.get("postedAt"),
        "author_url":           raw_comment.get("authorUrl"),
        "author_username":      raw_comment.get("authorUsername"),
        "platform_comment_id":  raw_comment.get("platformCommentId"),
        "reply_count":          raw_comment.get("replyCount", 0),
        # Rule #3: comment sentiment is always null at ingestion time.
        # The NLP stage will populate it later.
        "sentiment":            None,
    }


def _normalize_share(raw_share: dict) -> dict:
    """
    Normalize a single sampleShares entry.

    Upstream camelCase → snake_case.
    """
    return {
        "sharer_url":      raw_share.get("sharerUrl"),
        "sharer_username": raw_share.get("sharerUsername"),
        "shared_at":       raw_share.get("sharedAt"),
        "share_text":      normalize_text(raw_share.get("shareText") or ""),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_post(raw: dict) -> dict:
    """
    Validate and normalize a raw upstream PostWithDetails payload.

    Args:
        raw: The raw dict decoded from the upstream JSON payload.

    Returns:
        A normalized dict ready to be persisted and forwarded downstream.

    Raises:
        ValueError: If the payload fails input schema validation.
    """
    # Step 1 — validate against the input schema (raises ValueError on failure).
    assert_valid_input(raw)

    # Step 2 — derive platform from the URL (golden rule #1).
    url: str = raw["url"]
    platform: str = platform_from_url(url)

    # Step 3 — normalize engagement (camelCase → snake_case).
    raw_eng: dict = raw.get("engagement") or {}
    normalized_engagement = _normalize_engagement(raw_eng)

    # Step 4 — coverage fields.
    # stored_comments is how many comment rows we actually have;
    # commentCount is the total reported by the platform.
    stored_comment_rows: int = raw_eng.get("storedCommentRows", 0)
    comment_count: int = raw_eng.get("commentCount", 0)
    coverage: float = compute_coverage(stored_comment_rows, comment_count)

    # Attach coverage metadata directly onto the engagement dict so
    # downstream consumers can find them in one place.
    normalized_engagement["stored_comments"] = stored_comment_rows
    normalized_engagement["coverage"] = coverage

    # Step 5 — normalize comments (null sentinel is preserved per rule #3).
    raw_comments: list = raw.get("comments") or []
    normalized_comments = [_normalize_comment(c) for c in raw_comments]

    # Step 6 — normalize sampleShares.
    raw_shares: list = raw.get("sampleShares") or []
    normalized_shares = [_normalize_share(s) for s in raw_shares]

    # Step 7 — normalize reactionBreakdown keys to lowercase.
    reaction_breakdown = reaction_breakdown_to_dict(
        raw.get("reactionBreakdown") or {}
    )

    # Step 8 — compute content hash from the *raw* payload (uses id + caption
    # + comment texts, matching the libs/common definition exactly).
    post_content_hash: str = content_hash(raw)

    # Step 9 — assemble the normalized document.
    normalized: dict = {
        # Identity
        "post_id":           raw["id"],
        "campaign_id":       raw["campaignId"],
        "platform_post_id":  raw["platformPostId"],
        "url":               url,
        "platform":          platform,   # always derived, never hardcoded

        # Content
        "caption":    normalize_text(raw.get("caption") or ""),
        "media_type": raw["postType"],   # enum value kept verbatim

        # Timestamps (kept as ISO strings; DB layer converts to TIMESTAMP)
        "created_at":  raw["postedAt"],
        "scraped_at":  raw["scrapedAt"],

        # Upstream model scores — kept exactly as received (rules #2).
        # These are the scores computed by the upstream system; we do not
        # modify, recalculate, or override them.
        "baseline_sentiment":        raw.get("sentiment"),
        "baseline_viral_potential":  raw.get("viralPotential"),

        # Engagement (snake_case, with coverage appended)
        "engagement": normalized_engagement,

        # Reactions
        "reaction_breakdown": reaction_breakdown,

        # Shares
        "shares": normalized_shares,

        # Comments — sentiment is null at this stage (rule #3)
        "comments": normalized_comments,

        # Dedup / integrity
        "content_hash": post_content_hash,

        # Supplementary flags forwarded as-is (informational, read-only)
        "is_viral":           raw.get("isViral", False),
        "is_viral_candidate": raw.get("isViralCandidate", False),

        # Optional media URLs
        "video_url":  raw.get("videoUrl"),
        "photo_urls": raw.get("photoUrls") or [],
    }

    return normalized
