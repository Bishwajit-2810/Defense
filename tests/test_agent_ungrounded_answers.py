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


# ---------------------------------------------------------------------------
# 4. Giving up after the first rejection
# ---------------------------------------------------------------------------
#
# The guard above stops a fabricated briefing, but the run it leaves behind is
# still worthless: a live toxicity run ("Are toxic comments concentrated around
# specific targets?") opened with representative_comments on an invented
# post_id, was rejected before dispatch, read the rejection — which names
# top_posts as the fix — and then wrote its answer anyway. One tool call, zero
# rows, status failed. Nothing sent the model back to retrieval.

TOXICITY_MANIFESTS = [
    {
        "type": "function",
        "function": {
            "name": "representative_comments",
            "parameters": {
                "type": "object",
                "properties": {"post_id": {"type": "string"}, "sentiment": {"type": "string"}},
                "required": ["post_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "top_posts",
            "parameters": {
                "type": "object",
                "properties": {"campaign_id": {"type": "string"}, "metric": {"type": "string"}},
                "required": ["campaign_id"],
            },
        },
    },
]


class _ToxicityMCP(_StubMCP):
    """Both tools work; the model's own arguments are what fail."""

    def __init__(self):
        super().__init__()
        self.seen = []

    async def get_all_manifests(self):
        return TOXICITY_MANIFESTS

    async def call_tool(self, name, arguments):
        self.calls += 1
        self.seen.append((name, arguments))
        if name == "top_posts":
            return [{"post_id": REAL_ID, "toxicity": 0.71}]
        return [{"comment": "hostile text", "post_id": arguments.get("post_id")}]


class _GivesUpLLM:
    """The live sequence: invented post_id, then prose with no data."""

    def __init__(self, recover=True):
        self.recover = recover
        self.turn = 0
        self.messages = []

    async def chat(self, *args, **kwargs):
        self.turn += 1
        # Copied, not referenced: the runner appends to this same list between
        # turns, so a stored reference reads back as the run's final state.
        self.messages.append(list(kwargs.get("messages") or (args[1] if len(args) > 1 else [])))
        if self.turn == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "representative_comments",
                            "arguments": json.dumps({"post_id": "1234567890abcdef"}),
                        },
                    }
                ],
                "usage": {},
            }
        if self.turn == 3 and self.recover:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {
                            "name": "top_posts",
                            "arguments": json.dumps({"campaign_id": "all"}),
                        },
                    }
                ],
                "usage": {},
            }
        return {
            "content": f"Toxicity clusters around #12345 (draft {self.turn}).",
            "tool_calls": [],
            "usage": {},
        }


async def _toxicity_run(llm, mcp=None):
    runner = AgentRunner(llm_client=llm, mcp_client=mcp or _ToxicityMCP())
    return await runner.run(
        agent_def=AGENT_REGISTRY["toxicity"],
        query="Are toxic comments concentrated around specific targets?",
    )


@pytest.mark.asyncio
async def test_a_model_that_gives_up_after_a_rejection_is_sent_back_to_retrieval():
    llm = _GivesUpLLM()
    mcp = _ToxicityMCP()
    run = await _toxicity_run(llm, mcp)

    assert [name for name, _ in mcp.seen] == ["top_posts"], (
        "the run ended with one rejected call and never retrieved anything"
    )
    assert len(run.tools_used) == 2
    assert run.tools_used[1]["status"] == "ok"
    assert run.status == "completed"


@pytest.mark.asyncio
async def test_the_retry_names_a_tool_that_needs_no_post_id():
    llm = _GivesUpLLM()
    await _toxicity_run(llm)

    retry = llm.messages[2][-1]
    assert retry["role"] == "user"
    assert "top_posts" in retry["content"], "name the tool that can open the run"
    instruction = retry["content"].split("What failed:")[1].split("\n\n", 1)[1]
    assert "representative_comments" not in instruction, (
        "a tool that requires a post_id cannot be the one it retries with"
    )
    assert "do not answer from memory" in retry["content"].lower()


@pytest.mark.asyncio
async def test_the_retry_repeats_why_the_call_failed():
    llm = _GivesUpLLM()
    await _toxicity_run(llm)

    retry = llm.messages[2][-1]["content"]
    assert "representative_comments" in retry, "name the call that failed"
    assert "names exactly one post" in retry, "and the reason it failed"


