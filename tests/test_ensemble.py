"""The comment-label ensemble: combination, abstention, escalation, and the
single-writer rule for the aggregates.

Three defects motivate this file, and each has a test named after it:

  * the LLM/XLM-R/DistilBERT verdicts were written to `parallel_labels` and
    never became the comment's label, so a post reported one set of counts
    while every comment in the list showed another;
  * `sentiment_breakdown` was rewritten from LLM votes only, so its total no
    longer equalled the comment count and abstentions were invisible;
  * a source that could not run contributed a fabricated `neutral`, which is
    indistinguishable from a model that read the comment and judged it neutral.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from defense.libs import comment_groups
from defense.libs.ensemble import (
    UNCERTAIN,
    agreement_summary,
    cheap_disagreement,
    combine,
    should_escalate,
)


# ---------------------------------------------------------------------------
# combine()
# ---------------------------------------------------------------------------

def test_unanimous_voters_produce_full_agreement():
    v = combine({
        "heuristic": {"sentiment": "positive", "sentiment_score": 0.5},
        "xlmr": {"sentiment": "positive", "score": 0.9},
        "distilbert": {"sentiment": "positive", "score": 0.8},
    })
    assert v.label == "positive"
    assert v.agreement == 1.0
    assert v.unanimous is True
    assert v.abstained is False


def test_majority_wins_and_agreement_reports_the_margin():
    v = combine({
        "heuristic": {"sentiment": "positive"},
        "xlmr": {"sentiment": "positive"},
        "distilbert": {"sentiment": "negative"},
    })
    assert v.label == "positive"
    assert v.agreement == pytest.approx(2 / 3)
    assert v.unanimous is False


def test_a_split_abstains_rather_than_picking_a_side():
    """An even split is not a neutral comment — it is an unlabelled one."""
    v = combine({
        "heuristic": {"sentiment": "positive"},
        "xlmr": {"sentiment": "negative"},
    })
    assert v.label == UNCERTAIN
    assert v.abstained is True
    # Deliberately NOT "neutral": that would be a verdict about the comment.
    assert v.label != "neutral"


def test_the_llm_breaks_an_exact_tie_because_it_is_the_one_with_context():
    """With four voters a 2-2 split is common. The LLM is the only labeller
    that sees the post, so it decides — and the verdict records that it was a
    tie-break rather than agreement."""
    v = combine({
        "heuristic": {"sentiment": "positive"},
        "distilbert": {"sentiment": "positive"},
        "xlmr": {"sentiment": "negative"},
        "llm": {"sentiment": "negative"},
    })
    assert v.label == "negative"
    assert v.tie_broken_by == "llm"
    assert v.abstained is False
    # It is NOT dressed up as consensus.
    assert v.agreement == 0.5
    assert v.unanimous is False


def test_a_plurality_the_llm_is_part_of_stands():
    """2-1-1 with four voters: half the room backs it, and the half includes
    the labeller that read the post."""
    v = combine({
        "heuristic": {"sentiment": "positive"},
        "distilbert": {"sentiment": "neutral"},
        "xlmr": {"sentiment": "negative"},
        "llm": {"sentiment": "negative"},
    })
    assert v.label == "negative"
    assert v.tie_broken_by == "llm"
    assert v.agreement == 0.5


def test_a_plurality_the_llm_dissents_from_abstains():
    """The LLM alone against three is not half the room — no label is claimed."""
    v = combine({
        "heuristic": {"sentiment": "positive"},
        "distilbert": {"sentiment": "neutral"},
        "xlmr": {"sentiment": "neutral"},
        "llm": {"sentiment": "negative"},
    })
    assert v.label == UNCERTAIN


def test_a_tie_the_llm_did_not_vote_in_still_abstains():
    v = combine({
        "heuristic": {"sentiment": "positive"},
        "xlmr": {"sentiment": "negative"},
    })
    assert v.label == UNCERTAIN
    assert v.tie_broken_by is None


def test_the_tie_break_can_be_turned_off():
    v = combine(
        {
            "heuristic": {"sentiment": "positive"},
            "llm": {"sentiment": "negative"},
        },
        prefer_on_tie=None,
    )
    assert v.label == UNCERTAIN


def test_a_source_that_did_not_run_is_not_a_voter():
    """Absence must not be counted as agreement, or as a neutral vote."""
    v = combine({
        "heuristic": {"sentiment": "positive"},
        "xlmr": {},                                  # ran, produced nothing
        "distilbert": {"sentiment": None},           # unmappable label
    })
    assert v.voters == 1
    assert v.label == "positive"
    assert v.agreement == 1.0


def test_no_voters_at_all_is_uncertain_not_neutral():
    v = combine({})
    assert v.label == UNCERTAIN
    assert v.voters == 0
    assert v.abstained is True


def test_out_of_taxonomy_labels_are_discarded():
    v = combine({"xlmr": {"sentiment": "LABEL_0"}, "heuristic": {"sentiment": "positive"}})
    assert v.voters == 1
    assert v.label == "positive"


def test_score_is_the_mean_of_the_winning_voters():
    v = combine({
        "heuristic": {"sentiment": "negative", "sentiment_score": -0.4},
        "xlmr": {"sentiment": "negative", "score": -0.8},
        "distilbert": {"sentiment": "positive", "score": 0.9},
    })
    assert v.label == "negative"
    assert v.score == pytest.approx(-0.6)


# ---------------------------------------------------------------------------
# should_escalate() — the cost lever
# ---------------------------------------------------------------------------

def test_cheap_consensus_does_not_reach_the_llm():
    escalate, reason = should_escalate({
        "heuristic": {"sentiment": "positive"},
        "xlmr": {"sentiment": "positive"},
    })
    assert escalate is False
    assert reason == "cheap_consensus"


def test_cheap_disagreement_escalates():
    escalate, reason = should_escalate({
        "heuristic": {"sentiment": "positive"},
        "xlmr": {"sentiment": "negative"},
    })
    assert escalate is True
    assert reason == "cheap_disagreement"


def test_too_few_cheap_voters_escalates_with_its_own_reason():
    """Missing evidence and conflicting evidence are different problems."""
    escalate, reason = should_escalate({"heuristic": {"sentiment": "positive"}})
    assert escalate is True
    assert reason == "insufficient_cheap_voters"


def test_a_watchlist_mention_always_escalates():
    escalate, reason = should_escalate(
        {"heuristic": {"sentiment": "positive"}, "xlmr": {"sentiment": "positive"}},
        mentions_watchlist=True,
    )
    assert escalate is True
    assert reason == "watchlist_mention"


@pytest.mark.parametrize("kind", ["emoji", "filtered", "link"])
def test_textless_comments_never_reach_the_llm(kind):
    """There is nothing in them for a model to read — the cheapest wasted tokens."""
    escalate, reason = should_escalate({}, kind=kind)
    assert escalate is False
    assert reason == "no_text"


def test_cheap_disagreement_helper_ignores_the_llm_vote():
    labels = {
        "heuristic": {"sentiment": "positive"},
        "xlmr": {"sentiment": "positive"},
        "llm": {"sentiment": "negative"},
    }
    assert cheap_disagreement(labels) is False


# ---------------------------------------------------------------------------
# agreement_summary()
# ---------------------------------------------------------------------------

def test_agreement_summary_reports_the_share_the_llm_never_needed_to_see():
    verdicts = [
        combine({"heuristic": {"sentiment": "positive"}, "xlmr": {"sentiment": "positive"}}),
        combine({"heuristic": {"sentiment": "positive"}, "xlmr": {"sentiment": "positive"}}),
        combine({"heuristic": {"sentiment": "positive"}, "xlmr": {"sentiment": "negative"}}),
    ]
    s = agreement_summary(verdicts)
    assert s["comments"] == 3
    assert s["unanimous"] == 2
    assert s["unanimous_share"] == pytest.approx(2 / 3, abs=1e-4)
    assert s["abstained"] == 1


def test_agreement_summary_of_nothing_is_zero_not_a_crash():
    assert agreement_summary([])["comments"] == 0


# ---------------------------------------------------------------------------
# Near-duplicate grouping
# ---------------------------------------------------------------------------

def test_punctuation_and_case_do_not_make_a_new_opinion():
    groups, stats = comment_groups.group_indices(
        ["Nice post!", "nice post", "NICE POST...", "something else"]
    )
    assert stats["duplicates"] == 2
    assert groups == {0: [1, 2]}


def test_bangla_duplicates_group_too():
    groups, stats = comment_groups.group_indices(["ভালো", "ভালো!", "ভালো "])
    assert stats["duplicates"] == 2


def test_emoji_only_comments_are_never_grouped():
    """Their normalisation is empty; grouping them would propagate one label
    across every emoji reaction on the post."""
    groups, stats = comment_groups.group_indices(["❤️", "🤬", "😂😂"])
    assert groups == {}
    assert stats["duplicates"] == 0


def test_singletons_are_not_groups():
    groups, _ = comment_groups.group_indices(["a unique thought", "another one"])
    assert groups == {}


def test_representative_of_maps_members_back():
    groups, _ = comment_groups.group_indices(["same", "same", "same", "other"])
    assert comment_groups.representative_of(groups) == {1: 0, 2: 0}
