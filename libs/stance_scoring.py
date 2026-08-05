"""Target-stance scoring — the deterministic fallback and the aggregation.

Full design: ``stance_targets.md``. Two scorers share one output shape so the
pipeline never branches on which ran:

* **LLM path** — the matched targets are injected into the Stage-2 comment-stance
  prompt, which already runs for routed posts. No extra LLM calls, so this
  feature is free against the cost model (PROJECT_ASSESSMENT §6.8). Parsing lives
  in the Stage-2 worker; this module supplies the aggregation it feeds.
* **Deterministic path** (here) — alias proximity plus polarity cues. Cruder, but
  it runs in stub mode and in CI. Without it the offline suite could not cover
  the one genuinely novel component of the system.

Both tag their output with ``method``, matching the provenance discipline of
§5.3: a chart must be able to say whether its numbers came from a model or a
keyword heuristic.

**Target stance is kept in its own field, never merged into document-level
sentiment.** A comment can be positive in tone while opposing a listed target;
conflating the two is the fastest way to lose credibility on this feature.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .stance_targets import Match, Target, Targets

STANCES: tuple[str, ...] = ("supportive", "opposing", "neutral")

# Cues are attributed to the target mentioned **in the same clause**, not merely
# nearby. A flat character window does not work on this corpus: comments are
# short, so a window wide enough to catch the cue usually spans the whole
# comment, and "B is the best but A is corrupt" then scores both entities
# identically — destroying the one distinction the feature exists to make.
#
# Clause boundaries are punctuation plus contrastive conjunctions. This is
# explainable in a defense ("cues attach to the mention in their own clause"),
# which a distance threshold is not.
_CLAUSE_SPLIT = re.compile(
    r"[,;.!?\n।॥]+"
    r"|\b(?:but|however|though|although|whereas|kintu|tobe)\b"
    r"|কিন্তু|তবে|যদিও",
    re.IGNORECASE,
)

_WORD_RE = re.compile(r"[0-9A-Za-zঀ-৿]+")

# Cue lexicons. Deliberately small and shared in spirit with the comment
# analyzer's tables — this is a fallback, not a model, and pretending otherwise
# by growing the lists would only hide how crude it is.
_SUPPORT_CUES: frozenset[str] = frozenset({
    "good", "great", "best", "support", "love", "thanks", "respect", "salute",
    "valo", "bhalo", "shera", "dhonnobad", "vote",
    "ভালো", "সমর্থন", "ধন্যবাদ", "শ্রদ্ধা", "সেরা", "জয়",
})
_OPPOSE_CUES: frozenset[str] = frozenset({
    "bad", "worst", "fake", "liar", "thief", "corrupt", "shame", "resign",
    "chor", "vondo", "mithuk", "dalal", "golam",
    "চোর", "মিথ্যা", "ভন্ড", "দালাল", "লজ্জা", "দুর্নীতি", "পদত্যাগ",
})
_SUPPORT_EMOJI: tuple[str, ...] = ("❤", "🥰", "😍", "👍", "🙏", "💪", "👏", "🫶", "💯", "🔥")
_OPPOSE_EMOJI: tuple[str, ...] = ("😡", "🤬", "👎", "💩", "🤮", "🖕", "🤡", "😒", "🙄", "⚠")

#: Negators flip the polarity of a cue within the same window.
_NEGATORS: frozenset[str] = frozenset({"not", "no", "never", "na", "nai", "নয়", "না", "নেই"})


def _clauses(text: str) -> list[tuple[int, int]]:
    """Character spans of the clauses in ``text``."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _CLAUSE_SPLIT.finditer(text):
        if m.start() > pos:
            spans.append((pos, m.start()))
        pos = m.end()
    if pos < len(text):
        spans.append((pos, len(text)))
    return spans or [(0, len(text))]


def _cue_score(fragment: str) -> int:
    """Net polarity of the cues in a fragment: >0 supportive, <0 opposing."""
    tokens = {t.lower() for t in _WORD_RE.findall(fragment)}
    support = len(tokens & _SUPPORT_CUES) + sum(fragment.count(e) for e in _SUPPORT_EMOJI)
    oppose = len(tokens & _OPPOSE_CUES) + sum(fragment.count(e) for e in _OPPOSE_EMOJI)
    if tokens & _NEGATORS:
        support, oppose = oppose, support
    return support - oppose