@pytest.mark.asyncio
async def test_the_retry_is_bounded_and_the_run_still_fails_honestly():
    """A model that never recovers must not loop, and must not ship the draft."""
    llm = _GivesUpLLM(recover=False)
    mcp = _ToxicityMCP()
    run = await _toxicity_run(llm, mcp)

    assert mcp.seen == []
    assert llm.turn <= 4, f"retries are not bounded: {llm.turn} LLM turns"
    assert run.status == "failed"
    assert "#12345" not in run.answer, "the ungrounded draft must still be discarded"
    assert "No data was retrieved" in run.answer


@pytest.mark.asyncio
async def test_no_retry_once_a_call_has_succeeded():
    """Narrow guard: a partial failure is a normal run, not a dead one."""

    class _PartialLLM(_GivesUpLLM):
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
                                "name": "top_posts",
                                "arguments": json.dumps({"campaign_id": "all"}),
                            },
                        }
                    ],
                    "usage": {},
                }
            return {"content": f"Top post is {REAL_ID}.", "tool_calls": [], "usage": {}}

    llm = _PartialLLM()
    run = await _toxicity_run(llm)
    assert llm.turn == 2, "a run that retrieved data must not be pushed to retry"
    assert run.status == "completed"
    assert REAL_ID in run.answer


def test_discovery_tools_exclude_per_post_lookups():
    from defense.services.agents.runner import _discovery_tools

    assert _discovery_tools(TOXICITY_MANIFESTS) == ["top_posts"]


def test_discovery_tools_are_named_in_a_useful_order():
    from defense.services.agents.runner import _discovery_tools

    manifests = [
        {"type": "function", "function": {"name": n, "parameters": {}}}
        for n in ("reaction_mix", "semantic_search", "top_posts")
    ]
    assert _discovery_tools(manifests) == ["top_posts", "semantic_search", "reaction_mix"]


# ---------------------------------------------------------------------------
# 5. Invented quotes under real citations
# ---------------------------------------------------------------------------
#
# What the retry above uncovered. With the model sent back to retrieval it now
# calls top_posts, gets 10 real rows — and writes a "Representative Quotes"
# section anyway, though representative_comments never ran. The quotes are
# invented, the post IDs beside them are real, and every existing guard passes:
# nothing failed wholesale, and no citation is ungrounded. Verbatim from the
# live run: "the quotes are exact representations of the toxic comments
# retrieved from the tool".

from defense.services.agents.runner import _unverified_quotes  # noqa: E402

RETRIEVED_COMMENTS = json.dumps(
    [{"post_id": REAL_ID, "text": "This policy is a disgrace and everyone knows it."}]
)


def test_an_invented_quote_is_flagged():
    answer = f'Representative: "You\'re just a mindless drone, repeating what you\'ve been told." ({REAL_ID})'
    assert _unverified_quotes(answer, RETRIEVED_COMMENTS) == [
        "You're just a mindless drone, repeating what you've been told."
    ]


def test_a_retrieved_quote_is_not_flagged():
    answer = 'One commenter wrote: "This policy is a disgrace and everyone knows it."'
    assert _unverified_quotes(answer, RETRIEVED_COMMENTS) == []


def test_rewrapped_and_recased_quotes_are_still_grounded():
    """Re-wrapping a real quote is not fabrication."""
    answer = 'The comment — "This policy is a DISGRACE\n  and everyone knows it." — is typical.'
    assert _unverified_quotes(answer, RETRIEVED_COMMENTS) == []


def test_a_quote_shortened_with_an_ellipsis_is_checked_segment_by_segment():
    grounded = 'A commenter said "This policy is a disgrace ... everyone knows it."'
    invented = 'A commenter said "This policy is a disgrace ... and should be prosecuted now."'
    assert _unverified_quotes(grounded, RETRIEVED_COMMENTS) == []
    assert len(_unverified_quotes(invented, RETRIEVED_COMMENTS)) == 1


def test_short_quoted_values_are_not_treated_as_comment_text():
    """Argument values and emphasis must not raise a fabrication warning."""
    answer = 'Scope was "all" and the metric was "toxicity_score", ranked "high" to "low".'
    assert _unverified_quotes(answer, RETRIEVED_COMMENTS) == []


def test_curly_quotes_are_read_the_same_as_straight_ones():
    answer = "The comment “You're just a mindless drone, repeating what you've been told.” appeared."
    assert len(_unverified_quotes(answer, RETRIEVED_COMMENTS)) == 1


