"""Tests for watchlist-driven target stance (§6.4 / stance_targets.md).

This is the project's novelty item, so it gets the strongest coverage — and the
matcher gets most of it, because the matcher is where the feature actually
succeeds or fails.

The corpus writes the same entity in Bangla script, in romanized Banglish (with
no standard spelling), and in English, often in one thread. A watchlist that
matches one spelling silently matches almost nothing — the same
degrade-to-a-silent-no-op failure PROJECT_ASSESSMENT §5.1 catalogues four times.
So the tests below care as much about **not matching** (false positives inside
other words) as about matching.

The second thing under test is the separation §2 of stance_targets.md insists
on: target stance is its own field and is never merged into document-level
sentiment. A comment can be positive in tone while opposing a listed entity.
"""

import sys
import textwrap

sys.path.insert(0, '/home/bk/code/defense')

import pytest

from libs.stance_scoring import (
    aggregate_target_stances,
    normalize_llm_target_stances,
    score_comment_deterministic,
)
from libs.stance_targets import (
    StanceTargetsError,
    Target,
    Targets,
    load_targets,
    unmatched_targets,
)

# A watchlist shaped like a real one: one entity, three scripts, one initialism.
_YAML = textwrap.dedent("""
    version: 1
    owner: "test"
    neutral:
      - id: alpha
        display: "Alpha Party"
        aliases: ["আলফা", "alpha party", "alpha", "ALP"]
        notes: "test fixture"
      - id: beta
        display: "Beta Front"
        aliases: ["বিটা", "beta front"]
        notes: "test fixture"
""")


@pytest.fixture
def watchlist(tmp_path):
    p = tmp_path / "stance_targets.yml"
    p.write_text(_YAML, encoding="utf-8")
    return load_targets(p)


# ---------------------------------------------------------------------------
# Loading and validation — a bad watchlist must fail loudly
# ---------------------------------------------------------------------------

def test_absent_file_is_not_an_error(tmp_path):
    """The feature is opt-in; no file means the pipeline behaves as before."""
    targets = load_targets(tmp_path / "nope.yml")
    assert not targets
    assert targets.match("anything") == []


def test_target_without_aliases_is_rejected(tmp_path):
    """It could never match — exactly the silent no-op §5.1 is about."""
    p = tmp_path / "w.yml"
    p.write_text("neutral:\n  - id: ghost\n    aliases: []\n", encoding="utf-8")
    with pytest.raises(StanceTargetsError, match="could never match"):
        load_targets(p)


def test_duplicate_target_id_is_rejected(tmp_path):
    p = tmp_path / "w.yml"
    p.write_text(
        "neutral:\n  - id: a\n    aliases: [x1]\nfavored:\n  - id: a\n    aliases: [y1]\n",
        encoding="utf-8",
    )
    with pytest.raises(StanceTargetsError, match="duplicate target id"):
        load_targets(p)


def test_alias_claimed_by_two_targets_is_rejected(tmp_path):
    """Whichever won would be arbitrary, so refuse to choose."""
    p = tmp_path / "w.yml"
    p.write_text(
        "neutral:\n  - id: a\n    aliases: [shared]\n  - id: b\n    aliases: [shared]\n",
        encoding="utf-8",
    )
    with pytest.raises(StanceTargetsError, match="claimed by both"):
        load_targets(p)


def test_polarity_is_recorded_from_the_bucket(tmp_path):
    p = tmp_path / "w.yml"
    p.write_text(
        "favored:\n  - id: f\n    aliases: [foo]\nopposed:\n  - id: o\n    aliases: [bar]\n",
        encoding="utf-8",
    )
    t = load_targets(p)
    assert t.by_id("f").polarity == "favored"
    assert t.by_id("o").polarity == "opposed"


# ---------------------------------------------------------------------------
# Matching across the three scripts — the load-bearing part
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "আলফা একটি দল",                     # Bangla script
    "Alpha Party is here",              # English, full name
    "alpha party is here",              # case-insensitive
    "I support ALP",                    # initialism
    "ALPHA PARTY",                      # upper case
])
def test_matches_every_spelling(watchlist, text):
    assert "alpha" in watchlist.matched_ids(text)


def test_bangla_allows_a_suffix_but_not_a_prefix(watchlist):
    """Bengali attaches inflections directly — a trailing \\b would under-match."""
    assert "alpha" in watchlist.matched_ids("আলফার নেতা")      # suffix attached
    assert "alpha" not in watchlist.matched_ids("মহাআলফা")     # mid-word: not a mention


def test_initialism_does_not_fire_inside_a_word(watchlist):
    """A 3-letter alias would otherwise match inside unrelated words."""
    assert "alpha" not in watchlist.matched_ids("the ALPINE region")
    assert "alpha" not in watchlist.matched_ids("scalped")


