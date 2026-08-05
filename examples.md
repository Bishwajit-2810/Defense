# Worked Examples — Real Post-with-Details → Output JSON

Real records from the upstream **post-with-details** payload (verbatim from
[posts_with_details.json](posts_with_details.json)) run through the smart layer,
showing the exact input and the structured JSON the service returns. These match
the canonical schema in [architecture.md](architecture.md) §6, the input contract
in [data_contract.md](data_contract.md), and the API in [api_design.md](api_design.md).

> ## ⚠ These examples show the DESIGNED output, not a current run
>
> They were written to illustrate the full contract, and two parts of them are
> **not what the pipeline produces today** (4 August 2026 —
> [PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md)):
>
> - **`image_sentiment` is always `null`** and `vision_model` reports a *status*
>   (`fetch_failed` / `stub` / `model_unavailable`), not a model name. No image
>   bytes are reachable, so `post_summary_source` is always `"llm"`, never
>   `"vlm"`, and `post_summary_grounding` is `"caption"` (§5.2). The non-null
>   `image_sentiment` blocks below are aspirational.
> - **`comment_analysis` now carries more fields** than these examples show:
>   `sentiment_breakdown_substantive`, `reaction_only`, `provenance`,
>   `coverage_anomaly`, and a per-comment `kind`. `coverage` is **clamped to
>   1.0**. `method` can be `stub` — a deterministic hash, not sentiment — and the
>   `provenance` block is what tells you how many labels were real inferences.
>
> Everything else — the comment thread, the reaction cross-check, the recomputed
> sentiment, the Bangla summaries — is current. Regenerate against a live run
> before quoting any of this in a defense.

Three things to keep in mind:

- **Pipeline order** ([data_contract.md](data_contract.md) §4): (1) text
  sentiment on the caption, (2) **`image_sentiment`** from a visual model on the
  photo (we also **OCR it ourselves** — the payload no longer ships OCR)
  *— implemented but currently producing no signal, see the note above*, (3)
  fuse into `overall_sentiment`/`sentiment_score` **over the terms that carry a
  real verdict**, cross-checked against **`reaction_breakdown`**, (4) a
  `post_summary` **grounded on caption (+ OCR + image when available)**, (5)
  **per-comment sentiment** over the **embedded** comment thread — since the
  caps were lifted, that means *every non-emoji comment*, not the top 60.
- **Comments are live and embedded.** Each post ships a stored sample of its
  comments (`engagement.storedCommentRows` of `commentCount`), with `sentiment:
null` — **we compute it**. `comment_analysis.coverage` reports the sample size;
  we never imply we saw every comment.
- **Sentiment is recomputed**, keeping the upstream post `sentiment` as
  `baseline_sentiment`. Summaries are written **in the post's own language**;
  Banglish is folded into the dominant language. (This sample is 100% Bangla
  Facebook; the pipeline is platform-agnostic by URL host.)

---

## Example 1 — Facebook, Bangla, photo+text (grief post, embedded comments)

A heavy Bangla photo+text post commemorating the Shapla Chattar events, with an
image and a large comment thread. The crowd **`reaction_breakdown` is dominated by
`SAD` (65,289)** — a strong emotion signal that agrees with the recomputed negative
sentiment.

### Input — real record (verbatim, abridged)

