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


COMMENT_STANCE_PROMPT = """You analyze public reactions to a social-media post.
For EACH numbered comment, return TWO labels:

1. "s" = STANCE TOWARD THE POST:
- "positive": supports, agrees with, praises, thanks, or defends the post or its subject
- "negative": opposes, disagrees with, criticizes, mocks, insults, or attacks the post or its subject
- "neutral": no clear stance, off-topic, a plain question, or genuinely ambiguous

2. "e" = the commenter's dominant EMOTION, exactly one of:
- "anger", "sadness", "joy", "fear", "disgust", "surprise", "neutral"

Judge the comment IN CONTEXT of the post — sarcasm and mocking emojis under a claim are "negative".
Comments may be in Bangla, Banglish, or English.

The overarching context of these comments is:
\"\"\"{post_summary}\"\"\"
You must evaluate each comment's intent strictly relative to this summary. Do not guess the context. If the comment says 'this is terrible', rely on the summary to determine what 'this' refers to.

COMMENTS:
{comments}

Return ONLY JSON of this exact shape, one entry per comment number:
{{"labels":[{{"i":1,"s":"positive","e":"joy"}},{{"i":2,"s":"negative","e":"anger"}}]}}"""


# Appended to the stance prompt only when a batch mentions watchlist targets.
# It rides inside the call that is already being made, so target stance costs no
# additional LLM calls (stance_targets.md §6).
#
# "t" is deliberately a SEPARATE label from "s": stance toward the POST and
# stance toward a named ENTITY are different judgements and a comment can differ
# on them — praising a post that criticises an entity, for instance. Merging them
# would destroy exactly the distinction the feature exists to make.
TARGET_STANCE_BLOCK = """

3. "t" = STANCE TOWARD NAMED ENTITIES, when the comment mentions any of them.

Entities to watch (a comment may mention none, one, or several):
{targets}

For each entity the comment actually refers to, judge whether the commenter is:
- "supportive": defends, praises, agrees with, or sides with that entity
- "opposing": criticizes, mocks, insults, blames, or attacks that entity
- "neutral": mentions it without taking a side

Judge stance toward the ENTITY, not toward the post — they can differ. Omit "t"
entirely for a comment that mentions no listed entity; do not guess, and never
name an entity that is not in the list above.

With entities, an entry looks like:
{{"i":1,"s":"negative","e":"anger","t":[{{"target":"entity_id","stance":"opposing","evidence":"the phrase you judged from"}}]}}"""


def build_comment_stance_messages(
    post_summary: str,
    batch: list[dict],
    max_text: int = 200,
    targets: list[dict] | None = None,
) -> list[dict]:
    """Messages for one batch of comments → per-comment stance toward the post.

    When ``targets`` is supplied (``[{"id","display","aliases"}]``), the prompt
    also asks for stance toward each named entity — reusing this one call rather
    than issuing a second (stance_targets.md §6).
    """
    lines = []
    for idx, c in enumerate(batch, 1):
        text = (c.get("text") or "").replace("\n", " ").strip()[:max_text] or "(no text)"
        lines.append(f"{idx}: {text}")
    content = COMMENT_STANCE_PROMPT.format(
        post_summary=(post_summary or "(no summary)")[:800],
        comments="\n".join(lines),
    )
    if targets:
        # Aliases go in the prompt too: the model should recognise the entity
        # under the spellings the corpus actually uses, not only its display name.
        listed = "\n".join(
            f'- id "{t["id"]}" = {t.get("display") or t["id"]}'
            + (f' (also written: {", ".join(t.get("aliases") or [])})'
               if t.get("aliases") else "")
            for t in targets
        )
        content += TARGET_STANCE_BLOCK.format(targets=listed)
    return [{"role": "user", "content": content}]


COMMENT_SUMMARY_PROMPT = """You summarize how the public reacted in the comments of a social-media post.

POST being reacted to:
\"\"\"{post}\"\"\"

Aggregate stance of the {total} analysed comments (each scored as its stance TOWARD the post):
- positive (supportive/agreeing): {positive}
- negative (opposing/critical/mocking): {negative}
- neutral (no clear stance): {neutral}

Dominant emotions across the comments: {emotions}

A few representative comments:
{examples}

Write a concise 2-3 sentence summary in {lang} describing the overall mood of the comment section:
how people are reacting to the post, the balance of support vs. criticism, and any standout emotion or theme.
Be factual and neutral. Do not invent details that are not supported by the numbers or examples above."""


def build_comment_summary_messages(
    post: str,
    sentiment_breakdown: dict,
    emotion_breakdown: dict,
    representative: list[dict],
    target_lang: str,
) -> list[dict]:
    """Messages for a natural-language summary of a post's comment reactions."""
    sb = sentiment_breakdown or {}
    pos = int(sb.get("positive", 0))
    neg = int(sb.get("negative", 0))
    neu = int(sb.get("neutral", 0))
    total = pos + neg + neu

    eb = emotion_breakdown or {}
    top_emotions = sorted(
        ((k, int(v)) for k, v in eb.items() if v), key=lambda kv: kv[1], reverse=True
    )[:3]
    emotions = ", ".join(f"{k} ({v})" for k, v in top_emotions) or "none"

    examples = "\n".join(
        f"- [{(c.get('sentiment') or 'neutral')}] {(c.get('text') or '').replace(chr(10), ' ').strip()[:160]}"
        for c in (representative or [])[:4]
    ) or "(none)"

    content = COMMENT_SUMMARY_PROMPT.format(
        post=(post or "(no post text)")[:800],
        total=total,
        positive=pos,
        negative=neg,
        neutral=neu,
        emotions=emotions,
        examples=examples,
        lang=target_lang or "the post's language",
    )
    return [{"role": "user", "content": content}]


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
