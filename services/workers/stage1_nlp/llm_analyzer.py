"""LLM-backed Stage-1 NLP (the "Fast NLP worker" running on one small LLM).

architecture.md §3.4 has Stage 1 run a small-model suite (XLM-R/fastText/GLiNER/…)
over the caption and every comment. This module is the **LLM realisation** of that
same step: instead of a fleet of specialised classifiers, a single fast LLM (the
``stage1`` role — gemma3:4b on local Ollama by default) emits the whole NLP field
set in one structured-JSON call. Stage 2 then runs on a *different*, larger model
(the ``stage2`` role — qwen2.5:7b).

It is enabled by ``STAGE1_LLM=true`` (see ModelRegistry.llm_mode). The caller
(text_analyzer / comment_analyzer) wraps every entry-point so that any failure —
LLM down, bad JSON, missing model — falls back to the deterministic stub path,
keeping CI/offline runs green.

Embeddings are NOT produced here: a chat LLM can't emit a 768-dim sentence vector,
so the embedding stays on the SentenceTransformer (or the shared stub). This module
only covers the classification/extraction fields.
"""

from __future__ import annotations

import json
from typing import Any

# Label vocabularies — kept in lock-step with the stub/real paths in
# text_analyzer.py and comment_analyzer.py and with libs/schemas/output_schema.json.
_SENTIMENT_LABELS: frozenset[str] = frozenset({"positive", "negative", "neutral"})
_EMOTION_LABELS: tuple[str, ...] = (
    "joy", "sadness", "anger", "fear", "surprise", "disgust", "neutral",
)
_EMOTION_SET: frozenset[str] = frozenset(_EMOTION_LABELS)
_ENTITY_LABELS: frozenset[str] = frozenset(
    {"PERSON", "ORG", "GPE", "LOC", "EVENT", "DATE"}
)
_STAGE1_ROLE = "stage1"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_POST_PROMPT = """You are a multilingual social-media NLP engine for Bangla, English, and romanized "Banglish" (Bangla written in Latin letters, often mixed with English). Analyse the POST text and return ONE JSON object and nothing else.

POST text:
\"\"\"{text}\"\"\"

Return JSON with EXACTLY these keys:
{{
  "language": "bn" or "en" or "und",
  "sentiment": "positive" or "negative" or "neutral",
  "sentiment_score": number from -1.0 (very negative) to 1.0 (very positive),
  "emotion": one of "joy","sadness","anger","fear","surprise","disgust","neutral",
  "topics": array of up to 3 short lowercase tags (e.g. "politics","religion","crime","protest","grief","economy","sports","entertainment","bangladesh","india"),
  "intents": array of up to 2 of "inform","express_grievance","commemorate","call_to_action","question","promote",
  "toxicity_score": number 0.0 (clean) to 1.0 (very toxic/abusive),
  "hate_speech_score": number 0.0 to 1.0 (targeted hate against a group),
  "entities": array of {{"text": "...", "label": "PERSON" or "ORG" or "GPE" or "LOC" or "EVENT" or "DATE"}},
  "keywords": array of up to 5 salient words or short phrases taken from the text
}}

Judge sentiment and emotion in the writer's own voice. Output JSON only, no prose, no markdown fences."""


_COMMENTS_PROMPT = """You label public reactions in the comments of a social-media post. For EACH numbered comment return its sentiment and dominant emotion. Comments may be in Bangla, Banglish, or English; mocking/laughing emoji under a claim read as negative.

COMMENTS:
{comments}

Return ONLY JSON of this exact shape, one entry per comment number:
{{"labels":[{{"i":1,"s":"positive","e":"joy","k":["word"]}},{{"i":2,"s":"negative","e":"anger","k":[]}}]}}
where "s" is one of positive,negative,neutral; "e" is one of anger,sadness,joy,fear,disgust,surprise,neutral; "k" is up to 3 keywords from that comment (may be empty). Output JSON only."""


# ---------------------------------------------------------------------------
# Parsing / normalisation helpers
# ---------------------------------------------------------------------------

