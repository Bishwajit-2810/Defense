"""Model registry — lazy-loads all ML models on first use.

When MODEL_STUB_MODE=true (the default in dev/CI) every getter returns None
and the analyzers fall back to deterministic stub logic.  In production
(MODEL_STUB_MODE=false) each getter downloads / loads the real weights once
and caches the instance.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class ModelRegistry:
    """Central registry that owns all model handles.

    All attributes are private; call the getters so that lazy-loading and
    stub-mode short-circuits are applied consistently.
    """

    def __init__(self) -> None:
        self._stub_mode: bool = (
            os.getenv("MODEL_STUB_MODE", "true").lower() == "true"
        )
        # Cached model handles (None until first use)
        self._lang_detector: Any = None
        self._sentiment_tokenizer: Any = None
        self._sentiment_model: Any = None
        self._clip_processor: Any = None
        self._clip_model: Any = None
        self._ner_model: Any = None
        self._keyword_model: Any = None
        self._embedding_model: Any = None
        # Emotion / toxicity / topic classifiers (share the same HF pipeline API)
        self._emotion_pipeline: Any = None
        self._toxicity_pipeline: Any = None
        self._topic_pipeline: Any = None

    # ------------------------------------------------------------------
    # Public property
    # ------------------------------------------------------------------

    @property
    def stub_mode(self) -> bool:
        """True when running in stub mode (no GPU / model weights needed)."""
        return self._stub_mode

    # ------------------------------------------------------------------
    # Language detection
    # ------------------------------------------------------------------

    def get_lang_detector(self) -> Any:
        """Return a fastText language-identification model or None in stub mode."""
        if self._stub_mode:
            return None
        if self._lang_detector is None:
            try:
                import fasttext  # type: ignore

                # fastText ships a pre-trained lid.176.bin model; honour an
                # env-var override so the operator can point at a local copy.
                model_path = os.getenv(
                    "FASTTEXT_LANG_MODEL",
                    "/models/fasttext/lid.176.bin",
                )
                self._lang_detector = fasttext.load_model(model_path)
                logger.info("fastText language model loaded from %s", model_path)
            except Exception as exc:
                logger.error("Failed to load fastText model: %s", exc)
                raise
        return self._lang_detector

    # ------------------------------------------------------------------
    # Text sentiment / zero-shot classification
    # ------------------------------------------------------------------

    def get_sentiment_model(self) -> tuple[Any, Any] | None:
        """Return (tokenizer, model) for XLM-R/mBERT sentiment or None in stub mode."""
        if self._stub_mode:
            return None
        if self._sentiment_model is None:
            try:
                from transformers import (  # type: ignore
                    AutoModelForSequenceClassification,
                    AutoTokenizer,
                )

                model_name = os.getenv(
                    "SENTIMENT_MODEL",
                    "cardiffnlp/twitter-xlm-roberta-base-sentiment",
                )
                self._sentiment_tokenizer = AutoTokenizer.from_pretrained(model_name)
                self._sentiment_model = (
                    AutoModelForSequenceClassification.from_pretrained(model_name)
                )
                self._sentiment_model.eval()
                logger.info("Sentiment model loaded: %s", model_name)
            except Exception as exc:
                logger.error("Failed to load sentiment model: %s", exc)
                raise
        return (self._sentiment_tokenizer, self._sentiment_model)

    def get_emotion_pipeline(self) -> Any:
        """Return a HuggingFace pipeline for emotion detection or None in stub mode."""
        if self._stub_mode:
            return None
        if self._emotion_pipeline is None:
            try:
                from transformers import pipeline  # type: ignore

                model_name = os.getenv(
                    "EMOTION_MODEL",
                    "j-hartmann/emotion-english-distilroberta-base",
                )
                self._emotion_pipeline = pipeline(
                    "text-classification",
                    model=model_name,
                    top_k=None,
                )
                logger.info("Emotion pipeline loaded: %s", model_name)
            except Exception as exc:
                logger.error("Failed to load emotion pipeline: %s", exc)
                raise
        return self._emotion_pipeline

    def get_toxicity_pipeline(self) -> Any:
        """Return a HuggingFace pipeline for toxicity detection or None in stub mode."""
        if self._stub_mode:
            return None
        if self._toxicity_pipeline is None:
            try:
                from transformers import pipeline  # type: ignore

                model_name = os.getenv(
                    "TOXICITY_MODEL",
                    "unitary/toxic-bert",
                )
                self._toxicity_pipeline = pipeline(
                    "text-classification",
                    model=model_name,
                    top_k=None,
                )
                logger.info("Toxicity pipeline loaded: %s", model_name)
            except Exception as exc:
                logger.error("Failed to load toxicity pipeline: %s", exc)
                raise
        return self._toxicity_pipeline

    # ------------------------------------------------------------------
    # Vision (SigLIP / CLIP)
    # ------------------------------------------------------------------

    def get_clip_processor(self) -> Any:
        """Return a CLIP/SigLIP processor or None in stub mode."""
        if self._stub_mode:
            return None
        if self._clip_processor is None:
            self._load_clip()
        return self._clip_processor

    def get_clip_model(self) -> Any:
        """Return a CLIP/SigLIP model or None in stub mode."""
        if self._stub_mode:
            return None
        if self._clip_model is None:
            self._load_clip()
        return self._clip_model

    def _load_clip(self) -> None:
        try:
            from transformers import (  # type: ignore
                AutoProcessor,
                AutoModel,
            )

            model_name = os.getenv(
                "CLIP_MODEL",
                "google/siglip-base-patch16-224",
            )
            self._clip_processor = AutoProcessor.from_pretrained(model_name)
            self._clip_model = AutoModel.from_pretrained(model_name)
            self._clip_model.eval()
            logger.info("CLIP/SigLIP model loaded: %s", model_name)
        except Exception as exc:
            logger.error("Failed to load CLIP model: %s", exc)
            raise

    # ------------------------------------------------------------------
    # NER (GLiNER / spaCy)
    # ------------------------------------------------------------------

    def get_ner_model(self) -> Any:
        """Return a NER model instance or None in stub mode."""
        if self._stub_mode:
            return None
        if self._ner_model is None:
            try:
                # Prefer GLiNER when available; fall back to spaCy
                model_name = os.getenv("NER_MODEL", "gliner")
                if model_name == "gliner":
                    from gliner import GLiNER  # type: ignore

                    self._ner_model = GLiNER.from_pretrained(
                        "urchade/gliner_multi-v2.1"
                    )
                else:
                    import spacy  # type: ignore

                    self._ner_model = spacy.load(model_name)
                logger.info("NER model loaded: %s", model_name)
            except Exception as exc:
                logger.error("Failed to load NER model: %s", exc)
                raise
        return self._ner_model

    # ------------------------------------------------------------------
    # Keywords (KeyBERT)
    # ------------------------------------------------------------------

    def get_keyword_model(self) -> Any:
        """Return a KeyBERT instance or None in stub mode."""
        if self._stub_mode:
            return None
        if self._keyword_model is None:
            try:
                from keybert import KeyBERT  # type: ignore

                self._keyword_model = KeyBERT()
                logger.info("KeyBERT model loaded")
            except Exception as exc:
                logger.error("Failed to load KeyBERT: %s", exc)
                raise
        return self._keyword_model

    # ------------------------------------------------------------------
    # Embeddings (sentence-transformers)
    # ------------------------------------------------------------------

    def get_embedding_model(self) -> Any:
        """Return a SentenceTransformer embedding model or None in stub mode."""
        if self._stub_mode:
            return None
        if self._embedding_model is None:
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore

                # Default must be a 768-dim model — the pgvector column
                # (analysis_results.embedding) is vector(768). bge-m3 is
                # 1024-dim and would be truncated; see libs/embeddings.py.
                model_name = os.getenv(
                    "EMBEDDING_MODEL",
                    "paraphrase-multilingual-mpnet-base-v2",
                )
                self._embedding_model = SentenceTransformer(model_name)
                logger.info("Embedding model loaded: %s", model_name)
            except Exception as exc:
                logger.error("Failed to load embedding model: %s", exc)
                raise
        return self._embedding_model
