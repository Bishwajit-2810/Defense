"""Ensemble combination for per-comment sentiment — and the escalation gate.

Stage 2 collects up to eight independent opinions about a comment:

    xlmr                    DistilBERT-multilingual, 5-class      (cheap, local)
    distilbert              DistilBERT-multilingual student       (cheap, local)
    twitter_xlmr            XLM-R trained on social media         (cheap, local)
    banglabert              BanglaBERT/Electra on SentNoB         (cheap, local)
    bengali_sentiment_bert  BanglaBERT/Electra, 5-class           (cheap, local)
    mbert                   multilingual BERT, 1-5 stars          (cheap, local)
    modernbert              ModernBERT-base multilingual          (cheap, local)
    llm                     the context-aware stance pass   (expensive, per batch)

**Every voter here is a model.** Stage-1's emoji + lexicon heuristic used to be
listed as an eighth cheap voter under the name ``heuristic``; it no longer votes
at all (17 Aug 2026). It is a keyword rule, and in the shipped configuration a
large share of its verdicts are the deterministic hash stub — a label that is
reproducible and is not sentiment. Letting it vote meant a comment could be
labelled by a rule that never read it, and on the corpus's Bangla threads that
rule was the *majority* voter. The consequence is deliberate and must not be
patched around: **a comment no model read now reports** :data:`UNCERTAIN` **with**
``label_voters: 0``, which is the true state of it.

"Cheap" is per comment, not per run: the seven heads are ~135-280M-parameter
encoders that batch, while the LLM is a per-batch generation call. What they are
NOT is seven independent readings — five of the seven are multilingual heads
trained on overlapping data, so their agreement is correlated and a 7-0 vote is
weaker evidence than seven unrelated models would be.

Before this module the opinions were displayed side by side and nothing consumed
them: the comment's own ``sentiment`` stayed at Stage-1's heuristic while the
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
#: These names must match `Settings.stage2_classifier_names` exactly — a voter
#: missing here is collected into `parallel_labels` and then ignored by
#: `should_escalate`, which is a silent way to buy a model and not use it;
#: `tests/test_ensemble.py` asserts the two lists agree.
#:
#: ``heuristic`` was removed on 17 Aug 2026 — see the module docstring. Do not add
#: it back to make abstentions go away.
CHEAP_SOURCES: tuple[str, ...] = (
    "xlmr",
    "distilbert",
    "twitter_xlmr",
    "banglabert",
    "bengali_sentiment_bert",
    "mbert",
    "modernbert",
)

#: The expensive one.
LLM_SOURCE: str = "llm"

#: Below this share of voters behind the winner, the ensemble abstains (0.0 = max vote / plurality wins).
DEFAULT_MIN_AGREEMENT: float = 0.0


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
    """Combine per-source labels into one verdict by maximum votes (plurality).

    ``labels`` is ``{source: {"sentiment": ..., "sentiment_score"|"score": ...}}``.
    Sources with no usable label do not vote.

    With no voters at all the verdict is :data:`UNCERTAIN` at zero agreement —
    never "neutral", which would be indistinguishable from a comment every model
    read and judged neutral.

    The label with the most votes wins. In case of an exact tie for first place,
    ``prefer_on_tie`` decides the tie; otherwise the ensemble abstains as UNCERTAIN.
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

    # Tie-break rule: when there is an exact tie for first place, prefer_on_tie decides.
    tie_broken_by: str | None = None
    if tied and prefer_on_tie:
        preferred = next(
            (label for source, label, _ in votes if source == prefer_on_tie), None
        )
        if preferred is not None and len(tally.get(preferred, [])) == len(top_voters):
            top_label, top_voters = preferred, tally[preferred]
            agreement = len(top_voters) / n
            tie_broken_by = prefer_on_tie
            tied = False

    mean_score = sum(s for _, s in top_voters) / len(top_voters)
    unanimous = len(tally) == 1 and n >= 2

    if abstain and (tied or (tie_broken_by is None and agreement < min_agreement)):
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
    # Nothing to read: an emoji reaction or an empty/filtered comment has no text
    # for an LLM, and sending it is the cheapest possible way to waste tokens.
    # Note what follows from Stage 1's rule no longer voting: these comments end
    # up with NO verdict at all — `uncertain` at zero voters — and are counted in
    # `reaction_only` instead. An emoji reaction is crowd signal, not a sentiment
    # measurement, and the output now says so rather than passing off a lexicon
    # guess as a label.
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
