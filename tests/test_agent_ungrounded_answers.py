"""The three defects behind a fully fabricated briefing.

A live run asked "What are the most active discussions and sentiment trends
across all posts?", made three ``trend_query`` calls, had all three rejected by
pydantic, retrieved zero rows — and returned a formatted intelligence briefing
with invented post IDs (``#12345``), invented topics, invented per-topic
sentiment scores and invented quoted post text, with ``status: completed``.

Three things had to go wrong at once, and each gets its own tests here:

  1. ``metric`` is ``Literal["post_count", "avg_sentiment", "avg_toxicity"]``
     and the model sent a LIST. Nothing coerced it, though the manifest carries
     every tool's JSON Schema. Even the model's self-corrected third attempt —
     a one-element list — was rejected.
  2. A raised tool sets ``result = None``, which ``_describe_empty`` reported to
     the model as "the tool returned nothing for these arguments": the wrong
     diagnosis, and missing the "do NOT invent figures" warning that the
     empty-rows branch right below it carries.
  3. Nothing noticed that every call had failed. The run was finalised as
     ``completed`` and the fabricated briefing shipped.

No Ollama, Redis or MCP sidecar needed.
"""

import json
import sys

sys.path.insert(0, "/home/bk/code/defense/src/defense")

import pytest

from defense.services.agents.mcp_client import MCPClient
from defense.services.agents.registry import AGENT_REGISTRY
from defense.services.agents.runner import (
    AgentRunner,
    _describe_empty,
    _describe_error,
    _strip_tool_data_markup,
    _ungrounded_post_id_args,
    _unverified_citations,
)

# The real schema fastmcp derives from trend_query's signature.
TREND_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "campaign_id": {"type": "string"},
        "granularity": {"enum": ["hour", "day", "week"], "default": "day"},
        "metric": {
            "enum": ["post_count", "avg_sentiment", "avg_toxicity"],
            "default": "post_count",
            "type": "string",
        },
        "limit": {"type": "integer", "default": 100},
    },
}


def _client_with(schema: dict, tool_name: str = "trend_query") -> MCPClient:
    client = MCPClient()
    client._manifest_cache = [
        {"type": "function", "function": {"name": tool_name, "parameters": schema}}
    ]
    return client


# ---------------------------------------------------------------------------
# 1. Argument shape
# ---------------------------------------------------------------------------


def test_one_element_list_is_unwrapped_for_a_single_valued_parameter():
    """The model's third attempt: right value, wrong wrapper."""
    args, problems = _client_with(TREND_QUERY_SCHEMA).coerce_arguments(
        "trend_query", {"metric": ["avg_sentiment"], "granularity": "day"}
    )
    assert problems == []
    assert args["metric"] == "avg_sentiment", "a one-element list is unambiguous; unwrap it"
    assert args["granularity"] == "day", "untouched arguments must survive intact"


def test_multi_value_list_is_reported_not_guessed():
    """The model's first attempt: two metrics, one slot. Do not pick for it."""
    args, problems = _client_with(TREND_QUERY_SCHEMA).coerce_arguments(
        "trend_query", {"metric": ["post_count", "avg_sentiment"]}
    )
    assert len(problems) == 1
    message = problems[0]

    # The message has to say the three things the pydantic error did not.
    assert "single value, not a list" in message
    assert "did NOT run" in message, "the model must know it received no data"
    assert "once per value" in message, "and how to retry"
    assert "'post_count'" in message and "'avg_sentiment'" in message
    assert args["metric"] == ["post_count", "avg_sentiment"], (
        "arguments must not be mutated when the problem is unrepairable"
    )


TOP_POSTS_SCHEMA = {
    "type": "object",
    "properties": {
        "campaign_id": {"type": "string"},
        "metric": {
            "enum": ["total_reactions", "comment_count", "toxicity_score", "hate_speech_score"],
            "default": "total_reactions",
            "type": "string",
        },
        "limit": {"type": "integer"},
    },
}

TOP_POSTS_DESCRIPTION = (
    "Returns the top posts for a campaign ranked by the given metric. "
    "Each row includes: post_id, total_reactions, comment_count, toxicity_score, "
    "hate_speech_score, overall_sentiment."
)