def test_latin_alias_needs_token_boundaries(watchlist):
    assert "alpha" not in watchlist.matched_ids("alphabet soup")
    assert "alpha" in watchlist.matched_ids("alpha, and others")


def test_several_targets_in_one_comment(watchlist):
    ids = watchlist.matched_ids("আলফা and beta front both spoke")
    assert set(ids) == {"alpha", "beta"}


def test_match_reports_which_alias_fired(watchlist):
    """Debugging alias coverage is the whole reason this is recorded."""
    matches = watchlist.match("আলফা")
    assert [m.alias for m in matches] == ["আলফা"]


def test_longer_alias_wins_a_shared_prefix(watchlist):
    """"alpha party" and "alpha" both match; the specific one is reported first."""
    matches = watchlist.match("alpha party")
    assert matches[0].alias == "alpha party"


def test_unmatched_targets_are_reported(watchlist):
    """Silence is an alias bug far more often than it is absence of discussion."""
    matched = watchlist.matched_ids("only আলফা appears here")
    assert unmatched_targets(watchlist, matched) == ["beta"]


# ---------------------------------------------------------------------------
# Deterministic scorer — so the feature works in stub mode and CI
# ---------------------------------------------------------------------------

def test_deterministic_opposing(watchlist):
    text = "আলফা চোর"                       # "alpha [is a] thief"
    out = score_comment_deterministic(text, watchlist.match(text))
    assert out[0]["target"] == "alpha"
    assert out[0]["stance"] == "opposing"
    assert out[0]["method"] == "deterministic"


def test_deterministic_supportive(watchlist):
    text = "alpha party is the best, respect"
    out = score_comment_deterministic(text, watchlist.match(text))
    assert out[0]["stance"] == "supportive"


def test_deterministic_neutral_without_cues(watchlist):
    text = "alpha party held a meeting today in the capital city"
    out = score_comment_deterministic(text, watchlist.match(text))
    assert out[0]["stance"] == "neutral"


def test_deterministic_carries_evidence(watchlist):
    """A wrong verdict has to be debuggable in a live demo."""
    text = "alpha party is corrupt"
    out = score_comment_deterministic(text, watchlist.match(text))
    assert "corrupt" in out[0]["evidence"]


def test_overlapping_aliases_do_not_double_count_a_clause(watchlist):
    """Found by mutation-testing: removing the dedupe broke nothing in the suite.

    "alpha party" and "alpha" both fire on the same words, so the clause they sit
    in was scored twice. On a single-clause comment that only doubles a score and
    the label is unchanged — invisible. But when the same entity is praised in one
    clause and attacked in another, the two should cancel; double-counting the
    first tips the verdict to whichever clause happens to contain the longer
    alias.
    """
    text = "alpha party is the best, alpha is corrupt"
    out = score_comment_deterministic(text, watchlist.match(text))
    assert len(out) == 1, "one entry per target, not per alias occurrence"
    assert out[0]["stance"] == "neutral", (
        "praise in one clause and attack in another must cancel; "
        "double-counting the overlapping alias reads it as supportive"
    )


def test_dedupe_keeps_the_longest_alias_and_all_distinct_positions(watchlist):
    from libs.stance_scoring import _dedupe_overlapping

    text = "alpha party is the best, alpha is corrupt"
    kept = _dedupe_overlapping(list(watchlist.match(text)))
    # The overlapping pair collapses to the longer alias; the later, separate
    # mention survives — collapsing it too would lose the second clause.
    assert [m.alias for m in kept] == ["alpha party", "alpha"]
    assert sorted(m.start for m in kept) == [0, 25]


def test_two_targets_scored_independently(watchlist):
    """The point of the feature: one comment, opposite stances."""
    text = "beta front is the best but alpha party is corrupt"
    out = {e["target"]: e["stance"] for e in
           score_comment_deterministic(text, watchlist.match(text))}
    assert out == {"beta": "supportive", "alpha": "opposing"}


# ---------------------------------------------------------------------------
# LLM output normalisation — never trust an invented entity
# ---------------------------------------------------------------------------

def test_hallucinated_target_is_dropped():
    out = normalize_llm_target_stances(
        [{"target": "not_on_the_list", "stance": "opposing"}], {"alpha"}
    )
    assert out == []


def test_out_of_taxonomy_stance_is_dropped():
    out = normalize_llm_target_stances(
        [{"target": "alpha", "stance": "furious"}], {"alpha"}
    )
    assert out == []


def test_valid_llm_entry_is_kept_and_tagged():
    out = normalize_llm_target_stances(
        [{"target": "alpha", "stance": "opposing", "evidence": "chor"}], {"alpha"}
    )
    assert out == [{"target": "alpha", "stance": "opposing",
                    "evidence": "chor", "method": "llm"}]