def _parse_json(content: str) -> Any:
    """Parse a JSON object from an LLM response, tolerating ```json fences."""
    s = (content or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        inner = lines[1:-1] if lines and lines[-1].startswith("```") else lines[1:]
        s = "\n".join(inner).strip()
    return json.loads(s)


def _clamp(v: Any, lo: float, hi: float, default: float = 0.0) -> float:
    try:
        return round(max(lo, min(hi, float(v))), 4)
    except (TypeError, ValueError):
        return default


def _norm_sentiment(label: Any) -> str:
    s = str(label or "").lower().strip()
    return s if s in _SENTIMENT_LABELS else "neutral"


def _norm_emotion(label: Any) -> str:
    e = str(label or "").lower().strip()
    return e if e in _EMOTION_SET else "neutral"


def _emotion_block(primary: str) -> dict:
    """Build the {primary, scores} emotion block the output schema expects.

    The LLM returns a single dominant emotion; we synthesise a score
    distribution (primary weighted) so the shape matches the stub/real paths.
    """
    primary = _norm_emotion(primary)
    rest = round((1.0 - 0.7) / (len(_EMOTION_LABELS) - 1), 3)
    scores = {e: (0.7 if e == primary else rest) for e in _EMOTION_LABELS}
    return {"primary": primary, "scores": scores}


def _norm_str_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        s = str(item).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _norm_entities(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        label = str(item.get("label", "")).upper().strip()
        if not text or label not in _ENTITY_LABELS:
            continue
        out.append({"text": text, "label": label, "confidence": 0.9})
    return out[:20]


# ---------------------------------------------------------------------------
# Public entry-points
# ---------------------------------------------------------------------------

async def analyze_text_llm(
    text: str,
    llm: Any,
    backend_override: str | None = None,
) -> dict:
    """Run the full Stage-1 NLP suite on one text via the ``stage1`` LLM.

    Returns the classification/extraction fields (everything except language
    script flags and the embedding, which the caller fills). Raises on LLM /
    JSON failure so the caller can fall back to the deterministic stub.
    """
    messages = [{"role": "user", "content": _POST_PROMPT.format(text=text[:4000])}]
    response = await llm.chat(
        role=_STAGE1_ROLE,
        messages=messages,
        backend_override=backend_override,
        response_format={"type": "json_object"},
        max_tokens=512,
        temperature=0.0,
    )
    data = _parse_json(response.get("content", ""))
    if not isinstance(data, dict):
        raise ValueError("stage1 LLM did not return a JSON object")

    sentiment = _norm_sentiment(data.get("sentiment"))
    return {
        "language": (str(data.get("language") or "").lower().strip() or None),
        "sentiment": sentiment,
        "sentiment_score": _clamp(data.get("sentiment_score"), -1.0, 1.0),
        "sentiment_confidence": 0.9,
        "emotion": _emotion_block(data.get("emotion")),
        "topics": _norm_str_list(data.get("topics"), 3) or ["general"],
        "intents": _norm_str_list(data.get("intents"), 2) or ["inform"],
        "toxicity_score": _clamp(data.get("toxicity_score"), 0.0, 1.0),
        "hate_speech_score": _clamp(data.get("hate_speech_score"), 0.0, 1.0),
        "entities": _norm_entities(data.get("entities")),
        "keywords": _norm_str_list(data.get("keywords"), 5),
        # Provenance for the worker's processing block.
        "_llm_model": response.get("model"),
        "_llm_backend": response.get("backend"),
    }


async def classify_comments_llm(
    texts: list[str],
    llm: Any,
    backend_override: str | None = None,
    max_text: int = 140,
) -> list[dict | None]:
    """Label a batch of comment texts via the ``stage1`` LLM.

    Returns a list aligned with ``texts``; each slot is
    ``{"sentiment","sentiment_score","emotion","keywords"}`` or ``None`` when the
    model omitted that index (caller keeps its heuristic fallback for None slots).
    Raises on a hard LLM/JSON failure so the caller can fall back wholesale.
    """
    if not texts:
        return []

    lines = []
    for idx, t in enumerate(texts, 1):
        clean = (t or "").replace("\n", " ").strip()[:max_text] or "(no text)"
        lines.append(f"{idx}: {clean}")
    messages = [
        {"role": "user", "content": _COMMENTS_PROMPT.format(comments="\n".join(lines))}
    ]
    response = await llm.chat(
        role=_STAGE1_ROLE,
        messages=messages,
        backend_override=backend_override,
        response_format={"type": "json_object"},
        # ~40 tokens/comment covers {"i":N,"s":"...","e":"...","k":[...]}.
        max_tokens=min(4096, 40 * len(texts) + 64),
        temperature=0.0,
    )
    data = _parse_json(response.get("content", ""))
    labels = data.get("labels") if isinstance(data, dict) else data
    out: list[dict | None] = [None] * len(texts)
    if not isinstance(labels, list):
        return out

    _score = {"positive": 0.6, "negative": -0.6, "neutral": 0.0}
    for item in labels:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("i", item.get("index")))
        except (TypeError, ValueError):
            continue
        if not (1 <= idx <= len(texts)):
            continue
        sentiment = _norm_sentiment(item.get("s", item.get("sentiment")))
        out[idx - 1] = {
            "sentiment": sentiment,
            "sentiment_score": _score[sentiment],
            "emotion": _norm_emotion(item.get("e", item.get("emotion"))),
            "keywords": _norm_str_list(item.get("k", item.get("keywords")), 3),
        }
    return out
