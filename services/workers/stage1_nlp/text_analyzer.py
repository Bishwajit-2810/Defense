"""Full text NLP pipeline for a single text string.

In stub mode every sub-task returns plausible deterministic values derived
from the input text so end-to-end tests pass without GPU or model weights.

In real mode the pipeline calls:
  - fastText for language detection
  - XLM-R (cardiffnlp/twitter-xlm-roberta-base-sentiment) for sentiment
  - j-hartmann/emotion-english-distilroberta-base for emotion
  - unitary/toxic-bert for toxicity / hate-speech
  - GLiNER (urchade/gliner_multi-v2.1) for NER
  - KeyBERT for keywords
  - BAAI/bge-m3 (SentenceTransformer) for dense embeddings
"""

from __future__ import annotations

import re
import sys
import os

# Make the libs package importable when running from the service root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "libs"))
# ...and the repo root, for `libs.*` imports (no-op when PYTHONPATH already has it)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from common.utils import detect_script, is_banglish  # noqa: E402
from libs.embeddings import fit_dim  # noqa: E402
from libs.embeddings import stub_embedding as _shared_stub_embedding  # noqa: E402

from .models import ModelRegistry  # noqa: E402

# ---------------------------------------------------------------------------
# Bengali negative / positive seed words used by the stub heuristic
# ---------------------------------------------------------------------------

_BN_NEGATIVE_WORDS: frozenset[str] = frozenset(
    [
        "অপপ্রচার",
        "ইতর",
        "হুমকি",
        "ভন্ড",
        "মিথ্যা",
        "ঘৃণা",
        "নরক",
        "অত্যাচার",
        "শোষণ",
        "দুর্নীতি",
        "হত্যা",
        "মৃত্যু",
        "ক্ষতি",
        "কষ্ট",
        "বিপদ",
        "সংকট",
        "ক্রোধ",
        "রাগ",
        "ভয়",
        "আতঙ্ক",
    ]
)

_BN_POSITIVE_WORDS: frozenset[str] = frozenset(
    [
        "ভালো",
        "সুন্দর",
        "আনন্দ",
        "খুশি",
        "প্রেম",
        "ভালোবাসা",
        "সফল",
        "বিজয়",
        "উন্নতি",
        "শান্তি",
        "মুক্তি",
        "স্বাধীনতা",
        "গর্ব",
        "আশা",
        "বরকত",
        "কল্যাণ",
    ]
)

# Simple topic seed sets (stub only)
_TOPIC_SEEDS: list[tuple[str, list[str]]] = [
    ("politics", ["রাজনীতি", "সরকার", "নির্বাচন", "আওয়ামী", "বিএনপি", "politics", "govt"]),
    ("religion", ["ইসলাম", "মুসলিম", "হিন্দু", "ধর্ম", "মসজিদ", "islam", "muslim"]),
    ("crime", ["হত্যা", "গ্রেফতার", "পুলিশ", "আইন", "crime", "arrest", "murder"]),
    ("protest", ["আন্দোলন", "বিক্ষোভ", "মিছিল", "protest", "rally", "strike"]),
    ("grief", ["শোক", "মৃত্যু", "স্মরণ", "grief", "mourning", "remembrance"]),
    ("india", ["ভারত", "india", "হিন্দুস্তান"]),
    ("bangladesh", ["বাংলাদেশ", "bangladesh", "dhaka", "ঢাকা"]),
]

# Intent seed patterns (stub only)
_INTENT_SEEDS: list[tuple[str, list[str]]] = [
    ("inform", ["জানান", "জানুন", "তথ্য", "বিস্তারিত", "link", "লিঙ্ক"]),
    ("express_grievance", ["অপপ্রচার", "ক্ষোভ", "রাগ", "নরক", "অন্যায়"]),
    ("commemorate", ["স্মরণ", "শাহাদত", "শহীদ", "স্মৃতি"]),
    ("call_to_action", ["শেয়ার", "করুন", "সাপোর্ট", "support", "share", "vote"]),
    ("question", ["কেন", "কি", "কিভাবে", "why", "how", "what", "?"]),
]

# Seed NER labels for stub detection
_NER_SEEDS: list[tuple[str, str]] = [
    ("আওয়ামী লীগ", "ORG"),
    ("বিএনপি", "ORG"),
    ("বাংলাদেশ", "GPE"),
    ("ভারত", "GPE"),
    ("ঢাকা", "GPE"),
    ("শাপলা চত্বর", "LOC"),
    ("Bangladesh", "GPE"),
    ("India", "GPE"),
    ("Dhaka", "GPE"),
    ("Facebook", "ORG"),
]