def test_non_list_payload_is_tolerated():
    assert normalize_llm_target_stances(None, {"alpha"}) == []
    assert normalize_llm_target_stances({"target": "alpha"}, {"alpha"}) == []


# ---------------------------------------------------------------------------
# Aggregation — the actual product output
# ---------------------------------------------------------------------------

def _comments(*stances):
    return [
        {"target_stances": [{"target": "alpha", "stance": s, "method": "llm",
                             "alias": "আলফা"}]}
        for s in stances
    ]


def test_rollup_counts_by_stance(watchlist):
    roll = aggregate_target_stances(
        _comments("opposing", "opposing", "supportive", "neutral"), watchlist
    )
    assert roll["alpha"]["mentions"] == 4
    assert roll["alpha"]["opposing"] == 2
    assert roll["alpha"]["supportive"] == 1
    assert roll["alpha"]["neutral"] == 1


def test_unmentioned_targets_are_absent_not_zero_filled(watchlist):
    """"Nobody discussed X" and "everyone was neutral on X" are different findings."""
    roll = aggregate_target_stances(_comments("opposing"), watchlist)
    assert "beta" not in roll


def test_rollup_records_which_alias_matched(watchlist):
    roll = aggregate_target_stances(_comments("opposing"), watchlist)
    assert roll["alpha"]["aliases_matched"] == {"আলফা": 1}


def test_rollup_states_its_own_provenance(watchlist):
    roll = aggregate_target_stances(_comments("opposing"), watchlist)
    assert roll["alpha"]["method"] == "llm"
    assert roll["alpha"]["method_breakdown"] == {"llm": 1}


def test_rollup_echoes_the_declared_polarity(watchlist):
    """The editorial choice travels with the output so it stays visible."""
    roll = aggregate_target_stances(_comments("opposing"), watchlist)
    assert roll["alpha"]["polarity"] == "neutral"


def test_rollup_ignores_unknown_targets(watchlist):
    roll = aggregate_target_stances(
        [{"target_stances": [{"target": "ghost", "stance": "opposing"}]}], watchlist
    )
    assert roll == {}


def test_empty_watchlist_yields_no_rollup():
    assert aggregate_target_stances(_comments("opposing"), Targets()) == {}


# ---------------------------------------------------------------------------
# The separation §2 insists on
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_target_stance_is_a_separate_field_from_sentiment(tmp_path, monkeypatch):
    """A comment can be positive in tone while opposing a listed entity.

    If the two were merged, this distinction — the entire reason the feature
    exists — would be destroyed.
    """
    from services.workers.stage1_nlp import comment_analyzer as ca

    p = tmp_path / "w.yml"
    p.write_text(_YAML, encoding="utf-8")
    monkeypatch.setattr(ca, "_STANCE_TARGETS_PATH", str(p))
    ca.reset_targets_cache()

    class _Registry:
        stub_mode = True
        llm_mode = False

        def get_sentiment_model(self, hf_name):
            return None

    try:
        out = await ca.analyze_comments(
            [{"id": "1", "likes": 0,
              "text": "great news everyone, alpha party is corrupt and must go"}],
            _Registry(),
        )
    finally:
        ca.reset_targets_cache()

    comment = out["comments"][0]
    # Document-level sentiment is untouched by the target verdict...
    assert "sentiment" in comment
    # ...and the target verdict lives in its own field.
    assert comment["target_stances"][0]["target"] == "alpha"
    assert comment["target_stances"][0]["stance"] == "opposing"
    # The rollup is the product output.
    assert out["target_stances"]["alpha"]["opposing"] == 1


@pytest.mark.asyncio
async def test_comments_mentioning_nothing_carry_no_target_field(tmp_path, monkeypatch):
    from services.workers.stage1_nlp import comment_analyzer as ca

    p = tmp_path / "w.yml"
    p.write_text(_YAML, encoding="utf-8")
    monkeypatch.setattr(ca, "_STANCE_TARGETS_PATH", str(p))
    ca.reset_targets_cache()

    class _Registry:
        stub_mode = True
        llm_mode = False

        def get_sentiment_model(self, hf_name):
            return None

    try:
        out = await ca.analyze_comments(
            [{"id": "1", "likes": 0, "text": "a comment about nothing in particular"}],
            _Registry(),
        )
    finally:
        ca.reset_targets_cache()

    assert "target_stances" not in out["comments"][0]


def test_shipped_config_loads_and_is_marked_as_an_example():
    """The repo's own config must parse — and must not ship real politics."""
    targets = load_targets("config/stance_targets.yml")
    assert targets, "shipped watchlist should parse"
    assert all(t.polarity == "neutral" for t in targets.targets), (
        "the shipped example must not declare favored/opposed entities"
    )
