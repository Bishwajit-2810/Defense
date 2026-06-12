"""
Agent definitions registry.

Each AgentDefinition describes an agent's persona, which MCP tools it may call,
which LLM role to use, and the per-run budget cap (max_tool_calls).
"""

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class AgentDefinition:
    name: str
    description: str
    system_prompt: str
    tools: list[str]        # tool names from MCP manifest
    llm_role: str = "llm_b" # which LLM role to use
    max_tool_calls: int = 10


ANALYST_SYSTEM_PROMPT = """You are a social media analysis expert. You have access to analytics \
and retrieval tools to answer questions about social media campaigns.

When answering:
1. Always call tools to retrieve actual data — never make up statistics
2. Cite specific post_ids for claims about individual posts
3. If data is insufficient, say so explicitly
4. Write in clear, concise English unless asked otherwise
5. For sentiment/toxicity claims, always show the actual numbers

You are analyzing a corpus, not individual posts."""

COVERAGE_SYSTEM_PROMPT = """You are analyzing comment coverage for social media posts.
Your job is to identify posts where comment coverage is low (analyzed/total < 10%) and \
the post is viral or high-engagement. Use the fetch_more_comments tool to trigger \
additional comment collection for these posts."""

ALERTING_SYSTEM_PROMPT = """You are a monitoring agent for social media campaigns.
Check for: sudden spikes in negative sentiment (>50% negative in last 24h), \
toxicity spikes (avg toxicity > 0.6), viral posts with no analysis yet.
Report findings concisely with data citations."""

ANALYST_AGENT = AgentDefinition(
    name="analyst",
    description="Natural language Q&A over campaign corpus",
    system_prompt=ANALYST_SYSTEM_PROMPT,
    tools=[
        "trend_query",
        "sentiment_over_time",
        "top_posts",
        "reaction_mix",
        "semantic_search",
        "get_post",
        "get_thread",
        "representative_comments",
    ],
    llm_role="llm_b",
    max_tool_calls=10,
)

COVERAGE_AGENT = AgentDefinition(
    name="coverage",
    description="Deep-dive: find low-coverage posts and fetch more comments",
    system_prompt=COVERAGE_SYSTEM_PROMPT,
    tools=["top_posts", "get_post", "fetch_more_comments"],
    llm_role="llm_a",
    max_tool_calls=5,
)

ALERTING_AGENT = AgentDefinition(
    name="alerting",
    description="Scheduled monitoring: detect sentiment/toxicity spikes",
    system_prompt=ALERTING_SYSTEM_PROMPT,
    tools=["trend_query", "sentiment_over_time", "top_posts"],
    llm_role="llm_b",
    max_tool_calls=5,
)

AGENT_REGISTRY: dict[str, AgentDefinition] = {
    a.name: a for a in [ANALYST_AGENT, COVERAGE_AGENT, ALERTING_AGENT]
}
