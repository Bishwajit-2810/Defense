"""Watchlist-driven target stance — config loading and alias matching.

Full design: ``stance_targets.md``. This module is deliberately **pure**: it
reads a YAML file and matches strings. No Redis, no LLM, no framework — so the
offline test suite can cover the one genuinely novel component of the system,
and so both scorers (LLM and deterministic) can share it.

Why the matcher is the hard part
--------------------------------
This corpus writes the same entity in Bangla script, in romanized Banglish (with
no standard spelling), and in English, often within one thread. A watchlist that
matches only one spelling silently matches almost nothing — which is exactly the
silent-degradation failure PROJECT_ASSESSMENT §5.1 catalogues four times over.
So:

* Latin aliases match case-insensitively; Bangla has no case, so folding there is
  a no-op that only risks surprises.
* Bengali attaches suffixes directly to a word rather than separating them with a
  space, so a trailing word-boundary assertion under-matches. A Bangla alias may
  be followed by more Bangla; it may not be *preceded* by it.
* Short aliases (initialisms like "BNP", "AL") must match as whole tokens or they
  fire inside unrelated words.
* :func:`unmatched_targets` exists so a target that matched nothing across a whole
  run is reported as the alias-coverage bug it almost certainly is, rather than
  read as "nobody discussed it".

The *contents* of the watchlist are an editorial choice by the operator and a
**stated bias model**, not a measurement — see ``stance_targets.md`` §2. This
module has no opinion about who is on the list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Polarity buckets a target can be declared under.
#   favored / opposed — advocacy semantics: the operator has a side.
#   neutral           — monitoring semantics: track stance, impose no framing.
# `neutral` is the safer default; it yields target-dependent stance without
# encoding a political preference into the labels.
POLARITIES: tuple[str, ...] = ("favored", "opposed", "neutral", "always")

#: Aliases at or below this length must match as a whole token, or an
#: initialism fires inside unrelated words.
_SHORT_ALIAS_CHARS = 4

_BANGLA = r"ঀ-৿"
_BANGLA_RE = re.compile(f"[{_BANGLA}]")
#: Characters that may legitimately continue a Latin token.
_LATIN_WORD = re.compile(r"[0-9A-Za-z]")


class StanceTargetsError(ValueError):
    """Raised for a malformed watchlist — never swallowed.

    A bad watchlist must fail loudly at load: the whole point of the feature is
    that it matches, and a config that silently matches nothing is worse than no
    config at all.
    """


@dataclass(frozen=True)
class Target:
    """One watchlist entry."""

    id: str
    display: str
    polarity: str          # one of POLARITIES
    aliases: tuple[str, ...]
    notes: str = ""


@dataclass(frozen=True)
class Match:
    """One target mention found in a piece of text."""

    target_id: str
    alias: str             # the alias as written in the config
    start: int
    end: int


@dataclass
class Targets:
    """A loaded watchlist plus its compiled matchers."""

    targets: tuple[Target, ...] = ()
    version: Any = None
    owner: str = ""
    _patterns: list[tuple[str, str, re.Pattern]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if not self._patterns:
            self._patterns = _compile(self.targets)

    def __bool__(self) -> bool:
        return bool(self.targets)

    def by_id(self, target_id: str) -> Target | None:
        return next((t for t in self.targets if t.id == target_id), None)

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def match(self, text: str) -> list[Match]:
        """Every target mentioned in ``text``, in order of first appearance.

        A target is reported once per distinct alias occurrence; the caller can
        collapse to a set of target ids when it only cares *whether* a target was
        mentioned.
        """
        if not text or not self._patterns:
            return []
        out: list[Match] = []
        for target_id, alias, pattern in self._patterns:
            for m in pattern.finditer(text):
                out.append(Match(target_id, alias, m.start(), m.end()))
        out.sort(key=lambda m: (m.start, m.target_id))
        return out

    def matched_ids(self, text: str) -> list[str]:
        """Distinct target ids mentioned in ``text``, preserving first-seen order."""
        seen: dict[str, None] = {}
        for m in self.match(text):
            seen.setdefault(m.target_id, None)
        return list(seen)


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------

def _alias_pattern(alias: str) -> re.Pattern:
    """Compile one alias into a matcher appropriate to its script.

    Bangla: no case folding, and a trailing suffix is allowed (Bengali attaches
    inflections directly) while a leading one is not — matching mid-word would
    produce false positives.

    Latin: case-insensitive, with token boundaries. Short aliases always require
    boundaries; longer ones do too, but the practical effect is on initialisms.
    """
    escaped = re.escape(alias.strip())
    if _BANGLA_RE.search(alias):
        # Not preceded by another Bangla letter; may be followed by one.
        return re.compile(f"(?<![{_BANGLA}]){escaped}", re.UNICODE)
    flags = re.IGNORECASE | re.UNICODE
    if len(alias.strip()) <= _SHORT_ALIAS_CHARS:
        return re.compile(rf"(?<![0-9A-Za-z]){escaped}(?![0-9A-Za-z])", flags)
    # Longer Latin aliases: still boundary-anchored, but tolerate a trailing
    # possessive or plural, which is common in English comments.
    return re.compile(rf"(?<![0-9A-Za-z]){escaped}(?![0-9A-Za-z])", flags)


def _compile(targets: Iterable[Target]) -> list[tuple[str, str, re.Pattern]]:
    """Compile every alias, longest first so a longer alias wins a shared prefix."""
    pairs: list[tuple[str, str, re.Pattern]] = []
    for t in targets:
        for alias in t.aliases:
            if not alias.strip():
                continue
            pairs.append((t.id, alias, _alias_pattern(alias)))
    pairs.sort(key=lambda p: len(p[1]), reverse=True)
    return pairs


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _parse_entry(raw: Any, polarity: str, seen_ids: set[str]) -> Target:
    if not isinstance(raw, dict):
        raise StanceTargetsError(f"{polarity}: entry must be a mapping, got {type(raw).__name__}")
    tid = str(raw.get("id") or "").strip()
    if not tid:
        raise StanceTargetsError(f"{polarity}: an entry is missing 'id'")
    if tid in seen_ids:
        raise StanceTargetsError(f"duplicate target id {tid!r}")
    seen_ids.add(tid)

    aliases_raw = raw.get("aliases") or []
    if isinstance(aliases_raw, str):
        aliases_raw = [aliases_raw]
    aliases = tuple(str(a).strip() for a in aliases_raw if str(a).strip())
    if not aliases:
        # A target with no aliases can never match. Failing here is the whole
        # point of §5.1's lesson: make the silent no-op loud.
        raise StanceTargetsError(
            f"target {tid!r} has no aliases — it could never match anything"
        )
    return Target(
        id=tid,
        display=str(raw.get("display") or tid),
        polarity=polarity,
        aliases=aliases,
        notes=str(raw.get("notes") or ""),
    )


#: Operator entries live beside the shipped example, under a name git ignores.
#: The example file is documentation — naming real people in it commits an
#: editorial choice to the repository and hands it to everyone who clones it.
#: When the sibling `<name>.local.yml` exists it is loaded INSTEAD.
LOCAL_SUFFIX = ".local.yml"


def local_path_for(path: str | Path) -> Path:
    """The operator-owned override path for a given watchlist file."""
    p = Path(path)
    return p.with_name(p.name.replace(".yml", "").replace(".yaml", "") + LOCAL_SUFFIX)


def load_targets(path: str | Path, *, prefer_local: bool = True) -> Targets:
    """Load and validate a watchlist. Returns an empty Targets when absent.

    An absent file is not an error — the feature is opt-in, and a deployment
    without a watchlist should behave exactly as before. A *malformed* file is an
    error, loudly.

    A sibling ``*.local.yml`` wins when present: that is where an operator's real
    entries belong, so the tracked example stays an example. Pass
    ``prefer_local=False`` to read exactly the file named — used by the test
    that asserts the SHIPPED example declares no real entities, which must not
    be answered by whatever the local machine happens to have.
    """
    p = Path(path)
    local = local_path_for(p)
    if prefer_local and local.exists():
        p = local
    if not p.exists():
        return Targets()

    try:
        import yaml  # noqa: PLC0415  (optional at import time)
    except ImportError as exc:  # pragma: no cover - pyyaml is a declared dep
        raise StanceTargetsError("pyyaml is required to load a watchlist") from exc

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise StanceTargetsError(f"{p}: could not parse YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise StanceTargetsError(f"{p}: top level must be a mapping")

    targets: list[Target] = []
    seen_ids: set[str] = set()
    for polarity in POLARITIES:
        for entry in raw.get(polarity) or []:
            targets.append(_parse_entry(entry, polarity, seen_ids))

    # Two entities sharing an alias is a config bug, not something to resolve
    # silently — whichever won would be arbitrary.
    alias_owner: dict[str, str] = {}
    for t in targets:
        for alias in t.aliases:
            key = alias.lower()
            if key in alias_owner and alias_owner[key] != t.id:
                raise StanceTargetsError(
                    f"alias {alias!r} is claimed by both {alias_owner[key]!r} and {t.id!r}"
                )
            alias_owner[key] = t.id

    return Targets(
        targets=tuple(targets),
        version=raw.get("version"),
        owner=str(raw.get("owner") or ""),
    )


def unmatched_targets(targets: Targets, matched_ids: Iterable[str]) -> list[str]:
    """Targets that matched nothing — almost always an alias-coverage bug.

    Silence here reads as "nobody discussed this entity", which is exactly the
    wrong inference when the real cause is that the corpus spells it a way the
    watchlist does not list.
    """
    seen = set(matched_ids)
    return [t.id for t in targets.targets if t.id not in seen]


def watchlist_verdict(
    targets: Targets | None,
    post_text: str | None,
    comments: Iterable[dict],
) -> tuple[bool, str | None]:
    """Should this post raise a watchlist alert, and why? ``(alert, reason)``.

    Two rules, both in the config's own words:

    * an ``always`` target mentioned anywhere — post text or a comment — alerts
      regardless of stance, because that is what the bucket means;
    * a comment OPPOSING a ``favored`` target alerts, because that is the "under
      attack" case the dashboard names.

    Opposition to a target the operator did NOT declare favoured is not an
    alert. Firing on every listed entity made a *monitoring* watchlist
    (``neutral`` polarity — "report stance, impose no framing") behave like an
    advocacy one.

    Lives here, and not in its two callers, because it has two callers: Stage 2
    computes it when it ran (it has the LLM's per-entity stances) and the
    assembler computes it for every post so the alert does not go quiet exactly
    when the router starts bypassing post-level work. Those two were verbatim
    copies of this logic in different modules — and a rule duplicated across the
    cheap path and the expensive path is one that eventually disagrees with
    itself about whether to alert, which is the failure mode the two-path design
    was introduced to fix.

    The reason string names the target and the rule that fired: an ``always``
    match is a plain mention, not hostility, and a UI that describes every alert
    as an attack is misreading its own watchlist.
    """
    if not targets:
        return False, None

    for target_id in targets.matched_ids(post_text or ""):
        target = targets.by_id(target_id)
        if target and target.polarity == "always":
            display_name = getattr(target, "display", None) or getattr(target, "id", "")
            return True, f"always:{target.id} — Mention of {display_name} in post text"

    for comment in comments or ():
        for entry in comment.get("target_stances") or []:
            target = targets.by_id(entry.get("target"))
            if not target:
                continue
            if target.polarity == "always":
                display_name = getattr(target, "display", None) or getattr(target, "id", "")
                return True, f"always:{target.id} — Mention of {display_name} in a comment"
            if target.polarity == "favored" and entry.get("stance") == "opposing":
                display_name = getattr(target, "display", None) or getattr(target, "id", "")
                return True, f"opposing:{target.id} — Hostile comment opposing {display_name}"
    return False, None


__all__ = [
    "POLARITIES",
    "Match",
    "StanceTargetsError",
    "Target",
    "Targets",
    "load_targets",
    "local_path_for",
    "unmatched_targets",
    "watchlist_verdict",
]
