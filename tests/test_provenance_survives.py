"""Regression tests: Stage-1 provenance must SURVIVE to the canonical result.

Found by re-evaluating the earlier fixes rather than by a failing test, which is
itself the point — the assembler's `processing` dict was a hand-maintained
whitelist, and it silently dropped **ten** keys Stage 1 emits:

    unit, nlp_engine, llm_role, stub_mode, degraded_components,
    vision_used, vision_produced_signal, vision_model, vision_status,
    model_versions

Two consequences, both bad, and both invisible:

  * **`processing.stub_mode` never reached the canonical result.**
    PROJECT_ASSESSMENT §5.9 calls it "the only signal that the vector is
    synthetic" — and `assembler.py` itself reads
    `result["processing"]["stub_mode"]` back out to decide whether to flag a stub
    embedding. It was always `None`, so that flag was computed from the wrong
    thing.
  * **`degraded_components` never arrived either**, so the dashboard's
    "degraded" row rendered a confident `none` on every run — a false
    reassurance, which is worse than showing nothing.

This is the §5.1 failure for the fifth time: one component writes a set of
fields, the next reads a *different* set, and the mismatch degrades to a silent
omission rather than an error. The mitigation is the same one §5.1 prescribes —
assert that the reader sees the producer's real output.
"""

import asyncio
import json
import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')
sys.path.insert(0, '/home/bk/code/defense/src/defense/libs')
sys.path.insert(0, '/home/bk/code/defense/src/defense/services/workers/stage1_nlp')
sys.path.insert(0, '/home/bk/code/defense/src/defense/services/api')

import pytest

from services.ingestion.normalizer import normalize_post
from services.workers.assembler.builder import (
    _STAGE1_PROVENANCE_KEYS,
    build_canonical_result,
)
from services.workers.stage1_nlp import worker as stage1
from services.workers.stage1_nlp.models import ModelRegistry

_POST = {
    "id": "p1",
    "campaignId": "c1",
    "platformPostId": "1",
    "url": "https://www.facebook.com/1",
    "caption": "এই সিদ্ধান্তটি সম্পূর্ণ ভুল এবং জনগণের বিরুদ্ধে গিয়েছে",
    "postType": "TEXT",
    "photoUrls": [],
    "postedAt": "2026-05-04T18:19:14",
    "scrapedAt": "2026-05-05T17:39:44.464",
    "sentiment": -0.5,
    "viralPotential": 0.3,
    "engagement": {"commentCount": 2, "storedCommentRows": 2, "totalReactions": 10,
                   "shareCount": 1},
    "reactionBreakdown": {"LIKE": 10},
    "sampleShares": [],
    "comments": [
        {"id": "c1", "text": "এটি একটি দীর্ঘ এবং যথাযথ মন্তব্য যা যথেষ্ট শব্দ ধারণ করে",
         "likes": 3, "authorUsername": "a", "parentId": None},
        {"id": "c2", "text": "👍", "likes": 0, "authorUsername": "b", "parentId": None},
    ],
}


async def _pipeline(degraded=None):
    """Run Stage 1 + the assembler and return (stage1_result, canonical_result)."""
    registry = ModelRegistry()
    post = json.loads(json.dumps(_POST))
    (
        text_result, image_result, comment_analysis,
        post_summary, post_summary_lang, post_summary_grounding,
        overall, score,
    ) = await stage1._process_message(post, registry)
    s1 = stage1._build_result(
        post=post, text_result=text_result, image_result=image_result,
        comment_analysis=comment_analysis,
        post_summary=post_summary, post_summary_lang=post_summary_lang,
        post_summary_grounding=post_summary_grounding,
        overall_sentiment=overall,
        sentiment_score=score, stage1_ms=1.0,
        degraded_components=degraded if degraded is not None else registry.degraded_components(),
    )
    canonical = build_canonical_result(
        normalized_post=normalize_post(post), stage1_result=s1, stage2_result=None
    )
    return s1, canonical


@pytest.fixture(scope="module")
def result_pair():
    return asyncio.run(_pipeline())


# ---------------------------------------------------------------------------
# The whole class, in one assertion
# ---------------------------------------------------------------------------

def test_no_stage1_processing_key_is_silently_dropped(result_pair):
    """The generalisable check. A new Stage-1 provenance field either arrives in
    the canonical result or fails here — it cannot vanish quietly."""
    s1, canonical = result_pair
    s1_proc = s1.get("processing") or {}
    out_proc = canonical.get("processing") or {}

    dropped = sorted(set(s1_proc) - set(out_proc))
    assert not dropped, (
        f"Stage-1 emits these in `processing` and the assembler drops them: "
        f"{dropped}. Add them to _STAGE1_PROVENANCE_KEYS in "
        f"services/workers/assembler/builder.py, or stop emitting them."
    )


def test_provenance_key_list_matches_what_stage1_emits(result_pair):
    """Guards the other direction: a key listed but no longer produced is dead
    config, and quietly implies a signal that does not exist."""
    s1, _ = result_pair
    s1_proc = s1.get("processing") or {}
    stale = [k for k in _STAGE1_PROVENANCE_KEYS if k not in s1_proc]
    assert not stale, (
        f"_STAGE1_PROVENANCE_KEYS lists {stale}, which Stage 1 no longer emits"
    )


# ---------------------------------------------------------------------------
# The specific fields the assessment relies on
# ---------------------------------------------------------------------------

