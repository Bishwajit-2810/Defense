# Project Paper — Title Options

This document proposes candidate titles for the project paper describing the
**social-media "Smart Layer"** — a multilingual (Bangla / English / **Banglish**)
AI microservice that ingests social posts with their comment threads and returns
structured analytical JSON, using a **hybrid NLP → selective-LLM routing
pipeline** behind a pluggable (local Ollama/vLLM ⇄ Groq) backend.

> **Two words to drop, on the evidence (4 August 2026).**
>
> **"Multimodal"** — the image path is implemented but has never produced a
> signal in any runnable configuration (no image bytes are reachable; see
> [PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §5.2 and
> [data_contract.md](data_contract.md) §4). A title that claims multimodality
> invites the first question of the defense to be one with no good answer.
> Post sentiment is currently a **text** measurement over caption + comments.
>
> **"Cost-Efficient"** — defensible only if it is *measured*, and the measurement
> has moved. The routing rate is **16% of posts** on the shipped configuration
> (not single digits), and since every non-emoji comment now reaches the LLM the
> cost is **comment-dominated**: 85% of LLM calls are comment-level. The gate
> governs 55% of total spend. The claim that survives is *"cheap NLP filters
> which comments and which posts deserve an LLM"* — real, but narrower than the
> phrase "cost-efficient" implies on a title page.
>
> The honest distinctive claims are: **code-mixed Bangla/Banglish**, the
> **post+thread** unit with per-comment coverage, the **confidence-gated
> cascade** as a measured trade-off, the **runtime-switchable local/cloud
> backend**, and — since 5 August — **target-dependent stance over a
> configurable, alias-aware watchlist** ([stance_targets.md](stance_targets.md)),
> which is the one component with a plausible claim to novelty. Titles below are
> re-ordered accordingly.
>
> **A title option that leads on the novelty**, now that it is built:
>
> > *Who Are They Angry At? Target-Dependent Stance Detection over a Configurable
> > Watchlist for Code-Mixed Bangla–English–Banglish Social Media*
>
> Be ready to concede that aspect-based stance detection is established — the
> defensible part is the **code-mixed, under-resourced setting and the
> three-script alias problem**, not the method.

---

## Recommended title

> ### Selective Intelligence at Scale: A Confidence-Gated Hybrid NLP–LLM Pipeline for Code-Mixed Bangla–English–Banglish Social-Media Analysis

**Why:** it names what is actually demonstrated — the **confidence-gated
cascade** (measured, with a rate that responds to Stage-1 quality), the
**code-mixed Bangla/Banglish** focus (the genuinely under-resourced part), and
the **post + comment-thread** scope — without claiming multimodality the system
cannot currently show or a cost result the numbers do not support.

**Previous recommendation**, kept for the record:

> ~~A Cost-Efficient Hybrid NLP–LLM Smart Layer for Multilingual, Multimodal
> Social-Media Analysis in Bangla, English, and Banglish~~

It named three things, two of which the system cannot currently defend. Restore
"Multimodal" if the image objects are uploaded and vision numbers are reported;
restore "Cost-Efficient" if §9.8's end-to-end run produces a cost table.

---

## Five candidate titles

1. **Selective Intelligence at Scale: A Confidence-Gated Hybrid NLP–LLM Pipeline
   for Code-Mixed Bangla–English–Banglish Social-Media Analysis**
   *(recommended — every word is currently demonstrable)*

2. **The Thinking Layer: A Confidence-Gated Hybrid Pipeline for Scalable
   Bangla–English–Banglish Social-Media Understanding**
   *(emphasizes the smart routing/triage idea and horizontal scale; also safe)*

3. **From Posts to Structured Insight: A Microservice for Sentiment,
   Summarization, and Comment-Thread Analysis of Code-Mixed Social Media**
   *(emphasizes the input→structured-JSON contract and the post+thread unit —
   "Multimodal" removed from the original wording)*

4. **Designing a Production-Grade Multilingual Social-Media Analysis Microservice:
   A Hybrid NLP–LLM Architecture for Bangla, English, and Banglish**
   *(emphasizes the systems/architecture and production-engineering contribution
   — the framing §9's "Framing advice" recommends, and fully supported)*

5. ~~**A Cost-Efficient Hybrid NLP–LLM Smart Layer for Multilingual, Multimodal
   Social-Media Analysis in Bangla, English, and Banglish**~~
   *(the previous recommendation — hold until the image objects and a cost table
   exist; see the note at the top)*

---

## A few more, by emphasis

- **Bangla/Banglish-first framing:**
  *Understanding Banglish at Scale: A Hybrid NLP–LLM System for Code-Mixed
  Social-Media Sentiment and Insight*

- **Crowd-signal framing** (replaces the multimodal one — `reactionBreakdown` is
  real and in hand, images are not):
  *Fusing Text and Crowd Signals: Sentiment and Summarization for Multilingual
  Social-Media Posts and Their Comment Threads*

- **Engineering framing:**
  *Spending the LLM Wisely: A Confidence-Routed, Horizontally Scalable Smart Layer
  for Multilingual Social-Media Analysis*

- **Concise / punchy:**
  *Smart Layer: Hybrid NLP–LLM Analysis of Code-Mixed Social Media*

- **Multimodal framing** — *only if the image objects are uploaded and vision
  numbers reported:*
  *Fusing Text, Image, and Crowd Signals: Multimodal Sentiment and Summarization
  for Multilingual Social-Media Posts and Their Comment Threads*

---

## Title-building blocks (mix and match)

| Slot | Options |
| --- | --- |
| **Hook / concept** | Smart Layer · The Thinking Layer · Selective Intelligence · Spending the LLM Wisely |
| **Method** | Hybrid NLP–LLM · Confidence-Gated Routing · Hybrid Pipeline · ~~Multimodal Fusion~~ |
| **Quality** | Scalable · Production-Grade · Horizontally Scalable · ~~Cost-Efficient~~ |
| **Domain** | Social-Media Analysis / Analytics / Understanding · Post + Comment-Thread Analysis |
| **Languages** | Bangla, English, and Banglish · Code-Mixed (Banglish) · Multilingual |
| **Form** | Microservice · System · Architecture · Pipeline |

Struck-through blocks are the ones the current evidence does not support — see
the note at the top of this file.

> Suggested subtitle for any of the above:
> *"A hybrid NLP–LLM microservice that turns social posts and their comment threads
> into structured, multilingual insight."*