```json
{
  "id": "cmosjpp9305n0u9tskgmd1c4k",
  "campaignId": "cmoldmxzr02d8fu22vhvrg23c",
  "platformPostId": "4460219584209360",
  "url": "https://www.facebook.com/4460219584209360",
  "caption": "কাওকে ফাসির কাষ্ঠে ঝুলানোর আগে একবার শেষ ইচ্ছা পূরণ করা হয়। ... অথচ এই মানুষগুলার সাথে হয়েছে উল্টো।",
  "photoUrls": [
    "posts/cmoldmxzr02d8fu22vhvrg23c/4460219584209360/18f4cbb26803.jpg"
  ],
  "videoUrl": null,
  "postType": "PHOTO_TEXT",
  "postedAt": "2026-05-04T18:19:14",
  "scrapedAt": "2026-05-05T17:39:44.464",
  "sentiment": -0.85,
  "viralPotential": 0.78,
  "isViral": false,
  "engagement": {
    "reach": 0,
    "saves": 0,
    "shareCount": 3189,
    "impressions": 0,
    "commentCount": 1562,
    "totalReactions": 84979,
    "storedCommentRows": 112,
    "storedReactionRows": 0
  },
  "reactionBreakdown": {
    "SAD": 65289,
    "WOW": 56,
    "CARE": 125,
    "HAHA": 566,
    "LIKE": 18235,
    "LOVE": 682,
    "ANGRY": 26
  },
  "sampleShares": [],
  "comments": [
    {
      "id": "cmosktkag038n8jv53z8hx4ea",
      "platformCommentId": "…",
      "parentId": null,
      "likes": 574,
      "replyCount": 14,
      "category": "NEUTRAL",
      "sentiment": null,
      "authorUsername": "Abdur Rahman Wisdom's",
      "text": "এই ছবিগুলো প্রমাণ করে যে পুলিশ আমাদের বন্ধু ছিল না কখনো।"
    }
  ]
}
```

> The `comments[]` array holds **112 of 1,562** comments (`storedCommentRows`), each
> with `sentiment: null` — our pipeline computes the sentiment below.

### Output JSON

```json
{
  "post_id": "cmosjpp9305n0u9tskgmd1c4k",
  "campaign_id": "cmoldmxzr02d8fu22vhvrg23c",
  "platform": "facebook",
  "platform_post_id": "4460219584209360",
  "media_type": "PHOTO_TEXT",
  "language": "bn",
  "language_mix": ["bn"],
  "language_confidence": 0.98,
  "post_type": "commemoration",
  "post_summary": "শাপলা চত্বরের ঘটনার স্মরণে একটি আবেগঘন বাংলা পোস্ট; ছবিতে সেই রাতের দৃশ্য। পোস্ট ও মন্তব্যে শোক এবং আওয়ামী লীগের প্রতি ক্ষোভ প্রবল।",
  "post_summary_lang": "bn",
  "post_summary_grounding": ["caption", "image"],
  "overall_sentiment": "negative",
  "sentiment_score": -0.82,
  "text_sentiment": { "label": "negative", "score": -0.85 },
  "image_sentiment": {
    "label": "negative",
    "score": -0.7,
    "per_image": [-0.7]
  },
  "baseline_sentiment": -0.85,
  "baseline_viral_potential": 0.78,
  "emotion": "sadness",
  "intents": ["commemorate", "express_grievance"],
  "topics": ["shapla chattar", "2013", "politics", "grief"],
  "keywords": ["শাপলা", "শোক", "আওয়ামী লীগ"],
  "toxicity_score": 0.18,
  "hate_speech_score": 0.12,
  "engagement": {
    "reactions": 84979,
    "comment_count": 1562,
    "share_count": 3189,
    "stored_comments": 112
  },
  "reaction_breakdown": {
    "SAD": 65289,
    "LIKE": 18235,
    "LOVE": 682,
    "HAHA": 566,
    "CARE": 125,
    "WOW": 56,
    "ANGRY": 26
  },
  "image_analysis": {
    "image_count": 1,
    "ocr_text": "",
    "description": "a dark night-time scene of a crowd / security forces",
    "images": [
      {
        "ref": "photoUrls[0]",
        "sentiment": { "label": "negative", "score": -0.7 },
        "ocr_text": "",
        "description": "a dark night-time scene of a crowd / security forces"
      }
    ],
    "vision_model": "SigLIP (sentiment) + Qwen2.5-VL-7B (description+OCR)"
  },
  "comment_analysis": {
    "analyzed": 112,
    "coverage": "112/1562 stored",
    "sentiment_breakdown": { "positive": 6, "negative": 89, "neutral": 17 },
    "themes": [
      "grief and remembrance",
      "anger at Awami League",
      "calls for justice"
    ],
    "representative_comments": [
      {
        "author": "Abdur Rahman Wisdom's",
        "lang": "bn",
        "sentiment": "negative",
        "likes": 574,
        "text": "এই ছবিগুলো প্রমাণ করে যে পুলিশ আমাদের বন্ধু ছিল না কখনো।"
      }
    ]
  },
  "post_summary_source": "vlm",
  "confidence": 0.9,
  "processing": {
    "unit": "post+thread",
    "stage1_ms": 95,
    "llm_used": true,
    "llm_role": "LLM-A",
    "llm_backend": "local",
    "llm_model": "Qwen2.5-7B-Instruct",
    "vision_used": true,
    "vision_model": "Qwen2.5-VL-7B-Instruct"
  },
  "created_at": "2026-05-04T18:19:14",
  "scraped_at": "2026-05-05T17:39:44.464"
}
```