# ---------------------------------------------------------------------------
# Null / empty result helper
# ---------------------------------------------------------------------------

def _empty_result() -> dict:
    return {
        "language": None,
        "script": None,
        "is_banglish": False,
        "language_confidence": None,
        "sentiment": None,
        "sentiment_score": None,
        "sentiment_confidence": None,
        "emotion": {"primary": None, "scores": {}},
        "topics": [],
        "intents": [],
        "toxicity_score": None,
        "hate_speech_score": None,
        "entities": [],
        "keywords": [],
        "embedding": None,
    }


# ---------------------------------------------------------------------------
# Stub implementations
# ---------------------------------------------------------------------------

def _stub_language(text: str) -> tuple[str, str, bool, float]:
    """Return (language, script, is_banglish, confidence)."""
    script = detect_script(text)
    banglish = is_banglish(text)

    if script == "bengali":
        return "bn", "bengali", False, 0.97
    if script == "mixed" or banglish:
        return "bn", "mixed", banglish, 0.82
    if script == "latin":
        # Simple English vs Banglish heuristic
        if banglish:
            return "bn", "latin", True, 0.75
        return "en", "latin", False, 0.91
    return "und", "other", False, 0.50


def _stub_sentiment(text: str) -> tuple[str, float, float]:
    """Return (label, score, confidence).

    Deterministic: check negative seed words first, then positive, else
    use text length as a weak neutral proxy so output is reproducible.
    """
    lower = text.lower()
    neg_count = sum(1 for w in _BN_NEGATIVE_WORDS if w in text)
    pos_count = sum(1 for w in _BN_POSITIVE_WORDS if w in text)

    if neg_count > pos_count:
        # Scale score by how many negative signals
        score = max(-1.0, -0.5 - 0.1 * neg_count)
        return "negative", round(score, 3), round(0.7 + min(neg_count * 0.03, 0.25), 3)
    if pos_count > 0:
        score = min(1.0, 0.4 + 0.1 * pos_count)
        return "positive", round(score, 3), round(0.7 + min(pos_count * 0.03, 0.25), 3)

    # Neutral — use a tiny hash so output varies deterministically with input
    h = sum(ord(c) for c in text[:50]) % 100
    if h < 30:
        return "negative", round(-0.15 - h * 0.002, 3), 0.62
    if h > 70:
        return "positive", round(0.15 + (h - 70) * 0.002, 3), 0.61
    return "neutral", round((h - 50) * 0.005, 3), 0.65


def _stub_emotion(sentiment_label: str, text: str) -> dict:
    """Return {primary, scores} based on sentiment + text cues."""
    lower = text.lower()

    if "শোক" in text or "মৃত্যু" in text or "grief" in lower:
        primary = "sadness"
    elif "রাগ" in text or "ক্রোধ" in text or "angry" in lower:
        primary = "anger"
    elif "ভয়" in text or "আতঙ্ক" in text or "fear" in lower:
        primary = "fear"
    elif sentiment_label == "negative":
        primary = "sadness"
    elif sentiment_label == "positive":
        primary = "joy"
    else:
        primary = "neutral"

    emotion_map = {
        "joy": 0.1,
        "sadness": 0.1,
        "anger": 0.1,
        "fear": 0.1,
        "surprise": 0.1,
        "disgust": 0.1,
        "neutral": 0.4,
    }
    emotion_map[primary] = 0.7
    # Redistribute remaining probability
    rest = (1.0 - 0.7) / (len(emotion_map) - 1)
    scores = {k: round(0.7 if k == primary else rest, 3) for k in emotion_map}
    return {"primary": primary, "scores": scores}


def _stub_topics(text: str) -> list[str]:
    topics: list[str] = []
    for topic, seeds in _TOPIC_SEEDS:
        if any(seed.lower() in text.lower() for seed in seeds):
            topics.append(topic)
    return topics or ["general"]


def _stub_intents(text: str) -> list[str]:
    intents: list[str] = []
    for intent, seeds in _INTENT_SEEDS:
        if any(seed.lower() in text.lower() for seed in seeds):
            intents.append(intent)
    return intents or ["inform"]


