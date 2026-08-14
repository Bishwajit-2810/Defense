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
  - paraphrase-multilingual-mpnet-base-v2 (SentenceTransformer, 768-dim — must
    match the pgvector analysis_results.embedding vector(768) column; see
    libs/embeddings.py) for dense embeddings
  - embedding-prototype cosine for topics/intents (no extra model — reuses the
    sentence embedding above), unioned with the keyword-seed heuristic
"""

from __future__ import annotations

import structlog
import re
import sys
import os

# Make the libs package importable when running from the service root
# ...and the repo root, for `libs.*` imports (no-op when PYTHONPATH already has it)

from defense.libs.common.utils import detect_script, is_banglish  # noqa: E402
from defense.libs.embeddings import fit_dim  # noqa: E402
from defense.libs.embeddings import stub_embedding as _shared_stub_embedding  # noqa: E402
from defense.libs.labels import POST_TYPE_PROTOTYPES  # noqa: E402
from defense.libs.sentiment_models import resolve as _resolve_sentiment  # noqa: E402

from .llm_analyzer import analyze_text_llm  # noqa: E402
from .models import ModelRegistry  # noqa: E402

logger = structlog.get_logger(__name__)

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

# Post-type seed patterns (stub mode, and a high-precision prior in real mode).
# Vocabulary is libs/labels.POST_TYPES minus "other" (the no-match fallback).
_POST_TYPE_SEEDS: list[tuple[str, list[str]]] = [
    ("political", [
        "রাজনীতি", "সরকার", "নির্বাচন", "আওয়ামী", "বিএনপি", "জামায়াত", "মন্ত্রী",
        "প্রধানমন্ত্রী", "সংসদ", "ভোট", "রাষ্ট্র", "ক্ষমতা", "দল",
        "politics", "govt", "government", "election", "minister", "vote", "regime",
    ]),
    ("religious", [
        "ইসলাম", "আল্লাহ", "নামাজ", "কুরআন", "হাদিস", "মসজিদ", "দোয়া", "রমজান",
        "ঈদ", "শহীদ", "শাহাদত", "হিন্দু", "পূজা", "ধর্ম", "ইনশাআল্লাহ",
        "islam", "allah", "quran", "hadith", "namaz", "dua", "inshallah", "alhamdulillah",
    ]),
    ("complaint", [
        "অভিযোগ", "দুর্নীতি", "অন্যায়", "প্রতিবাদ", "ক্ষোভ", "ব্যর্থ", "অবহেলা",
        "ভোগান্তি", "হয়রানি", "অপপ্রচার", "শোষণ", "অত্যাচার",
        "complaint", "corruption", "injustice", "negligence", "harassment", "shame",
    ]),
    ("news", [
        "সংবাদ", "খবর", "প্রতিবেদন", "জানা গেছে", "সূত্র", "বিজ্ঞপ্তি", "গ্রেফতার",
        "নিহত", "আহত", "উদ্ধার", "ঘোষণা",
        "breaking", "news", "report", "reportedly", "sources said", "arrested", "killed",
    ]),
    ("opinion", [
        "আমার মতে", "মনে করি", "মনে হয়", "উচিত", "বিশ্লেষণ", "প্রশ্ন হলো", "মতামত",
        "i think", "in my opinion", "imo", "we should", "arguably",
    ]),
    ("promotion", [
        "অফার", "ছাড়", "বিক্রি", "অর্ডার", "দাম", "ইনবক্স", "যোগাযোগ করুন", "হোম ডেলিভারি",
        "offer", "discount", "sale", "order now", "price", "inbox", "buy now", "whatsapp",
    ]),
    ("humor", [
        "হাহা", "মজা", "কৌতুক", "ট্রল", "হাসতে", "মিম",
        "funny", "lol", "haha", "meme", "troll", "😂", "🤣",
    ]),
    ("personal", [
        "জন্মদিন", "শুভ জন্মদিন", "বিয়ে", "বিবাহ", "আমার পরিবার", "আমার জীবন",
        "ধন্যবাদ সবাইকে", "দুআ চাই",
        "birthday", "anniversary", "graduation", "my wedding", "my family", "thank you all",
    ]),
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
        # No text at all is a genuine "unknown", not "other" — Rule 2 routes it.
        "post_type": None,
        "post_type_confidence": 0.0,
        "toxicity_score": None,
        "hate_speech_score": None,
        "entities": [],
        "keywords": [],
        "embedding": None,
        "embedding_is_stub": None,
        "sentiment_route": None,
        "sentiment_model": None,
        "engine": None,
        "llm_backend": None,
        "llm_model": None,
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


def _stub_post_type(text: str) -> tuple[str | None, float]:
    """Classify the semantic post type from seed words: (label, confidence).

    Returns ``(None, 0.0)`` when no seed matches — Stage 1 says "I don't know"
    rather than guessing "other", because the router's Rule 2 has to be able to
    tell those two apart (an unknown type is what earns an LLM call).

    The confidence is deliberately modest: one seed hit lands *below* the router
    threshold, so a post classified on a single keyword still goes to Stage 2.
    Two independent hits clear it. These numbers are heuristic, not calibrated —
    calibrating them needs the labeled set evaluation.md calls for.
    """
    lower = text.lower()
    hits: dict[str, int] = {}
    for label, seeds in _POST_TYPE_SEEDS:
        n = sum(1 for seed in seeds if seed.lower() in lower)
        if n:
            hits[label] = n
    if not hits:
        return None, 0.0

    ranked = sorted(hits, key=lambda k: hits[k], reverse=True)
    top = ranked[0]
    top_hits = hits[top]
    runner_hits = hits[ranked[1]] if len(ranked) > 1 else 0

    confidence = 0.45 + 0.12 * min(top_hits, 3)          # 0.57 .. 0.81
    confidence -= 0.08 * min(runner_hits, 2)             # competing categories
    return top, round(max(0.30, min(confidence, 0.85)), 3)


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

def _real_language(text: str, detector: object) -> tuple[str, str, bool, float, str]:
    """Detect language, returning ``(lang, script, banglish, confidence, method)``.

    ``detector`` may be None — fastText is optional, and its getter now degrades
    rather than killing the post. The fallback is script-based detection, which
    for this corpus is the substantive part anyway (bn vs Latin vs code-mixed);
    fastText mainly contributes confidence calibration. `method` reports which
    ran, so the degradation is visible rather than inferred.
    """
    script = detect_script(text)
    banglish = is_banglish(text)

    if detector is not None:
        try:
            labels, probs = detector.predict(text.replace("\n", " "), k=1)  # type: ignore[union-attr]
            return (
                labels[0].replace("__label__", ""),
                script,
                banglish,
                float(probs[0]),
                "fasttext",
            )
        except Exception as exc:
            logger.warning("fastText prediction failed, using script detection", error=str(exc))

    # Deterministic fallback. Confidence is deliberately moderate: this is a
    # script heuristic, not a calibrated classifier, and overstating it would
    # feed the router's confidence gate a number it has not earned.
    lang_code = "bn" if script == "bengali" else ("mixed" if script == "mixed" else "en")
    return lang_code, script, banglish, 0.6, "script_heuristic"


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


def _real_sentiment_batch(
    texts: list[str], tokenizer: object, model: object
) -> list[tuple[str, float, float]]:
    """Run XLM-R sentiment over a BATCH in one forward pass (§5.11).

    Per-comment inference was one `tokenizer(...)` + one `model(...)` call per
    comment — 8,513 of 10,272 comments took that path with no batching, despite
    transformer inference being 10-30x faster batched. Before any latency
    benchmark is quoted, this matters: an unbatched throughput number is an
    artefact of a missing `batch` argument, not a property of the architecture.

    Padding to the longest sequence in the batch (not to 512) keeps the win
    real — a batch of short comments stays cheap.
    """
    if not texts:
        return []
    import torch  # type: ignore
    import torch.nn.functional as F  # type: ignore

    inputs = tokenizer(  # type: ignore[operator]
        texts,
        return_tensors="pt",
        truncation=True,
        max_length=512,
        padding=True,
    )
    with torch.no_grad():
        logits = model(**inputs).logits  # type: ignore[operator]
    probs = F.softmax(logits, dim=-1)

    label_map = {0: "negative", 1: "neutral", 2: "positive"}
    out: list[tuple[str, float, float]] = []
    for row in probs:
        idx = int(row.argmax())
        label = label_map.get(idx, "neutral")
        confidence = float(row[idx])
        score_map = {
            "negative": -float(row[0]),
            "neutral": 0.0,
            "positive": float(row[2]),
        }
        out.append((label, round(score_map[label], 4), round(confidence, 4)))
    return out


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


def _embed_with_provenance(text: str, embed_model: object) -> tuple[list[float], bool]:
    """``(vector, is_stub)`` — the honest pair, produced where the truth is known.

    The stub is "a deterministic unit vector seeded from the text hash (not
    semantic)", and it is **EMBEDDING_DIM-sized**, exactly like a real one. So no
    downstream consumer can recover this distinction from the vector itself.
    `persistence._resolve_embedding` tried, using the dimension, and therefore
    recorded every stub written in the default configuration as a real vector —
    the inverse of what the `embedding_is_stub` column exists to say
    (PROJECT_ASSESSMENT §13.2). This returns the flag from the one place that
    knows, so nothing has to guess.

    A real model that raises falls back to the stub **and says so**, rather than
    returning the empty vector it used to — which read downstream as "no
    embedding" and silently became a *different* stub, seeded from the post_id.
    """
    if embed_model is not None:
        vec = _real_embedding(text, embed_model)
        if vec:
            return vec, False
    return _stub_embedding(text), True


# ---------------------------------------------------------------------------
# Embedding-prototype topic / intent classification (real mode)
#
# Reuses the sentence embedding Stage-1 already computes (no extra model, per
# architecture §10 "one pass, many tasks"): each label is embedded once into a
# prototype vector and we score the post against them by cosine similarity. The
# keyword seeds remain a high-precision prior — we union the two. Floors are
# tunable from a labeled validation set (plan.md / evaluation.md).
# ---------------------------------------------------------------------------

# Label -> short descriptive phrase (embedded as the prototype). "general" /
# "inform" are deliberately excluded: they are the fallbacks when nothing scores
# above the floor.
_TOPIC_LABELS: dict[str, str] = {
    "politics": "politics, government, elections, political parties",
    "religion": "religion, islam, faith, mosque, religious practice",
    "crime": "crime, police, arrest, murder, law enforcement",
    "protest": "protest, demonstration, rally, strike, movement",
    "grief": "grief, mourning, death, remembrance, condolence",
    "india": "India, Indian politics or relations",
    "bangladesh": "Bangladesh, Dhaka, national affairs",
    "economy": "economy, prices, business, market, inflation",
    "sports": "sports, cricket, football, match, tournament",
    "entertainment": "entertainment, film, music, celebrity, drama",
}

_INTENT_LABELS: dict[str, str] = {
    "express_grievance": "expressing anger, grievance, or complaint",
    "commemorate": "commemorating or remembering a person or event",
    "call_to_action": "calling people to act, share, support, or vote",
    "question": "asking a question",
    "promote": "promoting or advertising a product or service",
}

from defense.libs.common.config import get_settings
config = get_settings()

# Tunable similarity floors / multi-label margin.
_TOPIC_PROTO_FLOOR: float = config.topic_proto_floor
_INTENT_PROTO_FLOOR: float = config.intent_proto_floor
_PROTO_MARGIN: float = config.proto_margin

# Prototype-vector cache, keyed by "<label-set>|<model-name>" so a model swap
# (EMBEDDING_MODEL change) transparently re-embeds the prototypes.
_PROTOTYPE_CACHE: dict[str, dict[str, list[float]]] = {}


def _embed_prototypes(name: str, labels: dict[str, str], embed_model: object) -> dict[str, list[float]]:
    """Embed each label phrase once (cached). Vectors are L2-normalized."""
    key = f"{name}|{config.embedding_model}"
    cached = _PROTOTYPE_CACHE.get(key)
    if cached is not None:
        return cached
    protos: dict[str, list[float]] = {}
    for label, phrase in labels.items():
        vec = embed_model.encode(phrase, normalize_embeddings=True)  # type: ignore[union-attr]
        protos[label] = [float(v) for v in vec]
    _PROTOTYPE_CACHE[key] = protos
    return protos


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity; safe for already-normalized or raw vectors."""
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    dot = sum(a[i] * b[i] for i in range(n))
    na = sum(x * x for x in a[:n]) ** 0.5
    nb = sum(x * x for x in b[:n]) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _classify_by_prototype(
    embedding: list[float] | None,
    name: str,
    labels: dict[str, str],
    embed_model: object,
    floor: float,
    top_k: int,
) -> list[str]:
    """Return labels whose prototype cosine clears the floor, near the top score.

    Empty list when the post embedding is missing or nothing beats ``floor`` —
    the caller then falls back to the keyword heuristic / default label.
    """
    if not embedding:
        return []
    protos = _embed_prototypes(name, labels, embed_model)
    sims = {label: _cosine(embedding, vec) for label, vec in protos.items()}
    ranked = sorted(sims, key=lambda label: sims[label], reverse=True)
    top_sim = sims[ranked[0]]
    if top_sim < floor:
        return []
    cutoff = max(floor, top_sim - _PROTO_MARGIN)
    return [label for label in ranked if sims[label] >= cutoff][:top_k]


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

