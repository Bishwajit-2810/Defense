"""Near-duplicate comment grouping — label one, propagate to the rest.

Comment threads on this corpus are extremely redundant: "সুন্দর", "nice", "❤️
❤️", "মাশাআল্লাহ" and their spelling variants recur hundreds of times under a
single post. Sending each occurrence to the LLM buys nothing — the model reads
the same string and returns the same label, at full price every time.

So: normalise, group identical normalisations, send ONE representative per group
to the expensive path, and propagate its verdict to the group. The propagation
is recorded per comment (``label_source: "propagated"`` plus the representative's
id), because a label that was copied rather than computed must not be
indistinguishable from one that was.

This is deliberately exact-match-after-normalisation rather than embedding
similarity: it needs no model, no vectors and no threshold to tune, it cannot
merge two comments that genuinely differ, and it already captures the bulk of
the redundancy. The embedding path (``libs/clustering.py``) stays what it is —
post-level clustering for reports.
"""

from __future__ import annotations

import re
import unicodedata

#: Collapse every run of whitespace.
_WS = re.compile(r"\s+")
#: Punctuation and symbol categories, dropped before comparison.
_STRIP_CATEGORIES = {"P", "S", "C"}
#: Bengali/Devanagari digits and Latin digits alike carry little signal here.
_DIGITS = re.compile(r"\d+")


def normalize(text: str) -> str:
    """A comparison key for near-duplicate detection.

    Case-folded, NFKC-normalised, stripped of punctuation, symbols (which
    includes emoji), and digits, with whitespace collapsed. Two comments sharing
    a key are the same written opinion as far as a sentiment model is concerned.

    Returns "" for anything with no textual content left — those are never
    grouped (an empty key would merge every emoji-only reaction into one group
    and propagate a single label across all of them).
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text).casefold()
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] not in _STRIP_CATEGORIES)
    s = _DIGITS.sub(" ", s)
    return _WS.sub(" ", s).strip()


def group_indices(texts: list[str], *, min_group: int = 2) -> tuple[dict[int, list[int]], dict]:
    """Group identical normalisations.

    Returns ``(groups, stats)`` where ``groups`` maps a representative index to
    the OTHER indices that share its key (never including the representative
    itself), and ``stats`` reports what the grouping bought.

    Only groups of at least ``min_group`` members are returned; a singleton is
    not a duplicate and gets the normal path.
    """
    by_key: dict[str, list[int]] = {}
    for idx, text in enumerate(texts):
        key = normalize(text)
        if not key:
            continue
        by_key.setdefault(key, []).append(idx)

    groups: dict[int, list[int]] = {}
    saved = 0
    for members in by_key.values():
        if len(members) < min_group:
            continue
        rep, rest = members[0], members[1:]
        groups[rep] = rest
        saved += len(rest)

    total = len(texts)
    stats = {
        "total": total,
        "groups": len(groups),
        "duplicates": saved,
        # The share of comments that never need their own expensive call.
        "duplicate_share": round(saved / total, 4) if total else 0.0,
    }
    return groups, stats


def representative_of(groups: dict[int, list[int]]) -> dict[int, int]:
    """Inverse index: member index → the representative that speaks for it."""
    out: dict[int, int] = {}
    for rep, members in groups.items():
        for m in members:
            out[m] = rep
    return out


__all__ = ["normalize", "group_indices", "representative_of"]
