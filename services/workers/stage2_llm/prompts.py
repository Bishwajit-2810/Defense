"""
Prompt templates for Stage-2 LLM tasks.

All templates use str.format() placeholders.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Summary prompt
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = """You are analyzing a social media post. Write a concise summary (2-3 sentences) in {lang}.
Ground your summary on all available information.

Post text: {caption}
OCR text from image: {ocr_text}
Image description: {image_description}
Language detected: {language}

Write the summary in {lang}. Be factual and neutral."""

# ---------------------------------------------------------------------------
# Post-type classification prompt
# ---------------------------------------------------------------------------

POST_TYPE_PROMPT = """Classify this social media post into exactly one category:
complaint, news, opinion, promotion, humor, personal, political, religious, other

Post: {text}
Language: {language}

Respond with JSON: {{"post_type": "...", "confidence": 0.0}}"""

# ---------------------------------------------------------------------------
# Insight / topic refinement prompt
# ---------------------------------------------------------------------------

INSIGHT_PROMPT = """Analyze this social media post and provide structured insights.
Post: {text}
Language: {language}
Detected sentiment: {sentiment}
Topics so far: {topics}

Respond with JSON: {{"refined_topics": [...], "intents": [...], "insight": "one sentence"}}"""


# ---------------------------------------------------------------------------
# Helpers — build message lists ready for LLMClient.chat()
# ---------------------------------------------------------------------------

def build_summary_messages(
    caption: str,
    ocr_text: str,
    image_description: str,
    language: str,
    target_lang: str,
    image_urls: list[str] | None = None,
) -> list[dict]:
    """Return a messages list for the summary task.

    When ``image_urls`` is given the message uses the OpenAI-compatible
    multimodal content format (text part + image_url parts) so a VLM can
    ground the summary on the actual image pixels, not just OCR text.
    """
    text_content = SUMMARY_PROMPT.format(
        lang=target_lang or language or "the post's language",
        caption=caption or "(no text)",
        ocr_text=ocr_text or "(none)",
        image_description=image_description or "(none)",
        language=language or "unknown",
    )
    if image_urls:
        content: list[dict] = [{"type": "text", "text": text_content}]
        # Cap at 2 images to bound VLM context / latency.
        for url in image_urls[:2]:
            content.append({"type": "image_url", "image_url": {"url": url}})
        return [{"role": "user", "content": content}]
    return [{"role": "user", "content": text_content}]


def build_post_type_messages(text: str, language: str) -> list[dict]:
    """Return a messages list for post-type classification."""
    content = POST_TYPE_PROMPT.format(
        text=text or "(no text)",
        language=language or "unknown",
    )
    return [{"role": "user", "content": content}]


def build_insight_messages(
    text: str,
    language: str,
    sentiment: str | float | None,
    topics: list[str],
) -> list[dict]:
    """Return a messages list for topic/intent refinement."""
    content = INSIGHT_PROMPT.format(
        text=text or "(no text)",
        language=language or "unknown",
        sentiment=str(sentiment) if sentiment is not None else "unknown",
        topics=", ".join(topics) if topics else "none",
    )
    return [{"role": "user", "content": content}]