_POST_TYPE_PROTO_FLOOR: float = config.post_type_proto_floor


def _post_type_by_prototype(
    embedding: list[float] | None,
    embed_model: object,
) -> tuple[str | None, float]:
    """Zero-shot post_type from label-prototype cosine: (label, confidence).

    Confidence is the top similarity rescaled over [floor, 0.60] — an *ordering*
    signal, not a calibrated probability, and it is capped at 0.9 to say so. The
    same caveat as the topic/intent floors above applies: the mapping should be
    fitted on a labeled validation set (evaluation.md §2).
    """
    if not embedding:
        return None, 0.0
    protos = _embed_prototypes("post_type", POST_TYPE_PROTOTYPES, embed_model)
    sims = {label: _cosine(embedding, vec) for label, vec in protos.items()}
    ranked = sorted(sims, key=lambda label: sims[label], reverse=True)
    top, top_sim = ranked[0], sims[ranked[0]]
    if top_sim < _POST_TYPE_PROTO_FLOOR:
        return None, 0.0
    span = max(0.60 - _POST_TYPE_PROTO_FLOOR, 1e-6)
    confidence = 0.5 + 0.4 * min((top_sim - _POST_TYPE_PROTO_FLOOR) / span, 1.0)
    # A close runner-up means the post sits between two categories.
    runner_sim = sims[ranked[1]] if len(ranked) > 1 else 0.0
    if top_sim - runner_sim < _PROTO_MARGIN:
        confidence -= 0.1
    return top, round(max(0.30, min(confidence, 0.90)), 3)


