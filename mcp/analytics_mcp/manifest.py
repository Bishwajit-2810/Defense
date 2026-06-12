"""
OpenAI-compatible tool manifest for analytics-mcp.
All tool definitions are consumed by GET /mcp/manifest and
forwarded to agent orchestrators that speak the OpenAI tool-call protocol.
"""

TOOL_MANIFEST = [
    {
        "type": "function",
        "function": {
            "name": "trend_query",
            "description": (
                "Query analytics time-series for a campaign. "
                "Returns period buckets with post count, avg sentiment, avg toxicity."
            ),
            "parameters": {
                "type": "object",
                "required": ["campaign_id", "from_date", "to_date"],
                "properties": {
                    "campaign_id": {
                        "type": "string",
                        "description": "UUID of the campaign to query.",
                    },
                    "metric": {
                        "type": "string",
                        "enum": ["post_count", "avg_sentiment", "avg_toxicity"],
                        "default": "post_count",
                        "description": "Primary metric to surface (all three are always returned).",
                    },
                    "from_date": {
                        "type": "string",
                        "format": "date",
                        "description": "Inclusive start date (YYYY-MM-DD).",
                    },
                    "to_date": {
                        "type": "string",
                        "format": "date",
                        "description": "Inclusive end date (YYYY-MM-DD).",
                    },
                    "granularity": {
                        "type": "string",
                        "enum": ["hour", "day", "week"],
                        "default": "day",
                        "description": "Time bucket size.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sentiment_over_time",
            "description": (
                "Returns sentiment breakdown (positive/negative/neutral/mixed counts) "
                "per time period for a campaign."
            ),
            "parameters": {
                "type": "object",
                "required": ["campaign_id", "from_date", "to_date"],
                "properties": {
                    "campaign_id": {
                        "type": "string",
                        "description": "UUID of the campaign to query.",
                    },
                    "from_date": {
                        "type": "string",
                        "format": "date",
                        "description": "Inclusive start date (YYYY-MM-DD).",
                    },
                    "to_date": {
                        "type": "string",
                        "format": "date",
                        "description": "Inclusive end date (YYYY-MM-DD).",
                    },
                    "granularity": {
                        "type": "string",
                        "enum": ["hour", "day", "week"],
                        "default": "day",
                        "description": "Time bucket size.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "top_posts",
            "description": "Returns top posts for a campaign ranked by a metric.",
            "parameters": {
                "type": "object",
                "required": ["campaign_id"],
                "properties": {
                    "campaign_id": {
                        "type": "string",
                        "description": "UUID of the campaign to query.",
                    },
                    "metric": {
                        "type": "string",
                        "enum": [
                            "total_reactions",
                            "comment_count",
                            "toxicity_score",
                            "hate_speech_score",
                        ],
                        "default": "total_reactions",
                        "description": "Column to rank posts by (descending).",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "maximum": 100,
                        "description": "Number of posts to return.",
                    },
                    "from_date": {
                        "type": "string",
                        "format": "date",
                        "description": "Optional inclusive start date filter (YYYY-MM-DD).",
                    },
                    "to_date": {
                        "type": "string",
                        "format": "date",
                        "description": "Optional inclusive end date filter (YYYY-MM-DD).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reaction_mix",
            "description": (
                "Returns aggregated reaction breakdown percentages "
                "(LIKE/LOVE/HAHA/WOW/SAD/ANGRY/CARE) for a campaign."
            ),
            "parameters": {
                "type": "object",
                "required": ["campaign_id"],
                "properties": {
                    "campaign_id": {
                        "type": "string",
                        "description": "UUID of the campaign to query.",
                    },
                    "from_date": {
                        "type": "string",
                        "format": "date",
                        "description": "Optional inclusive start date filter (YYYY-MM-DD).",
                    },
                    "to_date": {
                        "type": "string",
                        "format": "date",
                        "description": "Optional inclusive end date filter (YYYY-MM-DD).",
                    },
                },
            },
        },
    },
]
