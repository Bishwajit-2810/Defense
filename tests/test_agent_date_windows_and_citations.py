"""The 2023 briefing: a wrong date window, and synthetic data that hid it.

A live Analyst run, asked "what are the most active discussions and sentiment
trends across all posts?", returned a full-year 2023 sentiment table, a reaction
mix, and two post citations. Every figure in it was fabricated, by two
independent mechanisms compounding:

  1. `from_date`/`to_date` were **required** arguments described only as
     "Inclusive start date (YYYY-MM-DD)", and nothing told the model what day it
     is. llama3.1:8b filled them from its training era — 2023-01-01 to
     2023-12-31 — and `_clean_dates` honoured them, because it only rewrote
     literal placeholders ("YYYY"), blanks and none/null. A confidently wrong
     date passed straight through.

  2. Stub mode generated one row per period across *whatever* window it was
     handed, so the impossible window came back as a plausible year of data.
     Against real ClickHouse it would have returned nothing and the error would
     have been obvious. The reaction table in that briefing is reproducible
     here, exactly, from `random.seed("all" + "react")`.

And the citations under it — "Post ID 1234567890", with quoted comment text —
were invented outright: stub `top_posts` returns ids shaped `stub-post-0001`,
and `run.citations` collects only 20+ char CUIDs, so it was empty and nothing
contradicted the prose.
"""

from datetime import date, timedelta

import pytest

from defense.mcp_servers.analytics_mcp import server as analytics
from defense.services.agents.registry import ANALYST_AGENT
from defense.services.agents.runner import (
    AgentRunner,
    _describe_empty,
    _unverified_citations,
    _wrap_tool_result,
)

_TODAY = date.today()
_THE_2023_WINDOW = {"from_date": "2023-01-01", "to_date": "2023-12-31"}


# ---------------------------------------------------------------------------
# 1. The window the model asked for
# ---------------------------------------------------------------------------

def test_the_2023_window_yields_no_stub_data():
    """The regression: a training-era window must not produce a year of numbers."""
    assert analytics._stub_date_series(**_THE_2023_WINDOW, granularity="week") == []
    assert analytics._stub_sentiment_over_time("all", **_THE_2023_WINDOW, granularity="week") == []
    assert analytics._stub_trend_query("all", **_THE_2023_WINDOW, granularity="week") == []


def test_a_recent_window_still_produces_data():
    """The clamp must not empty out the windows that legitimately have data."""
    recent = {
        "from_date": (_TODAY - timedelta(days=14)).isoformat(),
        "to_date": _TODAY.isoformat(),
    }
    series = analytics._stub_date_series(**recent, granularity="day")
    assert len(series) == 15
    assert series[0] == recent["from_date"]
    assert series[-1] == recent["to_date"]


@pytest.fixture
def corpus(monkeypatch):
    """Pin the corpus bounds so date resolution does not depend on a live DB."""
    window = ("2026-04-29", "2026-05-19")   # the real bounds of this deployment
    monkeypatch.setattr(analytics, "_corpus_window", lambda: window)
    return window


def test_omitted_dates_default_to_the_window_the_corpus_covers(corpus):
    """"Last 30 days" is the wrong default for an archive.

    This deployment's 50 rows span 2026-04-29..2026-05-19 while today is
    2026-08-16 — a 30-day default returns nothing, so an agent correctly told to
    omit its dates would still get an empty answer.
    """
    assert analytics._clean_dates(None, None) == corpus


@pytest.mark.parametrize(
    "raw", ["", None, "YYYY-MM-DD", "none", "null", "not a date"],
    ids=["empty", "none-obj", "placeholder", "none-str", "null-str", "junk"],
)
def test_unusable_dates_fall_back_to_the_default_window(raw, corpus):
    assert analytics._clean_dates(raw, raw) == corpus