def test_quoting_the_operators_own_question_is_not_fabrication():
    question = 'Find comments about "the new border policy and its economic effects".'
    answer = 'You asked about "the new border policy and its economic effects" — here is what ran.'
    assert _unverified_quotes(answer, f"{question}\n[]") == []


@pytest.mark.asyncio
async def test_the_live_shape_is_flagged_end_to_end():
    """Real citations, real table, invented quotes — the run the retry produced."""

    class _QuotingLLM(_GivesUpLLM):
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
                                "name": "top_posts",
                                "arguments": json.dumps({"campaign_id": "all"}),
                            },
                        }
                    ],
                    "usage": {},
                }
            return {
                "content": (
                    f"## Representative Quotes\n"
                    f'* "You\'re just a mindless drone, repeating what you\'ve been told." '
                    f"(post_id: {REAL_ID})\n\n"
                    "Note that the quotes are exact representations of the toxic "
                    "comments retrieved from the tool."
                ),
                "tool_calls": [],
                "usage": {},
            }

    run = await _toxicity_run(_QuotingLLM())

    assert run.citations == [REAL_ID], "the citation itself is real — that is the trap"
    assert not run.unverified_citations, "so the citation guard sees nothing wrong"
    assert run.unverified_quotes, "the quote is what was invented, and it must be flagged"
    assert "Unverified quotes" in run.answer, "and flagged where the operator reads"


@pytest.mark.asyncio
async def test_a_discarded_answer_is_not_also_quote_flagged():
    """No double-reporting: the fabricated draft is already gone."""
    run = await _toxicity_run(_GivesUpLLM(recover=False))
    assert run.unverified_quotes == []
    assert "Unverified quotes" not in run.answer


# ---------------------------------------------------------------------------
# 6. One surplus argument killing the only useful call
# ---------------------------------------------------------------------------
#
# The live run that came back "completed" and answered nothing. The model called
# top_posts(metric='toxicity_score', granularity='day') — `granularity` belongs
# to trend_query — and fastmcp rejected the WHOLE call:
#
#   1 validation error for call[top_posts]
#   granularity  Unexpected keyword argument
#
# That was the only call in the run that returns toxicity scores. What survived
# was semantic_search, whose rows are post summaries with no toxicity in them,
# and the model wrote a description of the JSON payload instead of an answer.

from defense.services.agents.runner import _describe_dropped, _join_notes  # noqa: E402


def test_a_parameter_the_tool_does_not_have_is_dropped_not_dispatched():
    args, dropped, problems = _top_posts_client().drop_unsupported_arguments(
        "top_posts", {"metric": "toxicity_score", "limit": 10, "granularity": "day"}
    )
    assert dropped == ["granularity"]
    assert problems == [], "a call that can run must not be turned into a failure"
    assert args == {"metric": "toxicity_score", "limit": 10}


def test_supported_arguments_are_untouched():
    supplied = {"campaign_id": "all", "metric": "toxicity_score", "limit": 10}
    args, dropped, problems = _top_posts_client().drop_unsupported_arguments(
        "top_posts", dict(supplied)
    )
    assert (args, dropped, problems) == (supplied, [], [])


def test_a_misspelled_real_parameter_is_reported_rather_than_dropped():
    """Dropping `minimum_toxicity` would return unfiltered rows it thinks are filtered."""
    client = _top_posts_client()
    client._manifest_cache[0]["function"]["parameters"] = {
        "type": "object",
        "properties": {"metric": {"type": "string"}, "min_toxicity": {"type": "number"}},
    }
    args, dropped, problems = client.drop_unsupported_arguments(
        "top_posts", {"minimum_toxicity": 0.8}
    )
    assert dropped == [], "a near-miss carries meaning and must not vanish"
    assert len(problems) == 1
    assert "'min_toxicity'" in problems[0], "name the parameter it meant"
    assert "did NOT run" in problems[0]
    assert args == {"minimum_toxicity": 0.8}, "arguments are not mutated when reporting"


def test_nothing_is_dropped_without_a_schema():
    client = MCPClient()
    args, dropped, problems = client.drop_unsupported_arguments("top_posts", {"anything": 1})
    assert (args, dropped, problems) == ({"anything": 1}, [], [])


