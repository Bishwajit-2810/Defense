# Watchlist-Driven Target Stance

**Status: SPECIFIED, NOT BUILT.** Nothing described here exists in the repository
yet. This document is the design, the decisions that have to be made before
coding, and the validation plan — written first precisely because one of those
decisions (§2) shapes the output schema and is hard to change afterwards.

This is the project's **novelty item**. Everything else in the system is sound
engineering over established methods; this is the one piece that does something
the literature has not covered well for this language setting. It is scoped for a
**capstone defense**, not a paper — see [PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md)
§6.4 for the original specification and §8.5 for the paper-scoped version.

---

## 1. What it is, and what kind of novelty it is

**The requirement, as reported:** a file where names and topics can be listed, so
that talk *against* the listed entities scores negative and talk *for* them
scores positive.

**What exists today:** nothing like it. Sentiment is document-level, and the
Stage-2 stance prompt judges stance *toward the post*
([prompts.py](services/workers/stage2_llm/prompts.py)) — never toward a named
entity. The system can tell you a comment is angry. It cannot tell you **who it
is angry at**, which for political monitoring is the entire question.

**Be precise about the novelty claim, because it will be probed:**

| Claim | Honest? |
| ----- | ------- |
| "Aspect-based / target-dependent stance detection is novel" | **No.** Established for English, well-covered for product reviews and Twitter. |
| "Doing it over a *configurable, operator-supplied* entity list rather than a fixed annotated aspect set" | **Partly.** Less common, more of an engineering framing than a research one. |
| "Doing it on **code-mixed Bangla/Banglish**, where the same entity is written in three scripts and the matcher is the hard part" | **Yes — this is the defensible part.** Genuinely under-served, and the alias problem below is a real technical obstacle, not a formality. |

The line that survives scrutiny: **novel application and engineering in an
under-resourced language setting, not a novel method.** Claiming more invites a
question you cannot win. Claiming this much is both true and enough for a
capstone.

---

## 2. Decide this before writing any code

> **A file that declares "support for X is positive" encodes a political stance
> into the labels.** On a corpus of Bangladeshi political content this is not a
> footnote — it is the first thing a sharp examiner will ask about.

The position to take, and to build into the schema rather than bolt on
afterwards:

1. **Name it a _stated bias model_, not a measurement.** It is entirely
   legitimate for a monitoring product to encode "our client is X, tell us who is
   attacking X." It is indefensible presented as neutral sentiment analysis. Say
   which it is, in the document and out loud.
2. **Ship the file as a documented, versioned appendix.** The watchlist's
   *contents* are an editorial choice by the operator — not a finding, and not
   something this repository should decide. This document specifies the
   mechanism; whoever writes `stance_targets.yml` owns its politics and should
   sign the `notes:` field for each entry.
3. **Keep target stance in a SEPARATE output field from document-level
   sentiment.** This is the schema consequence and the reason to decide now. The
   two must never be summed, averaged, or merged into one chart. A results table
   that conflates "this comment is negative" with "this comment is negative
   *toward our client*" is the single most likely way to lose credibility on this
   feature.
4. **The neutral fallback stays neutral.** A comment mentioning no listed target
   gets no `target_stances` entry at all — not an empty-but-present verdict, and
   never a default of "neutral toward everyone."

**Defense answer, if asked directly:** *"It's a stated bias model. The file says
who the operator cares about; the system reports stance toward those entities in
a separate field from document-level sentiment, so the editorial choice is
visible and auditable rather than baked invisibly into one number."*

---

## 3. The config file

`config/stance_targets.yml`. `pyyaml 6.0.3` is already in the venv — no new
dependency.

```yaml
# STATED BIAS MODEL — see stance_targets.md §2.
# The contents of this file are an editorial choice, not a measurement.
version: 1
updated: 2026-08-05
owner: <who signed off on these entries>

favored:
  - id: entity_a
    display: "Entity A"
    aliases:
      - "এন্টিটি এ"          # Bangla script
      - "entity a"           # English
      - "entiti e"           # common Banglish romanization
      - "ent. a"             # abbreviation seen in the corpus
    notes: "Why this entity is on the favored list, and who decided."

opposed:
  - id: entity_b
    display: "Entity B"
    aliases: ["এন্টিটি বি", "entity b", "entiti bi"]
    notes: "..."

# Optional: entities to track WITHOUT a polarity, so stance is reported but
# no positive/negative framing is imposed. Prefer this when the goal is
# monitoring rather than advocacy — it keeps the output a measurement.
neutral:
  - id: entity_c
    display: "Entity C"
    aliases: [...]
    notes: "Tracked for volume, not scored for favour."
```

