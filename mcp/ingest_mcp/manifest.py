"""Tool manifest for ingest-mcp — OpenAI function-call format.

IMPORTANT: These tools trigger pulls from the upstream post-with-details API
and write ONLY to our own database.  They NEVER write back to upstream.

Exposed to the agent orchestrator via GET /mcp/manifest.
"""

from __future__ import annotations

TOOL_MANIFEST: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "pull_campaign",
            "description": (
                "Trigger an ingestion pull for a campaign over a date range. "
                "The job is pushed to the ingestion worker queue which fetches "
                "posts from the upstream API and writes them into our own "
                "database only — no data is written back to upstream."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "campaign_id": {
                        "type": "string",
                        "description": "Identifier of the campaign to pull.",
                    },
                    "from_date": {
                        "type": "string",
                        "format": "date-time",
                        "description": (
                            "Start of the date range to pull (ISO-8601, inclusive)."
                        ),
                    },
                    "to_date": {
                        "type": "string",
                        "format": "date-time",
                        "description": (
                            "End of the date range to pull (ISO-8601, inclusive)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Maximum number of posts to pull in this job "
                            "(default 100, max 1000)."
                        ),
                        "default": 100,
                        "minimum": 1,
                        "maximum": 1000,
                    },
                },
                "required": ["campaign_id", "from_date", "to_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_more_comments",
            "description": (
                "Trigger fetching additional comments for a post from the upstream "
                "API to raise comment coverage. Fetched comments are written only "
                "to our own comments table — no write-back to upstream."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "post_id": {
                        "type": "string",
                        "description": "Identifier of the post to fetch more comments for.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Maximum number of additional comments to fetch "
                            "(default 100, max 500)."
                        ),
                        "default": 100,
                        "minimum": 1,
                        "maximum": 500,
                    },
                },
                "required": ["post_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refresh_post",
            "description": (
                "Re-fetch a specific post from the upstream API and re-process it "
                "through the analysis pipeline. Useful when a post's engagement or "
                "reaction data has changed significantly. Writes only to our own "
                "database — no write-back to upstream."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "post_id": {
                        "type": "string",
                        "description": "Identifier of the post to refresh.",
                    },
                },
                "required": ["post_id"],
            },
        },
    },
]
