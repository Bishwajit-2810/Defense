"""Tool calls the model writes as text, and the campaign scope that provokes them.

A real Analyst run against llama3.1:8b, scoped to "all campaigns", produced this
as its briefing:

    It appears that the provided campaign ID is not valid. Let's try again with
    a different approach. ...

    {"name": "post_reaction_breakdown", "parameters": {"from_date":"YYYY-MM-DD",
     "to_date":"YYYY-MM-DD"}}

Three separate defects, all visible in that one run:

1. **Unscoped runs had no campaign value.** Tool schemas describe `campaign_id`
   as "CUID/UUID of the campaign to query" and it is required. With no campaign
   selected, the system prompt said nothing at all, so the model filled the
   argument with a placeholder, `_clean_campaign_id` rejected it as junk (which
   is correct — silently widening a scoped query answers a different question),
   and the run dead-ended. Both MCP servers accept `"all"`; nothing told the
   model that.

2. **A tool call written as text was accepted as the final answer.** The loop
   treated an empty `tool_calls` as "the agent is done".

3. **The run was still recorded `completed`.** Nothing, anywhere, said it had
   failed.
"""

import json

import pytest

from defense.services.agents.registry import ANALYST_AGENT
from defense.services.agents.runner import (
    _MAX_TEXT_TOOL_RECOVERIES,
    AgentRunner,
    _recover_text_tool_calls,
)

# The payload from the run above, verbatim.
_OBSERVED_ANSWER = """It appears that the provided campaign ID is not valid. Let's try again with a different approach.

To summarize positive vs negative reaction breakdowns across top posts, we can use the following tool call:

{"name": "post_reaction_breakdown", "parameters": {"from_date":"YYYY-MM-DD","to_date":"YYYY-MM-DD"}}
"""


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_the_observed_answer_is_recognised_as_a_tool_call():
    assert _recover_text_tool_calls(_OBSERVED_ANSWER) == [
        ("post_reaction_breakdown", {"from_date": "YYYY-MM-DD", "to_date": "YYYY-MM-DD"}),
    ]


@pytest.mark.parametrize(
    "text",
    [
        '```json\n{"name": "top_posts", "arguments": {"campaign_id": "all"}}\n```',
        '{"type": "function", "function": {"name": "top_posts", "arguments": {"campaign_id": "all"}}}',
        '{"name": "top_posts", "arguments": "{\\"campaign_id\\": \\"all\\"}"}',
        '{"name": "top_posts", "args": {"campaign_id": "all"}}',
    ],
    ids=["fenced", "openai-nested", "string-encoded-args", "args-key"],
)
def test_known_emission_shapes_are_recovered(text):
    assert _recover_text_tool_calls(text) == [("top_posts", {"campaign_id": "all"})]


def test_braces_inside_strings_do_not_break_the_scanner():
    text = '{"name": "semantic_search", "parameters": {"query": "why { and } trend"}}'
    assert _recover_text_tool_calls(text) == [
        ("semantic_search", {"query": "why { and } trend"}),
    ]


# ---------------------------------------------------------------------------
# What must NOT be treated as a call — a briefing is not a tool call
# ---------------------------------------------------------------------------

def test_a_real_briefing_that_quotes_json_is_left_alone():
    """The risk of recovery: re-executing an answer instead of showing it."""
    briefing = (
        "## Reaction breakdown\n\n"
        "Positive reactions dominate the top five posts (62% vs 31% negative), "
        "driven by post cm0abcdef12345678901234 which drew 5,000 reactions in a "
        "single day. The negative share is concentrated in two threads about "
        "the quota ruling, where angry reactions outnumber likes by roughly "
        "three to one.\n\n"
        "| post_id | positive | negative |\n"
        "|---|---|---|\n"
        "| cm0abcdef12345678901234 | 3100 | 1550 |\n\n"
        "This was retrieved with "
        '{"name": "top_posts", "parameters": {"campaign_id": "all"}} and the '
        "figures above are the tool's own totals, not estimates. Coverage across "
        "the window is complete, so no follow-up collection is needed."
    )
    assert _recover_text_tool_calls(briefing) == []


def test_a_name_without_arguments_is_not_a_call():
    """`{"name": ...}` appears in ordinary quoted data far too often."""
    assert _recover_text_tool_calls('The entity was {"name": "post_reaction_breakdown"}.') == []


@pytest.mark.parametrize("text", ["", "No JSON here at all.", "{not json}"])
def test_non_calls_are_ignored(text):
    assert _recover_text_tool_calls(text) == []


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

_CITED_POST = "cm0abcdef12345678901234"


class ScriptedLLM:
    """Returns each scripted turn in order; repeats the last one forever."""

    def __init__(self, *turns: dict):
        self.turns = list(turns)
        self.seen: list[list[dict]] = []

    async def chat(self, role=None, messages=None, tools=None, **kwargs):
        self.seen.append(list(messages))
        turn = self.turns[min(len(self.seen) - 1, len(self.turns) - 1)]
        return {
            "content": turn.get("content", ""),
            "tool_calls": turn.get("tool_calls", []),
            "backend": "local",
            "model": "llama3.1:8b",
            "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
        }

    @property
    def system_prompt(self) -> str:
        return self.seen[0][0]["content"]


