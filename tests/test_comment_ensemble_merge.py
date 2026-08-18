"""The Stage-2 merge is the SINGLE writer of every comment aggregate.

Before this, three places wrote them — Stage 1, the stance pass, and the comment
lane — and the last writer won. A post could report eight negative comments
while every comment in its own list read "neutral", because the breakdown was
rebuilt from LLM votes only while the per-comment labels were still Stage 1's.

These tests drive `_merge_ensemble` on real Stage-1-shaped comment dicts and
assert the properties that failure violated:

  * the counts sum to the number of comments;
  * the label on each comment is the one the counts were built from;
  * a label copied from a near-duplicate says so;
  * an emoji reaction is still counted as a reaction, not as a written opinion.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from defense.libs.stance_targets import watchlist_verdict
from defense.services.workers.stage2_llm import worker as w


def _comment(cid, text, sentiment="neutral", kind="substantive", **extra):
    c = {
        "id": cid,
        "text": text,
        "text_norm": text,
        "likes": 0,
        "author": "a",
        "parent_id": None,
        "sentiment": sentiment,
        "sentiment_score": 0.0,
        "emotion": "neutral",
        "method": "fast",
        "kind": kind,
        "emotion_method": "heuristic",
    }
    c.update(extra)
    return c


def _ca(comments):
    return {"analyzed": len(comments), "comments": comments}


def _vote(comment, source="xlmr", label=None, score=None):
    """Give the comment one real MODEL vote.

    These tests used to call `w._seed_heuristic_vote()`, which copied Stage 1's
    own label into `parallel_labels["heuristic"]`. Stage 1's keyword label no
    longer votes at all (17 Aug 2026), so the merge is now exercised the way it
    actually runs: with a cheap head's verdict.
    """
    label = label or comment["sentiment"]
    comment.setdefault("parallel_labels", {})[source] = {
        "sentiment": label,
        "score": comment.get("sentiment_score", 0.0) if score is None else score,
    }
    return comment


def _merge(ca, watchlist=None, reasons=None, dedup=None, voters=("xlmr",)):
    return w._merge_ensemble(
        ca, watchlist,
        escalation_reasons=reasons or {},
        dedup_stats=dedup or {"duplicates": 0, "duplicate_share": 0.0},
        voters_used=list(voters),
    )


# ---------------------------------------------------------------------------
# One label per comment, one set of counts, and they agree
# ---------------------------------------------------------------------------

def test_the_breakdown_totals_the_comments_it_describes():
    comments = [
        _comment("c1", "great", "positive"),
        _comment("c2", "awful", "negative"),
        _comment("c3", "ok", "neutral"),
        _comment("c4", "❤️", "positive", kind="emoji", method="emoji"),
    ]
    ca = _ca(comments)
    for c in comments:
        _vote(c)
    _merge(ca)

    assert sum(ca["sentiment_breakdown"].values()) == len(comments)


def test_every_comment_carries_the_label_the_counts_were_built_from():
    comments = [_comment("c1", "great", "positive"), _comment("c2", "awful", "negative")]
    ca = _ca(comments)
    for c in comments:
        _vote(c)
    _merge(ca)

    counted = {"positive": 0, "negative": 0, "neutral": 0, "uncertain": 0}
    for c in ca["comments"]:
        counted[c["sentiment"]] += 1
    assert counted == ca["sentiment_breakdown"]


def test_the_llm_vote_actually_changes_the_comments_label():
    """It used to land in parallel_labels and stop there."""
    c = _comment("c1", "this is fine", "neutral")
    _vote(c, "distilbert")                      # neutral
    c["parallel_labels"]["xlmr"] = {"sentiment": "negative", "score": -0.7}
    c["parallel_labels"]["llm"] = {"sentiment": "negative", "sentiment_score": -0.6}

    ca = _ca([c])
    _merge(ca, voters=("distilbert", "xlmr", "llm"))

    assert c["sentiment"] == "negative"      # 2 of 3 voters
    assert c["label_agreement"] == pytest.approx(2 / 3, abs=1e-3)
    assert c["method"] == "ensemble"


def test_a_split_is_reported_as_uncertain_in_its_own_bucket():
    c = _comment("c1", "hmm", "positive")
    _vote(c, "distilbert")                      # positive
    c["parallel_labels"]["xlmr"] = {"sentiment": "negative", "score": -0.9}

    ca = _ca([c])
    _merge(ca)

    assert c["sentiment"] == "uncertain"
    assert ca["sentiment_breakdown"]["uncertain"] == 1
    # ...and it did NOT quietly become a neutral.
    assert ca["sentiment_breakdown"]["neutral"] == 0


def test_emoji_reactions_stay_reactions():
    """The router used to relabel every emoji comment `filtered`, which pinned
    reaction_only to 0 and counted emoji as written opinion."""
    comments = [
        _comment("c1", "written opinion", "positive"),
        _comment("c2", "❤️", "positive", kind="emoji", method="emoji"),
        _comment("c3", "🤬", "negative", kind="emoji", method="emoji"),
    ]
    ca = _ca(comments)
    for c in comments:
        _vote(c)
    _merge(ca)

    assert ca["reaction_only"] == 2
    assert sum(ca["sentiment_breakdown_substantive"].values()) == 1


def test_method_breakdown_reports_what_actually_labelled_each_comment():
    """`provenance` reported 0% LLM for runs where every comment saw one."""
    c = _comment("c1", "text", "neutral")
    _vote(c)
    c["parallel_labels"]["llm"] = {"sentiment": "negative", "sentiment_score": -0.6}
    c["parallel_labels"]["xlmr"] = {"sentiment": "negative", "score": -0.8}

    ca = _ca([c])
    _merge(ca, voters=("xlmr", "llm"))

    assert ca["method_breakdown"] == {"ensemble": 1}
    assert ca["provenance"]["total"] == 1


def test_a_propagated_label_says_it_was_propagated():
    dup = _comment("c2", "same thing", "neutral")
    _vote(dup)
    dup["parallel_labels"]["llm"] = {"sentiment": "positive", "sentiment_score": 0.6}
    dup["label_source"] = "propagated"
    dup["propagated_from"] = "c1"

    ca = _ca([dup])
    _merge(ca, voters=("xlmr", "llm"))

    assert dup["label_source"] == "propagated"
    assert dup["method"] == "propagated"
    assert ca["method_breakdown"] == {"propagated": 1}


def test_the_ensemble_block_reports_what_the_agreement_bought():
    comments = [_comment(f"c{i}", f"text {i}", "positive") for i in range(4)]
    ca = _ca(comments)
    for c in comments:
        _vote(c)
    summary = _merge(
        ca,
        reasons={"cheap_consensus": 3, "cheap_disagreement": 1},
        dedup={"duplicates": 2, "duplicate_share": 0.5},
    )

    assert ca["ensemble"] is summary
    assert summary["comments"] == 4
    assert summary["escalated"] == 1
    assert summary["deduplicated"] == 2
    assert summary["escalation_reasons"]["cheap_consensus"] == 3


def test_the_llms_emotion_upgrades_the_heuristic_one():
    c = _comment("c1", "text", "negative")
    _vote(c)
    c["parallel_labels"]["llm"] = {"sentiment": "negative", "emotion": "anger"}

    _merge(_ca([c]), voters=("xlmr", "llm"))

    assert c["emotion"] == "anger"
    assert c["emotion_method"] == "llm"


@pytest.mark.parametrize("method", ["fast", "emoji", "model", "llm", "stub", "failed"])
def test_stage_1s_own_label_never_votes(method):
    """ONLY A MODEL MAY LABEL A COMMENT (17 Aug 2026).

    Stage 1's label used to be copied into `parallel_labels["heuristic"]` and
    counted as a voter — for every method except the hash `stub` and `failed`.
    That seeding is gone and `heuristic` is out of `ensemble.CHEAP_SOURCES`: it is
    an emoji + keyword rule, and in the shipped configuration most of its verdicts
    ARE the stub (11 of 12 comments on a real Bangla thread), so a free voter that
    answers every comment made abstention impossible to report.

    The merge must therefore find no voter here, whatever Stage 1 wrote.
    """
    c = _comment("c1", "দারুণ", "positive", method=method)
    ca = _ca([c])
    summary = _merge(ca, voters=())

    assert "heuristic" not in (c.get("parallel_labels") or {})
    assert c["label_voters"] == 0
    assert c["sentiment"] == "uncertain"
    assert summary["unread"] == 1
    # `method` is untouched, so `provenance` still discloses which cheap path ran.
    assert ca["method_breakdown"] == {method: 1}


def test_the_heuristic_seeder_is_gone_for_good():
    """A regression guard on the removal itself. Re-adding a seeded Stage-1 vote
    is the one change that would silently undo every abstention number above."""
    from defense.libs import ensemble

    assert not hasattr(w, "_seed_heuristic_vote")
    assert "heuristic" not in ensemble.CHEAP_SOURCES


def test_a_comment_nobody_read_is_uncertain_not_its_stub_label():
    """Declining the vote is only half the fix.

    The hash stub was refused a *vote* — but its label was still sitting in
    `comment["sentiment"]`, and the merge only
    overwrote it when at least one voter spoke. So with no sentiment model, no
    cached classifiers and no LLM verdict (the documented MODEL_STUB_MODE run),
    every comment kept its hash label, `sentiment_breakdown` counted it as a
    measurement, and `ensemble.abstained` simultaneously reported that all of
    them had abstained. Two contradicting aggregates in one payload.
    """
    comments = [
        _comment("c1", "ও আচ্ছা", "positive", method="stub"),
        _comment("c2", "Chatte thak", "negative", method="stub"),
        _comment("c3", "ভাই চালিয়ে যান", "positive", method="stub"),
    ]
    ca = _ca(comments)
    summary = _merge(ca, voters=())

    # Nobody voted, so nothing is claimed about any of them.
    assert [c["sentiment"] for c in comments] == ["uncertain"] * 3
    assert [c["label_voters"] for c in comments] == [0, 0, 0]
    assert all(c["label_agreement"] == 0.0 for c in comments)

    # And the aggregate says the same thing the per-comment labels do.
    assert ca["sentiment_breakdown"]["uncertain"] == 3
    assert ca["sentiment_breakdown"]["positive"] == 0
    assert ca["sentiment_breakdown"]["negative"] == 0
    assert summary["abstained"] == 3
    assert summary["unread"] == 3

    # `method` is untouched, so provenance still discloses that only the stub ran.
    assert ca["method_breakdown"] == {"stub": 3}


def test_one_voter_is_not_unanimity():
    """`unanimous_share` is documented as "the share the expensive model never
    needed to see". When the LLM is the ONLY labeller that ran — stub-mode
    Stage 1 abstains, no classifier weights cached — counting each comment as
    unanimous inverts that claim exactly."""
    comments = [
        _comment("c1", "x", method="stub", parallel_labels={
            "llm": {"sentiment": "positive", "sentiment_score": 0.6},
        }),
        _comment("c2", "y", method="stub", parallel_labels={
            "llm": {"sentiment": "negative", "sentiment_score": -0.6},
        }),
    ]
    ca = _ca(comments)
    summary = _merge(ca, voters=("llm",))

    assert summary["unanimous"] == 0
    assert summary["unanimous_share"] == 0.0
    # Reported instead, as its own fact: one labeller read these.
    assert summary["single_voter"] == 2
    assert summary["single_voter_share"] == 1.0
    # The labels still stand — a single voter is evidence, just not agreement.
    assert [c["sentiment"] for c in comments] == ["positive", "negative"]
    assert [c["label_voters"] for c in comments] == [1, 1]


def test_llm_coverage_is_counted_from_the_labels_not_the_intent():
    """A batch can fail. A post whose stance pass half-failed must not report
    full LLM coverage just because every comment was selected for one."""
    labelled = _comment("c1", "one", "positive")
    unlabelled = _comment("c2", "two", "positive")
    for c in (labelled, unlabelled):
        _vote(c)
    labelled["parallel_labels"]["llm"] = {"sentiment": "positive", "sentiment_score": 0.6}

    ca = _ca([labelled, unlabelled])
    summary = _merge(ca, reasons={"llm_all": 2}, voters=("xlmr", "llm"))

    assert summary["escalated"] == 2          # both were sent
    assert summary["llm_labelled"] == 1       # one came back
    assert summary["llm_share"] == 0.5


def test_comments_dropped_by_the_cap_are_reported_not_hidden():
    ca = _ca([_comment("c1", "x", "positive")])
    _vote(ca["comments"][0])
    summary = _merge(ca, reasons={"llm_all": 1, "capped_by_max_per_post": 40})
    assert summary["capped_out"] == 40


# ---------------------------------------------------------------------------
# The LLM's entity stances have to reach the field the watchlist reads
# ---------------------------------------------------------------------------

def test_llm_target_stances_are_merged_into_the_comments_own_field():
    """They were written to parallel_labels.llm and read from
    comment["target_stances"] — two fields that never met, so the tokens spent
    on target stance changed nothing."""
    c = _comment("c1", "about X", "neutral")
    c["target_stances"] = [
        {"target": "x", "stance": "neutral", "method": "deterministic"}
    ]
    c["parallel_labels"] = {
        "llm": {"sentiment": "negative", "target_stances": [
            {"target": "x", "stance": "opposing", "evidence": "…"}
        ]}
    }

    w._merge_llm_target_stances(c)

    assert len(c["target_stances"]) == 1
    assert c["target_stances"][0]["stance"] == "opposing"
    assert c["target_stances"][0]["method"] == "llm"


def test_merging_stances_is_a_no_op_without_an_llm_verdict():
    c = _comment("c1", "about X", "neutral")
    c["target_stances"] = [{"target": "x", "stance": "supportive", "method": "deterministic"}]
    w._merge_llm_target_stances(c)
    assert c["target_stances"][0]["method"] == "deterministic"


# ---------------------------------------------------------------------------
# The watchlist verdict
# ---------------------------------------------------------------------------

class _Target:
    def __init__(self, tid, polarity):
        self.id = tid
        self.polarity = polarity


class _Watchlist:
    def __init__(self, targets, matches=()):
        self._targets = {t.id: t for t in targets}
        self._matches = list(matches)

    def __bool__(self):
        return bool(self._targets)

    def by_id(self, tid):
        return self._targets.get(tid)

    def matched_ids(self, _text):
        return list(self._matches)


def test_an_always_target_in_the_post_text_alerts():
    wl = _Watchlist([_Target("t1", "always")], matches=["t1"])
    alert, reason = watchlist_verdict(wl, "a caption naming t1", [])
    assert alert is True
    assert "always:t1" in reason


def test_opposing_a_favored_target_alerts():
    wl = _Watchlist([_Target("t1", "favored")])
    comments = [_comment("c1", "x", target_stances=[{"target": "t1", "stance": "opposing"}])]
    alert, reason = watchlist_verdict(wl, "", comments)
    assert alert is True
    assert "opposing:t1" in reason


def test_opposing_an_undeclared_target_does_not_alert():
    """Opposition to a merely-monitored entity is not an attack on the operator.
    Firing on any `opposing` stance made a monitoring watchlist behave like an
    advocacy one."""
    wl = _Watchlist([_Target("t1", "neutral")])
    comments = [_comment("c1", "x", target_stances=[{"target": "t1", "stance": "opposing"}])]
    alert, reason = watchlist_verdict(wl, "", comments)
    assert alert is False
    assert reason is None


def test_no_watchlist_means_no_alert():
    assert watchlist_verdict(None, "anything", []) == (False, None)


def test_both_paths_share_one_alert_rule():
    """Stage 2 and the assembler must not carry separate copies of the rule.

    Each used to implement it independently, and a rule duplicated across the
    cheap path and the expensive path is one that eventually disagrees with
    itself about whether a post alerted.
    """
    from defense.services.workers.assembler import builder

    assert w._watchlist_verdict is watchlist_verdict
    assert builder.watchlist_verdict is watchlist_verdict