def test_a_tool_that_accepts_free_form_keys_is_left_alone():
    client = _top_posts_client()
    client._manifest_cache[0]["function"]["parameters"] = {
        "type": "object",
        "properties": {"metric": {"type": "string"}},
        "additionalProperties": True,
    }
    _, dropped, problems = client.drop_unsupported_arguments("top_posts", {"extra": 1})
    assert (dropped, problems) == ([], [])


def test_the_model_is_told_the_rows_are_not_filtered_by_what_was_dropped():
    note = _describe_dropped("top_posts", ["granularity"])
    assert "IGNORED ARGUMENTS" in note
    assert "`granularity`" in note
    assert "ran WITHOUT it" in note
    assert "not filtered, grouped or ranked by it" in note


def test_notes_on_one_result_are_combined_not_overwritten():
    """A dropped argument AND zero rows is the case most likely to mislead."""
    combined = _join_notes(_describe_dropped("top_posts", ["granularity"]), _describe_empty([]))
    assert "IGNORED ARGUMENTS" in combined and "EMPTY RESULT" in combined
    assert _join_notes(None, None) is None
    assert _join_notes(None, "only this") == "only this"


@pytest.mark.asyncio
async def test_the_live_call_now_runs_and_returns_rows():
    """End to end on the exact arguments the live run sent."""

    class _SchemaMCP(_ToxicityMCP):
        def __init__(self):
            super().__init__()
            self._client = MCPClient()
            self._client._manifest_cache = TOXICITY_MANIFESTS

        def coerce_arguments(self, name, args):
            return self._client.coerce_arguments(name, args)

        def drop_unsupported_arguments(self, name, args):
            return self._client.drop_unsupported_arguments(name, args)

    class _SurplusArgLLM(_GivesUpLLM):
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
                                "name": "top_posts",
                                "arguments": json.dumps(
                                    {
                                        "campaign_id": "all",
                                        "metric": "toxicity_score",
                                        "granularity": "day",
                                    }
                                ),
                            },
                        }
                    ],
                    "usage": {},
                }
            return {"content": f"Most toxic: {REAL_ID}.", "tool_calls": [], "usage": {}}

    mcp = _SchemaMCP()
    run = await _toxicity_run(_SurplusArgLLM(), mcp)

    assert [name for name, _ in mcp.seen] == ["top_posts"], "the call must reach the tool"
    assert "granularity" not in mcp.seen[0][1], "without the parameter it does not have"
    assert mcp.seen[0][1]["metric"] == "toxicity_score", "and with the one that matters"
    assert run.tools_used[0]["status"] == "ok"
    assert run.tools_used[0]["dropped_arguments"] == ["granularity"], (
        "the trace must show the operator what was ignored"
    )
    assert run.status == "completed"


# ---------------------------------------------------------------------------
# 7. The second hop that never happens
# ---------------------------------------------------------------------------
#
# `representative_comments` has not succeeded once in any observed live run. The
# model stops at top_posts — post-level aggregates, no comment text anywhere in
# them — and writes the Representative Quotes section from imagination. So the
# toxicity agent structurally cannot answer "are toxic comments concentrated
# around specific targets": it has never read a comment.

from defense.services.agents.runner import (  # noqa: E402
    _discovery_tools,
    _numbers_in,
    _per_post_tools,
    _second_hop_prompt,
    _uncorroborated_comment_stats,
)

QUOTING_DRAFT = (
    "## Representative Quotes\n"
    '* "You\'re just a mindless drone, following the herd." - Post #{pid}\n'
    "\n* Personal attacks: 40% of toxic comments\n"
)


def test_per_post_tools_are_the_ones_that_read_comments():
    assert _per_post_tools(TOXICITY_MANIFESTS) == ["representative_comments"]
    assert _discovery_tools(TOXICITY_MANIFESTS) == ["top_posts"]


def test_the_second_hop_prompt_hands_over_a_real_post_id():
    prompt = _second_hop_prompt("representative_comments", REAL_ID, ["some invented line"])
    assert f"post_id='{REAL_ID}'" in prompt, "the argument it gets wrong must be supplied"
    assert "came from the rows you already retrieved" in prompt
    assert "no tool in this run has returned a single comment" in prompt
    assert "delete the quotes" in prompt, "an empty result must not be filled back in"