def _top_posts_client(description: str = TOP_POSTS_DESCRIPTION) -> MCPClient:
    client = MCPClient()
    client._manifest_cache = [
        {
            "type": "function",
            "function": {
                "name": "top_posts",
                "description": description,
                "parameters": TOP_POSTS_SCHEMA,
            },
        }
    ]
    return client


def test_a_value_outside_the_enum_is_rejected_before_dispatch():
    """The second live misuse: right shape, value the tool does not offer.

    ``top_posts(metric="avg_sentiment")`` is a well-formed string, so the shape
    repair passed it straight through to pydantic and a wasted round trip.
    """
    args, problems = _top_posts_client().coerce_arguments(
        "top_posts", {"metric": "avg_sentiment", "limit": 50}
    )
    assert len(problems) == 1
    message = problems[0]
    assert "is not a value top_posts accepts" in message
    assert "did NOT run" in message
    assert "'total_reactions'" in message, "list what the tool does accept"
    assert args["limit"] == 50, "other arguments are untouched"


def test_the_rejection_says_the_field_is_already_in_the_rows():
    """Why the bad call was made: the model wanted per-post sentiment.

    ``top_posts`` cannot RANK by sentiment but returns ``overall_sentiment`` on
    every row. Saying so turns a dead end into a retry that works.
    """
    _, problems = _top_posts_client().coerce_arguments("top_posts", {"metric": "avg_sentiment"})
    assert "overall_sentiment" in problems[0]
    assert "read the field from the result" in problems[0]


def test_the_hint_is_omitted_when_the_tool_does_not_document_its_rows():
    """No convention, no guessing at field names."""
    client = _top_posts_client(description="Returns the top posts for a campaign.")
    _, problems = client.coerce_arguments("top_posts", {"metric": "avg_sentiment"})
    assert "'total_reactions'" in problems[0]
    assert "already returns" not in problems[0]


def test_the_hint_is_only_offered_for_ranking_parameters():
    """"Read it from the rows instead of ranking by it" is ranking advice.

    On `granularity` it produced "trend_query already returns period, count,
    avg_sentiment ... instead of ranking by it" — an answer to a question the
    model never asked.
    """
    schema = {
        "type": "object",
        "properties": {"granularity": {"enum": ["hour", "day", "week"], "type": "string"}},
    }
    client = MCPClient()
    client._manifest_cache = [
        {
            "type": "function",
            "function": {
                "name": "trend_query",
                "description": "Each row includes: period, count, avg_sentiment.",
                "parameters": schema,
            },
        }
    ]

    _, problems = client.coerce_arguments("trend_query", {"granularity": "daily"})
    assert "'hour', 'day', 'week'" in problems[0], "still name the valid values"
    assert "ranking by it" not in problems[0]


def test_the_rows_hint_reads_as_one_line():
    """It is parsed out of a wrapped docstring; a stray newline looks corrupt."""
    client = _top_posts_client(
        description="Returns top posts.\n    Each row includes: post_id,\n    overall_sentiment."
    )
    _, problems = client.coerce_arguments("top_posts", {"metric": "avg_sentiment"})
    assert "\n" not in problems[0]
    assert "post_id, overall_sentiment" in problems[0]


def test_enum_case_is_corrected_rather_than_rejected():
    """Case is a spelling difference, not a different request."""
    args, problems = _top_posts_client().coerce_arguments(
        "top_posts", {"metric": "Total_Reactions"}
    )
    assert problems == []
    assert args["metric"] == "total_reactions"


def test_valid_enum_values_pass_through_untouched():
    for value in TOP_POSTS_SCHEMA["properties"]["metric"]["enum"]:
        args, problems = _top_posts_client().coerce_arguments("top_posts", {"metric": value})
        assert problems == []
        assert args["metric"] == value


def test_a_one_element_list_of_a_valid_value_is_unwrapped_not_rejected():
    """Shape repair runs first, so the enum check sees the corrected value."""
    args, problems = _top_posts_client().coerce_arguments(
        "top_posts", {"metric": ["comment_count"]}
    )
    assert problems == []
    assert args["metric"] == "comment_count"


def test_parameters_without_an_enum_are_not_value_checked():
    args, problems = _top_posts_client().coerce_arguments(
        "top_posts", {"campaign_id": "anything-at-all", "limit": 7}
    )
    assert problems == []
    assert args == {"campaign_id": "anything-at-all", "limit": 7}


