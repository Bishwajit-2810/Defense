# Watchlist-Driven Target Stance

**Status: BUILT (5 August 2026). Validation and the watchlist contents are not.**

| Part | State |
| ---- | ----- |
| `src/defense/libs/stance_targets.py` — loader + alias matcher | **done**, 36 tests |
| `src/defense/libs/stance_scoring.py` — deterministic scorer + aggregation | **done** |
| Stage-1 matching on every comment of every post | **done** |
| Stage-2 LLM target stance, inside the existing stance call | **done** — adds **no** LLM calls |
| Output: per-comment `target_stances` + per-post rollup, own field | **done**, in the schema and the API |
| `config/stance_targets.yml` | **placeholder only** — one example entity, `neutral:` bucket, no real politics |
| §2's bias framing | **decided** — see below; the `neutral:` bucket is the default |
| §7's ~150-comment validation | **not done** — this is what makes the novelty claim measurable |

Two things remain, and neither is code: fill in the watchlist (an editorial
choice, §2) and run the validation (§7).

This document was written before the implementation, deliberately — §2's decision
shapes the output schema and is hard to change afterwards. It has been updated to
match what was built.

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

**What existed before this:** nothing like it. Sentiment was document-level, and
the Stage-2 stance prompt judged stance *toward the post*
([prompts.py](../src/defense/services/workers/stage2_llm/prompts.py)) — never toward a named
entity. The system could tell you a comment was angry. It could not tell you
**who it was angry at**, which for political monitoring is the entire question.
That is what this adds.

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

## 2. The bias framing — decided, and built into the schema

> **A file that declares "support for X is positive" encodes a political stance
> into the labels.** On a corpus of Bangladeshi political content this is not a
> footnote — it is the first thing a sharp examiner will ask about.

The position taken, and now enforced by the schema rather than by convention:

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

`config/stance_targets.yml` — **shipped, with a placeholder entity only.**
`pyyaml 6.0.3` was already in the venv, so no new dependency was added. The real
file is an editorial artefact; what ships is the mechanism plus one example under
`neutral:`, so the repository states no politics of its own.

Absent file = feature off, and the pipeline behaves exactly as before. A
*malformed* file fails loudly at load — a target with no aliases could never
match, and a silently-disabled watchlist would be §5.1's failure all over again.

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

### 4.1 `src/defense/libs/stance_targets.py`

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
[comment_analyzer.py](../src/defense/services/workers/stage1_nlp/comment_analyzer.py). Cruder,
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
- **…which also means it inherits that call's reach.** The Stage-2 comment pass
  covers whatever the router selected — by default every comment with text
  (`ROUTER_COMMENT_TOP_N=0`), so the LLM-scored target stances cover the whole
  thread. Set a positive cap for speed and they cover only that slice, while the
  Stage-1 deterministic verdict still covers every comment; the rollup then mixes
  the two, and `method: "llm"` per entry is the field to check before describing a
  per-target number as LLM-judged.
- **The deterministic scorer fills in** for bypassed posts and stub-mode runs.

### 6.1 The agent-facing surface — and why an unknown `target_id` raises

Two retrieval-MCP tools expose the rollup: `stance_by_target` (distribution per
entity) and `stance_over_time` (one row per period × entity). Both take an
optional `target_id`, and the `stance` agent reaches them.

That id used to be applied as a plain equality filter against whatever string the
model passed, which made **two very different situations look identical**:

```
stance_over_time(target_id="primary_political_figures")  ->  []
```

was briefed to the operator as *"no stance data exists for the primary political
figures"* — while the one entity actually on the watchlist had stance rows in
seven posts the whole time. The model cannot guess an id (this file is
gitignored; it never sees it) and it has no tool that lists one, so it invents
one — and an invented id is indistinguishable from a quiet corpus.

So the roster is the authority. An id that is not on the watchlist now **raises**,
the way a placeholder `campaign_id` does, and the error names every valid id:

```
target_id 'primary_political_figures' is not on the watchlist, so no stance was
ever scored for it. The watchlist tracks exactly: <id> (<display>), … Pass one of
those ids, or omit target_id to get every tracked target. Do not report this as
an absence of data in the corpus.
```

The runner turns that into a tool result the model reads, so the correction costs
one turn out of the budget rather than the whole run. Three details that follow
from §3.1:

- **Aliases resolve too** — id, display name, or any alias, including the Bangla
  and Banglish spellings, because those are what the retrieved comments contain
  and therefore what the model has in front of it when it picks an argument.
- **Comma-separated ids are split, not rejected.** It is the shape a model reaches
  for when the question names a group of people; every part still has to resolve.
- **A *tracked* target with no rows still returns empty.** That is a real answer —
  nobody mentioned them, or the aliases need work (`unmatched_targets` in
  [`stance_targets.py`](../src/defense/libs/stance_targets.py) is the signal for the
  second) — and it is the answer the tool docstrings promise.

If the watchlist file cannot be read at all, the filter falls back to the old
unvalidated behaviour and logs `target_id_unvalidated` rather than taking the
stance tools down with a bad config file.

---

## 7. Validating it — ~150 labelled comments

Scoped for a defense, not a paper. The goal is **one honest number about the new
thing**, not a scorecard.

Two metrics, and they must be reported separately because they fail differently:

1. **Mention detection** — precision and recall of the matcher, on comments
   sampled from `posts_with_details.json` (all 50 posts — comment text does not
   depend on the parent post carrying a caption). This is where the alias work is proved
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
already has all three: [prompts.py](../src/defense/services/workers/stage2_llm/prompts.py),
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

## 10. Effort — and what it actually cost

Estimated ~1 day for config + matcher + prompt path + deterministic fallback +
tests. That was about right; the matcher took most of it, and the two bugs worth
recording were both found by tests rather than by reading:

1. **Overlapping aliases double-counted.** `"alpha party"` and `"alpha"` both fire
   on the same words, so every cue in the surrounding clause was counted twice.
   Fixed by collapsing overlapping matches per target, keeping the longest.
2. **A flat character window does not work on short comments.** With a ±60-char
   window, *"B is the best but A is corrupt"* scored both entities identically,
   because the window spanned the whole comment. Replaced with **clause
   segmentation** — cues attach to the mention in their own clause — which is
   also the version that is explainable in a defense.

Both are the kind of thing that would have passed a manual smoke test and been
wrong on the corpus.

**Still outstanding:** §7's ~150-comment validation (half a day), and filling in
`config/stance_targets.yml`. Neither is code.

Related sequencing, now resolved: the real-mode smoke test found and fixed two
defects of its own ([PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §9.10), and
§5.6's tenant enforcement is implemented — what remains there is provisioning.