def test_corpus_bounds_are_read_from_clickhouse_and_cached(monkeypatch):
    calls: list[str] = []

    def _fake_query(sql, params=None):
        calls.append(sql)
        return [{"first": "2026-04-29 14:12:21", "last": "2026-05-19 14:25:39"}]

    monkeypatch.setattr(analytics, "STUB_MODE", False)
    monkeypatch.setattr(analytics, "_ch_query", _fake_query)
    monkeypatch.setattr(analytics, "_corpus_cache", None)

    assert analytics._corpus_window() == ("2026-04-29", "2026-05-19")
    assert analytics._corpus_window() == ("2026-04-29", "2026-05-19")
    assert len(calls) == 1, "bounds must be cached, not re-queried per tool call"


def test_explicit_dates_do_not_trigger_a_bounds_query(monkeypatch):
    """The lookup is per-omission, not per-call — handlers issue one query each."""
    calls: list[str] = []
    monkeypatch.setattr(analytics, "STUB_MODE", False)
    monkeypatch.setattr(analytics, "_ch_query", lambda sql, params=None: calls.append(sql) or [])
    monkeypatch.setattr(analytics, "_corpus_cache", None)

    analytics._clean_dates("2026-05-01", "2026-05-10")
    assert calls == []


def test_unreadable_corpus_bounds_fall_back_rather_than_failing(monkeypatch):
    """A default that might be wrong beats failing every query over it."""
    def _boom(sql, params=None):
        raise ConnectionError("clickhouse unreachable")

    monkeypatch.setattr(analytics, "STUB_MODE", False)
    monkeypatch.setattr(analytics, "_ch_query", _boom)
    monkeypatch.setattr(analytics, "_corpus_cache", None)

    assert analytics._corpus_window() == (
        (_TODAY - timedelta(days=30)).isoformat(),
        _TODAY.isoformat(),
    )


def test_a_future_start_is_rejected_with_todays_date():
    """The error has to carry today's date — that is what lets the model retry."""
    future = (_TODAY + timedelta(days=30)).isoformat()
    with pytest.raises(ValueError) as exc:
        analytics._clean_dates(future, future)
    assert _TODAY.isoformat() in str(exc.value)
    assert future in str(exc.value)


def test_an_inverted_window_is_rejected():
    with pytest.raises(ValueError, match="after to_date"):
        analytics._clean_dates("2026-06-01", "2026-05-01")


def test_a_future_end_is_trimmed_not_rejected():
    """No data can exist after today, so the window still asks the same question."""
    start = (_TODAY - timedelta(days=5)).isoformat()
    f_d, t_d = analytics._clean_dates(start, (_TODAY + timedelta(days=365)).isoformat())
    assert (f_d, t_d) == (start, _TODAY.isoformat())


def test_date_arguments_are_optional_so_the_model_need_not_guess():
    """A required argument with no known value is what forced the invention."""
    import inspect

    for tool in (analytics.trend_query, analytics.sentiment_over_time):
        sig = inspect.signature(tool.fn if hasattr(tool, "fn") else tool)
        for name in ("from_date", "to_date"):
            assert sig.parameters[name].default is None, f"{tool} {name} still required"


# ---------------------------------------------------------------------------
# 2. The agent has to know what day it is
# ---------------------------------------------------------------------------

class _OneShotLLM:
    def __init__(self, content: str = "Done.") -> None:
        self.content = content
        self.seen: list[list[dict]] = []

    async def chat(self, role=None, messages=None, tools=None, **kwargs):
        self.seen.append(list(messages))
        return {
            "content": self.content,
            "tool_calls": [],
            "backend": "local",
            "model": "llama3.1:8b",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }


class _StubMCP:
    def __init__(self, result=None) -> None:
        self.result = result

    async def get_all_manifests(self):
        return [
            {
                "type": "function",
                "function": {"name": n, "description": n, "parameters": {}},
            }
            for n in ANALYST_AGENT.tools
        ]

    def filter_tools(self, manifests, names):
        allowed = set(names)
        return [t for t in manifests if t["function"]["name"] in allowed]

    async def call_tool(self, tool_name, arguments):
        return self.result