def test_scalar_is_wrapped_where_a_list_belongs():
    schema = {"type": "object", "properties": {"post_ids": {"type": "array"}}}
    args, problems = _client_with(schema, "get_posts").coerce_arguments(
        "get_posts", {"post_ids": "cm0abcdef12345678901234"}
    )
    assert problems == []
    assert args["post_ids"] == ["cm0abcdef12345678901234"]


def test_unknown_shapes_are_left_alone():
    """Coercing against a schema we did not understand breaks working calls."""
    schema = {
        "type": "object",
        "properties": {
            "filters": {},  # no type, no enum — says nothing
            "untyped": {"description": "free-form"},
        },
    }
    payload = {"filters": ["a", "b"], "untyped": "x", "not_in_schema": ["y"]}
    args, problems = _client_with(schema, "t").coerce_arguments("t", payload)
    assert problems == []
    assert args == payload


def test_coercion_is_inert_without_a_manifest():
    """Discovery may not have run; that must not change how a call is made."""
    client = MCPClient()
    payload = {"metric": ["post_count", "avg_sentiment"]}
    args, problems = client.coerce_arguments("trend_query", payload)
    assert (args, problems) == (payload, [])


def test_nullable_and_anyof_parameters_are_read_correctly():
    schema = {
        "type": "object",
        "properties": {
            "campaign_id": {"type": ["string", "null"]},
            "tags": {"anyOf": [{"type": "array"}, {"type": "null"}]},
        },
    }
    args, problems = _client_with(schema, "t").coerce_arguments(
        "t", {"campaign_id": ["camp-1"], "tags": "politics"}
    )
    assert problems == []
    assert args["campaign_id"] == "camp-1", "['string','null'] is still a scalar slot"
    assert args["tags"] == ["politics"], "anyOf containing an array is an array slot"


# ---------------------------------------------------------------------------
# 1b. Wrapper delimiters leaking back out of the model
# ---------------------------------------------------------------------------
# Every tool result reaches the model inside
# <tool_data source="..." trust="untrusted"> ... </tool_data>. A live toxicity
# run copied the framing into an argument and called representative_comments
# with post_id='<tool_data>post_12345</tool_data>'.


@pytest.mark.parametrize(
    "leaked,expected",
    [
        ("<tool_data>post_12345</tool_data>", "post_12345"),
        ('<tool_data source="analytics-mcp" trust="untrusted">cm0abc</tool_data>', "cm0abc"),
        ("</tool_data>cm0abc", "cm0abc"),
        ("<TOOL_DATA>cm0abc</TOOL_DATA>", "cm0abc"),
        ("  <tool_data>  cm0abc  </tool_data>  ", "cm0abc"),
    ],
    ids=["bare", "attributes", "close-only", "uppercase", "padded"],
)
def test_leaked_delimiters_are_stripped_from_arguments(leaked, expected):
    assert _strip_tool_data_markup(leaked) == expected


def test_stripping_reaches_nested_arguments():
    payload = {"post_ids": ["<tool_data>a</tool_data>", "b"], "filter": {"q": "<tool_data>c</tool_data>"}}
    assert _strip_tool_data_markup(payload) == {"post_ids": ["a", "b"], "filter": {"q": "c"}}


def test_ordinary_values_are_untouched():
    for value in ("cm0abcdef12345678901234", "a < b and c > d", 42, 0.5, None, True, ""):
        assert _strip_tool_data_markup(value) == value


@pytest.mark.asyncio
async def test_a_leaked_delimiter_never_reaches_the_tool():
    """End to end: what the MCP server receives, and what the trace records."""

    class _RecordingMCP(_StubMCP):
        def __init__(self):
            super().__init__()
            self.seen = []

        async def call_tool(self, name, arguments):
            self.seen.append(arguments)
            return [{"post_id": "cm0abcdef12345678901234"}]

    class _LeakingLLM(_StubLLM):
        async def chat(self, *args, **kwargs):
            self.turn += 1
            if self.turn == 1:
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "trend_query",
                                "arguments": json.dumps(
                                    {"campaign_id": "<tool_data>cm0abc</tool_data>"}
                                ),
                            },
                        }
                    ],
                    "usage": {},
                }
            return {"content": "Done.", "tool_calls": [], "usage": {}}

    mcp = _RecordingMCP()
    run = await _run(llm=_LeakingLLM(), mcp=mcp)

    assert mcp.seen == [{"campaign_id": "cm0abc"}]
    assert run.tools_used[0]["arguments"] == {"campaign_id": "cm0abc"}, (
        "the trace must show what was actually sent, not the leaked form"
    )


