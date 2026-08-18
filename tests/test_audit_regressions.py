"""Guards for the defects found in the August 2026 audit.

Each test names the thing that broke. They are cheap, and every one of them
would have failed on the code as it stood.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "src" / "defense"


# ---------------------------------------------------------------------------
# 1. Every logger must accept keyword fields
# ---------------------------------------------------------------------------
# Seven modules built a stdlib logger (`logging.getLogger`) and then called it
# structlog-style (`log.warning("x", error=...)`). stdlib's Logger raises
# TypeError on unexpected kwargs, and every one of those ~29 call sites sat
# inside an `except` block — so the handler that existed to keep a post alive
# was the thing that killed it. Failing image fetch, failing model load,
# failing usage tracking: all of them raised instead of degrading.

_MODULES_THAT_LOG_KWARGS = [
    "defense.libs.llm.usage",
    "defense.libs.tracing",
    "defense.libs.embeddings",
    "defense.services.workers.assembler.persistence",
    "defense.services.workers.stage1_nlp.text_analyzer",
    "defense.services.workers.stage1_nlp.vision_analyzer",
    "defense.services.workers.stage1_nlp.comment_analyzer",
    "defense.services.workers.stage1_nlp.models",
]


@pytest.mark.parametrize("module_name", _MODULES_THAT_LOG_KWARGS)
def test_module_logger_accepts_structured_fields(module_name):
    import importlib

    mod = importlib.import_module(module_name)
    logger = getattr(mod, "logger", None) or getattr(mod, "log", None)
    assert logger is not None, f"{module_name} exposes no module logger"
    # The call every degradation path makes. On a stdlib logger this raises.
    logger.warning("probe_event", error="x", url="y", post_id="p1")


def test_no_module_pairs_a_stdlib_logger_with_keyword_calls():
    """The contract, asserted over the source rather than one import at a time."""
    import re

    offenders = []
    kwargs_call = re.compile(
        r"\b(?:logger|log)\.(?:debug|info|warning|error|exception)\([^)]*?[a-z_]+="
    )
    for path in PKG.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        if not re.search(r"^(?:logger|log) = logging\.getLogger", src, re.M):
            continue
        if kwargs_call.search(src):
            offenders.append(str(path.relative_to(PKG)))
    assert not offenders, (
        "these modules call a stdlib logger with structlog keyword fields, "
        f"which raises TypeError at runtime: {offenders}"
    )


# ---------------------------------------------------------------------------
# 2. Stage 1 must not lose the original comment text
# ---------------------------------------------------------------------------

def test_normalisation_adds_a_field_instead_of_overwriting_the_text():
    from defense.services.workers.stage1_nlp.comment_analyzer import normalize_for_model

    original = "দারুণ! ❤️ দেখুন https://example.com/x"
    norm = normalize_for_model(original)

    assert "link" in norm
    assert "https://example.com/x" not in norm
    assert "❤️" not in norm
    # The original is untouched — the caller keeps it as `text`.
    assert "❤️" in original


def test_an_emoji_only_comment_normalises_to_nothing_but_keeps_its_text():
    from defense.services.workers.stage1_nlp.comment_analyzer import (
        _comment_kind,
        normalize_for_model,
    )

    assert normalize_for_model("❤️❤️") == ""
    # ...and it is still an emoji reaction, not a "filtered" nothing.
    assert _comment_kind("❤️❤️") == "emoji"


# ---------------------------------------------------------------------------
# 3. The PDF export must not invent a neutral, and must escape LLM output
# ---------------------------------------------------------------------------

def test_export_reports_a_missing_image_sentiment_as_missing():
    """"Image: neutral 0.000" on a text-only post is the claim this project
    already retracted for the pipeline; the PDF reintroduced it."""
    from defense.services.api.routers.analysis import _result_to_html

    html = _result_to_html({
        "post_id": "p1",
        "overall_sentiment": "positive",
        "sentiment_score": 0.4,
        "text_sentiment": {"label": "positive", "score": 0.4},
        "image_sentiment": None,
        "comment_analysis": {"comments": []},
    })
    assert "no image analysed" in html


def test_export_escapes_model_written_fields():
    """post_type and emotion come from an LLM — the one source that can contain
    markup nobody reviewed. They were interpolated raw."""
    from defense.services.api.routers.analysis import _result_to_html

    html = _result_to_html({
        "post_id": "p1",
        "post_type": "<script>alert(1)</script>",
        "emotion": {"primary": "<img src=x onerror=1>"},
        "platform": "<b>fb</b>",
        "comment_analysis": {"comments": []},
    })
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x onerror=1>" not in html


def test_export_is_bounded():
    """It rendered up to 5,000 PDFs inside a single request."""
    from defense.services.api.routers.analysis import EXPORT_MAX_POSTS

    assert EXPORT_MAX_POSTS <= 500


# ---------------------------------------------------------------------------
# 4. The routing gate must gate
# ---------------------------------------------------------------------------

def test_wanting_a_summary_is_not_by_itself_a_reason_to_pay_for_stage_2():
    from defense.services.workers.router.rules import should_use_llm

    confident = {
        "confidence": 0.95, "post_type": "news", "post_type_confidence": 0.95,
        "toxicity_score": 0.0, "photo_urls": [], "caption_chars": 50,
        "script": "bengali", "is_banglish": False,
    }
    assert should_use_llm(confident, {})[0] is False
    assert should_use_llm(confident, {"want_summary": True})[0] is True


def test_stage_2_does_not_re_summarise_what_stage_1_already_wrote():
    from defense.services.workers.router.rules import get_task_flags

    with_summary = {"post_type": "news", "topics": ["a", "b"], "post_summary": "সারাংশ"}
    assert get_task_flags(with_summary, {})["want_summary"] is False

    without = {"post_type": "news", "topics": ["a", "b"], "post_summary": None}
    assert get_task_flags(without, {})["want_summary"] is True

    # An explicit opt-out still wins over the fallback.
    assert get_task_flags(without, {"want_summary": False})["want_summary"] is False


def _thread(*specs) -> dict:
    """A stage1_result carrying comments described as (likes, kind) pairs."""
    return {
        "comment_analysis": {
            "comments": [
                {"id": f"c{i}", "text": f"substantive comment text {i}", "likes": likes, "kind": kind,
                 "sentiment": "negative", "sentiment_score": -0.5, "method": "fast"}
                for i, (likes, kind) in enumerate(specs)
            ]
        }
    }


def test_router_selects_the_most_reacted_comments_for_stage_2():
    """The analysis set is top-N by reaction count, chosen once, in the router."""
    from defense.services.workers.router.rules import select_comments_for_stage2

    result = _thread((5, "substantive"), (100, "substantive"), (50, "substantive"))
    record = select_comments_for_stage2(result, {}, limit=2)

    comments = result["comment_analysis"]["comments"]
    assert [c["likes"] for c in comments if c["stage2_selected"]] == [100, 50]
    assert comments[0]["stage2_selected"] is False
    assert comments[0]["stage2_skip_reason"] == "below_top_n"

    # Nothing is dropped from the payload — the skipped comment still persists.
    assert len(comments) == 3
    assert record["total"] == 3 and record["selected"] == 2 and record["skipped"] == 1
    assert record["cutoff_likes"] == 50
    # Written where the assembler and the API can read it.
    assert result["comment_analysis"]["stage2_selection"] == record


def test_the_whole_thread_is_analysed_by_default():
    """`ROUTER_COMMENT_TOP_N` ships at 0 — every comment with text is analysed.

    A positive default is a silent coverage cut: the comments it drops get no model
    verdict at all (Stage 1's keyword label does not vote), so they report
    `uncertain` and the post-level `sentiment_breakdown` fills up with abstentions
    while the coverage label still says "all N analyzed". Capping is a deliberate,
    quoted choice, never the default.
    """
    from defense.libs.common.config import Settings
    from defense.services.workers.router import rules

    assert Settings.model_fields["router_comment_top_n"].default == 0

    result = _thread(*[(i, "substantive") for i in range(250)], (900, "emoji"))
    record = rules.select_comments_for_stage2(result, {}, limit=0)

    assert record["limit"] == 0
    assert record["selected"] == 250 == record["eligible"]
    assert record["skipped"] == 0
    comments = result["comment_analysis"]["comments"]
    assert all(c["stage2_selected"] for c in comments[:250])
    # The emoji reaction is still not selected — no model can read it.
    assert "stage2_selected" not in comments[250]


def test_the_launcher_does_not_override_the_coverage_knobs():
    """`run_all.py`'s env beats .env, so a coverage default set there silently
    overrides the operator's. It used to force COMMENT_STANCE_MAX_PER_POST=40,
    which is how a run showed "✓ all 2857 analyzed" beside "⚠ 2606 capped out"."""
    import run_all

    env = run_all.build_env()
    for key in ("ROUTER_COMMENT_TOP_N", "STAGE1_LLM_COMMENT_MAX",
                "COMMENT_STANCE_MAX_PER_POST"):
        assert key not in env, f"run_all.py must not set {key}"


def test_textless_comments_do_not_consume_a_stage_2_slot():
    """A 900-like emoji reaction gets no model call, so it must not take a slot."""
    from defense.services.workers.router.rules import select_comments_for_stage2

    result = _thread((900, "emoji"), (3, "substantive"), (2, "substantive"))
    record = select_comments_for_stage2(result, {}, limit=2)

    comments = result["comment_analysis"]["comments"]
    # Not ranked, and not marked either — Stage 2's own `no_text` accounting
    # still sees it.
    assert "stage2_selected" not in comments[0]
    assert comments[1]["stage2_selected"] is True and comments[2]["stage2_selected"] is True
    assert record["eligible"] == 2 and record["skipped"] == 0


def test_no_cap_selects_every_comment_with_text():
    from defense.services.workers.router.rules import select_comments_for_stage2

    result = _thread((1, "substantive"), (2, "substantive"))
    record = select_comments_for_stage2(result, {}, limit=0)
    assert record["selected"] == 2 and record["skipped"] == 0
    assert all(c["stage2_selected"] for c in result["comment_analysis"]["comments"])


def test_min_words_filters_comments_with_two_or_fewer_words():
    from defense.services.workers.router.rules import select_comments_for_stage2

    result = {
        "comment_analysis": {
            "comments": [
                {"id": "c1", "text": "nice", "likes": 50, "kind": "substantive"},  # 1 word -> skip
                {"id": "c2", "text": "good post", "likes": 40, "kind": "substantive"},  # 2 words -> skip
                {"id": "c3", "text": "❤️❤️🔥", "likes": 90, "kind": "emoji"},  # emoji -> skip
                {"id": "c4", "text": "this is great news today", "likes": 30, "kind": "substantive"},  # 5 words -> select
                {"id": "c5", "text": "আমি কিছু বললাম না ভাই", "likes": 20, "kind": "substantive"},  # 5 words -> select
            ]
        }
    }
    record = select_comments_for_stage2(result, {}, limit=200, min_words=3)
    assert record["eligible"] == 2
    assert record["selected"] == 2
    comments = result["comment_analysis"]["comments"]
    assert comments[0]["stage2_selected"] is False and comments[0]["stage2_skip_reason"] == "below_min_words"
    assert comments[1]["stage2_selected"] is False and comments[1]["stage2_skip_reason"] == "below_min_words"
    assert "stage2_selected" not in comments[2]  # emoji
    assert comments[3]["stage2_selected"] is True
    assert comments[4]["stage2_selected"] is True


def test_router_filters_duplicate_repeat_comments():
    """Duplicate / repeat comments (e.g. spam/reposts/bots) must be filtered in the router so Stage-2 gets fresh unique comments."""
    from defense.services.workers.router.rules import select_comments_for_stage2

    result = {
        "comment_analysis": {
            "comments": [
                {"id": "c1", "text": "উপদেষ্টা থাইকা কি করছো? মানুষ কিন্তু দেখছে🤣😂🤣", "likes": 50, "kind": "substantive"},
                {"id": "c2", "text": "উপদেষ্টা থাইকা কি করছো? মানুষ কিন্তু দেখছে🤣😂🤣", "likes": 10, "kind": "substantive"},  # duplicate -> skip
                {"id": "c3", "text": "উপদেষ্টা থাইকা কি করছো মানুষ কিন্তু দেখছে", "likes": 5, "kind": "substantive"},  # duplicate (near-duplicate) -> skip
                {"id": "c4", "text": "এই বিষয়ে বিস্তারিত জানতে চাই ভাই", "likes": 30, "kind": "substantive"},  # unique -> select
                {"id": "c5", "text": "great initiative for our country", "likes": 20, "kind": "substantive"},  # unique -> select
                {"id": "c6", "text": "❤️❤️🔥", "likes": 100, "kind": "emoji"},  # emoji -> skip
                {"id": "c7", "text": "ok nice", "likes": 80, "kind": "substantive"},  # <3 words -> skip
            ]
        }
    }
    record = select_comments_for_stage2(result, {}, limit=200, min_words=3)
    assert record["total"] == 7
    assert record["eligible"] == 3  # c1, c4, c5
    assert record["selected"] == 3
    assert record["duplicates"] == 2  # c2, c3

    comments = result["comment_analysis"]["comments"]
    # c1 (highest likes duplicate representative) selected
    assert comments[0]["stage2_selected"] is True
    # c2 (duplicate) marked skipped
    assert comments[1]["stage2_selected"] is False
    assert comments[1]["stage2_skip_reason"] == "duplicate"
    assert comments[1]["is_duplicate"] is True
    # c3 (near duplicate) marked skipped
    assert comments[2]["stage2_selected"] is False
    assert comments[2]["stage2_skip_reason"] == "duplicate"
    # c4, c5 selected
    assert comments[3]["stage2_selected"] is True
    assert comments[4]["stage2_selected"] is True
    # c6 emoji (no stage2_selected)
    assert "stage2_selected" not in comments[5]
    # c7 below min words
    assert comments[6]["stage2_selected"] is False
    assert comments[6]["stage2_skip_reason"] == "below_min_words"


def test_unselected_comments_are_uncertain_not_labelled_by_a_keyword_rule():
    """Capping cost must not manufacture a verdict either.

    An earlier version of this test asserted the opposite — that a comment outside
    the router's top-N kept Stage 1's label, seeded into the ensemble as a
    `heuristic` vote. That seeding was removed on 17 Aug 2026: Stage 1's label is
    an emoji + keyword rule (mostly the deterministic hash stub in the shipped
    configuration), and a free voter that answers on every comment makes
    abstention unreportable. So the honest answer for a comment no MODEL read is
    `uncertain` at zero voters, and `escalation_reason` says why.
    """
    import defense.services.workers.stage2_llm.worker as w

    ca = {
        "comments": [
            {"id": "in", "text": "a", "kind": "substantive", "likes": 9,
             "sentiment": "neutral", "method": "fast", "stage2_selected": True,
             "parallel_labels": {"llm": {"sentiment": "negative", "sentiment_score": -0.6}}},
            {"id": "out", "text": "b", "kind": "substantive", "likes": 0,
             "sentiment": "negative", "sentiment_score": -0.4, "method": "fast",
             "stage2_selected": False, "stage2_skip_reason": "below_top_n"},
        ],
        "stage2_selection": {"limit": 1, "selected": 1, "skipped": 1, "total": 2},
    }
    summary = w._merge_ensemble(
        ca, None, escalation_reasons={"llm_all": 1, "below_top_n": 1},
        dedup_stats={"duplicates": 0, "duplicate_share": 0.0}, voters_used=["llm"],
    )

    skipped = ca["comments"][1]
    assert skipped["sentiment"] == "uncertain"
    assert skipped["label_voters"] == 0
    assert skipped["label_sources"] == []
    assert skipped["escalation_reason"] == "below_top_n"
    # Stage 1's cheap path is still disclosed — just not as a verdict.
    assert skipped["method"] == "fast"
    assert ca["method_breakdown"]["fast"] == 1

    # Coverage of the thread and coverage of the selection are both reported,
    # so 1-of-2 cannot be read as a half-failed stance pass.
    assert summary["analysed"] == 1 and summary["not_analysed"] == 1
    assert summary["llm_share"] == 0.5 and summary["llm_share_analysed"] == 1.0


# ---------------------------------------------------------------------------
# 5. The watchlist example must stay an example
# ---------------------------------------------------------------------------

def test_the_tracked_watchlist_declares_no_real_entities():
    """It briefly listed a real person plus the words "positive" and "negative"
    as always-alert entities, so any comment containing either word raised
    "under attack" — and both words went into the LLM prompt as named entities."""
    from defense.libs.stance_targets import load_targets

    targets = load_targets(REPO / "config" / "stance_targets.yml", prefer_local=False)
    for t in targets.targets:
        assert t.polarity == "neutral", f"{t.id} is declared {t.polarity} in the shipped example"
        assert "EXAMPLE" in (t.notes or "").upper(), f"{t.id} is not marked as an example"


def test_an_operator_override_is_loaded_instead_when_present(tmp_path):
    from defense.libs.stance_targets import load_targets

    shipped = tmp_path / "targets.yml"
    shipped.write_text(
        "version: 1\nneutral:\n  - id: example\n    display: E\n    aliases: [e]\n",
        encoding="utf-8",
    )
    (tmp_path / "targets.local.yml").write_text(
        "version: 1\nalways:\n  - id: real\n    display: R\n    aliases: [r]\n",
        encoding="utf-8",
    )
    assert [t.id for t in load_targets(shipped).targets] == ["real"]
    assert [t.id for t in load_targets(shipped, prefer_local=False).targets] == ["example"]


# ---------------------------------------------------------------------------
# 6. The cheap classifiers must not fabricate verdicts
# ---------------------------------------------------------------------------

def test_an_unmappable_classifier_label_is_an_abstention_not_a_neutral():
    """A head with no id2label emits LABEL_0/LABEL_1. Mapping those to
    "neutral" turned every comment neutral and called it a model verdict."""
    from defense.services.workers.stage2_llm.worker import _map_sentiment_label

    assert _map_sentiment_label("LABEL_0") is None
    assert _map_sentiment_label("positive") == "positive"
    assert _map_sentiment_label("5 stars") == "positive"
    assert _map_sentiment_label("neutral") == "neutral"


def test_every_comment_with_text_goes_to_the_llm_by_default():
    """COMMENT_LLM_MODE=all is the shipped default: the UI compares LLM, XLM-R
    and DistilBERT on the SAME comment, which needs all three to have run.
    'escalate' stays available as the cost-optimised mode."""
    from defense.libs.common.config import get_settings

    assert get_settings().comment_llm_mode == "all"


@pytest.mark.asyncio
async def test_stage1_does_not_re_label_comments_the_ensemble_will_relabel():
    """Comments were being LLM-labelled twice: gemma3:4b in Stage 1, then
    qwen2.5:7b plus two classifiers in Stage 2. The Stage-1 pass is the
    pipeline's slowest step (a measured 120 s for one 25-comment batch, returning
    invalid JSON) and its verdict survives only as a single outvoted vote."""
    from defense.libs.common.config import get_settings
    from defense.services.workers.stage1_nlp import comment_analyzer as ca

    assert get_settings().stage1_llm_comments is False

    called = False

    class _Registry:
        llm_mode = True

        def get_llm_client(self):
            nonlocal called
            called = True
            return object()

    records = [{"kind": "substantive", "text": "a real comment here", "likes": 1}]
    assert await ca._llm_upgrade_comments(records, _Registry(), None, None) == 0
    assert called is False, "no LLM client should even be constructed"


def test_the_llm_is_grounded_on_the_summary():
    """The stance prompt judges a comment against what the post SAID."""
    src = (PKG / "services" / "workers" / "stage2_llm" / "worker.py").read_text(encoding="utf-8")
    lane = src.split("async def _comment_lane")[1]
    grounding = lane.split("watchlist = _targets()")[0]
    assert 'stage1_result.get("post_summary")' in grounding, (
        "the comment lane no longer grounds the stance pass on the post summary"
    )
    assert grounding.index('stage1_result.get("post_summary")') < grounding.index("post_context"), (
        "the summary must be preferred over the raw caption"
    )


@pytest.mark.asyncio
async def test_stub_mode_downloads_nothing_but_uses_what_is_cached(monkeypatch):
    """MODEL_STUB_MODE promises "no GPU, no downloads" — not "refuse models you
    already have". Returning None outright meant the two classifiers never ran
    in the default run_all.py configuration, and the UI showed a dash for both
    on every comment while the checkpoints sat in the local HF cache."""
    from defense.libs.common.config import get_settings
    from defense.services.workers.stage2_llm import worker as w

    monkeypatch.setenv("MODEL_STUB_MODE", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(w, "_PIPELINES", {}, raising=False)
    monkeypatch.setattr(w, "_PIPELINE_FAILED", set(), raising=False)

    # Not cached → skipped, and no load is attempted.
    monkeypatch.setattr(w, "_is_cached", lambda model_id: False)
    attempted = []
    comments = [{"text": "hello", "kind": "substantive"}]
    assert await w._run_hf_classifier(comments, "xlmr", "uncached/model") == 0
    assert "parallel_labels" not in comments[0]

    # Cached → the load IS attempted (stubbed here so no weights are needed).
    monkeypatch.setattr(w, "_PIPELINE_FAILED", set(), raising=False)
    monkeypatch.setattr(w, "_is_cached", lambda model_id: True)

    def _fake_pipeline(texts, truncation=True):
        attempted.append(list(texts))
        return [{"label": "positive", "score": 0.9} for _ in texts]

    monkeypatch.setattr(w, "_get_pipeline", lambda name, model_id: _fake_pipeline)
    comments = [{"text": "hello", "kind": "substantive"}]
    assert await w._run_hf_classifier(comments, "xlmr", "cached/model") == 1
    assert comments[0]["parallel_labels"]["xlmr"]["sentiment"] == "positive"
    assert attempted, "a cached checkpoint must actually be used"


def test_offline_policy_is_applied_before_any_transformers_import():
    """`transformers` reads TRANSFORMERS_OFFLINE at IMPORT time, so the switch
    only works if it is set first. Every transformers import in this tree is
    lazy precisely so that a call at worker start-up is early enough — if one
    ever becomes a module-level import, the offline promise is silently void
    and the loader goes to the network."""
    import re

    for rel in ("services/workers/stage1_nlp/worker.py",
                "services/workers/stage2_llm/worker.py"):
        src = (PKG / rel).read_text(encoding="utf-8")
        assert "apply_hf_offline_policy()" in src, f"{rel} never applies the policy"

    # No module-level `import transformers` anywhere: they must all stay inside
    # a function so the policy call can precede them.
    offenders = []
    top_level = re.compile(r"^(?:from|import)\s+(?:transformers|sentence_transformers)\b", re.M)
    for path in PKG.rglob("*.py"):
        if top_level.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(PKG)))
    assert not offenders, (
        "transformers imported at module level — TRANSFORMERS_OFFLINE can no "
        f"longer be applied in time in: {offenders}"
    )


def test_offline_policy_follows_stub_mode_and_can_be_overridden(monkeypatch):
    from defense.libs.common.config import apply_hf_offline_policy, get_settings

    for var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MODEL_STUB_MODE", "true")
    monkeypatch.delenv("HF_OFFLINE", raising=False)
    get_settings.cache_clear()
    assert apply_hf_offline_policy() is True
    assert os.environ["HF_HUB_OFFLINE"] == "1"

    for var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HF_OFFLINE", "false")
    get_settings.cache_clear()
    assert apply_hf_offline_policy() is False
    assert "HF_HUB_OFFLINE" not in os.environ


def test_cache_probe_fails_open():
    """A wrong 'yes' costs a download; a wrong 'no' silently deletes a
    labeller. So an unavailable cache API must not remove the voter."""
    from defense.services.workers.stage2_llm import worker as w

    assert w._is_cached("definitely/not-a-real-model-xyz") is False


def test_agreement_is_reported_with_its_voter_count():
    """`label_agreement: 1.0` over a SINGLE voter rendered as "100% agree",
    which reads as consensus when only one model spoke."""
    from defense.services.workers.stage2_llm import worker as w

    c = {"id": "c1", "text": "x", "kind": "substantive", "sentiment": "positive",
         "sentiment_score": 0.5, "emotion": "neutral", "method": "fast",
         "parallel_labels": {"xlmr": {"sentiment": "positive", "score": 0.5}}}
    ca = {"analyzed": 1, "comments": [c]}
    w._merge_ensemble(ca, None, escalation_reasons={}, dedup_stats={},
                      voters_used=["xlmr"])

    assert c["label_agreement"] == 1.0
    assert c["label_voters"] == 1          # ...out of one. Not a consensus.
    assert c["label_sources"] == ["xlmr"]


def test_the_destructive_e2e_test_cannot_run_by_accident():
    """`tests/test_pipeline_e2e.py` shells out to `run_all.py --reset` — a Redis
    FLUSHALL plus a truncate of Postgres and ClickHouse.

    It used to run on a bare `pytest`, which made a plain test run destructive to
    any machine with a live dev stack, and cost ~130 s (85% of the suite's wall
    clock) failing for want of a stack it could not have. AUDIT_PASS7 records the
    decision not to run it; this asserts the decision is enforced by the code
    rather than remembered by the reader.
    """
    import tests.test_pipeline_e2e as e2e

    reasons = [
        m.kwargs.get("reason", "")
        for m in e2e.pytestmark
        if m.name == "skipif"
    ]
    assert reasons, "the destructive e2e test must carry a skipif guard"
    assert "RUN_DESTRUCTIVE_E2E" in reasons[0]
    assert {m.name for m in e2e.pytestmark} >= {"e2e", "destructive", "skipif"}

    # And the guard must actually be closed in a normal environment.
    if not os.environ.get("RUN_DESTRUCTIVE_E2E"):
        skipif = next(m for m in e2e.pytestmark if m.name == "skipif")
        assert skipif.args[0] is True, "guard is open without the opt-in set"


def test_config_json_alone_does_not_count_as_a_cached_checkpoint(monkeypatch):
    """An interrupted prefetch leaves config.json + tokenizer and no weights.

    `_is_cached` used to answer True for that, so the head passed the stub-mode
    gate and then failed inside `pipeline(...)` — under the HF_HUB_OFFLINE=1
    that run_all.py sets, as a connection error rather than the true reason.
    """
    import huggingface_hub

    from defense.services.workers.stage2_llm import worker as w

    present: set[str] = {"config.json", "tokenizer.json", "special_tokens_map.json"}
    monkeypatch.setattr(
        huggingface_hub,
        "try_to_load_from_cache",
        lambda repo_id, filename: f"/cache/{filename}" if filename in present else None,
    )
    assert w._is_cached("some/repo") is False

    present.add("model.safetensors")
    assert w._is_cached("some/repo") is True


def test_sharded_checkpoints_count_as_cached(monkeypatch):
    """A sharded repo has no `model.safetensors`; its index is the marker."""
    import huggingface_hub

    from defense.services.workers.stage2_llm import worker as w

    monkeypatch.setattr(
        huggingface_hub,
        "try_to_load_from_cache",
        lambda repo_id, filename: (
            "/cache/index" if filename == "model.safetensors.index.json" else None
        ),
    )
    assert w._is_cached("some/sharded-repo") is True