@pytest.mark.asyncio
async def test_the_system_prompt_states_todays_date():
    llm = _OneShotLLM()
    await AgentRunner(llm_client=llm, mcp_client=_StubMCP()).run(
        agent_def=ANALYST_AGENT, query="Sentiment trends?"
    )
    prompt = llm.seen[0][0]["content"]
    assert _TODAY.isoformat() in prompt
    assert "OMIT the date arguments" in prompt
    assert "training data" in prompt


# ---------------------------------------------------------------------------
# 3. An empty result must read as "no data", not as "nothing"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("empty", [[], {}, "", None], ids=["list", "dict", "str", "none"])
def test_empty_results_are_named(empty):
    note = _describe_empty(empty)
    assert note is not None and "EMPTY RESULT" in note


@pytest.mark.parametrize(
    "populated", [[{"period": "2026-08-01"}], {"total": 1}, "rows", 0, False],
    ids=["list", "dict", "str", "zero", "false"],
)
def test_populated_results_are_not_named(populated):
    assert _describe_empty(populated) is None


def test_the_empty_note_tells_the_model_not_to_fill_the_gap():
    note = _describe_empty([])
    assert "Do NOT invent" in note
    assert "window" in note


def test_the_note_sits_inside_the_untrusted_delimiters():
    """It is operator commentary, but the model only reads it in the data block."""
    wrapped = _wrap_tool_result("trend_query", "[]", note=_describe_empty([]))
    assert wrapped.startswith('<tool_data source="trend_query" trust="untrusted">')
    assert "EMPTY RESULT" in wrapped
    # A payload cannot forge the note's position: the wrapper still closes once.
    assert wrapped.count("</tool_data>") == 1


@pytest.mark.asyncio
async def test_an_empty_tool_result_reaches_the_model_as_words():
    llm = _OneShotLLM(content="No data in range.")
    mcp = _StubMCP(result=[])
    runner = AgentRunner(llm_client=llm, mcp_client=mcp)

    # Drive one tool call, then let the model answer.
    class _ToolThenAnswer(_OneShotLLM):
        async def chat(self, role=None, messages=None, tools=None, **kwargs):
            self.seen.append(list(messages))
            if len(self.seen) == 1:
                return {
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "trend_query", "arguments": "{}"},
                    }],
                    "backend": "local", "model": "llama3.1:8b",
                    "usage": {"total_tokens": 10},
                }
            return {
                "content": "No data in range.", "tool_calls": [],
                "backend": "local", "model": "llama3.1:8b",
                "usage": {"total_tokens": 10},
            }

    llm = _ToolThenAnswer()
    runner = AgentRunner(llm_client=llm, mcp_client=mcp)
    run = await runner.run(agent_def=ANALYST_AGENT, query="Trends?")

    tool_msg = [m for m in llm.seen[-1] if m.get("role") == "tool"][0]
    assert "EMPTY RESULT" in tool_msg["content"]
    assert run.status == "completed"


# ---------------------------------------------------------------------------
# 4. Citations the model invents
# ---------------------------------------------------------------------------

_TOOL_OUTPUT = '[{"post_id": "cm0abcdef12345678901234", "total_reactions": 5000}]'


def test_the_fabricated_citations_from_the_live_run_are_caught():
    answer = (
        "Post Citations:\n\n"
        'Post ID 1234567890 (January 1, 2023): "I\'m so excited for this new year!"\n'
        'Post ID 9876543210 (December 31, 2023): "This year was a disaster!"'
    )
    assert _unverified_citations(answer, _TOOL_OUTPUT) == ["1234567890", "9876543210"]


def test_a_citation_the_tool_returned_is_accepted():
    answer = "Reactions peaked on post cm0abcdef12345678901234 (5,000 total)."
    assert _unverified_citations(answer, _TOOL_OUTPUT) == []


def test_short_numeric_ids_are_verified_against_raw_tool_output_not_the_cuid_list():
    """Facebook post ids are numeric — `run.citations` would never contain them."""
    numeric_output = '[{"post_id": "1234567890", "reactions": 12}]'
    answer = "Post ID 1234567890 drew 12 reactions."
    assert _unverified_citations(answer, numeric_output) == []