class _NoSecondHopLLM(_GivesUpLLM):
    """top_posts, then a quotes section it never retrieved. The live shape."""

    def __init__(self, hop=True):
        super().__init__()
        self.hop = hop

    async def chat(self, *args, **kwargs):
        self.turn += 1
        self.messages.append(list(kwargs.get("messages") or []))
        if self.turn == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "top_posts",
                            "arguments": json.dumps({"campaign_id": "all"}),
                        },
                    }
                ],
                "usage": {},
            }
        if self.turn == 3 and self.hop:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {
                            "name": "representative_comments",
                            "arguments": json.dumps({"post_id": REAL_ID}),
                        },
                    }
                ],
                "usage": {},
            }
        if self.turn == 4:
            return {
                "content": f'The top comment reads "hostile text" on post {REAL_ID}.',
                "tool_calls": [],
                "usage": {},
            }
        return {"content": QUOTING_DRAFT.format(pid=REAL_ID), "tool_calls": [], "usage": {}}


@pytest.mark.asyncio
async def test_a_quotes_section_without_comment_data_triggers_the_second_hop():
    llm = _NoSecondHopLLM()
    mcp = _ToxicityMCP()
    run = await _toxicity_run(llm, mcp)

    assert [name for name, _ in mcp.seen] == ["top_posts", "representative_comments"], (
        "the chain top_posts -> representative_comments is the agent's whole purpose"
    )
    assert mcp.seen[1][1]["post_id"] == REAL_ID, "with an ID that came from the rows"
    assert run.status == "completed"
    assert not run.unverified_quotes, "the rewritten section quotes retrieved text"
    assert "hostile text" in run.answer


@pytest.mark.asyncio
async def test_the_hop_prompt_offers_the_top_ranked_post():
    """Order matters: the first ID a tool returned, not an arbitrary set member."""

    class _TwoPostMCP(_ToxicityMCP):
        async def call_tool(self, name, arguments):
            self.calls += 1
            self.seen.append((name, arguments))
            if name == "top_posts":
                return [
                    {"post_id": REAL_ID, "toxicity": 0.9},
                    {"post_id": "cm0zzzzzz98765432109876", "toxicity": 0.4},
                ]
            return [{"comment": "hostile text"}]

    llm = _NoSecondHopLLM()
    await _toxicity_run(llm, _TwoPostMCP())
    assert f"post_id='{REAL_ID}'" in llm.messages[2][-1]["content"]


@pytest.mark.asyncio
async def test_the_hop_is_asked_for_once_and_the_answer_is_still_flagged():
    """A model that refuses the hop must not loop, and must not pass unflagged."""
    llm = _NoSecondHopLLM(hop=False)
    run = await _toxicity_run(llm)

    assert llm.turn <= 3, f"the hop is asked for once, not repeatedly: {llm.turn} turns"
    assert run.unverified_quotes, "the quote guard is still the backstop"
    assert "Unverified quotes" in run.answer


@pytest.mark.asyncio
async def test_no_hop_when_the_answer_quotes_what_was_retrieved():
    """Narrow guard: a grounded answer must not cost an extra round trip."""

    class _GroundedLLM(_NoSecondHopLLM):
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
                                "name": "top_posts",
                                "arguments": json.dumps({"campaign_id": "all"}),
                            },
                        }
                    ],
                    "usage": {},
                }
            return {"content": f"Most toxic post is {REAL_ID}.", "tool_calls": [], "usage": {}}

    llm = _GroundedLLM()
    run = await _toxicity_run(llm)
    assert llm.turn == 2
    assert run.status == "completed"


# --- the numbers the quote guard cannot see --------------------------------


def test_invented_comment_percentages_are_flagged():
    answer = "* Personal attacks: 30% of toxic comments\n* Hate speech: 40% of toxic comments"
    flagged = _uncorroborated_comment_stats(answer, tool_output="[]")
    assert len(flagged) == 2


def test_a_percentage_converted_from_a_retrieved_score_is_not_flagged():
    """toxicity_score 0.95 written as "95%" is arithmetic, not invention."""
    rows = json.dumps([{"post_id": REAL_ID, "toxicity_score": 0.95}])
    answer = "Hate speech reaches 95% on the worst post."
    assert _uncorroborated_comment_stats(answer, rows) == []


def test_post_level_figures_are_not_treated_as_comment_claims():
    rows = json.dumps([{"post_id": REAL_ID, "comment_count": 1112}])
    answer = f"| {REAL_ID} | 1112 | 0.95 |\nEngagement rose 40% quarter on quarter."
    assert _uncorroborated_comment_stats(answer, rows) == [], (
        "only claims about what comments CONTAIN are checked"
    )


