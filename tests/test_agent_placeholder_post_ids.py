"""The Spike Alerting run that wrote `[insert post ID]` and shipped as completed.

Run ``d50380ad``, Spike Alerting, llama3.1:8b-16k, 16,040 tokens. Its four tool
calls were, verbatim from the stored trace:

  1. ``trend_query(granularity=day, metric=avg_sentiment)``  -> ok, 14 rows
  2. ``sentiment_over_time(granularity=day)``                -> ok, 14 rows
  3. ``sentiment_over_time(granularity=day)``                -> skipped, repeat of 2
  4. ``sentiment_over_time(granularity=day)``                -> skipped, repeat of 2

Two repeats withdrew the tools (``_MAX_REPEATED_CALLS``), so the run wrote its
answer holding nothing but two tables of per-day sentiment counts. ``top_posts``
— the only tool in that agent's roster that returns a post_id — was never
called, and the prompt told it to produce "Key Trigger Posts with IDs and
citations". It wrote the section as ``Post ID: [insert post ID]`` twice, with an
invented comment quote under each.

Four separate things were wrong, and this file is one test per thing:

  1. ``_answers_with_placeholders`` did not fire, so the run was recorded
     ``completed``, no synthesis retry ran, and the template reached an operator;
  2. ``_CITED_ID_RE`` read the word ``insert`` as a cited post ID, so the warning
     block reported ``insert`` as an ungrounded citation;
  3. the model copied its own ``<tool_data source="…" trust="…">`` framing into
     the prose, and ``_QUOTED_SPAN_RE`` paired quote characters from the prose
     into those attributes — reporting ``(cited from <tool_data source=`` as
     ungrounded comment text, and swallowing one of the two REAL fabricated
     quotes in the process;
  4. ``_repeat_note`` said "call a DIFFERENT tool" without naming one.

No Ollama, Redis or MCP sidecar needed.
"""

import sys

sys.path.insert(0, "/home/bk/code/defense/src/defense")

from defense.services.agents.registry import ALERTING_AGENT
from defense.services.agents.runner import (
    _answers_with_placeholders,
    _denies_retrieved_posts,
    _is_non_answer,
    _repeat_note,
    _unverified_citations,
    _unverified_quotes,
)

# The trigger-posts section of the stored answer, byte for byte.
_TRIGGER_SECTION = (
    "Key Trigger Posts with IDs and citations\n"
    "* Post ID: [insert post ID]\n"
    "\t+ \"This is a terrible product. I'm so disappointed.\" "
    '(cited from <tool_data source="sentiment_over_time" trust="untrusted">)\n'
    "* Post ID: [insert post ID]\n"
    "\t+ \"I'm sick of this company's lies. They're just trying to scam us.\" "
    '(cited from <tool_data source="sentiment_over_time" trust="untrusted">)\n\n'
    "Note: The exact post IDs and citations are not provided as they were not "
    "included in the original output."
)

# What the run actually retrieved: two tools' worth of per-day sentiment counts.
# No post_id, no comment text, anywhere in it.
_TOOL_OUTPUT = (
    '{"rows": [{"period": "2026-05-02T00:00:00", "positive": 0, "negative": 1, '
    '"neutral": 0, "mixed": 0}, {"period": "2026-05-05T00:00:00", "positive": 0, '
    '"negative": 3, "neutral": 0, "mixed": 0}]}'
)


# ---------------------------------------------------------------------------
# 1. The placeholder that named the field it stood in for
# ---------------------------------------------------------------------------
# `_PLACEHOLDER_ANSWER_RE` required the closing bracket to follow the verb
# immediately (`\[?(?:insert|…)[\]:]`), so the bare "[insert]" in the existing
# tests was caught while "[insert post ID]" — the form a model writes, because
# the placeholder describes what belongs there — was not.


def test_a_placeholder_that_names_its_field_is_still_a_placeholder():
    assert _answers_with_placeholders("Post ID: [insert post ID]")
    assert _answers_with_placeholders(_TRIGGER_SECTION)
    assert _is_non_answer(_TRIGGER_SECTION)


def test_the_shapes_a_model_writes_instead_of_a_value():
    for claim in (
        "Post ID: [insert post ID]",
        "| [insert post_id] | 412 |",
        "Trigger post: <insert post id here>",
        "Toxicity: [TBD]",
        "Reach: [to be determined from trend_query]",
        "Author: [your value]",
        "Comment: [fill in from search_comments]",
        "Spike date: [N/A]",
    ):
        assert _answers_with_placeholders(claim), claim


def test_a_bracketed_recommendation_is_not_a_placeholder():
    """A hit fails the run, so bracketed imperatives must not trigger one."""
    for recommendation in (
        "Recommended: [Add a campaign filter to narrow the window].",
        "Next step: [Enter the watchlist id before re-running].",
    ):
        assert not _answers_with_placeholders(recommendation), recommendation


