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
    CHEAP_SOURCES,
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
        "mbert": {"sentiment": "positive", "sentiment_score": 0.5},
        "xlmr": {"sentiment": "positive", "score": 0.9},
        "distilbert": {"sentiment": "positive", "score": 0.8},
    })
    assert v.label == "positive"
    assert v.agreement == 1.0
    assert v.unanimous is True
    assert v.abstained is False


def test_majority_wins_and_agreement_reports_the_margin():
    v = combine({
        "mbert": {"sentiment": "positive"},
        "xlmr": {"sentiment": "positive"},
        "distilbert": {"sentiment": "negative"},
    })
    assert v.label == "positive"
    assert v.agreement == pytest.approx(2 / 3)
    assert v.unanimous is False


def test_a_split_abstains_rather_than_picking_a_side():
    """An even split is not a neutral comment — it is an unlabelled one."""
    v = combine({
        "mbert": {"sentiment": "positive"},
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
        "mbert": {"sentiment": "positive"},
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


def test_a_plurality_wins_by_max_votes():
    """2-1-1 with four voters: the label with max votes wins."""
    v = combine({
        "mbert": {"sentiment": "positive"},
        "distilbert": {"sentiment": "neutral"},
        "xlmr": {"sentiment": "negative"},
        "llm": {"sentiment": "negative"},
    })
    assert v.label == "negative"
    assert v.agreement == 0.5


def test_max_votes_wins_even_if_llm_dissents():
    """2 neutral vs 1 positive vs 1 negative: neutral has max votes (2 > 1), so neutral wins."""
    v = combine({
        "mbert": {"sentiment": "positive"},
        "distilbert": {"sentiment": "neutral"},
        "xlmr": {"sentiment": "neutral"},
        "llm": {"sentiment": "negative"},
    })
    assert v.label == "neutral"
    assert v.agreement == 0.5


def test_comment_4_neg_3_neu_1_pos_wins_negative():
    """User case: 4 negative vs 3 neutral vs 1 positive -> negative wins by max vote."""
    v = combine({
        "xlmr": {"sentiment": "negative"},
        "distilbert": {"sentiment": "negative"},
        "bengali_sentiment_bert": {"sentiment": "negative"},
        "mbert": {"sentiment": "negative"},
        "llm": {"sentiment": "neutral"},
        "twitter_xlmr": {"sentiment": "neutral"},
        "banglabert": {"sentiment": "neutral"},
        "modernbert": {"sentiment": "positive"},
    })
    assert v.label == "negative"
    assert v.agreement == 0.5
    assert v.abstained is False


def test_a_tie_the_llm_did_not_vote_in_still_abstains():
    v = combine({
        "mbert": {"sentiment": "positive"},
        "xlmr": {"sentiment": "negative"},
    })
    assert v.label == UNCERTAIN
    assert v.tie_broken_by is None


def test_the_tie_break_can_be_turned_off():
    v = combine(
        {
            "mbert": {"sentiment": "positive"},
            "llm": {"sentiment": "negative"},
        },
        prefer_on_tie=None,
    )
    assert v.label == UNCERTAIN


def test_a_source_that_did_not_run_is_not_a_voter():
    """Absence must not be counted as agreement, or as a neutral vote."""
    v = combine({
        "mbert": {"sentiment": "positive"},
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
    v = combine({"xlmr": {"sentiment": "LABEL_0"}, "mbert": {"sentiment": "positive"}})
    assert v.voters == 1
    assert v.label == "positive"


def test_score_is_the_mean_of_the_winning_voters():
    v = combine({
        "mbert": {"sentiment": "negative", "sentiment_score": -0.4},
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
        "mbert": {"sentiment": "positive"},
        "xlmr": {"sentiment": "positive"},
    })
    assert escalate is False
    assert reason == "cheap_consensus"


def test_cheap_disagreement_escalates():
    escalate, reason = should_escalate({
        "mbert": {"sentiment": "positive"},
        "xlmr": {"sentiment": "negative"},
    })
    assert escalate is True
    assert reason == "cheap_disagreement"


def test_too_few_cheap_voters_escalates_with_its_own_reason():
    """Missing evidence and conflicting evidence are different problems."""
    escalate, reason = should_escalate({"mbert": {"sentiment": "positive"}})
    assert escalate is True
    assert reason == "insufficient_cheap_voters"


def test_a_watchlist_mention_always_escalates():
    escalate, reason = should_escalate(
        {"mbert": {"sentiment": "positive"}, "xlmr": {"sentiment": "positive"}},
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
        "mbert": {"sentiment": "positive"},
        "xlmr": {"sentiment": "positive"},
        "llm": {"sentiment": "negative"},
    }
    assert cheap_disagreement(labels) is False


# ---------------------------------------------------------------------------
# agreement_summary()
# ---------------------------------------------------------------------------

def test_agreement_summary_reports_the_share_the_llm_never_needed_to_see():
    verdicts = [
        combine({"mbert": {"sentiment": "positive"}, "xlmr": {"sentiment": "positive"}}),
        combine({"mbert": {"sentiment": "positive"}, "xlmr": {"sentiment": "positive"}}),
        combine({"mbert": {"sentiment": "positive"}, "xlmr": {"sentiment": "negative"}}),
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


# ---------------------------------------------------------------------------
# Roster integrity — the defect where five heads were declared and none voted
# ---------------------------------------------------------------------------

def test_every_configured_classifier_is_a_recognised_cheap_voter():
    """A voter Stage 2 runs but `CHEAP_SOURCES` omits is bought and not used.

    `should_escalate` and `cheap_disagreement` filter `parallel_labels` down to
    CHEAP_SOURCES, so a name missing there costs a forward pass per comment and
    then contributes nothing to the decision it was paid for.
    """
    from defense.libs.common.config import get_settings

    names = [name for name, _ in get_settings().stage2_classifier_roster]
    assert names, "roster is empty — no cheap voters would run at all"
    assert set(names) <= set(CHEAP_SOURCES), (
        f"not in CHEAP_SOURCES: {sorted(set(names) - set(CHEAP_SOURCES))}"
    )


def test_cheap_sources_contains_only_models():
    """Every cheap voter must be a model that read the comment.

    `heuristic` — Stage 1's emoji + lexicon rule, and mostly the deterministic
    hash stub in the shipped configuration — was removed on 17 Aug 2026. A free
    voter that answers on every comment cannot abstain, so `abstained`,
    `unread` and `single_voter` could never report a run where no model loaded.
    """
    from defense.libs.common.config import get_settings

    assert "heuristic" not in CHEAP_SOURCES
    # …and CHEAP_SOURCES is exactly the configured roster: no extras, none missing.
    names = [name for name, _ in get_settings().stage2_classifier_roster]
    assert set(CHEAP_SOURCES) == set(names)


def test_roster_has_no_duplicate_voter_names():
    """Two slots under one name means the second silently overwrites the first
    in `parallel_labels` — seven models loaded, six opinions counted."""
    from defense.libs.common.config import get_settings

    names = [name for name, _ in get_settings().stage2_classifier_roster]
    assert len(names) == len(set(names))


def test_roster_has_no_duplicate_checkpoints():
    """The same weights under two names is not a second opinion: it votes twice
    and inflates `agreement` with a copy of itself."""
    from defense.libs.common.config import get_settings

    models = [m for _, m in get_settings().stage2_classifier_roster]
    assert len(models) == len(set(models))