def _combine_post_type(
    seed: tuple[str | None, float],
    proto: tuple[str | None, float],
) -> tuple[str | None, float]:
    """Merge the seed-keyword and prototype verdicts into one (label, confidence)."""
    seed_label, seed_conf = seed
    proto_label, proto_conf = proto
    if seed_label and proto_label:
        if seed_label == proto_label:
            # Two independent methods agreeing is the strongest signal available.
            return seed_label, round(min(0.95, max(seed_conf, proto_conf) + 0.10), 3)
        # Disagreement: keep the stronger label but report the *lower* confidence,
        # so a contested post routes to Stage 2 instead of shipping a coin flip.
        label = seed_label if seed_conf >= proto_conf else proto_label
        return label, round(min(seed_conf, proto_conf), 3)
    if seed_label:
        return seed_label, seed_conf
    return proto_label, proto_conf


async def analyze_sentiment(
    text: str | None,
    registry: ModelRegistry,
    sentiment_override: str | None = None,
) -> tuple[str, float, float]:
    """Sentiment-only classification for a single string — (label, score, confidence).

    A lightweight cousin of :func:`analyze_text` used for per-comment scoring.
    It deliberately skips the expensive embedding / NER / keyword stages so the
    hybrid comment classifier can score thousands of comments per post without
    generating a dense vector for each one. The sentiment model is chosen by the
    comment's own language (BanglaBERT/BanglishBERT/XLM-R via the shared router),
    honouring ``sentiment_override``; falls back to the deterministic stub when
    ``MODEL_STUB_MODE=true`` or the model is unavailable.

    Thin wrapper over :func:`analyze_sentiment_engine`, which additionally
    reports *which* engine produced the label.
    """
    label, score, conf, _engine = await analyze_sentiment_engine(
        text, registry, sentiment_override
    )
    return label, score, conf