def _stub_toxicity(text: str) -> tuple[float, float]:
    """Return (toxicity_score, hate_speech_score)."""
    neg_count = sum(1 for w in _BN_NEGATIVE_WORDS if w in text)
    base = min(0.1 * neg_count, 0.9)
    # hate_speech is a subset of toxicity
    hate = round(base * 0.5, 3)
    return round(base, 3), hate


def _stub_ner(text: str) -> list[dict]:
    entities = []
    for entity_text, label in _NER_SEEDS:
        if entity_text in text:
            entities.append(
                {"text": entity_text, "label": label, "confidence": 0.88}
            )
    return entities


def _stub_keywords(text: str) -> list[str]:
    """Extract a handful of longest words as stub keywords."""
    # Filter out very short tokens and punctuation
    tokens = re.findall(r"[\wঀ-৿]{4,}", text)
    seen: set[str] = set()
    unique = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    # Return up to 5, favouring longer tokens
    unique.sort(key=len, reverse=True)
    return unique[:5]


def _stub_embedding(text: str) -> list[float]:
    """Deterministic EMBEDDING_DIM-dim pseudo-embedding (shared with API/MCP).

    Dim must match the pgvector ``analysis_results.embedding`` column, so the
    stub lives in ``libs.embeddings`` and is shared by every embedding producer
    and consumer.
    """
    return _shared_stub_embedding(text)


# ---------------------------------------------------------------------------
# Real-model helpers (called when stub_mode is False)
# ---------------------------------------------------------------------------

def _real_language(text: str, detector: object) -> tuple[str, str, bool, float]:
    """Use fastText to detect language."""
    import numpy as np  # noqa: F401 (just in case)

    labels, probs = detector.predict(text.replace("\n", " "), k=1)  # type: ignore[union-attr]
    lang_code = labels[0].replace("__label__", "")
    confidence = float(probs[0])
    script = detect_script(text)
    banglish = is_banglish(text)
    return lang_code, script, banglish, confidence


def _real_sentiment(text: str, tokenizer: object, model: object) -> tuple[str, float, float]:
    """Run XLM-R sentiment classification."""
    import torch  # type: ignore
    import torch.nn.functional as F  # type: ignore

    inputs = tokenizer(  # type: ignore[operator]
        text,
        return_tensors="pt",
        truncation=True,
        max_length=512,
    )
    with torch.no_grad():
        logits = model(**inputs).logits  # type: ignore[operator]
    probs = F.softmax(logits, dim=-1).squeeze()
    label_map = {0: "negative", 1: "neutral", 2: "positive"}
    idx = int(probs.argmax())
    label = label_map.get(idx, "neutral")
    confidence = float(probs[idx])
    # Map to -1..1 score
    score_map = {"negative": -float(probs[0]), "neutral": 0.0, "positive": float(probs[2])}
    return label, round(score_map[label], 4), round(confidence, 4)


def _real_emotion(text: str, pipeline: object) -> dict:
    results = pipeline(text[:512])  # type: ignore[operator]
    if results and isinstance(results[0], list):
        results = results[0]
    scores = {r["label"].lower(): round(float(r["score"]), 4) for r in results}
    primary = max(scores, key=scores.get)  # type: ignore[arg-type]
    return {"primary": primary, "scores": scores}


def _real_toxicity(text: str, pipeline: object) -> tuple[float, float]:
    results = pipeline(text[:512])  # type: ignore[operator]
    if results and isinstance(results[0], list):
        results = results[0]
    score_map = {r["label"].lower(): float(r["score"]) for r in results}
    tox = score_map.get("toxic", score_map.get("toxicity", 0.0))
    hate = score_map.get("hate", score_map.get("hate_speech", 0.0))
    return round(tox, 4), round(hate, 4)


def _real_ner(text: str, ner_model: object) -> list[dict]:
    entity_types = ["PERSON", "ORG", "GPE", "LOC", "EVENT", "DATE"]
    try:
        entities = ner_model.predict_entities(text, entity_types)  # type: ignore[union-attr]
        return [
            {
                "text": e["text"],
                "label": e["label"],
                "confidence": round(float(e.get("score", 0.9)), 4),
            }
            for e in entities
        ]
    except Exception:
        return []


def _real_keywords(text: str, kw_model: object) -> list[str]:
    try:
        kws = kw_model.extract_keywords(  # type: ignore[union-attr]
            text,
            keyphrase_ngram_range=(1, 2),
            stop_words=None,
            top_n=5,
        )
        return [kw for kw, _ in kws]
    except Exception:
        return []


