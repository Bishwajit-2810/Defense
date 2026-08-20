"""Split a long post into retrievable pieces.

Why
---
One vector per post averages a long caption into mush. A 5,463-character post in
this corpus — and 9 of 50 exceed 1,200 — argues three separate things, and the
single vector it gets is the centroid of all three: close to none of them, and
reachable by a query about any one of them only by luck. Chunking gives each
argument its own vector, and gives a citation somewhere to point.

It also does nothing at all to a short post, which is most of this corpus (median
caption: 166 characters). That is the intended behaviour, not a limitation —
below the threshold a post is one chunk whose text is the whole caption, so its
vector is exactly the vector it had before and nothing about its retrieval
changes. The cost of chunking is paid only where there is something to gain.

How
---
Recursive boundary splitting, coarsest first: paragraphs, then sentences, then a
hard character cut for text with no boundaries in it at all (a 2,000-character
run-on with no punctuation is a real shape in scraped captions). Pieces are
accumulated up to ``target_chars`` and carry ``overlap_chars`` of the previous
piece, so a sentence spanning a boundary is not lost to both sides.

Bengali punctuation is first-class here: the daṛi ``।`` is the sentence
terminator for most of this corpus, and a splitter that knew only ``.!?`` would
see a 3,000-character Bangla post as one unsplittable sentence and fall through
to the hard character cut — chopping mid-word and producing exactly the mush
chunking exists to avoid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Below this, a post is a single chunk and chunking is a no-op.
DEFAULT_MIN_CHARS = 600
#: Target size of a chunk. Roughly 2-4 sentences of Bangla prose, comfortably
#: inside the 128-token window most sentence encoders truncate at.
DEFAULT_TARGET_CHARS = 700
#: Carried from the end of the previous chunk into the next.
DEFAULT_OVERLAP_CHARS = 120

#: Paragraph break: one or more blank lines.
_PARA_RE = re.compile(r"\n\s*\n+")

#: Sentence end. `।` is the Bengali daṛi; `?` and `!` are shared; `.` is included
#: but is the weakest signal here (it appears in URLs and abbreviations far more
#: often than it ends a Bangla sentence). The lookbehind keeps the terminator
#: attached to the sentence it ends.
_SENT_RE = re.compile(r"(?<=[।?!\.])\s+")

_WS_RE = re.compile(r"[ \t]+")


@dataclass(frozen=True)
class Chunk:
    """One retrievable piece of a post."""

    idx: int
    text: str
    #: Character offset in the source text. Kept so a future highlight/citation
    #: feature can point at the span rather than re-finding it by string search.
    start: int


def _normalise(text: str) -> str:
    """Collapse horizontal whitespace, keep paragraph structure."""
    return _WS_RE.sub(" ", (text or "").replace("\r\n", "\n")).strip()


def _hard_split(text: str, target: int) -> list[str]:
    """Last resort for text with no paragraph or sentence boundary in it.

    Prefers to break at a space near the target so a word is not cut in half;
    falls back to a flat cut for scripts and strings that have no spaces either.
    """
    out: list[str] = []
    remaining = text
    while len(remaining) > target:
        window = remaining[:target]
        cut = window.rfind(" ")
        # Only honour a space if it is not so early that the chunk becomes tiny.
        if cut < target // 2:
            cut = target
        out.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        out.append(remaining)
    return [p for p in out if p]


def _atoms(text: str, target: int, overlap: int) -> list[str]:
    """The smallest pieces we are willing to move independently.

    Paragraphs, subdivided into sentences when a paragraph is itself over
    target, subdivided by character count when a sentence is.

    Hard splits are cut to ``target - overlap`` rather than ``target`` so that a
    piece plus the tail carried onto it still fits the budget. Without the
    headroom a boundaryless 2,000-character caption produced chunks of
    ``target + overlap``, which is over the window the sentence encoder
    truncates at — the chunk would be silently clipped at encode time, losing
    exactly the text the overlap was added to preserve.
    """
    room = max(target - overlap, target // 2)
    atoms: list[str] = []
    for para in _PARA_RE.split(text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= target:
            atoms.append(para)
            continue
        for sent in _SENT_RE.split(para):
            sent = sent.strip()
            if not sent:
                continue
            if len(sent) <= target:
                atoms.append(sent)
            else:
                atoms.extend(_hard_split(sent, room))
    return atoms


def chunk_text(
    text: str,
    *,
    min_chars: int = DEFAULT_MIN_CHARS,
    target_chars: int = DEFAULT_TARGET_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[Chunk]:
    """Split ``text`` into chunks. Short text yields exactly one chunk.

    The single-chunk case is load-bearing: it means every post has at least one
    row in the chunk table, so chunk-based retrieval never has to fall back to a
    different table for short posts, and "no chunk matched" always means the
    post genuinely did not match rather than that it was never chunked.
    """
    clean = _normalise(text)
    if not clean:
        return []
    if len(clean) <= min_chars:
        return [Chunk(idx=0, text=clean, start=0)]

    atoms = _atoms(clean, target_chars, overlap_chars)
    if not atoms:  # pragma: no cover — _normalise already rejected empty text
        return [Chunk(idx=0, text=clean, start=0)]

    chunks: list[str] = []
    buf = ""
    for atom in atoms:
        if buf and len(buf) + 1 + len(atom) > target_chars:
            chunks.append(buf)
            # Overlap: carry the tail of the chunk just closed. Word-aligned so
            # the carried fragment is readable on its own — it is shown to a
            # human as `matched_chunk` and fed to a cross-encoder as text.
            tail = buf[-overlap_chars:] if overlap_chars else ""
            if tail:
                space = tail.find(" ")
                tail = tail[space + 1 :] if space != -1 else tail
            candidate = f"{tail} {atom}".strip() if tail else atom
            # Overlap is a nice-to-have; overflowing the encoder's window is not.
            # When the two conflict the overlap goes, so a chunk never exceeds
            # the budget just because context was carried into it.
            buf = atom if (tail and len(candidate) > target_chars) else candidate
        else:
            buf = f"{buf} {atom}".strip() if buf else atom
    if buf:
        chunks.append(buf)

    # Offsets are resolved by scanning forward, so an overlap does not make the
    # next chunk appear to start before the previous one.
    out: list[Chunk] = []
    cursor = 0
    for i, body in enumerate(chunks):
        probe = body[:40]
        found = clean.find(probe, cursor)
        start = found if found != -1 else cursor
        out.append(Chunk(idx=i, text=body, start=start))
        cursor = max(cursor, start + max(len(body) - overlap_chars, 1))
    return out


__all__ = ["Chunk", "chunk_text", "DEFAULT_MIN_CHARS", "DEFAULT_TARGET_CHARS"]