@pytest.mark.asyncio
async def test_invented_statistics_are_flagged_when_no_comment_was_read():
    run = await _toxicity_run(_NoSecondHopLLM(hop=False))
    assert run.unverified_stats, "40% of toxic comments, from a run that read no comments"
    assert "Unverified statistics" in run.answer
    assert "No comment-level tool returned data" in run.answer


@pytest.mark.asyncio
async def test_statistics_are_not_second_guessed_once_comments_were_read():
    """With comment rows in hand a percentage may be a real aggregate."""

    class _StatsAfterHopLLM(_NoSecondHopLLM):
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
                                "arguments": json.dumps({"post_id": REAL_ID}),
                            },
                        }
                    ],
                    "usage": {},
                }
            return {
                "content": "Personal attacks: 40% of toxic comments.",
                "tool_calls": [],
                "usage": {},
            }

    run = await _toxicity_run(_StatsAfterHopLLM(), _ToxicityMCP())
    assert run.unverified_stats == []


def test_a_digit_inside_a_cuid_does_not_ground_a_percentage():
    """How the first version of this guard cleared the line it was written for.

    "4" is a substring of ``cm0abcdef12345678901234``, so a textual test read
    "40%" as corroborated by a result set containing no such figure.
    """
    rows = json.dumps([{"post_id": REAL_ID, "toxicity": 0.71}])
    assert _uncorroborated_comment_stats("Personal attacks: 40% of toxic comments", rows) == [
        "Personal attacks: 40% of toxic comments"
    ]


def test_numbers_are_read_as_numbers_not_text():
    assert _numbers_in('[{"toxicity": 0.95, "count": 1112}]') == [0.95, 1112.0]
    assert _numbers_in("") == []


@pytest.mark.asyncio
async def test_the_hop_is_not_asked_for_again_once_a_comment_was_read():
    """The live regression: one comment tool succeeded, another had not.

    `representative_comments` returned a row and this still pushed, because
    `get_thread` was untouched. Asked a second time to go and read toxic
    comments, llama3.1:8b refused — "I cannot provide information or guidance on
    harmful behavior" — and the whole briefing was replaced by the refusal.
    """
    manifests = TOXICITY_MANIFESTS + [
        {
            "type": "function",
            "function": {
                "name": "get_thread",
                "parameters": {
                    "type": "object",
                    "properties": {"post_id": {"type": "string"}},
                    "required": ["post_id"],
                },
            },
        }
    ]

    class _TwoCommentToolMCP(_ToxicityMCP):
        async def get_all_manifests(self):
            return manifests

    def _call(call_id, name, args):
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
            "usage": {},
        }

    class _HopThenInventLLM(_NoSecondHopLLM):
        """The full live chain, then a quotes section that outruns the data."""

        async def chat(self, *args, **kwargs):
            self.turn += 1
            if self.turn == 1:
                return _call("call-1", "top_posts", {"campaign_id": "all"})
            if self.turn == 2:
                # Grounded now: the ID came from the top_posts rows above.
                return _call("call-2", "representative_comments", {"post_id": REAL_ID})
            return {"content": QUOTING_DRAFT.format(pid=REAL_ID), "tool_calls": [], "usage": {}}

    llm = _HopThenInventLLM()
    run = await _toxicity_run(llm, _TwoCommentToolMCP())

    assert [e["tool_name"] for e in run.tools_used] == [
        "top_posts",
        "representative_comments",
    ], "the chain ran; there is nothing left to push it towards"
    assert llm.turn == 3, "a run that has read a comment must not be pushed again"
    assert run.unverified_quotes, "the quote guard remains the backstop for what it invents"


# ---------------------------------------------------------------------------
# 8. The answer that describes the data instead of using it
# ---------------------------------------------------------------------------
#
# Two live runs ended this way, one of them AFTER the chain above had worked and
# real comment rows were in hand:
#
#   "The provided data appears to be a JSON dump of comments... Each comment is
#    represented by an object with the following keys: `id`, `comment_id`...
#    Here's an example of how you could parse this JSON data in Python: ```python
#    import json ..."
#
# Nothing fabricated, nothing flagged, status `completed` — and no answer to the
# operator's question anywhere in it.

from defense.services.agents.runner import _describes_the_payload, _is_code_output  # noqa: E402

