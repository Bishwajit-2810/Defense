"""The context-window overflow behind two "completed" runs that answered nothing.

Both runs asked the Comparative agent "Compare comment sentiment distribution
with post caption tone." Both called ``semantic_search`` with ``limit=50`` over a
32-post corpus, which serialised to 76,666 characters — ~39,000 tokens, because
``json.dumps`` escaped every Bengali character to ``\\uXXXX``. The local model
runs on Ollama, which silently discards a prompt over its context window, oldest
messages first, and reports only the tokens it actually evaluated.

So the system prompt and the operator's question fell out of the context, and
each run answered from whatever survived at the tail:

  * the first walked through the JSON's field names ("The ``embedding_is_stub``
    field is set to True for all posts, indicating…" — which inverts what the
    flag means);
  * the second opened "The original user question was not provided" and briefed
    the operator on a ``top_posts`` argument error instead.

Both were recorded ``completed``, the second with 32 citations attached. Four
things had to be true at once, and each gets its own tests here:

  1. tool results were serialised with ``ensure_ascii=True``, doubling the token
     cost of a Bengali corpus for no added information;
  2. nothing capped a single result, so one call could exceed any window;
  3. the question was never restated, so it aged out of the context;
  4. the non-answer guards did not fire on either shape.

No Ollama, Redis or MCP sidecar needed.
"""

import json
import sys

sys.path.insert(0, "/home/bk/code/defense/src/defense")

from defense.services.agents.runner import (
    _MAX_REPEATED_CALLS,
    _MAX_TOOL_RESULT_CHARS,
    _call_signature,
    _repeat_note,
    _answers_about_the_run,
    _answers_with_placeholders,
    _cap_tool_result,
    _describes_the_payload,
    _dumps,
    _ids_from_result,
    _is_non_answer,
    _question_reminder,
)

# A row shaped like semantic_search's, with Bengali text in it — the escaping is
# the whole point, so the text has to be real Bengali.
_BANGLA = "তারেক রহমানের বিরুদ্ধে এই অভিযোগ সম্পূর্ণ মিথ্যা এবং রাজনৈতিক উদ্দেশ্যপ্রণোদিত"


def _row(i: int) -> dict:
    return {
        "post_id": f"cmp{i:022d}",
        "campaign_id": "cmold8r5301u8fu22m7flh3pc",
        "overall_sentiment": "negative",
        "post_summary": _BANGLA,
        "matched_chunk": _BANGLA,
        "embedding_is_stub": True,
        "score": 0.0163,
    }


# ---------------------------------------------------------------------------
# 1. Serialisation
# ---------------------------------------------------------------------------


def test_bangla_is_not_escaped_into_six_bytes_per_character():
    """The escaping was half the token bill of every tool result in this corpus."""
    rows = [_row(i) for i in range(8)]
    escaped = json.dumps(rows, default=str)          # what the runner used to do
    ours = _dumps(rows)

    assert "\\u09a4" in escaped                       # ত, escaped
    assert "\\u" not in ours
    assert _BANGLA in ours                            # readable, and searchable
    # Not a marginal saving: the escape form is multiples of the plain one.
    assert len(ours) < len(escaped) / 2


def test_dumps_still_survives_unserialisable_values():
    """`default=str` has to stay: rows carry dates and Decimals."""
    from datetime import date

    assert "2026-08-19" in _dumps({"period": date(2026, 8, 19)})


# ---------------------------------------------------------------------------
# 2. The per-result cap
# ---------------------------------------------------------------------------


def test_a_small_result_is_passed_through_untouched():
    rows = [_row(0)]
    kept, text, note = _cap_tool_result(rows)
    assert kept == rows
    assert note is None
    assert json.loads(text) == rows


def test_an_oversized_list_is_trimmed_to_whole_rows():
    kept, text, note = _cap_tool_result([_row(i) for i in range(200)])

    assert len(kept) < 200
    assert len(text) <= _MAX_TOOL_RESULT_CHARS
    # Whole rows, so what reaches the model is still valid JSON it can reason
    # over rather than a JSON object cut in half.
    assert json.loads(text) == kept
    assert note and "TRUNCATED" in note


def test_the_model_is_told_how_much_it_is_not_seeing():
    """A silently shortened list reads as a complete answer to the query."""
    kept, _, note = _cap_tool_result([_row(i) for i in range(200)])

    assert f"showing {len(kept)} of 200 rows" in note
    assert "not the whole result set" in note.lower()
    # And what to do about it, since the model is the one that can fix it.
    assert "limit" in note