async def analyze_sentiment_engine(
    text: str | None,
    registry: ModelRegistry,
    sentiment_override: str | None = None,
) -> tuple[str, float, float, str]:
    """Like :func:`analyze_sentiment`, plus the engine that produced the label.

    The engine is ``"model"`` only when a transformer actually ran. It is
    ``"stub"`` both in ``MODEL_STUB_MODE`` and when a real-mode model turned out
    to be unavailable — the two cases are indistinguishable in the output
    otherwise, and callers were tagging both as model inferences. Callers must
    report this rather than assume, because ``_stub_sentiment`` derives its
    label from ``sum(ord(c) for c in text[:50]) % 100`` — deterministic and
    reproducible, and not sentiment.
    """
    if not text or not text.strip():
        return "neutral", 0.0, 0.0, "empty"

    text = text.strip()

    if registry.stub_mode:
        label, score, conf = _stub_sentiment(text)
        return label, score, conf, "stub"

    _key, hf_name = _resolve_sentiment(
        detect_script(text), is_banglish(text), None, sentiment_override
    )
    sent_pair = registry.get_sentiment_model(hf_name)
    if sent_pair is not None:
        tokenizer, sent_model = sent_pair
        label, score, conf = _real_sentiment(text, tokenizer, sent_model)
        return label, score, conf, "model"
    label, score, conf = _stub_sentiment(text)
    return label, score, conf, "stub"