def test_a_briefing_that_reports_real_values_is_not_a_template():
    """The guard must not fire on the metrics table this same run got right."""
    real = (
        "## Alert Level & Immediate Threat Assessment\nAlert Level: Elevated\n\n"
        "| Period | Positive | Negative |\n|---|---|---|\n"
        "| 2026-05-05T00:00:00 | 0 | 3 |\n| 2026-05-06T00:00:00 | 0 | 3 |\n\n"
        "No trigger posts identified: no post-level data was retrieved."
    )
    assert not _answers_with_placeholders(real)
    assert not _is_non_answer(real)


# ---------------------------------------------------------------------------
# 2. "insert" is not a fabricated citation
# ---------------------------------------------------------------------------
# `_CITED_ID_RE` matches 6+ characters after the words "post id". "insert" is
# exactly six, so the operator's warning block read: 'These post/comment IDs
# appear above but were not returned by any tool call in this run: `insert`.'
# Nothing was cited — the answer is a template, which is a different and much
# worse finding, and test 1 is what reports it.


def test_a_placeholder_is_not_reported_as_a_fabricated_id():
    assert _unverified_citations(_TRIGGER_SECTION, _TOOL_OUTPUT) == []


def test_a_genuinely_invented_id_is_still_caught():
    """Stripping placeholders must not blunt the check it shares a regex with."""
    fabricated = "Trigger post: Post ID: 1234567890 drove the spike."
    assert _unverified_citations(fabricated, _TOOL_OUTPUT) == ["1234567890"]


def test_a_placeholder_next_to_a_real_id_hides_neither():
    mixed = "Post ID: [insert post ID]\nPost ID: 9876543210\n"
    assert _unverified_citations(mixed, _TOOL_OUTPUT) == ["9876543210"]


# ---------------------------------------------------------------------------
# 3. Leaked <tool_data> framing is quote characters, not quotation
# ---------------------------------------------------------------------------
# The model reads every tool result inside <tool_data source="…" trust="…">, and
# here it copied that framing into its prose. Those attribute quotes pair with
# the prose quotes, so `_QUOTED_SPAN_RE` returned spans running from one line
# into the next — and because the garbage span consumed the closing quote of the
# first fabricated quote, the SECOND fabricated quote went unreported entirely.
# The noise was not just noise: it was hiding a fabrication.


def test_leaked_tool_data_tags_are_not_reported_as_comment_text():
    flagged = _unverified_quotes(_TRIGGER_SECTION, _TOOL_OUTPUT)
    for span in flagged:
        assert "tool_data" not in span, span
        assert "insert post ID" not in span, span


def test_both_fabricated_quotes_are_reported():
    """Neither tool in this run returned any comment text, so both are invented."""
    flagged = _unverified_quotes(_TRIGGER_SECTION, _TOOL_OUTPUT)
    assert flagged == [
        "This is a terrible product. I'm so disappointed.",
        "I'm sick of this company's lies. They're just trying to scam us.",
    ]


def test_a_quote_a_tool_did_return_is_still_grounded():
    answer = 'One commenter wrote "the road has been closed for three weeks now".'
    returned = '{"comments": [{"text": "The road has been closed for three weeks now!"}]}'
    assert _unverified_quotes(answer, returned) == []


# ---------------------------------------------------------------------------
# 4. Telling the model where to go instead of the loop
# ---------------------------------------------------------------------------
# Calls 3 and 4 repeated call 2 byte for byte. The note it got each time said
# "Either call a DIFFERENT tool or different arguments, or answer" — true, and
# useless to an 8B model that has to pick the tool itself. Naming the untried
# ones puts `top_posts` in front of it while it still has budget to call it.


def test_the_repeat_note_names_the_tools_not_yet_called():
    note = _repeat_note("sentiment_over_time", 2, untried=["top_posts"])
    assert "already ran as call #2" in note
    assert "`top_posts`" in note
    assert "post IDs" in note


def test_the_repeat_note_survives_with_nothing_left_to_suggest():
    """Every tool tried: the note must still say the call did not run."""
    for untried in (None, []):
        note = _repeat_note("sentiment_over_time", 2, untried=untried)
        assert "NOT run again" in note
        assert "answer the operator's question" in note.lower()
        assert "not yet called" not in note.lower()


# ---------------------------------------------------------------------------
# 5. The roster the prompt is written against
# ---------------------------------------------------------------------------
# The prompt now asserts that `top_posts` is the ONLY tool this agent has that
# returns post_ids. That claim is about the roster, so it breaks silently if the
# roster changes — this is the test that notices.


def test_the_alerting_prompt_names_the_only_tool_that_returns_post_ids():
    assert ALERTING_AGENT.tools == ["trend_query", "sentiment_over_time", "top_posts"]
    prompt = ALERTING_AGENT.system_prompt
    assert "top_posts" in prompt
    assert "only tool that returns post_ids" in prompt.lower()
    # And an out that is not a placeholder, for when it was not called.
    assert "No trigger posts identified" in prompt
    assert "[insert post ID]" in prompt  # named as the thing never to write