PAYLOAD_NARRATION = (
    "The provided data appears to be a JSON dump of comments on a social media "
    "platform. Each comment is represented by an object with the following keys:\n"
    "* `id`: a unique identifier for the comment\n"
    "* `text`: the text content of the comment\n\n"
    "Here's an example of how you could parse this JSON data in Python:\n"
    "```python\nimport json\nwith open('comments.json') as f:\n    data = json.load(f)\n```\n"
)


def test_code_further_down_the_answer_is_detected():
    """The 300-character window that let the live answer through."""
    assert _is_code_output(PAYLOAD_NARRATION), (
        "the python block sits ~1200 chars in; only the first 300 were scanned"
    )


def test_an_ordinary_briefing_is_not_mistaken_for_code():
    briefing = (
        "## Executive Findings\nToxicity is concentrated on three posts.\n\n"
        f"| post_id | toxicity |\n| --- | --- |\n| {REAL_ID} | 0.95 |\n"
    )
    assert not _is_code_output(briefing)
    assert not _describes_the_payload(briefing)


def test_payload_narration_is_detected_without_any_code():
    prose_only = PAYLOAD_NARRATION.split("Here's an example")[0]
    assert not _is_code_output(prose_only), "no code in this half"
    assert _describes_the_payload(prose_only), "but it is still not an answer"


def test_one_stray_phrase_does_not_trip_the_narration_check():
    """A methodology note is not a data-structure walkthrough."""
    answer = (
        "## Findings\nThe provided data covers three campaigns. Toxicity is "
        f"concentrated on {REAL_ID}, which drew 1112 comments."
    )
    assert not _describes_the_payload(answer)


@pytest.mark.asyncio
async def test_a_narrated_payload_is_retried_and_then_reported_as_failed():
    """It answers nothing, so it must not be handed over as a finished briefing."""

    class _NarratingLLM(_NoSecondHopLLM):
        def __init__(self):
            super().__init__()
            self.synth_prompt = None

        async def chat(self, *args, **kwargs):
            self.turn += 1
            msgs = kwargs.get("messages") or []
            if self.turn == 1:
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "top_posts",
                                "arguments": json.dumps({"campaign_id": "all"}),
                            },
                        }
                    ],
                    "usage": {},
                }
            if self.turn == 3:
                self.synth_prompt = msgs[-1]["content"]
            return {"content": PAYLOAD_NARRATION, "tool_calls": [], "usage": {}}

    llm = _NarratingLLM()
    run = await _toxicity_run(llm)

    assert llm.turn == 3, "one synthesis retry, not a loop"
    assert "Answer this question and nothing else" in llm.synth_prompt
    assert "concentrated around specific targets" in llm.synth_prompt, (
        "restate the question — after a large tool result it is out of view"
    )
    assert run.status == "failed", "a payload walkthrough is not a completed briefing"
    assert "described the tool output instead of answering" in run.error
    assert "retrieved data is intact" in run.error


@pytest.mark.asyncio
async def test_a_recovered_synthesis_is_accepted():
    """The retry usually works; when it does, the run is a normal success."""

    class _RecoveringLLM(_NoSecondHopLLM):
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
                                "name": "top_posts",
                                "arguments": json.dumps({"campaign_id": "all"}),
                            },
                        }
                    ],
                    "usage": {},
                }
            if self.turn == 2:
                return {"content": PAYLOAD_NARRATION, "tool_calls": [], "usage": {}}
            return {
                "content": f"## Findings\nToxicity concentrates on {REAL_ID}.",
                "tool_calls": [],
                "usage": {},
            }

    run = await _toxicity_run(_RecoveringLLM())
    assert run.status == "completed"
    assert "Toxicity concentrates on" in run.answer
    assert "```python" not in run.answer


# ---------------------------------------------------------------------------
# 9. The wholly invented briefing that still got through
# ---------------------------------------------------------------------------
#
# A live run made the chain work — top_posts and a comment tool both returned
# rows — and then ignored all of it: a table of posts 12345 / 67890 / 34567 that
# do not exist, three invented quotes, and "40% / 30% / 30%". The quote guard
# caught the quotes. The other two guards did not fire:
#
#   * "(Post ID: 12345)" is five digits, and _CITED_ID_RE needs six characters;
#     there is no "#", so _HASH_ID_RE missed it too.
#   * the statistics check was skipped because a comment tool HAD succeeded —
#     the exemption meant for genuine aggregates, claimed by a section whose own
#     quotes were fabricated.