async def analyze_sentiment_batch(
    texts: list[str],
    registry: ModelRegistry,
    sentiment_override: str | None = None,
) -> list[tuple[str, float, float, str]]:
    """Sentiment for MANY strings, batched per resolved model (§5.11).

    Returns one ``(label, score, confidence, engine)`` per input, in order.

    Comments are grouped by the model their language routes them to, then each
    group runs as a single forward pass. Unbatched inference was the single
    biggest throughput lever left in the pipeline: 8,513 of 10,272 comments went
    through one `model(...)` call each.

    Falls back to the per-item path for stub mode and for any group whose model
    is unavailable, so behaviour is identical — only the speed changes.
    """
    results: list[tuple[str, float, float, str] | None] = [None] * len(texts)

    # Empty and whitespace-only inputs never reach a model.
    pending: list[int] = []
    for i, t in enumerate(texts):
        if not t or not t.strip():
            results[i] = ("neutral", 0.0, 0.0, "empty")
        else:
            pending.append(i)

    if registry.stub_mode:
        for i in pending:
            label, score, conf = _stub_sentiment(texts[i].strip())
            results[i] = (label, score, conf, "stub")
        return [r for r in results if r is not None]

    # Group by the model each text's language routes it to — a batch must be
    # one model, and this corpus mixes bn / en / banglish within a thread.
    groups: dict[str, list[int]] = {}
    for i in pending:
        text = texts[i].strip()
        _key, hf_name = _resolve_sentiment(
            detect_script(text), is_banglish(text), None, sentiment_override
        )
        groups.setdefault(hf_name, []).append(i)

    for hf_name, idxs in groups.items():
        pair = registry.get_sentiment_model(hf_name)
        if pair is None:
            for i in idxs:
                label, score, conf = _stub_sentiment(texts[i].strip())
                results[i] = (label, score, conf, "stub")
            continue
        tokenizer, model = pair
        try:
            batch = [texts[i].strip() for i in idxs]
            for i, (label, score, conf) in zip(
                idxs, _real_sentiment_batch(batch, tokenizer, model)
            ):
                results[i] = (label, score, conf, "model")
        except Exception as exc:
            # One bad batch must not cost the post its labels.
            logger.warning("batched sentiment failed", hf_name=hf_name, error=str(exc))
            for i in idxs:
                label, score, conf = _stub_sentiment(texts[i].strip())
                results[i] = (label, score, conf, "stub")

    return [r if r is not None else ("neutral", 0.0, 0.0, "stub") for r in results]