# ---------------------------------------------------------------------------
# 6. The toxicity verdict, routed to the tool that measures toxicity
# ---------------------------------------------------------------------------
# The run after the post-ID fix returned "There are no sudden spikes in negative
# sentiment (>50% neg) or elevated toxicity", and a run before it, on identical
# data from identical tools, returned the opposite. Neither had grounds:
# `sentiment_over_time` carries no toxicity field, and `trend_query` — which
# does, on every row, whatever `metric` is passed — was called for
# `avg_sentiment` and its `avg_toxicity` column never read.
#
# These assert on FACTS the prompt has to carry, not on its phrasing: the first
# draft pinned exact sentences and every one of them broke on the next reword,
# which teaches nothing about whether the prompt is still correct.


def _prompt() -> str:
    return ALERTING_AGENT.system_prompt


def test_the_prompt_names_the_column_the_toxicity_threshold_lives_in():
    assert "avg_toxicity" in _prompt()


def test_the_prompt_says_one_trend_query_call_covers_both_measures():
    """The observed runs spent a second call re-asking for rows they had."""
    p = _prompt()
    assert "avg_sentiment" in p and "avg_toxicity" in p
    assert "do not call it again" in p.lower() or "one call" in p.lower()


def test_the_prompt_says_which_tool_has_no_toxicity_field():
    p = _prompt()
    assert "sentiment_over_time" in p
    assert "no toxicity field" in p.lower()


def test_the_toxicity_line_is_required_and_needs_its_figure():
    """Keying the out on "trend_query was not called" made the model drop the
    topic instead: three live runs called trend_query — so the out did not apply
    — and none of the three mentioned toxicity at all."""
    p = _prompt()
    assert "required" in p.lower()
    assert "without its figure" in p.lower() or "with the peak" in p.lower()
    assert "not assessed" in p


def test_the_prompt_keeps_toxicity_and_negative_sentiment_apart():
    """Both wrong verdicts read a sentiment table and concluded about toxicity."""
    assert "different measurements" in _prompt().lower()


def test_the_prompt_rules_out_the_invalid_metric_the_model_keeps_passing():
    """`top_posts(metric="overall_sentiment")` errored in 3 of 9 live runs. It is
    a row field, not one of the four ranking metrics — and a rejected call is a
    call spent."""
    from defense.mcp_servers.analytics_mcp.server import top_posts

    p = _prompt()
    assert "not a metric" in p.lower() or "not a ranking metric" in p.lower()
    # The metrics the prompt promises must be exactly the ones the tool takes.
    allowed = _literal_values(top_posts, "metric")
    for metric in allowed:
        assert f'"{metric}"' in p, metric
    assert "overall_sentiment" not in allowed


def test_the_budget_covers_the_tools_the_prompt_asks_for():
    """A live run reached the old cap of 5 and returned "[Budget cap of 5 tool
    calls reached]" as the briefing. The prompt routes across all three tools and
    a duplicate call still spends the count."""
    assert ALERTING_AGENT.max_tool_calls >= len(ALERTING_AGENT.tools) * 2


def test_the_prompt_stayed_short_enough_to_be_followed():
    """Piling rules on made an 8B model worse, not better: at 3,883 characters
    one live run dropped both required verdict lines and another hit the budget
    cap. This is a ratchet against re-inflating it."""
    assert len(_prompt()) < 2500, len(_prompt())


# ---------------------------------------------------------------------------
# 7. The tool contract the prompt is written against
# ---------------------------------------------------------------------------


def _literal_values(fn, param: str) -> set:
    """The values a Literal-annotated parameter accepts.

    Via get_type_hints, not inspect.signature: analytics_mcp/server.py defers
    annotation evaluation, so signature() hands back the string
    "Literal['post_count', ...]" and every membership test on it passes
    vacuously — which is how the first draft of these tests "passed".
    """
    from typing import get_args, get_type_hints

    hint = get_type_hints(fn, include_extras=False)[param]
    return set(get_args(hint))


def test_top_posts_can_actually_rank_by_the_metrics_the_prompt_names():
    """The prompt tells the model to pass these; the tool must accept them."""
    import inspect

    from defense.mcp_servers.analytics_mcp.server import top_posts

    assert "min_toxicity" in inspect.signature(top_posts).parameters
    allowed = _literal_values(top_posts, "metric")
    assert {"toxicity_score", "hate_speech_score", "total_reactions"} <= allowed


def test_trend_query_still_carries_toxicity():
    """The whole routing rests on this; it is one Literal away from silent drift."""
    from defense.mcp_servers.analytics_mcp.server import trend_query

    assert "avg_toxicity" in (trend_query.__doc__ or "")
    assert "avg_toxicity" in _literal_values(trend_query, "metric")


def test_sentiment_over_time_still_has_no_toxicity_to_read():
    """The prompt tells the model this tool cannot support a toxicity claim."""
    from defense.mcp_servers.analytics_mcp.server import sentiment_over_time

    assert "toxicity" not in (sentiment_over_time.__doc__ or "").lower()
