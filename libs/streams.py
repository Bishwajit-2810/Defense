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


def _spec(stream_env: str, stream_default: str, group_env: str, group_default: str) -> StreamSpec:
    return StreamSpec(
        name=os.getenv(stream_env, stream_default),
        group=os.getenv(group_env, group_default),
    )


# The pipeline, in order:
#   ingestion → nlp:stage1 → router → {llm:stage2 | assembler} → assembler
INGESTION = _spec("INGESTION_STREAM", "ingestion:queue",
                  "INGESTION_GROUP", "ingestion-workers")

STAGE1_NLP = _spec("NLP_STAGE1_STREAM", "nlp:stage1:queue",
                   "NLP_STAGE1_GROUP", "stage1-nlp-group")

ROUTER = _spec("ROUTER_STREAM", "router:queue",
               "ROUTER_GROUP", "router-workers")

STAGE2_LLM = _spec("STAGE2_LLM_STREAM", "llm:stage2:queue",
                   "STAGE2_LLM_GROUP", "stage2-llm-workers")

ASSEMBLER = _spec("ASSEMBLER_STREAM", "assembler:queue",
                  "ASSEMBLER_GROUP", "assembler-group")

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
