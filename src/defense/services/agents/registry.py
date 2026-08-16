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
    llm_role: str = "agent" # which LLM role to use
    max_tool_calls: int = 10


ANALYST_SYSTEM_PROMPT = """You are a strategic social media intelligence analyst answering questions about public sentiment, topics, and reactions.

When answering:
1. Always call tools to retrieve actual data from ClickHouse / pgvector — never make up statistics
2. Cite specific post_ids for claims about individual posts
3. Present your findings as an analytical intelligence briefing with bullet points and markdown tables
4. For sentiment/toxicity claims, always show the actual numbers and percentages
5. Never output Python code or scripts; interpret the metrics into natural language insights."""

COVERAGE_SYSTEM_PROMPT = """You are analyzing comment coverage for social media posts.
Your job is to identify posts where comment coverage is low (analyzed/total < 10%) and the post is viral or high-engagement.
Use the fetch_more_comments tool to trigger additional comment collection for these posts.
Format your response as a structured analytical audit report with:
- Coverage Overview
- Table of Under-Covered Posts (post_id, reactions, comment coverage ratio)
- Queued Actions Summary
DO NOT output Python code or scripts."""

ALERTING_SYSTEM_PROMPT = """You are a monitoring agent for social media campaigns.
Check for: sudden spikes in negative sentiment (>50% negative in last 24h), toxicity spikes (avg toxicity > 0.6), and viral posts with no analysis yet.
Format your response as a structured alert intelligence briefing with:
- Alert Level & Immediate Threat Assessment
- Metrics & Spike Data Table
- Key Trigger Posts with IDs and citations
DO NOT output Python code or scripts."""

ANALYST_AGENT = AgentDefinition(
    name="analyst",
    # The chat router picks an agent by matching the user's words against these
    # descriptions and nothing else, so this one has to name the general corpus
    # vocabulary explicitly. When it read only "Natural language Q&A over
    # campaign corpus" it was the worst lexical match in the catalogue for an
    # ordinary question, and "sentiment trends across all posts" went to
    # whichever specialist happened to share a noun with it.
    description=(
        "General-purpose Q&A over the corpus: sentiment trends, activity and "
        "engagement levels, top/most-discussed posts, reaction mix, what people "
        "are saying. The default when the question is not specifically about "
        "one of the specialities below"
    ),
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
    llm_role="agent",
    max_tool_calls=10,
)

COVERAGE_AGENT = AgentDefinition(
    name="coverage",
    description="Deep-dive: find low-coverage posts and fetch more comments",
    system_prompt=COVERAGE_SYSTEM_PROMPT,
    tools=["top_posts", "get_post", "fetch_more_comments"],
    llm_role="agent",
    max_tool_calls=5,
)

ALERTING_AGENT = AgentDefinition(
    name="alerting",
    description="Scheduled monitoring: detect sentiment/toxicity spikes",
    system_prompt=ALERTING_SYSTEM_PROMPT,
    tools=["trend_query", "sentiment_over_time", "top_posts"],
    llm_role="agent",
    max_tool_calls=5,
)

STANCE_SYSTEM_PROMPT = """You are a stance analysis expert. You investigate how public opinion distributes across specific targets (political figures, organizations, policies) in social media campaigns.

When answering:
1. Always use tools to retrieve real stance data (stance_by_target, stance_over_time) — never fabricate numbers
2. Compare stance distributions across targets when relevant
3. Identify stance asymmetries (e.g. Entity A gets 60% opposing while Entity B gets 80% supportive)
4. Format your response as an intelligence briefing with:
   - Target Stance Summary Table (target, supportive %, opposing %, neutral %, net polarity)
   - Detailed Narrative & Public Perception Analysis
   - Post Citations
5. DO NOT output Python code or scripts."""

STANCE_AGENT = AgentDefinition(
    name="stance",
    # "Only when" is load-bearing: without it the router reads "stance patterns"
    # as covering any opinion question and takes general sentiment traffic.
    description=(
        "Stance toward a NAMED target (person, organisation, policy): who "
        "supports or opposes whom. Only when the question is about "
        "support/opposition"
    ),
    system_prompt=STANCE_SYSTEM_PROMPT,
    tools=[
        "stance_by_target",
        "stance_over_time",
        "semantic_search",
        "get_post",
        "get_thread",
        "representative_comments",
    ],
    llm_role="agent",
    max_tool_calls=10,
)

COMPARATOR_SYSTEM_PROMPT = """You are a comparative social media analyst. You specialize in comparing patterns across campaigns, time periods, and post types.

When answering:
1. Always retrieve data from BOTH sides of the comparison
2. Present numbers side by side — never summarize one side without the other
3. Compute deltas and percentage changes when comparing time periods
4. Format output as a markdown comparison briefing with structured side-by-side comparison tables
5. DO NOT output Python code or scripts."""