class RecordingMCP:
    """Advertises the Analyst's real tools and records what gets dispatched."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def get_all_manifests(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"Mock {name}",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in ANALYST_AGENT.tools
        ]

    def filter_tools(self, manifests, tool_names):
        allowed = set(tool_names)
        return [t for t in manifests if t["function"]["name"] in allowed]

    async def call_tool(self, tool_name: str, arguments: dict):
        self.calls.append((tool_name, arguments))
        return {"post_id": _CITED_POST, "positive": 3100, "negative": 1550}


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_real_tool_written_as_text_is_dispatched():
    """Right intent, wrong channel — execute it rather than printing it."""
    llm = ScriptedLLM(
        {"content": '{"name": "top_posts", "parameters": {"campaign_id": "all", "limit": 5}}'},
        {"content": f"Positive reactions lead on post {_CITED_POST}."},
    )
    mcp = RecordingMCP()

    run = await AgentRunner(llm_client=llm, mcp_client=mcp).run(
        agent_def=ANALYST_AGENT, query="Summarize reactions."
    )

    assert mcp.calls == [("top_posts", {"campaign_id": "all", "limit": 5})]
    assert run.status == "completed"
    assert run.answer == f"Positive reactions lead on post {_CITED_POST}."
    assert _CITED_POST in run.citations
    # The dispatch is recorded like any other, so the trace shows it.
    assert run.tools_used[0]["tool_name"] == "top_posts"


@pytest.mark.asyncio
async def test_a_hallucinated_tool_written_as_text_gets_the_real_tool_list():
    """The observed run: `post_reaction_breakdown` does not exist."""
    # No tool runs in this scenario, so the recovered answer cites nothing —
    # citing an id here would be ungrounded and flagged as such.
    recovered_answer = "No reaction data was retrieved for this window."
    llm = ScriptedLLM({"content": _OBSERVED_ANSWER}, {"content": recovered_answer})
    mcp = RecordingMCP()

    run = await AgentRunner(llm_client=llm, mcp_client=mcp).run(
        agent_def=ANALYST_AGENT, query="Summarize reactions."
    )

    assert mcp.calls == []  # nothing hallucinated is ever dispatched
    correction = llm.seen[-1][-1]
    assert correction["role"] == "user"
    assert "post_reaction_breakdown" in correction["content"]
    for tool in ANALYST_AGENT.tools:
        assert tool in correction["content"]

    assert run.status == "completed"
    assert run.answer == recovered_answer
    assert "post_reaction_breakdown" not in (run.answer or "")


@pytest.mark.asyncio
async def test_an_unrecoverable_tool_call_fails_the_run():
    """A model that only ever emits JSON must not produce a `completed` run."""
    llm = ScriptedLLM({"content": _OBSERVED_ANSWER})

    run = await AgentRunner(llm_client=llm, mcp_client=RecordingMCP()).run(
        agent_def=ANALYST_AGENT, query="Summarize reactions."
    )

    assert run.status == "failed"
    assert "post_reaction_breakdown" in (run.error or "")
    # Recovery is bounded: initial turn + N corrections + one prose synthesis.
    assert len(llm.seen) == 1 + _MAX_TEXT_TOOL_RECOVERIES + 1
    assert run.usage["total_tokens"] > 0  # spend is still accounted for


@pytest.mark.asyncio
async def test_recovery_does_not_hijack_a_genuine_answer():
    briefing = "Positive reactions lead 62% to 31% across the window."
    llm = ScriptedLLM({"content": briefing})
    mcp = RecordingMCP()

    run = await AgentRunner(llm_client=llm, mcp_client=mcp).run(
        agent_def=ANALYST_AGENT, query="Summarize reactions."
    )

    assert len(llm.seen) == 1
    assert mcp.calls == []
    assert run.status == "completed"
    assert run.answer == briefing


# ---------------------------------------------------------------------------
# Campaign scope — the condition that provoked the failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unscoped_runs_tell_the_model_to_pass_all():
    llm = ScriptedLLM({"content": "Done."})

    await AgentRunner(llm_client=llm, mcp_client=RecordingMCP()).run(
        agent_def=ANALYST_AGENT, query="Summarize reactions.", campaign_id=None
    )

    prompt = llm.system_prompt
    assert "ALL CAMPAIGNS" in prompt
    assert '"all"' in prompt
    assert "Never invent an id" in prompt


@pytest.mark.asyncio
async def test_scoped_runs_pin_the_campaign_id():
    llm = ScriptedLLM({"content": "Done."})

    await AgentRunner(llm_client=llm, mcp_client=RecordingMCP()).run(
        agent_def=ANALYST_AGENT, query="Summarize reactions.", campaign_id="cm0camp123456"
    )

    prompt = llm.system_prompt
    assert "campaign_id=cm0camp123456" in prompt
    assert "Pass exactly this value" in prompt
    assert "ALL CAMPAIGNS" not in prompt


@pytest.mark.asyncio
async def test_all_is_accepted_by_the_analytics_server():
    """The value the prompt now instructs the model to send must actually work."""
    from defense.mcp_servers.analytics_mcp import server as analytics
    from defense.mcp_servers.retrieval_mcp import server as retrieval

    for mod in (analytics, retrieval):
        assert mod._clean_campaign_id("all") is None  # None == no campaign filter
        assert mod._clean_campaign_id(json.loads('"all"')) is None