def test_a_cuid_never_returned_by_any_tool_is_flagged():
    answer = "See post cm0deadbeef98765432100 for the spike."
    assert _unverified_citations(answer, _TOOL_OUTPUT) == ["cm0deadbeef98765432100"]


def test_a_markdown_table_header_is_not_a_citation():
    answer = "| post_id | positive |\n|---|---|\n| cm0abcdef12345678901234 | 3100 |"
    assert _unverified_citations(answer, _TOOL_OUTPUT) == []


def test_an_answer_with_no_citations_is_clean():
    assert _unverified_citations("Sentiment is 62% positive.", _TOOL_OUTPUT) == []


@pytest.mark.asyncio
async def test_ungrounded_citations_are_flagged_on_the_run_and_in_the_answer():
    """The operator reads the briefing, not the run record — flag both."""
    llm = _OneShotLLM(content='Post ID 1234567890: "This year was a disaster!"')
    run = await AgentRunner(llm_client=llm, mcp_client=_StubMCP()).run(
        agent_def=ANALYST_AGENT, query="Top posts?"
    )

    assert run.unverified_citations == ["1234567890"]
    assert "Unverified citations" in run.answer
    assert "1234567890" in run.answer
    # The original text is preserved — the flag is an annotation, not a redaction.
    assert 'This year was a disaster!' in run.answer


@pytest.mark.asyncio
async def test_a_grounded_answer_is_left_untouched():
    llm = _OneShotLLM(content="Reactions peaked on post cm0abcdef12345678901234.")

    class _Grounded(_StubMCP):
        async def call_tool(self, tool_name, arguments):
            return {"post_id": "cm0abcdef12345678901234", "total_reactions": 5000}

    # Force one tool call so the id enters the verified corpus.
    class _ToolThenCite(_OneShotLLM):
        async def chat(self, role=None, messages=None, tools=None, **kwargs):
            self.seen.append(list(messages))
            if len(self.seen) == 1:
                return {
                    "content": "",
                    "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "top_posts", "arguments": "{}"},
                    }],
                    "backend": "local", "model": "m", "usage": {"total_tokens": 5},
                }
            return {
                "content": "Reactions peaked on post cm0abcdef12345678901234.",
                "tool_calls": [], "backend": "local", "model": "m",
                "usage": {"total_tokens": 5},
            }

    run = await AgentRunner(llm_client=_ToolThenCite(), mcp_client=_Grounded()).run(
        agent_def=ANALYST_AGENT, query="Top posts?"
    )

    assert run.unverified_citations == []
    assert run.answer == "Reactions peaked on post cm0abcdef12345678901234."
    assert "cm0abcdef12345678901234" in run.citations


# ---------------------------------------------------------------------------
# 5. Stub mode is a deployment choice, not a launcher one
# ---------------------------------------------------------------------------

def test_the_launcher_does_not_hardcode_stub_mode():
    """run_all.py forced ANALYTICS_MCP_STUB=true, overriding .env's false."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "run_all.py").read_text()
    assert '"ANALYTICS_MCP_STUB": "true"' not in src
    assert '"RETRIEVAL_MCP_STUB": "true"' not in src
    assert '_env_flag(env, "ANALYTICS_MCP_STUB")' in src
    assert '_env_flag(env, "RETRIEVAL_MCP_STUB")' in src


def test_env_flag_prefers_the_process_env_then_dotenv_then_default():
    import run_all

    assert run_all._env_flag({"ANALYTICS_MCP_STUB": "true"}, "ANALYTICS_MCP_STUB") == "true"
    # .env in this repo sets it to false; an unset, unknown key falls to default.
    assert run_all._env_flag({}, "NO_SUCH_FLAG_XYZ") == "false"
    assert run_all._env_flag({}, "NO_SUCH_FLAG_XYZ", default="true") == "true"
