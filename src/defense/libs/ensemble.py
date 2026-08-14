"""Ensemble combination for per-comment sentiment — and the escalation gate.

Stage 2 collects up to four independent opinions about a comment:

    heuristic   Stage-1's emoji + lexicon rule (free, every comment)
    xlmr        a multilingual sentiment head    (cheap, local)
    distilbert  a second multilingual head       (cheap, local)
    llm         the context-aware stance pass    (expensive, per batch)

Before this module they were displayed side by side and nothing consumed them:
the comment's own ``sentiment`` stayed at Stage-1's heuristic while the
post-level breakdown was recomputed from the LLM alone, so one payload carried
two aggregates that disagreed and `method_breakdown` reported 0% LLM for a run
in which every comment had been sent to one.

Two things are built here.

**Combination.** :func:`combine` turns the opinions into one label plus the
number that makes it honest — ``agreement``, the share of voters behind the
winner. A 3-way split is not a neutral comment; it is a comment we cannot label,
and :data:`UNCERTAIN` says so instead of silently picking a side. Abstention is
reported, never hidden: the caller writes the agreement onto the comment and the
aggregate counts abstentions in their own bucket.

**Escalation.** :func:`should_escalate` is the cost lever that replaces the
post-level routing gate. Cost in this system is comment-dominated (~85% of LLM
calls are comment-level), so gating *which comments* reach the LLM is worth far
more than gating which posts do. The cheap voters run on everything; the LLM is
spent only where they disagree, where they have nothing to say, or where a
watchlist entity is mentioned and a wrong call is expensive.

Both functions are pure — no Redis, no models, no framework — so the behaviour
that decides how the budget is spent is testable offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The taxonomy a voter may return. Anything else is discarded as a non-vote.
VALID_LABELS: frozenset[str] = frozenset({"positive", "negative", "neutral"})

#: Reported when the voters cannot agree. Deliberately NOT "neutral": neutral is
#: a verdict about the comment, uncertain is a verdict about our confidence.
UNCERTAIN: str = "uncertain"

#: Voters that cost nothing per comment beyond a forward pass already batched.
CHEAP_SOURCES: tuple[str, ...] = ("heuristic", "xlmr", "distilbert")

#: The expensive one.
LLM_SOURCE: str = "llm"

#: Below this share of voters behind the winner, the ensemble abstains.
DEFAULT_MIN_AGREEMENT: float = 0.6


@dataclass
class Verdict:
    """One combined opinion, with everything needed to explain it."""

    label: str
    score: float
    agreement: float                      # share of voters behind `label`
    voters: int                           # how many sources actually voted
    sources: list[str] = field(default_factory=list)   # who voted, in order
    winning_sources: list[str] = field(default_factory=list)
    unanimous: bool = False
    abstained: bool = False
    #: Set when an exact tie was decided by one source rather than by weight of
    #: numbers. A tie-break is not consensus; `agreement` still reports the tie.
    tie_broken_by: str | None = None

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "score": round(self.score, 3),
            "agreement": round(self.agreement, 3),
            "voters": self.voters,
            "sources": list(self.sources),
            "unanimous": self.unanimous,
            "abstained": self.abstained,
            "tie_broken_by": self.tie_broken_by,
        }


def _votes(labels: dict) -> list[tuple[str, str, float]]:
    """[(source, label, score)] for every source that returned a usable label.

    A source that failed, was skipped, or returned something outside the
    taxonomy is simply not a voter — it must not be counted as agreement, and it
    must not be counted as a neutral vote either. Fabricating a neutral for an
    absent model is the failure this codebase already fixed once for image
    sentiment; the same rule applies here.
    """
    out: list[tuple[str, str, float]] = []
    for source, payload in (labels or {}).items():
        if not isinstance(payload, dict):
            continue
        label = payload.get("sentiment")
        if not isinstance(label, str) or label not in VALID_LABELS:
            continue
        raw = payload.get("sentiment_score", payload.get("score"))
        try:
            score = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            score = 0.0
        out.append((source, label, score))
    return out


def combine(
    labels: dict,
    *,
    min_agreement: float = DEFAULT_MIN_AGREEMENT,
    abstain: bool = True,
    prefer_on_tie: str | None = LLM_SOURCE,
) -> Verdict:
    """Combine per-source labels into one verdict.

    ``labels`` is ``{source: {"sentiment": ..., "sentiment_score"|"score": ...}}``.
    Sources with no usable label do not vote.

    With no voters at all the verdict is :data:`UNCERTAIN` at zero agreement —
    never "neutral", which would be indistinguishable from a comment every model
    read and judged neutral.

    ``prefer_on_tie`` breaks an exact tie in favour of one source. It defaults to
    the LLM because the LLM is the only labeller that sees the POST: it judges
    stance toward what was said, while the small heads judge the comment's text
    in isolation. On an even number of voters a 2-2 split is common, and calling
    every one of those "uncertain" would throw away the one opinion that had the
    context.

    A tie-break is NOT consensus, and the verdict says so: ``agreement`` still
    reports the real 0.5 and ``tie_broken_by`` names the source that decided it,
    so the UI keeps flagging the comment as contested.
    """
    votes = _votes(labels)
    if not votes:
        return Verdict(label=UNCERTAIN, score=0.0, agreement=0.0, voters=0, abstained=True)

    tally: dict[str, list[tuple[str, float]]] = {}
    for source, label, score in votes:
        tally.setdefault(label, []).append((source, score))

    ranked = sorted(tally.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    top_label, top_voters = ranked[0]
    n = len(votes)
    agreement = len(top_voters) / n
    tied = len(ranked) > 1 and len(ranked[1][1]) == len(top_voters)

    # Half-the-room rule. With four voters an exact 2-2 split, or a 2-1-1
    # plurality, is common — and calling all of those "uncertain" throws away
    # the one opinion that had the post as context. So: when at least half the
    # voters back a label and `prefer_on_tie` is one of them, that label stands.
    # Below half, or with the preferred source dissenting, it still abstains.
    tie_broken_by: str | None = None
    if prefer_on_tie and (tied or agreement < min_agreement):
        preferred = next(
            (label for source, label, _ in votes if source == prefer_on_tie), None
        )
        if preferred is not None and len(tally[preferred]) / n >= 0.5:
            top_label, top_voters = preferred, tally[preferred]
            agreement = len(top_voters) / n
            tie_broken_by = prefer_on_tie
            tied = False

    mean_score = sum(s for _, s in top_voters) / len(top_voters)
    # Unanimity needs a room. One voter agreeing with itself is not consensus,
    # and `unanimous_share` is documented as "the share the expensive model
    # never needed to see" — which inverts the truth precisely when the LLM was
    # the ONLY labeller (stub-mode Stage 1 abstains, no classifier weights
    # cached, every comment sent to the LLM: unanimous_share read 1.0).
    unanimous = len(tally) == 1 and n >= 2

    # A deliberately broken tie is 0.5 agreement by definition, so the
    # threshold must not then reject what the tie-break just decided.
    if abstain and (tied or (tie_broken_by is None and agreement < min_agreement)):
        # Keep the score of the plurality so a downstream chart can still show
        # which way it leaned, but do not claim the label.
        return Verdict(
            label=UNCERTAIN,
            score=mean_score,
            agreement=agreement,
            voters=n,
            sources=[s for s, _, _ in votes],
            winning_sources=[s for s, _ in top_voters],
            unanimous=False,
            abstained=True,
        )

    return Verdict(
        label=top_label,
        score=mean_score,
        agreement=agreement,
        voters=n,
        sources=[s for s, _, _ in votes],
        winning_sources=[s for s, _ in top_voters],
        unanimous=unanimous,
        abstained=False,
        tie_broken_by=tie_broken_by,
    )


def cheap_disagreement(labels: dict) -> bool:
    """True when the cheap voters do not all say the same thing.

    Fewer than two cheap voters is not disagreement — it is missing evidence,
    which :func:`should_escalate` treats separately so the two reasons stay
    distinguishable in the logs.
    """
    cheap = {k: v for k, v in (labels or {}).items() if k in CHEAP_SOURCES}
    votes = _votes(cheap)
    return len({label for _, label, _ in votes}) > 1


def should_escalate(
    labels: dict,
    *,
    kind: str | None = None,
    mentions_watchlist: bool = False,
    min_cheap_voters: int = 2,
) -> tuple[bool, str]:
    """Decide whether this comment is worth an LLM call. Returns (escalate, reason).

    The reason string is logged and counted, so the share of the budget each
    reason consumes is a measured quantity rather than an assumption — the same
    standard the post-level router is held to.
    """
    # Nothing to read: an emoji reaction or an empty/filtered comment has no
    # text for an LLM, and the emoji heuristic is the appropriate label for it.
    if kind in ("emoji", "filtered", "link"):
        return False, "no_text"

    # A wrong call about a listed entity is the expensive kind of wrong.
    if mentions_watchlist:
        return True, "watchlist_mention"

    cheap = {k: v for k, v in (labels or {}).items() if k in CHEAP_SOURCES}
    votes = _votes(cheap)
    if len(votes) < min_cheap_voters:
        return True, "insufficient_cheap_voters"

    if len({label for _, label, _ in votes}) > 1:
        return True, "cheap_disagreement"

    return False, "cheap_consensus"


def agreement_summary(verdicts: list[Verdict]) -> dict:
    """Corpus-level view of how often the voters agreed — the headline number.

    ``unanimous_share`` is the fraction of comments every voter labelled the
    same way, and it requires at least two voters — see :func:`combine`.

    ``single_voter`` is reported alongside it because the two numbers are only
    meaningful together: a corpus where one labeller ran alone has nothing to
    agree or disagree with, and an ``unanimous_share`` read without it invites
    exactly the wrong conclusion ("the cheap voters covered everything") about a
    run the expensive model carried by itself.
    """
    total = len(verdicts)
    if not total:
        return {
            "comments": 0,
            "unanimous": 0,
            "unanimous_share": 0.0,
            "abstained": 0,
            "abstained_share": 0.0,
            "single_voter": 0,
            "single_voter_share": 0.0,
            "unread": 0,
            "mean_agreement": 0.0,
        }
    unanimous = sum(1 for v in verdicts if v.unanimous)
    abstained = sum(1 for v in verdicts if v.abstained)
    single = sum(1 for v in verdicts if v.voters == 1)
    unread = sum(1 for v in verdicts if v.voters == 0)
    return {
        "comments": total,
        "unanimous": unanimous,
        "unanimous_share": round(unanimous / total, 4),
        "abstained": abstained,
        "abstained_share": round(abstained / total, 4),
        # One labeller, and no labeller at all, are distinct facts about
        # coverage — and neither is agreement.
        "single_voter": single,
        "single_voter_share": round(single / total, 4),
        "unread": unread,
        "mean_agreement": round(sum(v.agreement for v in verdicts) / total, 4),
    }


__all__ = [
    "UNCERTAIN",
    "VALID_LABELS",
    "CHEAP_SOURCES",
    "LLM_SOURCE",
    "DEFAULT_MIN_AGREEMENT",
    "Verdict",
    "combine",
    "cheap_disagreement",
    "should_escalate",
    "agreement_summary",
]