**What did the work:** Stage-1 ran text sentiment on the caption (`-0.85`), a visual
model + **our OCR** on the photo (`image_sentiment -0.7`), and fused them to `-0.82`
— **agreeing with the `SAD`-dominated `reaction_breakdown`**, which we use as a
cross-check. The **112 embedded comments** were each scored by us (the upstream
shipped `sentiment: null`) → an 89/17/6 negative/neutral/positive breakdown, with
**coverage `112/1562`** surfaced (we analyzed the stored sample, not all 1,562). A
VLM produced the Bangla `post_summary` grounded on caption + image.

---

## Example 2 — Facebook, Bangla, text-only (embedded comments)

A text-only opinion post. No image → `image_sentiment` is `null`; the comment
thread still drives a rich `comment_analysis`. `reaction_breakdown` skews
`SAD`/`ANGRY`.

### Input — real record (verbatim, abridged)

```json
{
  "id": "cmouf3g7p0dnae4hkfuk6spet",
  "campaignId": "cmold8r5301u8fu22m7flh3pc",
  "platformPostId": "122161870454710684",
  "url": "https://www.facebook.com/122161870454710684",
  "caption": "মুসলিমদের জন্য ভারত ন’রকে পরিণত হয়েছে।\n\nলিঙ্ক কমেন্টে",
  "photoUrls": [],
  "postType": "TEXT",
  "postedAt": "2026-05-06T16:45:03",
  "scrapedAt": "2026-05-11T14:55:21.045",
  "sentiment": -0.85,
  "viralPotential": 0.78,
  "engagement": {
    "shareCount": 3136,
    "commentCount": 6567,
    "totalReactions": 26700,
    "storedCommentRows": 607,
    "storedReactionRows": 0,
    "reach": 0,
    "saves": 0,
    "impressions": 0
  },
  "reactionBreakdown": {
    "SAD": 13047,
    "WOW": 29,
    "CARE": 14,
    "HAHA": 948,
    "LIKE": 11219,
    "LOVE": 80,
    "ANGRY": 1363
  },
  "comments": [
    {
      "id": "…",
      "parentId": null,
      "likes": 0,
      "replyCount": 0,
      "category": "NEUTRAL",
      "sentiment": null,
      "authorUsername": "Atondrila Morshed",
      "text": "Iran ra esb dekhe na chokhe?!!!"
    }
  ]
}
```

### Output JSON