def _real_embedding(text: str, embed_model: object) -> list[float]:
    try:
        vector = embed_model.encode(text, normalize_embeddings=True)  # type: ignore[union-attr]
        # fit_dim guards against a model whose output dim != EMBEDDING_DIM
        # (logs a warning once and truncates/pads to the pgvector column dim).
        return fit_dim([round(float(v), 6) for v in vector])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

async def analyze_sentiment(text: str | None, registry: ModelRegistry) -> tuple[str, float, float]:
    """Sentiment-only classification for a single string — (label, score, confidence).

    A lightweight cousin of :func:`analyze_text` used for per-comment scoring.
    It deliberately skips the expensive embedding / NER / keyword stages so the
    hybrid comment classifier can score thousands of comments per post without
    generating a dense vector for each one. Uses the real XLM-R sentiment model
    when available (``MODEL_STUB_MODE=false``), otherwise the deterministic stub.
    """
    if not text or not text.strip():
        return "neutral", 0.0, 0.0

    text = text.strip()

    if registry.stub_mode:
        return _stub_sentiment(text)

    sent_pair = registry.get_sentiment_model()
    if sent_pair is not None:
        tokenizer, sent_model = sent_pair
        return _real_sentiment(text, tokenizer, sent_model)
    return _stub_sentiment(text)


async def analyze_text(text: str | None, registry: ModelRegistry) -> dict:
    """Run the full text NLP pipeline on a single text string.

    Returns a dict with all NLP fields.  When text is None or empty, returns
    an all-null / all-empty result so callers never have to guard against
    missing keys.
    """
    if not text or not text.strip():
        return _empty_result()

    text = text.strip()

    if registry.stub_mode:
        language, script, banglish, lang_conf = _stub_language(text)
        sentiment_label, sentiment_score, sentiment_conf = _stub_sentiment(text)
        emotion = _stub_emotion(sentiment_label, text)
        topics = _stub_topics(text)
        intents = _stub_intents(text)
        toxicity, hate = _stub_toxicity(text)
        entities = _stub_ner(text)
        keywords = _stub_keywords(text)
        embedding = _stub_embedding(text)
    else:
        # --- Language ---
        detector = registry.get_lang_detector()
        language, script, banglish, lang_conf = _real_language(text, detector)

        # --- Sentiment ---
        sent_pair = registry.get_sentiment_model()
        if sent_pair is not None:
            tokenizer, sent_model = sent_pair
            sentiment_label, sentiment_score, sentiment_conf = _real_sentiment(
                text, tokenizer, sent_model
            )
        else:
            sentiment_label, sentiment_score, sentiment_conf = _stub_sentiment(text)

        # --- Emotion ---
        emo_pipe = registry.get_emotion_pipeline()
        if emo_pipe is not None:
            emotion = _real_emotion(text, emo_pipe)
        else:
            emotion = _stub_emotion(sentiment_label, text)

        # --- Toxicity ---
        tox_pipe = registry.get_toxicity_pipeline()
        if tox_pipe is not None:
            toxicity, hate = _real_toxicity(text, tox_pipe)
        else:
            toxicity, hate = _stub_toxicity(text)

        # --- NER ---
        ner_model = registry.get_ner_model()
        if ner_model is not None:
            entities = _real_ner(text, ner_model)
        else:
            entities = _stub_ner(text)

        # --- Keywords ---
        kw_model = registry.get_keyword_model()
        if kw_model is not None:
            keywords = _real_keywords(text, kw_model)
        else:
            keywords = _stub_keywords(text)

        # --- Embedding ---
        embed_model = registry.get_embedding_model()
        if embed_model is not None:
            embedding = _real_embedding(text, embed_model)
        else:
            embedding = _stub_embedding(text)

        # Topics / intents remain heuristic for now (use stub logic as prior)
        topics = _stub_topics(text)
        intents = _stub_intents(text)

    return {
        "language": language,
        "script": script,
        "is_banglish": banglish,
        "language_confidence": lang_conf,
        "sentiment": sentiment_label,
        "sentiment_score": sentiment_score,
        "sentiment_confidence": sentiment_conf,
        "emotion": emotion,
        "topics": topics,
        "intents": intents,
        "toxicity_score": toxicity,
        "hate_speech_score": hate,
        "entities": entities,
        "keywords": keywords,
        "embedding": embedding,
    }
