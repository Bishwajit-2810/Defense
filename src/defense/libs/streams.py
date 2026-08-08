"""Canonical Redis stream + consumer-group names — one source of truth.

Why this exists
---------------
Four independent bugs in this system have had the identical shape: one
component writes a string, another reads a *different* string, and the mismatch
degrades to a silent no-op rather than an error (PROJECT_ASSESSMENT §5.1). The
KEDA autoscalers were instance #2 — the manifests named consumer groups
(`router-group`, `stage2-llm-group`) and streams (`stage1_nlp:queue`,
`stage2_llm:queue`) that no worker ever creates. A `redis-streams` trigger
pointed at a group that does not exist reports **no backlog**, so ingestion, the
router and Stage 2 — the only stage where scaling changes cost or latency —
never scaled, while the manifests looked correct.

Every worker imports its stream and group from here, and
``tests/test_streams.py`` asserts the KEDA manifests agree with these values.
A rename now breaks a test instead of silently disabling an autoscaler.

Each name stays env-overridable, because deployments legitimately shard streams
— but the default, which is what the manifests are generated against, lives in
exactly one place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class StreamSpec:
    """A stream and the consumer group its workers read it with."""

    name: str
    group: str


from defense.libs.common.config import get_settings
config = get_settings()

def _spec(stream_val: str, group_val: str) -> StreamSpec:
    return StreamSpec(
        name=stream_val,
        group=group_val,
    )


# The pipeline, in order:
#   ingestion → nlp:stage1 → router → {llm:stage2 | assembler} → assembler
INGESTION = _spec(config.ingestion_stream, config.ingestion_group)

STAGE1_NLP = _spec(config.nlp_stage1_stream, config.nlp_stage1_group)

ROUTER = _spec(config.router_stream, config.router_group)

STAGE2_LLM = _spec(config.stage2_llm_stream, config.stage2_llm_group)

ASSEMBLER = _spec(config.assembler_stream, config.assembler_group)

#: Keyed by the KEDA ScaledObject / Deployment name, so a manifest check can be
#: a direct lookup rather than a hand-maintained parallel list.
ALL: dict[str, StreamSpec] = {
    "ingestion": INGESTION,
    "stage1-nlp": STAGE1_NLP,
    "router": ROUTER,
    "stage2-llm": STAGE2_LLM,
    "assembler": ASSEMBLER,
}

__all__ = [
    "StreamSpec",
    "INGESTION",
    "STAGE1_NLP",
    "ROUTER",
    "STAGE2_LLM",
    "ASSEMBLER",
    "ALL",
]