def _dedupe_overlapping(matches: list[Match]) -> list[Match]:
    """Collapse overlapping matches for the same target, keeping the longest.

    "alpha party" and "alpha" both fire on the same words; counting the clause
    twice would double every cue in it.
    """
    kept: list[Match] = []
    for m in sorted(matches, key=lambda x: (x.target_id, -(x.end - x.start), x.start)):
        if any(
            k.target_id == m.target_id and m.start < k.end and k.start < m.end
            for k in kept
        ):
            continue
        kept.append(m)
    return kept


def score_comment_deterministic(text: str, matches: Iterable[Match]) -> list[dict]:
    """Stance toward each matched target, from cues **in the same clause**.

    No model involved — this is the fallback that keeps the feature working in
    stub mode and CI. It is genuinely crude: it will misread sarcasm, irony and
    any cue that sits in a different clause from its subject. That is why the
    LLM path exists and why every entry is tagged ``method: "deterministic"``.

    ``evidence`` is the clause the verdict came from, so a wrong call is
    debuggable in a demo rather than mysterious.
    """
    matches = _dedupe_overlapping(list(matches))
    if not matches:
        return []

    clause_spans = _clauses(text)

    per_target: dict[str, list[int]] = {}
    evidence: dict[str, str] = {}
    for m in matches:
        # The clause containing this mention (the last one that starts at or
        # before it, so a mention inside a trailing fragment still lands).
        clause = next(
            ((lo, hi) for lo, hi in clause_spans if lo <= m.start < hi),
            (0, len(text)),
        )
        fragment = text[clause[0]:clause[1]]
        per_target.setdefault(m.target_id, []).append(_cue_score(fragment))
        evidence.setdefault(m.target_id, fragment.strip()[:160])

    out: list[dict] = []
    for target_id, scores in per_target.items():
        total = sum(scores)
        stance = "supportive" if total > 0 else "opposing" if total < 0 else "neutral"
        out.append({
            "target": target_id,
            "stance": stance,
            "evidence": evidence[target_id],
            "method": "deterministic",
        })
    return out


def normalize_llm_target_stances(parsed: Any, valid_ids: set[str]) -> list[dict]:
    """Coerce an LLM's ``target_stances`` payload into the canonical shape.

    Anything the model invents — an unknown target id, a stance outside the
    taxonomy — is dropped rather than passed through. A hallucinated entity
    appearing in a stance table would be worse than a missing one.
    """
    if not isinstance(parsed, list):
        return []
    out: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or item.get("t") or "").strip()
        stance = str(item.get("stance") or item.get("s") or "").strip().lower()
        if target not in valid_ids or stance not in STANCES:
            continue
        out.append({
            "target": target,
            "stance": stance,
            "evidence": str(item.get("evidence") or item.get("e") or "")[:160],
            "method": "llm",
        })
    return out


def aggregate_target_stances(
    comments: Iterable[dict],
    targets: Targets,
) -> dict[str, dict]:
    """Per-post, per-target rollup — the actual product output.

    A per-comment stance list is raw material; this is the answer a dashboard
    panel renders and the thing to point at in a defense.

    Targets that were never mentioned are **absent**, not present-with-zeros:
    "nobody talked about X" and "everybody was neutral about X" are different
    findings and must not look the same.
    """
    rollup: dict[str, dict] = {}
    for c in comments:
        for entry in c.get("target_stances") or []:
            tid = entry.get("target")
            target: Target | None = targets.by_id(tid) if tid else None
            if target is None:
                continue
            bucket = rollup.setdefault(tid, {
                "display": target.display,
                "polarity": target.polarity,
                "mentions": 0,
                "supportive": 0,
                "opposing": 0,
                "neutral": 0,
                "method_breakdown": {},
                "aliases_matched": {},
            })
            bucket["mentions"] += 1
            stance = entry.get("stance")
            if stance in STANCES:
                bucket[stance] += 1
            method = entry.get("method") or "unknown"
            bucket["method_breakdown"][method] = bucket["method_breakdown"].get(method, 0) + 1
            alias = entry.get("alias")
            if alias:
                bucket["aliases_matched"][alias] = bucket["aliases_matched"].get(alias, 0) + 1

    # A single `method` per target, so the output can state its own provenance
    # without the caller having to reduce the breakdown.
    for bucket in rollup.values():
        mb = bucket["method_breakdown"]
        bucket["method"] = "llm" if mb.get("llm") else ("deterministic" if mb else "none")
    return rollup


__all__ = [
    "STANCES",
    "aggregate_target_stances",
    "normalize_llm_target_stances",
    "score_comment_deterministic",
]
