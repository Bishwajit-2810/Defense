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
    """Resolve a relative object-storage key to a full URL."""
    if photo_url.startswith("http://") or photo_url.startswith("https://"):
        return photo_url
    # Build from MinIO / S3 env vars
    endpoint = os.getenv("MINIO_ENDPOINT", "http://localhost:9000").rstrip("/")
    bucket = os.getenv("MINIO_BUCKET", "defense")
    return f"{endpoint}/{bucket}/{photo_url}"


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------

def _stub_vision_result() -> dict:
    return {
        "image_sentiment": "neutral",
        "image_sentiment_score": 0.0,
        "description": None,
        "ocr_text": "",
    }


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
    and ocr_text.
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
        logger.error("Failed to fetch image %s: %s", url, exc)
        # Return a neutral stub on fetch failure rather than crashing the worker
        return _stub_vision_result()

    # OCR
    ocr_text = _ocr_image(image_bytes)

    # Image sentiment via CLIP / SigLIP
    processor = registry.get_clip_processor()
    model = registry.get_clip_model()
    if processor is not None and model is not None:
        try:
            label, score = _clip_sentiment(image_bytes, processor, model)
        except Exception as exc:
            logger.warning("CLIP sentiment failed for %s: %s", url, exc)
            label, score = "neutral", 0.0
    else:
        label, score = "neutral", 0.0

    return {
        "image_sentiment": label,
        "image_sentiment_score": score,
        "description": None,  # VLM description is a Stage-2 task
        "ocr_text": ocr_text,
    }