async def _analyze_text_llm_path(
    text: str,
    registry: ModelRegistry,
    backend_override: str | None,
) -> dict | None:
    """Run Stage-1 NLP via the `stage1` LLM. Returns None on any failure.

    The LLM produces the classification/extraction fields; script/Banglish flags
    are computed deterministically and the embedding still comes from the
    SentenceTransformer (or the shared stub), since a chat LLM can't emit one.
    """
    llm = registry.get_llm_client()
    if llm is None:
        return None
    try:
        nlp = await analyze_text_llm(text, llm, backend_override)
    except Exception as exc:
        logger.warning("stage1 LLM text analysis failed, falling back to stub", error=str(exc))
        return None

    script = detect_script(text)
    banglish = is_banglish(text)
    language = nlp.get("language") or _stub_language(text)[0]

    # Fall back to the seed heuristic when the LLM declined to classify the post
    # type, so Rule 2 escalates only genuinely unclassifiable posts.
    post_type, post_type_conf = nlp["post_type"], nlp["post_type_confidence"]
    if post_type is None:
        post_type, post_type_conf = _stub_post_type(text)

    # Embedding: real model when available (non-stub), else the shared stub.
    embed_model = registry.get_embedding_model()
    embedding, embedding_is_stub = _embed_with_provenance(text, embed_model)

    return {
        "language": language,
        "script": script,
        "is_banglish": banglish,
        "language_confidence": 0.9 if nlp.get("language") else 0.75,
        "sentiment": nlp["sentiment"],
        "sentiment_score": nlp["sentiment_score"],
        "sentiment_confidence": nlp["sentiment_confidence"],
        "emotion": nlp["emotion"],
        "topics": nlp["topics"],
        "intents": nlp["intents"],
        "post_type": post_type,
        "post_type_confidence": post_type_conf,
        "toxicity_score": nlp["toxicity_score"],
        "hate_speech_score": nlp["hate_speech_score"],
        "entities": nlp["entities"],
        "keywords": nlp["keywords"],
        "embedding": embedding,
        # Whether that vector is the deterministic hash stub. Carried, never
        # inferred from its shape — see _embed_with_provenance (§13.2).
        "embedding_is_stub": embedding_is_stub,
        # The "model" is the stage1 LLM itself; route records that the LLM produced it.
        "sentiment_route": "llm:stage1",
        "sentiment_model": nlp.get("_llm_model"),
        "engine": "llm",
        "llm_backend": nlp.get("_llm_backend"),
        "llm_model": nlp.get("_llm_model"),
    }