# ---------------------------------------------------------------------------
# 1c. Post IDs the model invents and passes INTO a tool
# ---------------------------------------------------------------------------
# The input-side twin of an invented citation. Two live toxicity runs opened
# with representative_comments — holding no retrieved data — using
# post_id='post_12345' (invented) and post_id='all' (a wildcard that IS valid
# for the same tool's `sentiment` parameter, but names nothing as a post_id).

REAL_ID = "cm0abcdef12345678901234"


def test_an_invented_post_id_is_rejected():
    problems = _ungrounded_post_id_args({"post_id": "post_12345"}, grounding="find toxic comments")
    assert len(problems) == 1
    assert "not a post ID that any tool in this run returned" in problems[0]
    assert "call top_posts" in problems[0], "say how to get a real one"


def test_the_all_wildcard_is_rejected_as_a_post_id():
    problems = _ungrounded_post_id_args({"post_id": "all"}, grounding="are toxic comments concentrated?")
    assert len(problems) == 1
    assert "names exactly one post" in problems[0]


def test_an_id_a_tool_returned_is_accepted():
    grounding = json.dumps([{"post_id": REAL_ID, "toxicity_score": 0.6}])
    assert _ungrounded_post_id_args({"post_id": REAL_ID}, grounding) == []


def test_an_id_the_operator_typed_is_accepted():
    """No tool returned it, but the operator asked about it by name."""
    assert _ungrounded_post_id_args({"post_id": REAL_ID}, f"summarise {REAL_ID} for me") == []


def test_lists_of_post_ids_are_checked_element_by_element():
    grounding = json.dumps([{"post_id": REAL_ID}])
    problems = _ungrounded_post_id_args({"post_ids": [REAL_ID, "post_99999"]}, grounding)
    assert len(problems) == 1
    assert "post_99999" in problems[0]


def test_other_arguments_are_not_treated_as_post_ids():
    """`sentiment='all'` is a documented value for representative_comments."""
    assert _ungrounded_post_id_args(
        {"post_id": REAL_ID, "sentiment": "all", "limit": 5}, json.dumps([{"post_id": REAL_ID}])
    ) == []


@pytest.mark.asyncio
async def test_the_ungrounded_lookup_never_reaches_the_tool():
    """End to end: the wasted call is not spent, and the model is told why."""

    class _RecordingMCP(_StubMCP):
        def __init__(self):
            super().__init__()
            self.seen = []

        async def call_tool(self, name, arguments):
            self.seen.append(arguments)
            return [{"post_id": REAL_ID}]

    class _GuessingLLM(_StubLLM):
        async def chat(self, *args, **kwargs):
            self.turn += 1
            if self.turn == 1:
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "representative_comments",
                                "arguments": json.dumps({"post_id": "all", "sentiment": "all"}),
                            },
                        }
                    ],
                    "usage": {},
                }
            return {"content": "I could not retrieve any data.", "tool_calls": [], "usage": {}}

    mcp = _RecordingMCP()
    run = await _run(llm=_GuessingLLM(), mcp=mcp)

    assert mcp.seen == [], "the tool must never be called with an ungrounded post_id"
    assert run.tools_used[0]["status"] == "error"
    assert "names exactly one post" in run.tools_used[0]["error"]


@pytest.mark.asyncio
async def test_the_system_prompt_states_the_lookup_order():
    llm = _StubLLM("Done.")

    class _NoToolLLM(_StubLLM):
        async def chat(self, *args, **kwargs):
            self.seen_prompt = args[1][0]["content"] if args else kwargs["messages"][0]["content"]
            return {"content": "Done.", "tool_calls": [], "usage": {}}

    llm = _NoToolLLM()
    await _run(llm=llm)
    assert "call top_posts or semantic_search FIRST" in llm.seen_prompt
    assert "never pass 'all' as a post_id" in llm.seen_prompt


