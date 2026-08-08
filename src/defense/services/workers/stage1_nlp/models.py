"""Model registry — lazy-loads all ML models on first use.

When MODEL_STUB_MODE=true (the default in dev/CI) every getter returns None
and the analyzers fall back to deterministic stub logic.  In production
(MODEL_STUB_MODE=false) each getter downloads / loads the real weights once
and caches the instance.
"""

from __future__ import annotations

import logging
import os
from defense.libs.common.config import get_settings
config = get_settings()
from typing import Any

logger = logging.getLogger(__name__)


class ModelRegistry:
    """Central registry that owns all model handles.

    All attributes are private; call the getters so that lazy-loading and
    stub-mode short-circuits are applied consistently.
    """

    def __init__(self) -> None:
        self._stub_mode: bool = (
            config.model_stub_mode
        )
        # LLM-backed Stage-1 NLP: when true, the caption + comments are analysed
        # by the `stage1` LLM (gemma3:4b on local Ollama by default) instead of
        # the small-model suite / stub. Independent of stub_mode: the LLM can
        # carry the NLP while embeddings still fall back to the stub. Any LLM
        # failure degrades to the deterministic stub in the analyzers.
        self._llm_mode: bool = config.stage1_llm
        self._llm_client: Any = None
        # Cached model handles (None until first use)
        self._lang_detector: Any = None
        # One flag per component: a 10k-post batch must not emit 10k copies
        # of the same import error.
        self._lang_detector_failed = False
        self._failed_sentiment_models: set[str] = set()
        self._emotion_failed = False
        self._toxicity_failed = False
        self._clip_failed = False
        self._ner_failed = False
        self._keyword_failed = False
        self._embedding_failed = False
        # Sentiment models are cached per HF checkpoint name so the language-aware
        # router (libs/sentiment_models.py) can switch between them at runtime.
        self._sentiment_models: dict[str, tuple[Any, Any]] = {}
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

    @property
    def llm_mode(self) -> bool:
        """True when Stage-1 NLP runs on the `stage1` LLM (STAGE1_LLM=true)."""
        return self._llm_mode

    def get_llm_client(self) -> Any:
        """Return the shared backend-agnostic LLMClient, or None if disabled.

        Lazily constructed on first use so stub/CI runs never import the OpenAI
        client. The `stage1` role resolves to STAGE1_LOCAL_MODEL (gemma3:4b).
        """
        if not self._llm_mode:
            return None
        if self._llm_client is None:
            try:
                from defense.libs.llm import LLMClient  # noqa: PLC0415

                self._llm_client = LLMClient()
                logger.info("Stage-1 LLMClient initialised (STAGE1_LLM=true)")
            except Exception as exc:
                logger.error("Failed to init Stage-1 LLMClient", error=str(exc))
                # Disable llm_mode so callers stop retrying and use the stub.
                self._llm_mode = False
                return None
        return self._llm_client

    def degraded_components(self) -> list[str]:
        """Components that fell back because their model could not be loaded.

        Real mode reports ``engine: "models"`` because MODEL_STUB_MODE is false —
        but that says which path was *intended*, not which ran. With every
        optional dependency missing, a run can report `engine: "models"` while
        producing entirely heuristic output, which is the §5.2 failure
        (provenance recording the intended code path rather than the executed
        one) reappearing one layer over.

        This list is the executed truth. Empty means nothing degraded.
        """
        degraded: list[str] = []
        if self._lang_detector_failed:
            degraded.append("language")
        if self._failed_sentiment_models:
            degraded.append("sentiment")
        if self._emotion_failed:
            degraded.append("emotion")
        if self._toxicity_failed:
            degraded.append("toxicity")
        if self._clip_failed:
            degraded.append("vision")
        if self._ner_failed:
            degraded.append("ner")
        if self._keyword_failed:
            degraded.append("keywords")
        if self._embedding_failed:
            degraded.append("embedding")
        return degraded

    # ------------------------------------------------------------------
    # Language detection
    # ------------------------------------------------------------------

    def get_lang_detector(self) -> Any:
        """Return a fastText language-identification model, or None if unavailable.

        Returns None — it does not raise. This used to re-raise, which made it
        the ONLY model getter that could kill a post: every sibling
        (`get_sentiment_model`, `get_emotion_pipeline`, the CLIP pair) returns
        None and lets the caller degrade. So a single missing optional
        dependency took down the whole real-mode pipeline at the first post,
        which is what blocks §9.8 from even starting on a fresh checkout.

        Degrading is safe here because the fallback is genuinely reasonable:
        language detection for this corpus is script-based (`detect_script` /
        `is_banglish`), and fastText's contribution is mostly confidence
        calibration. The caller records that the deterministic detector ran, so
        the degradation is reported rather than hidden — the standard this
        codebase is held to elsewhere (§5.2, §5.3).
        """
        if self._stub_mode:
            return None
        if self._lang_detector is None and not self._lang_detector_failed:
            try:
                import fasttext  # type: ignore

                # fastText ships a pre-trained lid.176.bin model; honour an
                # env-var override so the operator can point at a local copy.
                model_path = config.fasttext_lang_model
                self._lang_detector = fasttext.load_model(model_path)
                logger.info("fastText language model loaded", path=model_path)
            except Exception as exc:
                # Log ONCE, not once per post — a 10k-post batch must not emit
                # 10k identical errors.
                self._lang_detector_failed = True
                logger.error(
                    "fastText unavailable (%s) — falling back to deterministic "
                    "script detection for language. Install fasttext and set "
                    "FASTTEXT_LANG_MODEL to restore model-based detection.",
                    exc,
                )
        return self._lang_detector

    # ------------------------------------------------------------------
    # Text sentiment / zero-shot classification
    # ------------------------------------------------------------------

    def get_sentiment_model(self, model_name: str | None = None) -> tuple[Any, Any] | None:
        """Return (tokenizer, model) for the named sentiment checkpoint.

        ``model_name`` is the HF checkpoint chosen by the language-aware router
        (libs/sentiment_models.py); defaults to ``SENTIMENT_MODEL`` when omitted.
        Each distinct checkpoint is loaded once and cached, so switching models
        at runtime only pays the load cost on first use of each.
        Returns None in stub mode.
        """
        if self._stub_mode:
            return None

        name = model_name or config.sentiment_model
        # Already known-unloadable: return without re-logging. A 10k-post batch
        # must not emit 10k copies of the same import error.
        if name in self._failed_sentiment_models:
            return None
        cached = self._sentiment_models.get(name)
        if cached is not None:
            return cached

        try:
            from transformers import (  # type: ignore
                AutoModelForSequenceClassification,
                AutoTokenizer,
            )

            tokenizer = AutoTokenizer.from_pretrained(name)
            model = AutoModelForSequenceClassification.from_pretrained(name)
            model.eval()
            self._sentiment_models[name] = (tokenizer, model)
            logger.info("Sentiment model loaded", name=name)
        except Exception as exc:
            logger.error(
                "Failed to load sentiment model %s: %s — falling back to the "
                "deterministic stub for this model. The result reports "
                "method='stub' so the fallback is visible (\u00a75.3).",
                name, exc,
            )
            self._failed_sentiment_models.add(name)
            return None
        return self._sentiment_models[name]

    def get_emotion_pipeline(self) -> Any:
        """Return a HuggingFace pipeline for emotion detection or None in stub mode."""
        if self._stub_mode:
            return None
        if self._emotion_failed:
            return None
        if self._emotion_pipeline is None:
            try:
                from transformers import pipeline  # type: ignore

                model_name = config.emotion_model
                self._emotion_pipeline = pipeline(
                    "text-classification",
                    model=model_name,
                    top_k=None,
                )
                logger.info("Emotion pipeline loaded", model_name=model_name)
            except Exception as exc:
                logger.error(
                    "Failed to load emotion pipeline: %s — emotion falls back "
                    "to the emoji/lexicon heuristic.", exc,
                )
                self._emotion_failed = True
                return None
        return self._emotion_pipeline

    def get_toxicity_pipeline(self) -> Any:
        """Return a HuggingFace pipeline for toxicity detection or None in stub mode."""
        if self._stub_mode:
            return None
        if self._toxicity_failed:
            return None
        if self._toxicity_pipeline is None:
            try:
                from transformers import pipeline  # type: ignore

                model_name = config.toxicity_model
                self._toxicity_pipeline = pipeline(
                    "text-classification",
                    model=model_name,
                    top_k=None,
                )
                logger.info("Toxicity pipeline loaded", model_name=model_name)
            except Exception as exc:
                logger.error(
                    "Failed to load toxicity pipeline: %s — toxicity falls back "
                    "to the keyword heuristic, which never exceeded 0.2 on this "
                    "corpus (§4.6). Router rule 5 will effectively be inert.", exc,
                )
                self._toxicity_failed = True
                return None
        return self._toxicity_pipeline

    # ------------------------------------------------------------------
    # Vision (SigLIP / CLIP)
    # ------------------------------------------------------------------

    def get_clip_processor(self) -> Any:
        """Return a CLIP/SigLIP processor or None in stub mode."""
        if self._stub_mode:
            return None
        if self._clip_failed:
            return None
        if self._clip_processor is None:
            self._load_clip()
        return self._clip_processor

    def get_clip_model(self) -> Any:
        """Return a CLIP/SigLIP model or None in stub mode."""
        if self._stub_mode:
            return None
        if self._clip_failed:
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

            model_name = config.clip_model
            self._clip_processor = AutoProcessor.from_pretrained(model_name)
            self._clip_model = AutoModel.from_pretrained(model_name)
            self._clip_model.eval()
            logger.info("CLIP/SigLIP model loaded", model_name=model_name)
        except Exception as exc:
            logger.error(
                "Failed to load CLIP model: %s — image sentiment reports "
                "vision_status=model_unavailable rather than a fake neutral "
                "(§5.2).", exc,
            )
            self._clip_failed = True
            return None

    # ------------------------------------------------------------------
    # NER (GLiNER / spaCy)
    # ------------------------------------------------------------------

    def get_ner_model(self) -> Any:
        """Return a NER model instance or None in stub mode."""
        if self._stub_mode:
            return None
        if self._ner_failed:
            return None
        if self._ner_model is None:
            try:
                # Prefer GLiNER when available; fall back to spaCy
                model_name = config.ner_model
                if model_name == "gliner":
                    from gliner import GLiNER  # type: ignore

                    self._ner_model = GLiNER.from_pretrained(
                        "urchade/gliner_multi-v2.1"
                    )
                else:
                    import spacy  # type: ignore

                    self._ner_model = spacy.load(model_name)
                logger.info("NER model loaded", model_name=model_name)
            except Exception as exc:
                logger.error(
                    "Failed to load NER model: %s — entities fall back to the "
                    "seed-list heuristic.", exc,
                )
                self._ner_failed = True
                return None
        return self._ner_model

    # ------------------------------------------------------------------
    # Keywords (KeyBERT)
    # ------------------------------------------------------------------

    def get_keyword_model(self) -> Any:
        """Return a KeyBERT instance or None in stub mode."""
        if self._stub_mode:
            return None
        if self._keyword_failed:
            return None
        if self._keyword_model is None:
            try:
                from keybert import KeyBERT  # type: ignore

                self._keyword_model = KeyBERT()
                logger.info("KeyBERT model loaded")
            except Exception as exc:
                logger.error(
                    "Failed to load KeyBERT: %s — keywords fall back to the "
                    "longest-token heuristic.", exc,
                )
                self._keyword_failed = True
                return None
        return self._keyword_model

    # ------------------------------------------------------------------
    # Embeddings (sentence-transformers)
    # ------------------------------------------------------------------

    def get_embedding_model(self) -> Any:
        """Return a SentenceTransformer embedding model or None in stub mode."""
        if self._stub_mode:
            return None
        if self._embedding_failed:
            return None
        if self._embedding_model is None:
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore

                # Default must be a 768-dim model — the pgvector column
                # (analysis_results.embedding) is vector(768). bge-m3 is
                # 1024-dim and would be truncated; see libs/embeddings.py.
                model_name = config.embedding_model
                self._embedding_model = SentenceTransformer(model_name)
                logger.info("Embedding model loaded", model_name=model_name)
            except Exception as exc:
                logger.error(
                    "Failed to load embedding model: %s — embeddings fall back "
                    "to the hash stub, which is NOT semantic. Rows are marked "
                    "embedding_is_stub so search can disclose it (§5.9).", exc,
                )
                self._embedding_failed = True
                return None
        return self._embedding_model
