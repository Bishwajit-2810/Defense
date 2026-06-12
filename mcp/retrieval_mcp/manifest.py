"""Tool manifest for retrieval-mcp — OpenAI function-call format.

Exposed to the agent orchestrator via GET /mcp/manifest.
"""

from __future__ import annotations

TOOL_MANIFEST: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "semantic_search",
            "description": (
                "Vector-similarity search over post embeddings (Postgres/pgvector). "
                "Returns the top-N posts most relevant to the query, optionally "
                "filtered by campaign and/or sentiment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search query to embed and search.",
                    },
                    "campaign_id": {
                        "type": "string",
                        "description": "Optional campaign identifier to restrict results.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default 10, max 50).",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 50,
                    },
                    "sentiment_filter": {
                        "type": "string",
                        "description": (
                            "Only return posts whose overall_sentiment matches this value. "
                            "Omit or pass null to return all sentiments."
                        ),
                        "enum": ["positive", "negative", "neutral", "mixed"],
                        "nullable": True,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_post",
            "description": (
                "Fetch the full analysis result for a single post from Postgres "
                "(analysis_results table)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "post_id": {
                        "type": "string",
                        "description": "Unique identifier of the post.",
                    },
                },
                "required": ["post_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_thread",
            "description": (
                "Fetch a post together with all of its stored comments from Postgres. "
                "Useful for thread-level analysis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "post_id": {
                        "type": "string",
                        "description": "Unique identifier of the post.",
                    },
                    "include_comments": {
                        "type": "boolean",
                        "description": (
                            "When true (default) attach the full comments list to the "
                            "response. Set false to return only the post header."
                        ),
                        "default": True,
                    },
                },
                "required": ["post_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "representative_comments",
            "description": (
                "Return the top representative comments for a post, ranked by likes "
                "and optionally filtered by sentiment. Comments are drawn from the "
                "comment_analysis.representative_comments field stored in the analysis "
                "result JSON."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "post_id": {
                        "type": "string",
                        "description": "Unique identifier of the post.",
                    },
                    "sentiment": {
                        "type": "string",
                        "description": (
                            "Filter comments by sentiment bucket. "
                            "'all' (default) returns comments of any sentiment."
                        ),
                        "enum": ["positive", "negative", "neutral", "all"],
                        "default": "all",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of comments to return (default 5).",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 50,
                    },
                },
                "required": ["post_id"],
            },
        },
    },
]