# ---------------------------------------------------------------------------
# 2. How a failure is described to the model
# ---------------------------------------------------------------------------


def test_a_failed_call_is_not_described_as_an_empty_result():
    """The misdiagnosis that started it: `result = None` read as "no rows"."""
    validation_error = (
        "1 validation error for call[trend_query] metric Input should be "
        "'post_count', 'avg_sentiment' or 'avg_toxicity'"
    )
    note = _describe_error(validation_error)

    assert "FAILED" in note
    assert validation_error in note, "the model needs the actual error to fix the call"
    assert "did not run" in note
    assert "not describe this as an absence of data" in note, (
        "a failed call is not evidence about the corpus, and the model reported "
        "it as if it were"
    )


def test_every_no_data_path_forbids_inventing_data():
    """The warning must not depend on which way the call produced nothing."""
    for note in (_describe_error("boom"), _describe_empty([]), _describe_empty(None)):
        assert note is not None
        assert "not invent" in note.lower() or "do not invent" in note.lower(), (
            f"missing anti-fabrication warning: {note!r}"
        )


# ---------------------------------------------------------------------------
# 3. The grounding guard
# ---------------------------------------------------------------------------

FABRICATED = """## Active Discussions and Sentiment Trends

| Rank | Post ID | Topic |
|------|---------|-------|
| 1 | #12345 | Economic Growth |
| 2 | #67890 | Climate Change |

Average sentiment: -0.23 (slightly negative)
"""


class _StubMCP:
    """Every tool call fails, exactly as the live run did."""

    def __init__(self, error="metric Input should be 'post_count'"):
        self.error = error
        self.calls = 0

    async def get_all_manifests(self):
        return [
            {"type": "function", "function": {"name": "trend_query", "parameters": TREND_QUERY_SCHEMA}}
        ]

    def filter_tools(self, manifests, names):
        return manifests

    def server_for(self, name):
        return "analytics-mcp"

    def coerce_arguments(self, name, args):
        return args, []

    async def call_tool(self, name, arguments):
        self.calls += 1
        raise ValueError(self.error)


class _StubLLM:
    """Asks for a tool once, then answers — with fabricated content."""

    def __init__(self, answer=FABRICATED):
        self.answer = answer
        self.turn = 0

    async def chat(self, *args, **kwargs):
        self.turn += 1
        if self.turn == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "trend_query",
                            "arguments": json.dumps({"metric": ["post_count", "avg_sentiment"]}),
                        },
                    }
                ],
                "usage": {},
            }
        return {"content": self.answer, "tool_calls": [], "usage": {}}


async def _run(llm=None, mcp=None):
    runner = AgentRunner(llm_client=llm or _StubLLM(), mcp_client=mcp or _StubMCP())
    return await runner.run(agent_def=AGENT_REGISTRY["analyst"], query="sentiment trends?")


@pytest.mark.asyncio
async def test_run_with_every_call_failed_does_not_report_completed():
    run = await _run()
    assert run.status == "failed", (
        "a run that read nothing from the corpus reported 'completed', which is "
        "how a fabricated briefing reached the operator looking like a result"
    )
    assert run.error and "no data was retrieved" in run.error.lower()


@pytest.mark.asyncio
async def test_the_fabricated_draft_is_discarded_not_annotated():
    run = await _run()
    assert "#12345" not in run.answer, "invented post IDs must not survive into the answer"
    assert "Economic Growth" not in run.answer, "nor invented topics"
    assert "-0.23" not in run.answer, "nor invented figures"


@pytest.mark.asyncio
async def test_the_replacement_answer_tells_the_operator_what_broke():
    run = await _run(mcp=_StubMCP(error="metric Input should be 'post_count'"))
    assert "No data was retrieved" in run.answer
    assert "trend_query" in run.answer, "name the tool that failed"
    assert "metric Input should be" in run.answer, "and the reason it failed"


@pytest.mark.asyncio
async def test_a_successful_call_leaves_the_answer_alone():
    """The guard must be narrow: it fires only when NOTHING succeeded."""

    class _OKMCP(_StubMCP):
        async def call_tool(self, name, arguments):
            self.calls += 1
            return [{"post_id": "cm0abcdef12345678901234", "avg_sentiment": 0.4}]

    run = await _run(llm=_StubLLM("Sentiment is 0.4 for cm0abcdef12345678901234."), mcp=_OKMCP())
    assert run.status == "completed"
    assert "0.4" in run.answer


