"""Regression tests: the dashboard must render the fields the API returns.

PROJECT_ASSESSMENT §13.8. Four of Pass 6's six findings were one thing — *a fix
applied at the layer where the defect was noticed, and not at the layer where the
value is consumed* — and the rule that came out of it was: **when a fix adds a
signal, follow the signal to the surface a human reads and assert it there.**

The dashboard is that surface, and it has already been the missing hop twice:
§11.1 carried Stage 2's `insight` to the API and §12.4d had to carry it the last
hop; the report layer's cluster summaries never got that second pass at all until
§13.1. Both were invisible because every test stopped at the response model.

So these tests read `dashboard/app.js` and assert the field names appear. That is
a coarse check — it proves a field is *referenced*, not that it renders
correctly — but it is precisely the class of bug that keeps recurring here: a
field nothing reads at all. A rename on either side breaks it, which is the
point.
"""

import pathlib
import re
import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')

import pytest

from libs.llm.usage import ALL_LANES

_REPO = pathlib.Path('/home/bk/code/defense')
_APP_JS = (_REPO / 'dashboard_legacy/app.js').read_text(encoding='utf-8')
_INDEX = (_REPO / 'dashboard_legacy/index.html').read_text(encoding='utf-8')
_CSS = (_REPO / 'dashboard_legacy/styles.css').read_text(encoding='utf-8')


def _code_only(src: str) -> str:
    """`src` with comment lines removed.

    This file's whole premise is that a field must be *read*, not merely
    mentioned — and this codebase comments heavily, citing the very field names
    under test. Scanning the raw source made the reuse assertion pass against a
    dashboard that had stopped reading `reused_from` entirely, because the
    explanatory comments still named it. Caught by mutation-testing the test.
    """
    out = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(('//', '*', '/*', '*/')):
            continue
        out.append(line)
    return "\n".join(out)


_APP_CODE = _code_only(_APP_JS)


# ---------------------------------------------------------------------------
# GET /v1/usage — the cost story
# ---------------------------------------------------------------------------


def test_every_usage_lane_is_named_in_the_dashboard():
    """`lane_split` gained `stage1`, `interactive` and `agent` when tracking
    moved into LLMClient. A dashboard that knows only `post` and `comment` puts
    the newly-counted spend straight back out of sight — which is §13.4's defect
    with extra steps."""
    missing = [lane for lane in ALL_LANES
               if f"'{lane}'" not in _APP_CODE and f'"{lane}"' not in _APP_CODE
               and f'{lane}:' not in _APP_CODE]
    assert not missing, f"lanes counted by the API but never named in the UI: {missing}"


def test_the_lane_hint_table_covers_every_lane():
    """The UI's own legend must not drift from `libs/llm/usage.py`."""
    block = re.search(r'var LANE_HINTS = \{(.*?)\};', _APP_CODE, re.S)
    assert block, "LANE_HINTS table not found in app.js"
    for lane in ALL_LANES:
        assert re.search(rf'\b{lane}\s*:', block.group(1)), f"{lane} has no hint"


def test_pipeline_tokens_is_rendered():
    """`total_tokens` now includes chat and agent spend, which is per-question
    and unrelated to corpus size. `pipeline_tokens` is the per-post figure §6.8
    quotes, and showing only the total would attribute a chatbot session to the
    posts."""
    # Assert the CARD, not merely a read of the field: `tokenScopeSubtitle` also
    # reads it, so "is the field referenced" stays true even with the stat gone.
    assert re.search(r"makeStatCard\(\s*'Pipeline tokens'", _APP_CODE), (
        'the per-post token figure must be shown as its own stat, not folded '
        'into the all-callers total'
    )
    assert 'usage.pipeline_tokens' in _APP_CODE


def test_the_comment_share_is_not_divided_by_interactive_traffic():
    """The card says "% of calls are comment-level". Reading `call_share` off the
    response would now divide by chat and agent calls too — a per-post figure
    silently diluted by per-question spend, still labelled post-vs-comment."""
    assert 'pipelineCalls' in _APP_CODE, "comment share must be computed over pipeline lanes only"
    assert 'commentLane.call_share' not in _APP_CODE, (
        "call_share spans every lane now; computing the pipeline share from it "
        "reintroduces the mislabelled denominator"
    )


# ---------------------------------------------------------------------------
# Near-duplicate reuse (§13.3)
# ---------------------------------------------------------------------------


def test_reused_posts_are_disclosed_in_the_ui():
    """A reused post has `stage1_ms: 0`, `stage2_ms: 0` and `llm_used: false`,
    which reads as a cheap successful analysis rather than an inherited one.
    Only `processing.reused_from` distinguishes them.

    Asserted as **property reads**, not as the string appearing somewhere: a
    mention in a comment or a tooltip is exactly the kind of evidence that makes
    a grep-based test pass over dead code.
    """
    # A real read is preceded by an identifier and a dot; prose is not.
    real_reads = re.findall(r'(\w+)\.reused_from\b', _APP_CODE)
    assert len(real_reads) >= 2, (
        f"expected the detail modal AND the post list to read reused_from; "
        f"found reads on: {real_reads}"
    )
    assert 'near-dup' in _APP_CODE, "the post list should flag a reused row"
    assert 'source_post_id' in _APP_CODE, "the disclosure must name the source post"


def test_an_unanalysed_comment_thread_explains_itself():
    """Absent and zero are different findings and must not look the same — the
    rule `aggregate_target_stances` already applies to target stances. A reused
    post's thread is deliberately unread, and `comment_analysis.provenance.note`
    is the only thing that says so."""
    assert 'caProv' in _APP_CODE or 'provenance.note' in _APP_CODE, (
        "a thread with analyzed == 0 must surface its provenance note"
    )


# ---------------------------------------------------------------------------
# Fields earlier passes had to chase to this same surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field,section", [
    ("insight", "§11.1 / §12.4d — Stage-2 insight"),
    ("embedding_clusters", "§13.1 — report cluster summaries"),
    ("embedding_clusters_are_stub", "§13.1 / §13.2 — clustered over stub vectors"),
    ("embedding_is_stub", "§5.9 / §13.2 — non-semantic search results"),
    ("degraded_components", "§9.11 — degraded real-mode components"),
])
def test_hard_won_fields_still_reach_the_dashboard(field, section):
    """Each of these was computed, paid for, and dropped before something forced
    it to a reader. Assert the last hop so it is not quietly lost again."""
    assert field in _APP_CODE, f"{field} ({section}) is no longer referenced by the dashboard"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