```json
{
  "post_id": "cmouf3g7p0dnae4hkfuk6spet",
  "campaign_id": "cmold8r5301u8fu22m7flh3pc",
  "platform": "facebook",
  "platform_post_id": "122161870454710684",
  "media_type": "TEXT",
  "language": "bn",
  "language_mix": ["bn", "banglish", "en"],
  "language_confidence": 0.95,
  "post_type": "opinion",
  "post_summary": "ভারতে মুসলিমদের পরিস্থিতি নিয়ে একটি ক্ষুব্ধ মতামত পোস্ট; মন্তব্যে ক্ষোভ ও উদ্বেগ প্রবল, অনেকে লিঙ্ক/রেফারেন্স শেয়ার করেছেন।",
  "post_summary_lang": "bn",
  "post_summary_grounding": ["caption"],
  "overall_sentiment": "negative",
  "sentiment_score": -0.8,
  "text_sentiment": { "label": "negative", "score": -0.8 },
  "image_sentiment": null,
  "baseline_sentiment": -0.85,
  "baseline_viral_potential": 0.78,
  "emotion": "anger",
  "intents": ["express_grievance", "inform"],
  "topics": ["india", "muslims", "politics"],
  "keywords": ["ভারত", "মুসলিম"],
  "toxicity_score": 0.34,
  "hate_speech_score": 0.21,
  "engagement": {
    "reactions": 26700,
    "comment_count": 6567,
    "share_count": 3136,
    "stored_comments": 607
  },
  "reaction_breakdown": {
    "SAD": 13047,
    "LIKE": 11219,
    "ANGRY": 1363,
    "HAHA": 948,
    "LOVE": 80,
    "WOW": 29,
    "CARE": 14
  },
  "comment_analysis": {
    "analyzed": 607,
    "coverage": "607/6567 stored",
    "sentiment_breakdown": { "positive": 41, "negative": 466, "neutral": 100 },
    "themes": [
      "anger at India's treatment of Muslims",
      "calls for awareness",
      "links/references shared"
    ]
  },
  "post_summary_source": "llm",
  "confidence": 0.9,
  "processing": {
    "unit": "post+thread",
    "stage1_ms": 130,
    "llm_used": true,
    "llm_role": "LLM-A",
    "llm_backend": "local",
    "llm_model": "Qwen2.5-7B-Instruct"
  },
  "created_at": "2026-05-06T16:45:03",
  "scraped_at": "2026-05-11T14:55:21.045"
}
```

**What did the work:** text-only → `image_sentiment: null`, `post_summary_source:
"llm"`. The caption sentiment (`-0.8`) is recomputed (upstream `-0.85` kept as
`baseline_sentiment`). The **607 embedded comments** were scored by us into a
466/100/41 breakdown with **coverage `607/6567`**, and the `SAD`+`ANGRY`
`reaction_breakdown` corroborates the negative read.

---

## Example 3 — Facebook, `null` caption, PHOTO (image + OCR carry the post)

A photo post with **no caption** — the image and **our OCR** carry the analysis,
and the comment thread is analyzed live. Shows our recompute diverging from a
softer upstream baseline (upstream `-0.28`; reactions are heavily `SAD`+`ANGRY`).

### Input — real record (verbatim, abridged)

```json
{
  "id": "cmp2y5w5z0vj78cx6jrpqaxw4",
  "campaignId": "cmolspflk0n3pkrd0wmnrpf3q",
  "platformPostId": "1032556609118249",
  "url": "https://www.facebook.com/1032556609118249",
  "caption": null,
  "photoUrls": [
    "posts/cmolspflk0n3pkrd0wmnrpf3q/1032556609118249/be027579ea6.jpg"
  ],
  "postType": "PHOTO",
  "postedAt": "2026-05-12T10:28:16",
  "scrapedAt": "2026-05-13T18:14:39.091",
  "sentiment": -0.28,
  "viralPotential": 0.65,
  "engagement": {
    "shareCount": 2706,
    "commentCount": 1318,
    "totalReactions": 38356,
    "storedCommentRows": 87,
    "storedReactionRows": 0,
    "reach": 0,
    "saves": 0,
    "impressions": 0
  },
  "reactionBreakdown": {
    "SAD": 18947,
    "ANGRY": 11407,
    "LIKE": 7752,
    "HAHA": 133,
    "WOW": 65,
    "LOVE": 34,
    "CARE": 18
  },
  "comments": [
    {
      "id": "…",
      "parentId": null,
      "likes": 7,
      "replyCount": 0,
      "category": "NEUTRAL",
      "sentiment": null,
      "authorUsername": "Sheikh Mohammed Rashed",
      "text": "রাম রাজ্যে বলে কথা 😂"
    }
  ]
}
```

### Output JSON

