"""Regression tests (§5.5 / §9.6): the KEDA manifests must name the streams and
consumer groups the workers actually create.

"KEDA autoscaling on queue depth" is listed as implemented production
engineering, but three of five scalers could never scale and none of them could
scale from zero:

  * the manifests named `ingestion-group`, `router-group`, `stage2-llm-group`
    and the streams `stage1_nlp:queue` / `stage2_llm:queue` — none of which any
    worker creates. A ``redis-streams`` trigger pointed at a group that does not
    exist reports **no backlog**, so ingestion, the router and Stage 2 never
    scaled while the manifests read as correct;
  * ``pendingEntriesCount`` counts messages delivered to a consumer and not yet
    ACKed. With ``minReplicaCount: 0`` there is no consumer, nothing is ever
    delivered, the count stays 0, and KEDA never wakes the deployment.

This is the §5.1 mitigation applied to the fourth instance of that failure:
names live once in ``libs/streams.py``, the workers import them, and this test
asserts the manifests agree. A rename now breaks a test instead of silently
disabling an autoscaler.
"""

import sys
from pathlib import Path

sys.path.insert(0, '/home/bk/code/defense')
# The assembler runs as a standalone container and imports its siblings flatly
# (`from builder import ...`), so its own directory has to be importable here.
sys.path.insert(0, '/home/bk/code/defense/services/workers/assembler')

import pytest
import yaml

from libs import streams

MANIFEST = Path('/home/bk/code/defense/deploy/k8s/keda-scaledobjects.yaml')

# ScaledObject metadata.name → the pipeline stage it scales.
_SCALER_TO_STAGE = {
    "ingestion-scaler": "ingestion",
    "worker-router-scaler": "router",
    "worker-stage1-nlp-scaler": "stage1-nlp",
    "worker-stage2-llm-scaler": "stage2-llm",
    "worker-assembler-scaler": "assembler",
}


def _scaled_objects() -> dict[str, dict]:
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == "ScaledObject"}


@pytest.fixture(scope="module")
def scalers() -> dict[str, dict]:
    return _scaled_objects()


def test_every_stage_has_a_scaler(scalers):
    assert set(scalers) == set(_SCALER_TO_STAGE)


@pytest.mark.parametrize("scaler_name,stage", sorted(_SCALER_TO_STAGE.items()))
def test_scaler_matches_the_workers_stream_and_group(scalers, scaler_name, stage):
    spec = streams.ALL[stage]
    triggers = [
        t for t in scalers[scaler_name]["spec"]["triggers"]
        if t["type"] == "redis-streams"
    ]
    assert triggers, f"{scaler_name} has no redis-streams trigger"
    meta = triggers[0]["metadata"]

    assert meta["stream"] == spec.name, (
        f"{scaler_name} watches stream {meta['stream']!r} but the worker reads "
        f"{spec.name!r} — the trigger would report no backlog forever"
    )
    assert meta["consumerGroup"] == spec.group, (
        f"{scaler_name} watches group {meta['consumerGroup']!r} but the worker "
        f"creates {spec.group!r} — the trigger would report no backlog forever"
    )


@pytest.mark.parametrize("scaler_name", sorted(_SCALER_TO_STAGE))
def test_scale_from_zero_uses_lag_not_pending_entries(scalers, scaler_name):
    spec = scalers[scaler_name]["spec"]
    trigger = next(
        t for t in spec["triggers"] if t["type"] == "redis-streams"
    )["metadata"]

    if int(spec.get("minReplicaCount", 1)) == 0:
        assert "lagCount" in trigger, (
            f"{scaler_name} scales from zero but uses pendingEntriesCount, "
            "which is always 0 with no consumer attached — it can never wake up"
        )
        assert "pendingEntriesCount" not in trigger


def test_stream_names_are_distinct():
    """A copy-paste between two stages would silently merge two queues."""
    names = [s.name for s in streams.ALL.values()]
    groups = [s.group for s in streams.ALL.values()]
    assert len(set(names)) == len(names)
    assert len(set(groups)) == len(groups)


def test_workers_import_their_names_from_libs_streams():
    """The producers' constants must BE the source of truth, not copies of it."""
    from services.ingestion import service as ingestion
    from services.workers.assembler import assembler
    from services.workers.router import router
    from services.workers.stage1_nlp import worker as stage1
    from services.workers.stage2_llm import worker as stage2

    assert (ingestion.INGESTION_STREAM, ingestion.CONSUMER_GROUP) == (
        streams.INGESTION.name, streams.INGESTION.group)
    assert (stage1.INPUT_STREAM, stage1.CONSUMER_GROUP) == (
        streams.STAGE1_NLP.name, streams.STAGE1_NLP.group)
    assert (router.ROUTER_QUEUE, router.CONSUMER_GROUP) == (
        streams.ROUTER.name, streams.ROUTER.group)
    assert (stage2.STAGE2_QUEUE, stage2.CONSUMER_GROUP) == (
        streams.STAGE2_LLM.name, streams.STAGE2_LLM.group)
    assert (assembler.STREAM_KEY, assembler.CONSUMER_GROUP) == (
        streams.ASSEMBLER.name, streams.ASSEMBLER.group)


def test_the_monitoring_view_reads_the_same_names_the_workers_use():
    """The dashboard's Pipeline tab was the last un-pinned copy of these names.

    `routers/pipeline.py::_STAGES` hardcoded all five stream AND group names as
    string literals. They matched the defaults, so nothing was visibly wrong —
    but every name is env-overridable by design, and `_stage_stats` swallows the
    `xinfo_groups` error a wrong group produces and returns zeros. Under any
    override the tab would render an idle, healthy pipeline while work queued:
    the §5.5 KEDA failure mode, reproduced in the view you would use to notice
    it (PROJECT_ASSESSMENT §13.7b).
    """
    sys.path.insert(0, '/home/bk/code/defense/services/api')
    sys.path.insert(0, '/home/bk/code/defense/services/api/routers')
    from routers.pipeline import _STAGES  # noqa: PLC0415

    seen = {stream: group for _key, _label, stream, group in _STAGES}
    expected = {spec.name: spec.group for spec in streams.ALL.values()}
    assert seen == expected


def test_the_pipeline_hands_off_to_the_next_stages_stream():
    """Each producer must write to the stream the next consumer reads."""
    from services.ingestion import service as ingestion
    from services.workers.router import router
    from services.workers.stage1_nlp import worker as stage1

    assert ingestion.NLP_STREAM == streams.STAGE1_NLP.name
    assert stage1.OUTPUT_STREAM == streams.ROUTER.name
    assert router.STAGE2_QUEUE == streams.STAGE2_LLM.name
    assert router.ASSEMBLER_QUEUE == streams.ASSEMBLER.name
