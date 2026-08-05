"""Vision analysis pipeline for a single image URL.

Responsibilities:
  1. Download the image from object storage (MinIO / S3-compatible).
  2. Run OCR with Tesseract (pytesseract) to extract embedded text.
  3. Run zero-shot image-sentiment classification with SigLIP / CLIP using
     labels ["positive feeling", "negative feeling", "neutral feeling"].

In stub mode:
  - All three steps are skipped.
  - Returns a plausible neutral result so the downstream fusion can proceed.

In real mode:
  - The image is fetched with httpx.
  - OCR is run with pytesseract (Bengali + English tessdata must be present).
  - SigLIP / CLIP computes cosine similarities for the three sentiment labels.

The caller passes the *raw* photoUrl string exactly as it comes from the
upstream payload — typically a relative object-storage key like:
  "posts/<campaignId>/<platformPostId>/abc123.jpg"

The worker resolves it to a full URL before calling this module.  If the
caller passes a full URL (http / https), it is used as-is.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_SENTIMENT_LABELS: list[str] = [
    "positive feeling",
    "negative feeling",
    "neutral feeling",
]

_LABEL_TO_KEY = {
    "positive feeling": "positive",
    "negative feeling": "negative",
    "neutral feeling": "neutral",
}

# Score mapping: positive → +, negative → -, neutral → 0
_LABEL_TO_SCORE: dict[str, float] = {
    "positive": 1.0,
    "negative": -1.0,
    "neutral": 0.0,
}


# ---------------------------------------------------------------------------
# Image fetching
# ---------------------------------------------------------------------------

async def _fetch_image_bytes(url: str) -> bytes:
    """Download image bytes from a URL using httpx."""
    import httpx  # type: ignore

    timeout = float(os.getenv("IMAGE_FETCH_TIMEOUT", "10"))
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


def _resolve_image_url(photo_url: str) -> str:
    """Resolve a relative object-storage key to a full URL.

    ``MINIO_ENDPOINT`` ships in the root ``.env`` without a URL scheme
    (``minio:9000``), which made httpx raise ``UnsupportedProtocol`` before a
    single byte was fetched — and the caller turned that into a "neutral"
    verdict. The scheme is now supplied when it is missing, loudly, so a
    misconfigured endpoint is a log line rather than a silent neutral.
    """
    if photo_url.startswith("http://") or photo_url.startswith("https://"):
        return photo_url
    # Build from MinIO / S3 env vars
    endpoint = os.getenv("MINIO_ENDPOINT", "http://localhost:9000").rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        logger.warning(
            "MINIO_ENDPOINT %r has no URL scheme; assuming http://. "
            "Set it to a full URL (e.g. http://minio:9000).",
            endpoint,
        )
        endpoint = f"http://{endpoint}"
    bucket = os.getenv("MINIO_BUCKET", "defense")
    return f"{endpoint}/{bucket}/{photo_url}"


# ---------------------------------------------------------------------------
# Result statuses
# ---------------------------------------------------------------------------
# Every vision result says how it was produced. Only "ok" is a model verdict;
# the rest are absences that used to be indistinguishable from one, because a
# fetch failure returned the same hardcoded neutral/0.0 that a real neutral
# image would — while `_build_result` labelled the run `vision_model: "SigLIP"`.
# A real-mode run therefore reported "SigLIP produced neutral" for an image it
# never saw. Downstream code must branch on this, not on MODEL_STUB_MODE.
STATUS_OK = "ok"                        # a model actually looked at the image
STATUS_STUB = "stub"                    # stub mode: no model was loaded
STATUS_FETCH_FAILED = "fetch_failed"    # the bytes could not be retrieved
STATUS_MODEL_UNAVAILABLE = "model_unavailable"  # bytes fetched, no CLIP/SigLIP
STATUS_MODEL_FAILED = "model_failed"    # bytes fetched, the model raised

#: Statuses whose sentiment field is a genuine model output.
USABLE_STATUSES = frozenset({STATUS_OK})


def _absent_vision_result(status: str, ocr_text: str = "") -> dict:
    """A vision result carrying no sentiment signal, labelled with why.

    ``image_sentiment``/``image_sentiment_score`` are None rather than
    "neutral"/0.0 precisely so that a consumer cannot mistake the absence of a
    verdict for a neutral one.
    """
    return {
        "image_sentiment": None,
        "image_sentiment_score": None,
        "description": None,
        "ocr_text": ocr_text,
        "status": status,
    }


def _stub_vision_result() -> dict:
    return _absent_vision_result(STATUS_STUB)


# ---------------------------------------------------------------------------
# Real-mode helpers
# ---------------------------------------------------------------------------

def _ocr_image(image_bytes: bytes) -> str:
    """Run Tesseract OCR on image bytes; return extracted text."""
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        # Run Bengali + English OCR
        lang = os.getenv("TESSERACT_LANG", "ben+eng")
        text: str = pytesseract.image_to_string(img, lang=lang)
        return text.strip()
    except Exception as exc:
        logger.warning("OCR failed: %s", exc)
        return ""


def _clip_sentiment(
    image_bytes: bytes,
    processor: Any,
    model: Any,
) -> tuple[str, float]:
    """Return (sentiment_label, score) using SigLIP / CLIP zero-shot."""
    import torch  # type: ignore
    from PIL import Image  # type: ignore

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    inputs = processor(
        text=_SENTIMENT_LABELS,
        images=img,
        return_tensors="pt",
        padding=True,
    )
    with torch.no_grad():
        outputs = model(**inputs)

    # SigLIP uses logits_per_image; CLIP also exposes it
    if hasattr(outputs, "logits_per_image"):
        logits = outputs.logits_per_image.squeeze()
    else:
        # Fallback: compute cosine from embeddings
        img_feat = outputs.image_embeds
        txt_feat = outputs.text_embeds
        import torch.nn.functional as F  # type: ignore

        logits = (img_feat @ txt_feat.T).squeeze()

    probs = torch.softmax(logits, dim=-1).tolist()
    idx = int(torch.tensor(probs).argmax())
    label_raw = _SENTIMENT_LABELS[idx]
    label = _LABEL_TO_KEY[label_raw]
    # Signed score: positive → +prob, negative → -prob, neutral → 0
    pos_prob = probs[0]
    neg_prob = probs[1]
    score = round(pos_prob - neg_prob, 4)
    return label, score


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

async def analyze_image(
    photo_url: str | None,
    registry: Any,  # ModelRegistry — avoid circular import with TYPE_CHECKING
) -> dict | None:
    """Analyse a single photo.

    Returns None when photo_url is None (text-only post).
    Returns a dict with image_sentiment, image_sentiment_score, description,
    ocr_text, and ``status`` — see the STATUS_* constants. Only ``status ==
    "ok"`` carries a real model verdict; every other status has
    ``image_sentiment is None`` so that downstream fusion excludes it instead
    of treating a failure as a neutral image.
    """
    if not photo_url:
        return None

    if registry.stub_mode:
        return _stub_vision_result()

    # --- Real mode ---
    url = _resolve_image_url(photo_url)
    try:
        image_bytes = await _fetch_image_bytes(url)
    except Exception as exc:
        # Never crash the worker over one unreachable image — but never pass the
        # failure off as a neutral verdict either.
        logger.error("Failed to fetch image %s: %s", url, exc)
        return _absent_vision_result(STATUS_FETCH_FAILED)

    # OCR — independent of the sentiment model, so it survives a CLIP failure.
    ocr_text = _ocr_image(image_bytes)

    # Image sentiment via CLIP / SigLIP
    processor = registry.get_clip_processor()
    model = registry.get_clip_model()
    if processor is None or model is None:
        logger.warning("CLIP/SigLIP unavailable; no image sentiment for %s", url)
        return _absent_vision_result(STATUS_MODEL_UNAVAILABLE, ocr_text)

    try:
        label, score = _clip_sentiment(image_bytes, processor, model)
    except Exception as exc:
        logger.warning("CLIP sentiment failed for %s: %s", url, exc)
        return _absent_vision_result(STATUS_MODEL_FAILED, ocr_text)

    return {
        "image_sentiment": label,
        "image_sentiment_score": score,
        "description": None,  # VLM description is a Stage-2 task
        "ocr_text": ocr_text,
        "status": STATUS_OK,
    }