```json
{
  "post_id": "cmp2y5w5z0vj78cx6jrpqaxw4",
  "campaign_id": "cmolspflk0n3pkrd0wmnrpf3q",
  "platform": "facebook",
  "platform_post_id": "1032556609118249",
  "media_type": "PHOTO",
  "language": "bn",
  "language_mix": ["bn", "en"],
  "language_confidence": 0.9,
  "post_type": "news",
  "post_summary": "ক্যাপশনহীন একটি ছবি-পোস্ট (সংবাদ-গ্রাফিক); ছবির লেখা ও মন্তব্য থেকে বোঝা যায় ভারত-সংক্রান্ত একটি স্পর্শকাতর ঘটনা — মন্তব্যে শোক ও ক্ষোভ মিশ্রিত।",
  "post_summary_lang": "bn",
  "post_summary_grounding": ["ocr", "image"],
  "overall_sentiment": "negative",
  "sentiment_score": -0.6,
  "text_sentiment": null,
  "image_sentiment": {
    "label": "negative",
    "score": -0.55,
    "per_image": [-0.55]
  },
  "baseline_sentiment": -0.28,
  "baseline_viral_potential": 0.65,
  "emotion": "sadness",
  "intents": ["inform"],
  "topics": ["india", "politics", "news"],
  "keywords": ["রাম রাজ্য", "ভারত"],
  "toxicity_score": 0.2,
  "hate_speech_score": 0.14,
  "engagement": {
    "reactions": 38356,
    "comment_count": 1318,
    "share_count": 2706,
    "stored_comments": 87
  },
  "reaction_breakdown": {
    "SAD": 18947,
    "ANGRY": 11407,
    "LIKE": 7752,
    "HAHA": 133,
    "WOW": 65,
    "LOVE": 34,
    "CARE": 18
  },
  "image_analysis": {
    "image_count": 1,
    "ocr_text": "<our OCR of the graphic text>",
    "description": "a news-style headline graphic",
    "images": [
      {
        "ref": "photoUrls[0]",
        "sentiment": { "label": "negative", "score": -0.55 },
        "ocr_text": "<our OCR>",
        "description": "a news-style headline graphic"
      }
    ],
    "vision_model": "SigLIP (sentiment) + Qwen2.5-VL-7B (description+OCR)"
  },
  "comment_analysis": {
    "analyzed": 87,
    "coverage": "87/1318 stored",
    "sentiment_breakdown": { "positive": 9, "negative": 55, "neutral": 23 },
    "themes": [
      "reactions to India-related news",
      "sarcasm / 'Ram Rajya' jabs",
      "shared news links"
    ]
  },
  "post_summary_source": "vlm",
  "confidence": 0.84,
  "processing": {
    "unit": "post+thread",
    "stage1_ms": 70,
    "llm_used": true,
    "llm_role": "LLM-A",
    "llm_backend": "local",
    "llm_model": "Qwen2.5-7B-Instruct",
    "vision_used": true,
    "vision_model": "Qwen2.5-VL-7B-Instruct"
  },
  "created_at": "2026-05-12T10:28:16",
  "scraped_at": "2026-05-13T18:14:39.091"
}
```

**What did the work:** `caption` is `null`, so `text_sentiment` is `null` — the
**image + our OCR** carry the post (the upstream no longer ships OCR). The visual
model + `SAD`/`ANGRY`-heavy `reaction_breakdown` push the recompute to **`-0.6`**,
notably more negative than the upstream baseline `-0.28` (kept as
`baseline_sentiment`). The **87 embedded comments** were scored by us
(coverage `87/1318`).

---

## How these map to the owner's request

The owner asked for `{ post_summary, sentiment_analysis, "and something like that" }`,
over the **real** post-with-details records. The schema delivers:

- **`post_summary`** — in the original language (`post_summary_lang`), **grounded on
  caption + OCR + image** (`post_summary_grounding`), so a `null`-caption photo post
  is still summarized from its picture (Example 3).
- **`sentiment_analysis`** — **multimodal and full-thread**: `text_sentiment`
  (caption), `image_sentiment` (the photo), the fused post-level
  `overall_sentiment`/`sentiment_score` (cross-checked against
  `reaction_breakdown`), and **our** per-comment sentiment aggregated into
  `comment_analysis.sentiment_breakdown` with **coverage**. The upstream post score
  is kept as `baseline_sentiment`. **Order: post text → image → fuse → summary →
  comments.**
- **"something like that"** — `post_type`, `media_type`, `image_analysis`,
  `reaction_breakdown`, `shares`, `intents`, `topics`, `keywords`,
  `comment_analysis.themes`, toxicity, engagement (with comment **coverage**), and
  `campaign_id`/`platform` provenance — rich structured signal, not just two fields.