The `neutral:` bucket is worth defaulting to. It gives you target-dependent
stance — the interesting part — **without** the editorial baggage of declaring
who is good. If the owner wants advocacy semantics, `favored`/`opposed` are
there; if they want defensible monitoring, `neutral` alone does the job.

### 3.1 Aliases are the load-bearing part

Not an extra field — **the feature works or fails here.** This corpus writes the
same entity in Bangla script, in romanized Banglish (with no standard spelling),
and in English, often within one thread. A watchlist that matches only one
spelling will silently match almost nothing — and it will fail *quietly*, which
is the exact failure mode [PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §5.1
catalogues four times over.

Concretely, expect for a single person:

- native-script name, sometimes with honorifics attached without a space;
- three to six romanizations, differing in vowel choices and doubled consonants;
- an English rendering that may differ from all of them;
- an abbreviation or party initialism;
- a nickname or epithet that carries the stance by itself.

**Design consequences:**

- **Match case-insensitively for Latin, exactly for Bangla.** Bangla has no case;
  lowercasing is a no-op there and only risks surprises.
- **Require token boundaries for short aliases.** A two- or three-character
  abbreviation will otherwise match inside unrelated words. Set a minimum length
  below which an alias must match as a whole token.
- **Bangla word boundaries are not spaces.** Bengali attaches suffixes directly,
  so `entity + suffix` is one token. A pure `\b` regex under-matches. Allow a
  suffix after a Bangla alias, but not a prefix.
- **Log unmatched targets per run.** A target that matched zero comments across
  the whole corpus is almost certainly an alias-coverage bug, not an absence of
  discussion. Make that visible instead of assuming silence means agreement.
- **Store which alias matched**, not just that the target did. It is the fastest
  way to debug both misses and false positives, and it is interesting data in
  itself — *which* spelling dominates is a small finding about the corpus.

---

## 4. Components

### 4.1 `libs/stance_targets.py`

Loader plus matcher. **Pure functions, no I/O beyond reading the YAML, no
framework.** Unit-testable and reusable, which matters because the offline test
suite has no LLM available.

- `load_targets(path) -> Targets` — parsed, validated, with duplicate-alias
  detection across entries (two entities sharing an alias is a config bug worth
  failing on, not silently resolving).
- `match(text) -> list[Match]` — every target mentioned, with the matched alias
  and its span. Spans matter: the deterministic scorer needs proximity.

### 4.2 Two scorers, one interface

Both return the same shape, so the pipeline does not branch on which ran:

**(a) LLM path** — inject the matched targets into the Stage-2 stance prompt so
the model judges stance *toward that target*, returning
`[{target, stance, evidence}]`. `evidence` is the span the model based it on —
it is what makes a wrong verdict debuggable in a demo.

**(b) Deterministic fallback** — alias proximity plus polarity cues, using the
existing emoji and lexicon tables in
[comment_analyzer.py](services/workers/stage1_nlp/comment_analyzer.py). Cruder,
but it runs in stub mode and in CI. **Without it, the offline suite cannot cover
this feature at all**, which for a capstone means the one novel component is also
the one with no tests.

### 4.3 Aggregation

Per post, per target: counts of supportive / opposing / neutral comments, plus
total mentions. **This aggregate is the actual product output** — the thing a
dashboard panel renders and the thing you point at in a defense. A per-comment
stance list is raw material; the per-target rollup is the answer.

---

## 5. Output shape

Additive to `comment_analysis`. Nothing existing changes meaning.

```jsonc
"target_stances": {                    // NEW — never merged into sentiment_breakdown
  "entity_a": {
    "display": "Entity A",
    "polarity": "favored",             // from the config; echoed so output is self-describing
    "mentions": 47,
    "supportive": 31,
    "opposing": 12,
    "neutral": 4,
    "method": "llm",                   // llm | deterministic — same provenance discipline as §5.3
    "aliases_matched": { "এন্টিটি এ": 40, "entity a": 7 }
  }
}
```

Per comment, alongside the existing `sentiment` / `emotion` / `method` / `kind`:

```jsonc
"target_stances": [
  { "target": "entity_a", "stance": "opposing", "evidence": "…", "method": "llm" }
]
```

Absent entirely when no target is mentioned — **not** an empty object with
neutral defaults, which would make "nobody talked about X" indistinguishable from
"everybody was neutral about X."

**`method` carries through**, matching the discipline established in §5.3: a
chart must be able to say whether its numbers came from a model or a keyword
heuristic. This feature has both, so this field is not optional.

---

## 6. Where it runs

Reuse the existing lane rather than adding one:

- **Matching is free and runs in Stage 1**, on every comment of every post. It is
  string matching; there is no reason to gate it. This also means mention
  *volume* is available for bypassed posts, which is a useful cheap signal on its
  own.
- **LLM stance scoring rides inside the existing Stage-2 comment-stance call** —
  the matched targets go into a prompt that is already being sent. No new LLM
  calls, so this feature is **free** against the cost model in
  [PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §6.8. Worth saying out loud: it
  adds a capability without adding a lane.
- **The deterministic scorer fills in** for bypassed posts and stub-mode runs.

---

## 7. Validating it — ~150 labelled comments

Scoped for a defense, not a paper. The goal is **one honest number about the new
thing**, not a scorecard.

Two metrics, and they must be reported separately because they fail differently:

1. **Mention detection** — precision and recall of the matcher, on comments
   sampled from `posts_text_only.json`. This is where the alias work is proved
   or disproved, and it is the number that matters most. **Recall is the one to
   watch**: a missed alias is invisible, an over-match is obvious.
2. **Stance agreement** — of the correctly-matched mentions, how often the stance
   matches a human's. Report the LLM and deterministic scorers separately; the
   gap between them is itself interesting.

Sampling notes:

- Draw **within language buckets** (`bn` / `en` / `banglish`), not top-N by
  likes — otherwise the validation inherits the corpus's engagement bias
  ([evaluation.md](evaluation.md) §1) and measures the easy half.
- **Deliberately include comments that mention nothing**, or precision is
  unmeasurable.
- One annotator is sufficient at this scale. Say so, and report the small-n
  caveat rather than implying more rigour than you bought.

---

## 8. What this deliberately does not use

**LangChain / LangGraph — recommended against**, though the requirement mentioned
them.

The need is one config file, one prompt slot, and one parser. This repository
already has all three: [prompts.py](services/workers/stage2_llm/prompts.py),
`_safe_json_parse`, and the role-based `LLMClient`. LangChain would add a large
dependency tree and a second prompt-templating system, and — the deciding factor
— **it would not run in the offline stub path** that the entire test suite and
`eval/measure_routing_rate.py` depend on. The one novel component would become
the one untestable component.

Keeping the scorers as pure functions costs nothing and leaves the door open: if
graph orchestration is ever wanted (multi-step target resolution, human-in-the-
loop review), they wrap as LangGraph nodes without touching the pipeline.

**This is a recommendation, not a blocker.** If the requirement is specifically
"demonstrate LangGraph in the thesis," it can be added as a thin orchestration
layer over the same functions — but then it is a demonstration of LangGraph, and
should be described as such rather than as an architectural need.

---

## 9. Defense notes

**What to demonstrate live.** Edit `stance_targets.yml`, re-run one post, show
the per-target rollup change. That is a better demo than any static chart,
because it shows the system is *configurable*, not hard-coded — and it takes
thirty seconds.

**Three questions to have answers ready for:**

| Question | Answer |
| -------- | ------ |
| *"Isn't this just sentiment analysis?"* | No — document-level sentiment says a comment is negative; this says *who it is negative toward*. They are separate fields for exactly that reason, and a comment can be positive overall while opposing a listed target. |
| *"You've encoded your own politics into the labels."* | Yes, deliberately and visibly. It is a stated bias model (§2), versioned, with a documented owner. The alternative — an implicit bias in the training data — is worse because it is invisible. The `neutral:` bucket exists for when monitoring rather than advocacy is wanted. |
| *"How do you know the matcher works on Banglish?"* | The §7 numbers, reported as precision/recall on mention detection specifically, with the caveat that it is ~150 comments and one annotator. |

**What not to claim.** That it is a novel method; that it generalizes beyond the
alias lists you wrote; that the stance verdicts are validated at scale. The
honest scope — *a working, configurable, tested capability with a small
validation set, in a language setting where this is hard* — is a good capstone
result on its own.

---

## 10. Effort

~1 day for config + matcher + prompt path + deterministic fallback + tests, plus
roughly half a day for the ~150-comment validation. The §2 decisions should be
settled **before** any of it, because they determine the output schema.

Not on the critical path but worth sequencing after: the real-mode smoke test
(`MODEL_STUB_MODE=false` has never been run end to end) and
[PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §5.6, the one remaining §5 finding
that is still claimed in the pitch.
