"""Shared sentiment-model registry + language-aware routing.

Single source of truth for *which* sentiment encoder Stage-1 uses, consumed by
both the Stage-1 worker (to load the model) and the API (`/v1/config/nlp`, to
expose/switch it). Mirrors how the LLM backend is configured, but the choice is
resolved **per post by detected language**, with an optional manual override.

Selection precedence (per post):
    manual override (if available)  >  auto-route by language  >  DEFAULT (XLM-R)

Each option maps to a HuggingFace checkpoint resolved from an env var. Only
checkpoints with a *sentiment classification head* work as drop-ins. XLM-R ships
one and is the default; BanglaBERT / BanglishBERT / mBERT are bare encoders, so
their slots stay "unavailable" (and routing falls back to XLM-R) until you point
their env var at a sentiment-fine-tuned checkpoint.

Routing buckets come from Stage-1's existing language detection:
    bengali script        -> "bn"        (BanglaBERT preferred)
    banglish / mixed      -> "banglish"  (BanglishBERT preferred)
    everything else       -> "en"        (XLM-R)
"""

from __future__ import annotations

import os
from typing import Optional

DEFAULT_KEY = "xlmr"

# key -> (label, env var holding the checkpoint, built-in default checkpoint,
#         language buckets this model is preferred for).
# hf_default = None means "no working checkpoint shipped" — the slot is wired but
# unavailable until <env var> is set, and auto-route falls back to the default.
_OPTIONS: dict[str, dict] = {
    "xlmr": {
        "label": "XLM-R (multilingual, social)",
        "env": "SENTIMENT_MODEL",
        # NOTE: this repo ships `sentencepiece.bpe.model` and no `tokenizer.json`,
        # so under transformers 5 the load fails with "`tiktoken` is required to
        # read a `tiktoken` file" unless tiktoken/sentencepiece is installed —
        # neither is a dependency here. Dormant while MODEL_STUB_MODE=true (the
        # weights are never loaded); it bites the moment stub mode is turned off.
        # `cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual` is the
        # same model family WITH a fast tokenizer, which is why Stage 2's
        # `twitter_xlmr` slot uses that one (see config.stage2_classifier_3).
        "hf_default": "cardiffnlp/twitter-xlm-roberta-base-sentiment",
        "languages": ["en", "banglish", "bn"],  # general fallback for any bucket
    },
    "banglishbert": {
        "label": "BanglishBERT (romanized Banglish / code-mixed)",
        "env": "BANGLISHBERT_SENTIMENT_MODEL",
        "hf_default": None,  # needs a sentiment-fine-tuned checkpoint
        "languages": ["banglish"],
    },
    "banglabert": {
        "label": "BanglaBERT (pure Bangla script)",
        "env": "BANGLABERT_SENTIMENT_MODEL",
        "hf_default": None,  # needs a sentiment-fine-tuned checkpoint
        "languages": ["bn"],
    },
    "mbert": {
        "label": "mBERT (baseline)",
        "env": "MBERT_SENTIMENT_MODEL",
        "hf_default": None,  # needs a sentiment-fine-tuned checkpoint
        "languages": [],  # baseline only — never auto-routed, manual-force only
    },
}

# Auto-route preference order per bucket (first available wins, else DEFAULT).
_BUCKET_PREFERENCE: dict[str, list[str]] = {
    "bn": ["banglabert", "xlmr"],
    "banglish": ["banglishbert", "xlmr"],
    "en": ["xlmr"],
}


def valid_keys() -> list[str]:
    return list(_OPTIONS.keys())


def label(key: str) -> str:
    opt = _OPTIONS.get(key)
    return opt["label"] if opt else key


def configured_hf_name(key: str) -> Optional[str]:
    """The checkpoint a key resolves to (env override > built-in), or None."""
    opt = _OPTIONS.get(key)
    if not opt:
        return None
    from defense.libs.common.config import get_settings
    config = get_settings()
    val = getattr(config, opt["env"].lower(), None)
    name = val if val is not None else (opt["hf_default"] or "")
    return name or None


def is_available(key: str) -> bool:
    """True when the key has a usable checkpoint configured."""
    return configured_hf_name(key) is not None


def bucket_for(script: Optional[str], is_banglish: bool, language: Optional[str]) -> str:
    """Map Stage-1 language signals to a routing bucket."""
    if script == "bengali":
        return "bn"
    if is_banglish or script == "mixed":
        return "banglish"
    return "en"


def auto_route_key(script: Optional[str], is_banglish: bool, language: Optional[str]) -> str:
    """Pick the best *available* model for the detected language; else DEFAULT."""
    bucket = bucket_for(script, is_banglish, language)
    for key in _BUCKET_PREFERENCE.get(bucket, [DEFAULT_KEY]):
        if is_available(key):
            return key
    return DEFAULT_KEY


def resolve(
    script: Optional[str] = None,
    is_banglish: bool = False,
    language: Optional[str] = None,
    override: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Resolve the sentiment model for one post → (key, hf_name).

    A manual ``override`` wins when it names an available option; otherwise the
    language buckets pick one; either way an unavailable choice falls back to
    DEFAULT (XLM-R). ``hf_name`` is None only when even the default is unset.
    """
    if override and is_available(override):
        return override, configured_hf_name(override)

    key = auto_route_key(script, is_banglish, language)
    if not is_available(key):
        key = DEFAULT_KEY
    return key, configured_hf_name(key)


def options_status() -> list[dict]:
    """Serializable option list for the API / dashboard."""
    return [
        {
            "key": key,
            "label": opt["label"],
            "hf_name": configured_hf_name(key),
            "available": is_available(key),
            "languages": opt["languages"],
        }
        for key, opt in _OPTIONS.items()
    ]


def route_table() -> dict[str, str]:
    """What each language bucket currently resolves to (keys), for display."""
    return {
        "bn": auto_route_key("bengali", False, "bn"),
        "banglish": auto_route_key("latin", True, "bn"),
        "en": auto_route_key("latin", False, "en"),
    }
