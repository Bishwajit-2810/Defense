"""Common utility functions for the defense system."""

import hashlib
import re
import unicodedata
from urllib.parse import urlparse


def platform_from_url(url: str) -> str:
    """Parse a URL and return the social platform name.

    Returns one of: "facebook", "instagram", "x", "twitter", "telegram",
    "youtube", or "other".
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
    except Exception:
        return "other"

    # Normalise: strip www. prefix for cleaner matching
    hostname = hostname.lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]

    if hostname in ("facebook.com", "fb.com"):
        return "facebook"
    if hostname == "instagram.com":
        return "instagram"
    if hostname in ("twitter.com", "x.com"):
        return "x"
    if hostname in ("t.me", "telegram.me", "telegram.org"):
        return "telegram"
    if hostname in ("youtube.com", "youtu.be"):
        return "youtube"
    return "other"


def normalize_text(text: str) -> str:
    """Unicode NFC normalization with leading/trailing whitespace stripped.

    Returns empty string for None or empty input.
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFC", text)
    return normalized.strip()


# Common Banglish romanization patterns (words frequently used in
# romanized Bengali writing).
_BANGLISH_WORDS = re.compile(
    r"\b("
    r"ami|tumi|apni|amar|tomar|apnar|"
    r"bhai|bon|dada|didi|vai|"
    r"ki|ke|keno|kothay|kobe|kivabe|"
    r"na|haa|hya|"
    r"ache|achho|achen|achis|"
    r"hobe|hobo|hoyeche|hoise|"
    r"kore|korte|korbo|korchi|"
    r"jabo|jao|gelo|gesi|giyechi|"
    r"boro|choto|bhalo|kharap|"
    r"diye|niye|theke|por|age|"
    r"ekhon|pore|kal|aj|aaj|"
    r"shob|kono|onek|kicu|kichu|"
    r"bhaia|mama|chacha|mamu|"
    r"lagbe|lagche|lage|"
    r"jani|janina|bujhi|bujhina|"
    r"dekhi|dekho|dekhe|"
    r"boro|choto|sundor|"
    r"ato|eto|koto|"
    r"jodi|tahole|kintu|karon|tai"
    r")\b",
    re.IGNORECASE,
)

# Latin character detector (basic ASCII letters)
_LATIN_CHARS = re.compile(r"[a-zA-Z]")


def is_banglish(text: str) -> bool:
    """Detect whether text is likely Banglish (romanized Bengali).

    Heuristic: the text contains Latin characters AND matches at least two
    common Banglish romanization patterns.  A single word match is treated
    as coincidental English; two or more matches indicate Banglish.
    """
    if not text:
        return False
    if not _LATIN_CHARS.search(text):
        return False
    matches = _BANGLISH_WORDS.findall(text)
    return len(matches) >= 2


def detect_script(text: str) -> str:
    """Detect the writing script used in text.

    Returns:
        "bengali"  - text contains Bengali Unicode characters (U+0980–U+09FF)
        "latin"    - text contains only ASCII/Latin characters
        "mixed"    - text contains both Bengali and Latin characters
        "other"    - text contains neither
    """
    if not text:
        return "other"

    has_bengali = any("ঀ" <= ch <= "৿" for ch in text)
    has_latin = bool(re.search(r"[a-zA-Z]", text))

    if has_bengali and has_latin:
        return "mixed"
    if has_bengali:
        return "bengali"
    if has_latin:
        return "latin"
    return "other"


def content_hash(post: dict) -> str:
    """Compute a deterministic SHA-256 hash for a post.

    Hash input: post["id"] + post.get("caption", "") + comment texts
    sorted by comment id.
    """
    post_id = str(post.get("id", ""))
    caption = post.get("caption", "") or ""

    comments = post.get("comments", []) or []
    sorted_comments = sorted(comments, key=lambda c: c.get("id", ""))
    comment_texts = "".join(str(c.get("text", "")) for c in sorted_comments)

    raw = post_id + caption + comment_texts
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def truncate_for_llm(text: str, max_chars: int = 2000) -> str:
    """Truncate text to max_chars, appending '...' when truncation occurs."""
    if not text:
        return text if text is not None else ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def reaction_breakdown_to_dict(rb: dict) -> dict:
    """Normalise reactionBreakdown keys to lowercase.

    Returns a new dict with all keys lower-cased.  Values are preserved
    as-is.  The function does not enforce that values sum to the total.
    """
    if not rb:
        return {}
    return {k.lower(): v for k, v in rb.items()}


def compute_coverage(analyzed: int, total_comment_count: int) -> float:
    """Return the fraction of comments that have been analyzed, clamped to 1.0.

    If total_comment_count is 0, returns 1.0 (full coverage by convention).

    **Clamped.** Five posts in the corpus store more comments than the platform
    reports having — up to 112 stored against a reported `commentCount` of 42,
    which used to render as "266.7% coverage". It is not replies inflating the
    numerator (0 of 10,272 comments carry a `parentId`); it is an upstream
    inconsistency. Absorbing it silently produced a number that cannot be true;
    use :func:`coverage_anomaly` to surface it as a data-quality event instead.
    """
    if total_comment_count == 0:
        return 1.0
    return min(1.0, analyzed / total_comment_count)


def coverage_anomaly(analyzed: int, total_comment_count: int) -> dict | None:
    """Describe an impossible coverage ratio, or None when there is nothing wrong.

    ``analyzed > total_comment_count`` means the upstream `commentCount` and the
    stored comment rows disagree. That is worth reporting — it bounds how much
    the coverage figure can be trusted — but it must not be reported as coverage
    above 100%.
    """
    if total_comment_count <= 0 or analyzed <= total_comment_count:
        return None
    return {
        "kind": "stored_exceeds_reported",
        "analyzed": analyzed,
        "reported_comment_count": total_comment_count,
        "raw_ratio": round(analyzed / total_comment_count, 4),
    }