async def analyze_text(
    text: str | None,
    registry: ModelRegistry,
    sentiment_override: str | None = None,
    backend_override: str | None = None,
) -> dict:
    """Run the full text NLP pipeline on a single text string.

    Returns a dict with all NLP fields.  When text is None or empty, returns
    an all-null / all-empty result so callers never have to guard against
    missing keys.
    """
    if not text or not text.strip():
        return _empty_result()

    text = text.strip()

    # LLM-backed Stage-1 path (STAGE1_LLM=true). Falls back to stub/real on error.
    if registry.llm_mode:
        llm_result = await _analyze_text_llm_path(text, registry, backend_override)
        if llm_result is not None:
            return llm_result

    if registry.stub_mode:
        language, script, banglish, lang_conf = _stub_language(text)
        # Record which model the router *would* pick (transparency in stub mode);
        # the stub sentiment is model-independent.
        sent_route, sent_model_name = _resolve_sentiment(
            script, banglish, language, sentiment_override
        )
        sentiment_label, sentiment_score, sentiment_conf = _stub_sentiment(text)
        emotion = _stub_emotion(sentiment_label, text)
        topics = _stub_topics(text)
        intents = _stub_intents(text)
        post_type, post_type_conf = _stub_post_type(text)
        toxicity, hate = _stub_toxicity(text)
        entities = _stub_ner(text)
        keywords = _stub_keywords(text)
        embedding = _stub_embedding(text)
        embedding_is_stub = True
        engine = "stub"
        lang_method = "stub"
    else:
        engine = "models"
        # --- Language ---
        detector = registry.get_lang_detector()
        language, script, banglish, lang_conf, lang_method = _real_language(text, detector)

        # --- Sentiment (language-routed model) ---
        sent_route, sent_model_name = _resolve_sentiment(
            script, banglish, language, sentiment_override
        )
        sent_pair = registry.get_sentiment_model(sent_model_name)
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
        embedding, embedding_is_stub = _embed_with_provenance(text, embed_model)

        # --- Topics / intents ---
        # Union the keyword seeds (high precision) with embedding-prototype
        # classification (recall for paraphrases / Banglish), reusing the post
        # embedding. Prototypes need a real embedding model + real (non-stub)
        # vectors; otherwise fall back to the heuristic alone.
        #
        # `not embedding_is_stub` is the second half of that sentence, and it used
        # to be missing: a real model that *failed* left an empty vector, which
        # `_classify_by_prototype` read as "no embedding" and skipped. Now the
        # fallback returns a usable stub, so the flag — not the vector's
        # emptiness — is what keeps prototypes off hash noise.
        if embed_model is not None and not embedding_is_stub:
            proto_topics = _classify_by_prototype(
                embedding, "topics", _TOPIC_LABELS, embed_model, _TOPIC_PROTO_FLOOR, top_k=3
            )
            proto_intents = _classify_by_prototype(
                embedding, "intents", _INTENT_LABELS, embed_model, _INTENT_PROTO_FLOOR, top_k=2
            )
            seed_topics = [t for t in _stub_topics(text) if t != "general"]
            seed_intents = [i for i in _stub_intents(text) if i != "inform"]
            # dict.fromkeys preserves order while de-duplicating.
            topics = list(dict.fromkeys(seed_topics + proto_topics)) or ["general"]
            intents = list(dict.fromkeys(seed_intents + proto_intents)) or ["inform"]
            post_type, post_type_conf = _combine_post_type(
                _stub_post_type(text),
                _post_type_by_prototype(embedding, embed_model),
            )
        else:
            topics = _stub_topics(text)
            intents = _stub_intents(text)
            post_type, post_type_conf = _stub_post_type(text)

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
        # Stage-1's own best-effort post_type. Stage 2 refines it when the router
        # asks (low confidence / unknown); the assembler prefers Stage 2's.
        "post_type": post_type,
        "post_type_confidence": post_type_conf,
        "toxicity_score": toxicity,
        "hate_speech_score": hate,
        "entities": entities,
        "keywords": keywords,
        "embedding": embedding,
        # Whether that vector is the deterministic hash stub. Carried, never
        # inferred from its shape — see _embed_with_provenance (§13.2).
        "embedding_is_stub": embedding_is_stub,
        # Which sentiment model the language router selected for this post.
        "sentiment_route": sent_route,
        "sentiment_model": sent_model_name,
        # Which engine produced this result: "stub" | "models" | "llm".
        "engine": engine,
        # How language was detected: "fasttext" | "script_heuristic" | "stub".
        # fastText is optional; when it is absent the pipeline degrades to script
        # detection rather than failing the post, and says which ran.
        "language_method": lang_method,
        "llm_backend": None,
        "llm_model": None,
    }