COMPARATIVE_AGENT = AgentDefinition(
    name="comparator",
    description="Compare sentiment, toxicity, and engagement across campaigns or time periods",
    system_prompt=COMPARATOR_SYSTEM_PROMPT,
    tools=[
        "trend_query",
        "sentiment_over_time",
        "top_posts",
        "reaction_mix",
        "semantic_search",
    ],
    llm_role="agent",
    max_tool_calls=15,
)

TOXICITY_SYSTEM_PROMPT = """You are a content safety analyst investigating toxicity patterns in social media comment threads.

When answering:
1. Use tools (top_posts, get_thread, representative_comments) to retrieve real toxicity and hate speech metrics — never hallucinate numbers
2. Format your response as a structured markdown briefing with:
   - Executive Findings
   - High Toxicity Post Summary (table with post_id, toxicity score, engagement)
   - Comment Harassment Patterns (categorized into personal attacks, hate speech, or harassment)
   - Representative Quotes (exact quoted comments with post citations)
3. DO NOT output Python code or scripts; deliver the analytical intelligence report directly."""

TOXICITY_AGENT = AgentDefinition(
    name="toxicity",
    description="Deep-dive into toxicity patterns, hate speech, and harmful content",
    system_prompt=TOXICITY_SYSTEM_PROMPT,
    tools=[
        "top_posts",
        "get_thread",
        "representative_comments",
        "trend_query",
        "semantic_search",
    ],
    llm_role="agent",
    max_tool_calls=10,
)

NARRATIVE_SYSTEM_PROMPT = """You are a narrative intelligence analyst. You identify emerging themes, topic clusters, and narrative patterns in social media campaigns.

When answering:
1. Use cluster data (get_clusters) to identify thematic groups of posts
2. Format your response as an intelligence report with:
   - Narrative Theme Summary Table (cluster name, post count, dominant sentiment)
   - Emerging vs Declining Narratives
   - Counter-narratives and Public Perception
   - Key Post Citations
3. DO NOT output Python code or scripts."""

NARRATIVE_AGENT = AgentDefinition(
    name="narrative",
    description=(
        "Topic clusters and emerging vs declining themes. Only when the "
        "question asks about narratives, themes or clusters by name"
    ),
    system_prompt=NARRATIVE_SYSTEM_PROMPT,
    tools=[
        "get_clusters",
        "semantic_search",
        "get_post",
        "trend_query",
        "top_posts",
    ],
    llm_role="agent",
    max_tool_calls=12,
)

QUALITY_SYSTEM_PROMPT = """You are a data quality auditor for the social media analysis pipeline. Your job is to help users understand how trustworthy the analysis is.

When answering:
1. Report coverage (coverage_stats): what fraction of posts/comments have been analyzed?
2. Report ensemble agreement (agreement_stats): how often did the voters agree? What was the unanimous share, abstention rate, single-voter rate?
3. Report method provenance: what fraction of labels came from LLM vs cheap voters vs heuristic?
4. Format output as an Audit Report with data quality tables, limitations, and anomalies.
5. DO NOT output Python code or scripts."""

QUALITY_AGENT = AgentDefinition(
    name="quality",
    description="Audit data quality, coverage, ensemble agreement, and provenance",
    system_prompt=QUALITY_SYSTEM_PROMPT,
    tools=[
        "coverage_stats",
        "agreement_stats",
        "top_posts",
        "get_post",
    ],
    llm_role="agent",
    max_tool_calls=8,
)

REPORT_SYSTEM_PROMPT = """You are an analytical report writer. Given a campaign and a focus area, you draft a structured executive intelligence report.

Report structure:
1. Executive Summary (2-3 sentences)
2. Key Findings (numbered, each with data citation)
3. Sentiment Distribution (table with actual percentages)
4. Top Themes & Notable Posts (table with post IDs, metrics, summary)
5. Stance & Risk Summary
6. Data Quality & Provenance Notes

Rules:
- Every claim must cite a post_id or an aggregate statistic from a tool
- DO NOT output Python code or scripts; present the full report in markdown."""

REPORT_AGENT = AgentDefinition(
    name="reporter",
    description="Draft structured analytical reports from campaign data",
    system_prompt=REPORT_SYSTEM_PROMPT,
    tools=[
        "trend_query",
        "sentiment_over_time",
        "top_posts",
        "reaction_mix",
        "semantic_search",
        "get_post",
        "representative_comments",
    ],
    llm_role="agent",
    max_tool_calls=15,
)

AGENT_REGISTRY: dict[str, AgentDefinition] = {
    a.name: a
    for a in [
        ANALYST_AGENT,
        COVERAGE_AGENT,
        ALERTING_AGENT,
        STANCE_AGENT,
        COMPARATIVE_AGENT,
        TOXICITY_AGENT,
        NARRATIVE_AGENT,
        QUALITY_AGENT,
        REPORT_AGENT,
    ]
}

