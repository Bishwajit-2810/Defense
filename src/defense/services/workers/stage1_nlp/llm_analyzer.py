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
import re
from typing import Any

import structlog

from defense.libs.labels import POST_TYPES, POST_TYPE_SET  # noqa: E402
from defense.libs.llm.usage import LANE_STAGE1

logger = structlog.get_logger(__name__)

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
  "post_type": one of {post_types} — the single kind of post this is, or null if the text does not say,
  "post_type_confidence": number 0.0 to 1.0 — how sure you are of post_type,
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

#: One `{"i": …}` label object, for salvaging a truncated batch response.
_LABEL_OBJECT_RE = re.compile(r'\{[^{}]*"i"\s*:[^{}]*\}')
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _parse_json(content: str, *, salvage_labels: bool = False) -> Any:
    """Parse a JSON object from an LLM response, tolerating ```json fences.

    ``salvage_labels`` opts in to recovering whole ``{"i": …}`` entries from a
    response that was cut off mid-array — worth doing for a 40-comment batch,
    where losing the last entry should not cost the other 39.

    It is OFF by default and never silent. Applied unconditionally it returned
    ``{"labels": [...]}`` for *every* task, including the post-level ones that
    have no `labels` key at all, so a truncated post-type response came back as
    a well-formed answer to a different question.
    """
    s = (content or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        inner = lines[1:-1] if lines and lines[-1].startswith("```") else lines[1:]
        s = "\n".join(inner).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        if not salvage_labels:
            raise
        objects = []
        for match in _LABEL_OBJECT_RE.finditer(s):
            try:
                objects.append(json.loads(_TRAILING_COMMA_RE.sub(r"\1", match.group(0))))
            except Exception:
                continue
        if not objects:
            raise
        logger.warning(
            "llm_json_salvaged",
            recovered=len(objects),
            chars=len(s),
            hint="response was not valid JSON; whole label objects were recovered",
        )
        return {"labels": objects}


def _clamp(v: Any, lo: float, hi: float, default: float = 0.0) -> float:
    try:
        return round(max(lo, min(hi, float(v))), 4)
    except (TypeError, ValueError):
        return default


def _parse_sentiment(label: Any) -> str | None:
    """Strict sentiment parse — None when the model gave no usable label.

    Coercing a missing/garbled label to "neutral" publishes a verdict the model
    never reached, and the surrounding defaults make it look deliberate: score
    0.0, toxicity 0.0, no keywords, a flat high confidence. Callers must treat
    None as a failed analysis and fall back, not as "this post is neutral".
    """
    s = str(label or "").lower().strip()
    return s if s in _SENTIMENT_LABELS else None


def _coherent_score(label: str, raw: Any) -> float:
    """The model's score for ``label``, repaired when the number contradicts it.

    Small models routinely emit ``{"sentiment": "negative", "sentiment_score": 0}``.
    Taking that 0.0 at face value flattens the post to neutral during fusion, so
    fall back to a half-strength score with the label's own sign.
    """
    score = _clamp(raw, -1.0, 1.0)
    if label == "positive" and score <= 0.0:
        return 0.5
    if label == "negative" and score >= 0.0:
        return -0.5
    return score


def _sentiment_confidence(score: float) -> float:
    """Confidence derived from the strength the model itself reported.

    A chat LLM emits no class probabilities, so |sentiment_score| is the only
    self-reported signal available. A fixed 0.9 (the previous value) made every
    answer — including empty ones — look equally trustworthy.
    """
    return round(0.5 + 0.45 * min(abs(score), 1.0), 3)


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


def _parse_post_type(label: Any, raw_conf: Any) -> tuple[str | None, float]:
    """Validate the model's post_type against the canonical vocabulary.

    An out-of-vocabulary or missing label becomes ``(None, 0.0)`` — "unknown",
    which is what the router's Rule 2 escalates. Coercing it to "other" with a
    default confidence would hide the failure behind a plausible answer.
    """
    s = str(label or "").lower().strip()
    if s not in POST_TYPE_SET or s == "other":
        return None, 0.0
    # A chat LLM's self-reported confidence is not calibrated; cap it below the
    # agreement-backed ceiling the non-LLM path uses.
    return s, min(_clamp(raw_conf, 0.0, 1.0, default=0.6) or 0.6, 0.9)


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
    prompt = _POST_PROMPT.format(
        text=text[:4000],
        post_types=", ".join(f'"{t}"' for t in POST_TYPES),
    )
    messages = [{"role": "user", "content": prompt}]
    response = await llm.chat(
        role=_STAGE1_ROLE,
        messages=messages,
        backend_override=backend_override,
        response_format={"type": "json_object"},
        max_tokens=512,
        temperature=0.0,
        usage_lane=LANE_STAGE1,
        usage_task="stage1_post",
    )
    data = _parse_json(response.get("content", ""))
    if not isinstance(data, dict):
        raise ValueError("stage1 LLM did not return a JSON object")

    sentiment = _parse_sentiment(data.get("sentiment"))
    if sentiment is None:
        # Includes the `{}` reply grammar-constrained decoding can produce: valid
        # JSON, zero analysis. Raise so the caller falls back instead of recording
        # a fabricated "neutral".
        raise ValueError(
            f"stage1 LLM returned no usable sentiment (keys={sorted(data)!r})"
        )
    sentiment_score = _coherent_score(sentiment, data.get("sentiment_score"))
    post_type, post_type_conf = _parse_post_type(
        data.get("post_type"), data.get("post_type_confidence")
    )
    return {
        "language": (str(data.get("language") or "").lower().strip() or None),
        "sentiment": sentiment,
        "sentiment_score": sentiment_score,
        "sentiment_confidence": _sentiment_confidence(sentiment_score),
        "emotion": _emotion_block(data.get("emotion")),
        "topics": _norm_str_list(data.get("topics"), 3) or ["general"],
        "intents": _norm_str_list(data.get("intents"), 2) or ["inform"],
        "post_type": post_type,
        "post_type_confidence": post_type_conf,
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
        usage_lane=LANE_STAGE1,
        usage_task="stage1_comments",
    )
    # Batch responses are the one place salvage is right: a cut-off array still
    # holds valid labels for most of the batch.
    data = _parse_json(response.get("content", ""), salvage_labels=True)
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
        sentiment = _parse_sentiment(item.get("s", item.get("sentiment")))
        if sentiment is None:
            # Leave the slot None so the caller keeps its heuristic label for this
            # comment rather than counting an unanswered one as neutral.
            continue
        out[idx - 1] = {
            "sentiment": sentiment,
            "sentiment_score": _score[sentiment],
            "emotion": _norm_emotion(item.get("e", item.get("emotion"))),
            "keywords": _norm_str_list(item.get("k", item.get("keywords")), 3),
        }
    return out