def test_stub_mode_reaches_the_canonical_result(result_pair):
    """§5.9 calls this "the only signal that the vector is synthetic"."""
    _s1, canonical = result_pair
    assert "stub_mode" in canonical["processing"]
    assert isinstance(canonical["processing"]["stub_mode"], bool)


def test_nlp_engine_reaches_the_canonical_result(result_pair):
    """Which engine was intended — read alongside degraded_components."""
    _s1, canonical = result_pair
    assert canonical["processing"]["nlp_engine"] in ("stub", "models", "llm")


def test_degraded_components_reaches_the_canonical_result():
    """It rendered a confident "none" in the dashboard because it never arrived."""
    _s1, canonical = asyncio.run(_pipeline(degraded=["sentiment", "emotion"]))
    assert canonical["processing"]["degraded_components"] == ["sentiment", "emotion"]


def test_model_versions_reaches_the_canonical_result(result_pair):
    """Reproducibility: which concrete models produced this row."""
    _s1, canonical = result_pair
    assert "model_versions" in canonical["processing"]


def test_language_method_reaches_the_canonical_result(result_pair):
    """Was added to `analyze_text`'s return but never copied into the result —
    a field living only in an intermediate dict is one no consumer can read."""
    _s1, canonical = result_pair
    assert canonical.get("language_method") in ("fasttext", "script_heuristic", "stub")


def test_comment_provenance_reaches_the_canonical_result(result_pair):
    """`comment_analysis` is copied wholesale, so this always worked — pin it."""
    _s1, canonical = result_pair
    ca = canonical["comment_analysis"]
    assert "provenance" in ca
    assert "method_breakdown" in ca
    assert ca["comments"][0].get("method")
    assert ca["comments"][0].get("kind")


# ---------------------------------------------------------------------------
# The assembler reads stub_mode back out — so it has to be there
# ---------------------------------------------------------------------------

def test_the_assembler_can_read_back_the_key_it_depends_on(result_pair):
    """`assembler.py` computes `embedding_is_stub` from
    `result["processing"]["stub_mode"]`. When the builder dropped that key the
    expression fell through to `not stage1_embedding`, so a stub embedding — which
    exists and is non-empty — was reported as NOT a stub. Exactly backwards."""
    _s1, canonical = result_pair
    proc = canonical.get("processing") or {}
    stage1_embedding = [0.1] * 768          # a stub vector is still a vector
    embedding_is_stub = bool(proc.get("stub_mode") or not stage1_embedding)
    assert embedding_is_stub is True, (
        "a stub-mode run with a stub embedding must report embedding_is_stub=True"
    )


def test_canonical_result_is_schema_valid(result_pair):
    """build_canonical_result validates internally; assert it did not regress
    now that ten more keys flow through `processing`."""
    from defense.contracts.schemas.validator import validate_output

    _s1, canonical = result_pair
    valid, errors = validate_output(canonical)
    assert valid, errors


# ---------------------------------------------------------------------------
# The SAME bug existed one layer further up — the API response model
# ---------------------------------------------------------------------------
# `ProcessingResult` was a six-field whitelist, and Pydantic silently discarded
# everything else. So even after the assembler was fixed, eleven provenance
# fields were computed, persisted, and then dropped on the way OUT of the API —
# `role_models` among them, which had been believed to be reaching the dashboard.
#
# Three layers, the same failure at two of them. These tests cover the whole
# chain rather than one hop, because that is the only version that would have
# caught it.

def _api_response(canonical):
    """Push a canonical result through the API's row → response mapper."""
    import datetime

    from routers.analysis import _row_to_result

    now = datetime.datetime.now(datetime.timezone.utc)
    row = {
        "id": 1,
        "post_id": canonical["post_id"],
        "campaign_id": canonical["campaign_id"],
        "result": canonical,
        "created_at": now,
        "scraped_at": now,
    }
    return _row_to_result(row).model_dump()


def test_api_response_keeps_every_processing_key(result_pair):
    """End of the chain: Stage 1 → assembler → Postgres → API → dashboard."""
    _s1, canonical = result_pair
    resp = _api_response(canonical)

    canonical_proc = canonical.get("processing") or {}
    resp_proc = resp.get("processing") or {}
    dropped = sorted(set(canonical_proc) - set(resp_proc))
    assert not dropped, (
        f"the API response model drops these provenance keys: {dropped}. "
        f"ProcessingResult uses extra='allow' precisely so this cannot happen — "
        f"check it has not been tightened."
    )


def test_api_response_exposes_the_signals_the_dashboard_reads():
    """The dashboard's Trace tab renders these three. Each was unreachable."""
    _s1, canonical = asyncio.run(_pipeline(degraded=["sentiment"]))
    proc = _api_response(canonical).get("processing") or {}

    assert proc.get("degraded_components") == ["sentiment"]
    assert "stub_mode" in proc
    assert proc.get("nlp_engine") in ("stub", "models", "llm")


def test_api_response_exposes_language_method(result_pair):
    _s1, canonical = result_pair
    assert _api_response(canonical).get("language_method") in (
        "fasttext", "script_heuristic", "stub",
    )


def test_api_response_keeps_comment_provenance(result_pair):
    _s1, canonical = result_pair
    ca = _api_response(canonical).get("comment_analysis") or {}
    assert (ca.get("provenance") or {}).get("total") is not None
    assert ca.get("method_breakdown")