INVENTED_BRIEFING = (
    "| post_id | toxicity score |\n| --- | --- |\n| 12345 | 0.85 |\n| 67890 | 0.92 |\n\n"
    "* **Personal Attacks**: 40% of toxic comments target individuals.\n\n"
    '> "You\'re just a brainless drone who can\'t think for yourself." (Post ID: 12345)\n'
    '> "All [group] people are stupid." (Post ID: 67890)\n'
)


def test_a_five_digit_invented_post_id_is_flagged():
    unverified = _unverified_citations(INVENTED_BRIEFING, tool_output="[]")
    assert "12345" in unverified and "67890" in unverified


def test_prose_after_the_words_post_id_is_not_read_as_a_citation():
    """Why this is a separate numeric pattern instead of a shorter _CITED_ID_RE."""
    assert _unverified_citations("The Post ID field is a CUID.", tool_output="[]") == []


def test_a_five_digit_id_the_tools_returned_is_not_flagged():
    rows = json.dumps([{"post_id": "12345", "toxicity": 0.85}])
    assert "12345" not in _unverified_citations(INVENTED_BRIEFING, rows)


@pytest.mark.asyncio
async def test_invented_statistics_are_flagged_when_the_quotes_beside_them_are():
    """Comment rows were retrieved — and the section is invented anyway."""

    def _call(cid, name, args):
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": cid,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
            "usage": {},
        }

    class _InventsAnywayLLM(_NoSecondHopLLM):
        async def chat(self, *args, **kwargs):
            self.turn += 1
            if self.turn == 1:
                return _call("c1", "top_posts", {"campaign_id": "all"})
            if self.turn == 2:
                return _call("c2", "representative_comments", {"post_id": REAL_ID})
            return {"content": INVENTED_BRIEFING, "tool_calls": [], "usage": {}}

    run = await _toxicity_run(_InventsAnywayLLM())

    assert run.unverified_quotes, "the quotes are invented"
    assert run.unverified_stats, (
        "so the 40% beside them does not get the aggregate exemption"
    )
    assert run.unverified_citations, "and neither do the five-digit post IDs"


def test_a_coincidental_integer_in_the_payload_does_not_ground_a_percentage():
    """How the live "40% of toxic comments" cleared the check.

    Ten posts and a comment thread contain "30" and "40" somewhere — a like
    count, a timestamp fragment — whatever the answer claims about categories.
    """
    rows = json.dumps(
        [{"post_id": REAL_ID, "comment_count": 40, "created_at": "2026-05-30T00:00:00"}]
    )
    flagged = _uncorroborated_comment_stats("Personal attacks: 40% of toxic comments", rows)
    assert flagged == ["Personal attacks: 40% of toxic comments"]


def test_a_percentage_a_tool_actually_reported_is_not_flagged():
    """reaction_mix returns a `percentages` object; citing it is legitimate."""
    payload = json.dumps({"percentages": {"like": 40.0, "angry": 30.0}})
    assert _uncorroborated_comment_stats("40% of the comments are angry reactions", payload) == []


def test_a_percentage_written_as_a_percentage_by_the_tool_is_not_flagged():
    payload = json.dumps([{"summary": "42% of toxic comments were removed"}])
    assert _uncorroborated_comment_stats("42% of toxic comments were removed", payload) == []


@pytest.mark.asyncio
async def test_a_guard_does_not_flag_another_guards_warning():
    """Each guard appends to the answer; the next must not read what it wrote.

    Live: a flagged quote containing "30% of toxic comments" was reported a
    second time as an invented statistic — the warning quoting the warning.
    """

    def _call(cid, name, args):
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": cid,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
            "usage": {},
        }

    class _QuotesAStatLLM(_NoSecondHopLLM):
        async def chat(self, *args, **kwargs):
            self.turn += 1
            if self.turn == 1:
                return _call("c1", "top_posts", {"campaign_id": "all"})
            if self.turn == 2:
                return _call("c2", "representative_comments", {"post_id": REAL_ID})
            return {
                "content": 'A commenter wrote "Hate speech: 30% of toxic comments, roughly".',
                "tool_calls": [],
                "usage": {},
            }

    run = await _toxicity_run(_QuotesAStatLLM())

    assert len(run.unverified_quotes) == 1
    assert not any("Unverified" in s for s in run.unverified_stats), (
        "the statistics guard must not re-read the quotes warning"
    )