def test_a_row_too_big_to_send_whole_does_not_bust_the_budget():
    """There is no whole-row subset to send, so the text is cut instead.

    The alternative — returning the row anyway — would defeat the cap in the one
    case it matters most, which is how the window overflowed to begin with.
    """
    fat = [{"post_id": "cmp1", "text": "x" * (_MAX_TOOL_RESULT_CHARS * 2)}]
    kept, text, note = _cap_tool_result(fat)

    assert len(text) <= _MAX_TOOL_RESULT_CHARS
    assert kept is None                    # so nothing past the cut is cited
    assert note and "TRUNCATED" in note


def test_an_oversized_dict_reports_ids_from_the_text_it_actually_sent():
    """`kept` is None for a dict, so citations come off the truncated text.

    Crediting the answer with an id that was cut before it reached the model is
    the citation-inflation bug one layer down.
    """
    big = {"head": "x" * _MAX_TOOL_RESULT_CHARS, "post_id": "cmpTAILONLYaaaaaaaaaaaaaa"}
    kept, text, note = _cap_tool_result(big)

    assert kept is None
    assert len(text) <= _MAX_TOOL_RESULT_CHARS
    assert note and "TRUNCATED" in note
    posts, _ = _ids_from_result(kept, text)
    assert "cmpTAILONLYaaaaaaaaaaaaaa" not in posts


def test_citations_come_only_from_the_rows_that_were_shown():
    rows = [_row(i) for i in range(200)]
    kept, text, _ = _cap_tool_result(rows)

    posts, _ = _ids_from_result(kept, text)
    shown_ids = {r["post_id"] for r in kept}
    assert set(posts) <= shown_ids
    assert rows[-1]["post_id"] not in posts       # trimmed away, never cited


# ---------------------------------------------------------------------------
# 3. Keeping the question in view
# ---------------------------------------------------------------------------


def test_the_reminder_carries_the_question_verbatim():
    q = "Compare comment sentiment distribution with post caption tone."
    text = _question_reminder(q)
    assert q in text


def test_the_reminder_does_not_forbid_a_second_hop():
    """A run that still needs data must stay free to fetch it."""
    text = _question_reminder("anything").lower()
    assert "call another tool" in text
    # And it names the two failures it exists to prevent.
    assert "do not describe the tool output" in text
    assert "your own tool calls" in text


# ---------------------------------------------------------------------------
# 4. The guards, against the answers that actually shipped
# ---------------------------------------------------------------------------

# Verbatim from run 16f15178 — the field-by-field walkthrough.
_PAYLOAD_WALKTHROUGH = """The provided text is a list of social media posts with
their corresponding sentiment analysis results. The posts are in Bengali, and the
sentiment analysis is done using a machine learning model.

Here's a breakdown of the data:

Each post has a unique ID (post_id) and a campaign ID (campaign_id).
The overall_sentiment field indicates whether the post has a positive (+),
negative (-), or neutral (0) sentiment.
The embedding_is_stub field is set to True for all posts, indicating that the
model used a pre-trained language model as a starting point for its analysis.
The chunk_idx field is set to 0 for all posts, indicating that only one chunk was
identified in each post."""

# Verbatim from run 51238b57 — the briefing about its own tool error.
_SELF_BRIEFING = """## Section: Analytical Directive Response

The original user question was not provided. However, based on the tool call
response, I can infer that the operator is attempting to retrieve data using the
top_posts tool with an invalid metric.

The tool call failed because overall_sentiment is not a valid value for the
top_posts tool. The correct values are listed in the table above.

Please rephrase your question or provide more context so I can assist you better."""


def test_the_field_walkthrough_is_caught():
    """It tripped one pattern and needed two, so it shipped as a briefing."""
    assert _describes_the_payload(_PAYLOAD_WALKTHROUGH)
    assert _is_non_answer(_PAYLOAD_WALKTHROUGH)


def test_the_briefing_about_its_own_tool_error_is_caught():
    assert _answers_about_the_run(_SELF_BRIEFING)
    assert _is_non_answer(_SELF_BRIEFING)


def test_a_lost_question_is_conclusive_on_its_own():
    """Only a truncated context makes a model say the operator asked nothing."""
    for claim in (
        "The original user question was not provided.",
        "No question was provided, so I cannot proceed.",
        "You didn't provide a question for me to answer.",
        "I don't see a question in this conversation.",
        "This conversation just started, so there is nothing to compare.",
    ):
        assert _answers_about_the_run(claim), claim