@pytest.mark.asyncio
async def test_a_run_that_used_no_tools_is_not_treated_as_failed():
    """An agent that answers without tools has not failed any call."""

    class _NoToolLLM(_StubLLM):
        async def chat(self, *args, **kwargs):
            return {"content": "Stance is measured per target.", "tool_calls": [], "usage": {}}

    run = await _run(llm=_NoToolLLM())
    assert run.status == "completed"
    assert run.answer == "Stance is measured per target."


# ---------------------------------------------------------------------------
# The citation backstop that missed
# ---------------------------------------------------------------------------


def test_hash_style_invented_ids_are_flagged():
    """`#12345` is neither CUID-shaped nor preceded by "post id" in a table."""
    unverified = _unverified_citations(FABRICATED, tool_output="[]")
    assert "12345" in unverified and "67890" in unverified


def test_hash_ids_the_tools_returned_are_not_flagged():
    tool_output = json.dumps([{"post_id": "12345", "reactions": 10}])
    assert _unverified_citations(FABRICATED, tool_output) == ["67890"]


def test_markdown_headings_and_ranks_are_not_citations():
    answer = "# Findings\n\n## Top 3\n\nRank #1 and #23 lead the set."
    assert _unverified_citations(answer, tool_output="") == []


# ---------------------------------------------------------------------------
# The real tools, not a hand-written schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_real_analytics_tools_document_what_they_return():
    """The rows-hint is parsed out of the live description; keep it parseable.

    Without this the coercion message quietly loses its most useful half the
    first time someone rewords a docstring.
    """
    from defense.mcp_servers.analytics_mcp import server as analytics
    from defense.services.agents.mcp_client import _returned_fields

    tools = {t.name: t for t in await analytics.mcp.list_tools()}

    fields = _returned_fields(tools["top_posts"].description)
    assert fields and "overall_sentiment" in fields, (
        "top_posts must advertise overall_sentiment, or the model has no way to "
        "know sentiment is already in the rows it gets back"
    )
    assert _returned_fields(tools["trend_query"].description)
    assert _returned_fields(tools["sentiment_over_time"].description)


@pytest.mark.asyncio
async def test_the_documented_fields_are_the_fields_actually_returned():
    """Documentation the model trusts has to match the SQL.

    Checked against the stub builders, which are written to mirror the SELECT
    lists exactly — reaching the real ClickHouse would make this a test of
    whether a container is up.
    """
    from defense.mcp_servers.analytics_mcp import server as analytics
    from defense.services.agents.mcp_client import _returned_fields

    tools = {t.name: t for t in await analytics.mcp.list_tools()}
    # The stub corpus is the last _STUB_CORPUS_DAYS days by design, so a fixed
    # date window silently yields zero rows and this asserts nothing.
    from datetime import date, timedelta

    window = ((date.today() - timedelta(days=3)).isoformat(), date.today().isoformat(), "day")

    for name, rows in (
        ("top_posts", analytics._stub_top_posts("camp-1", "total_reactions", 3, None)),
        ("trend_query", analytics._stub_trend_query("camp-1", *window)),
        ("sentiment_over_time", analytics._stub_sentiment_over_time("camp-1", *window)),
    ):
        documented = {f.strip() for f in _returned_fields(tools[name].description).split(",")}
        actual = set(rows[0])
        assert documented == actual, (
            f"{name} documents {sorted(documented)} but returns {sorted(actual)}"
        )


@pytest.mark.asyncio
async def test_the_rejected_live_call_is_now_caught_before_dispatch():
    """End to end on the real schema: top_posts(metric='avg_sentiment')."""
    from defense.mcp_servers.analytics_mcp import server as analytics

    tools = {t.name: t for t in await analytics.mcp.list_tools()}
    client = MCPClient()
    client._manifest_cache = [
        {
            "type": "function",
            "function": {
                "name": "top_posts",
                "description": tools["top_posts"].description,
                "parameters": tools["top_posts"].parameters,
            },
        }
    ]

    _, problems = client.coerce_arguments("top_posts", {"metric": "avg_sentiment"})
    assert len(problems) == 1
    assert "overall_sentiment" in problems[0]