def test_a_real_briefing_is_not_flagged():
    """The guards must not eat answers, including honest ones about missing data."""
    briefings = [
        # Numbers, a table, a citation.
        "## Target Stance Summary\n| Target | Supportive | Opposing |\n|---|---|---|\n"
        "| Tarek Rahman | 8.5% | 10.6% |\n\nAcross 7 posts, 47 mentions were scored; "
        "four fifths are neutral. Post Citations: cmoldbhw0026ffu22lt3nzj33.",
        # The honest "I cannot answer this half" shape the prompts ask for.
        "## Findings\nNo comment-level sentiment is reachable with the tools "
        "available to me: sentiment_over_time returns post counts, not comment "
        "labels. Post caption tone: 18 negative, 11 positive, 3 neutral.",
        # A methodology caveat that names fields and a failed call once each.
        "## Coverage Audit\nEvery embedding in the corpus is a deterministic stub, "
        "so semantic ranking is arbitrary and only the lexical arm is meaningful. "
        "The reaction_mix call failed, so engagement is missing. The following "
        "fields were unavailable: hate_speech_score for 4 posts.",
    ]
    for text in briefings:
        assert not _is_non_answer(text), text[:60]


# ---------------------------------------------------------------------------
# 5. The empty template
# ---------------------------------------------------------------------------
# From the run made AFTER the fixes above: 14 tool calls across 5 tools, 20,411
# prompt tokens of real data read — and then the plan written out instead of the
# findings. Structurally a briefing, which is what makes it dangerous.
_EMPTY_TEMPLATE = """## Comparison of Comment Sentiment Distribution and Post Caption Tone

### Top Posts by Comment Sentiment Distribution

| post_id | total_reactions | comment_count | overall_sentiment |
| --- | --- | --- | --- |
| ... | ... | ... | ... |

### Comparison

|  | Comment Sentiment | Post Caption Tone |
| --- | --- | --- |
| Positive | ... | ... |
| Negative | ... | ... |

Note: The actual values will be filled in based on the data retrieved from the
function calls."""


def test_a_briefing_with_no_findings_in_it_is_caught():
    assert _answers_with_placeholders(_EMPTY_TEMPLATE)
    assert _is_non_answer(_EMPTY_TEMPLATE)


def test_the_promise_to_fill_values_in_later_is_conclusive():
    for claim in (
        "Note: The actual values will be filled in based on the data retrieved.",
        "Values will be populated once the query returns.",
        "Sentiment: [insert] and toxicity: [TBD].",
    ):
        assert _answers_with_placeholders(claim), claim


def test_one_elided_row_is_not_a_template():
    """A real table may elide its tail; a table made only of ellipses may not."""
    real = (
        "## Findings\nOnly 623 of 8,698 comments carry an ensemble label.\n"
        "| post | comments |\n|---|---|\n| cmp1 | 1562 |\n| ... | ... |"
    )
    assert not _answers_with_placeholders(real)
    assert not _is_non_answer(real)


# ---------------------------------------------------------------------------
# 6. The loop the context fix uncovered
# ---------------------------------------------------------------------------
# With the window fixed the model stopped bailing out early and started looping:
# a live run spent 11 of its 15 calls on byte-identical
# `semantic_search(query="comment sentiment", campaign_id="all")` calls, hit the
# budget cap, and returned "[Budget cap of 15 tool calls reached]" as the
# briefing.


def test_absent_and_explicitly_null_arguments_are_the_same_call():
    """The exact shape the looping run produced: nulls on one turn, omitted the next."""
    a = {"query": "comment sentiment", "campaign_id": "all"}
    b = {"campaign_id": "all", "query": "comment sentiment", "to_date": None,
         "tenant_id": None, "sentiment_filter": None}
    assert _call_signature("semantic_search", a) == _call_signature("semantic_search", b)


def test_a_changed_argument_is_a_different_call():
    base = {"query": "comment sentiment", "campaign_id": "all"}
    narrowed = {**base, "sentiment_filter": "positive"}
    assert _call_signature("semantic_search", base) != _call_signature(
        "semantic_search", narrowed
    )
    assert _call_signature("top_posts", base) != _call_signature("semantic_search", base)


def test_the_repeat_note_says_it_did_not_run_and_what_to_do():
    note = _repeat_note("semantic_search", 3)
    assert "already ran as call #3" in note
    assert "NOT run again" in note
    # The two ways out, so the model is not left with only the loop.
    assert "different tool" in note.lower()
    assert "answer the operator's question" in note.lower()


def test_the_budget_is_never_the_only_thing_stopping_a_loop():
    """Tools are withdrawn after a couple of repeats, well inside any budget."""
    assert _MAX_REPEATED_CALLS < 5
