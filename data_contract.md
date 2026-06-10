# Upstream Data Contract — Post API, Comment API & Integration

**Authoritative description of the real input the smart layer consumes**, how it
integrates with the existing platform, and how every upstream field maps into our
analysis. This supersedes the earlier sketched input schema (the invented
`{post_id, platform, text, author, comments[]}` shape) everywhere it still appears
in older docs — the real shapes below are the source of truth. Worked
input→output runs over real records are in [examples.md](examples.md); our REST
contracts are in [api_design.md](api_design.md); the canonical output schema is in
[architecture.md](architecture.md) §6.

---

## 0. The big picture — a service on top of an existing platform

The smart layer is **not** the scraper. It is a downstream analysis service that
sits next to a **pre-existing social-media monitoring platform** (the "upstream").
That platform already scrapes posts and comments across Facebook, Telegram, X,
Instagram, etc., persists them, and exposes them over REST:

- a **Post API** — returns scraped posts (shape in §1, real sample in
  [social_posts.json](social_posts.json)),
- a **Comment API** — returns the comments/replies for a post (shape in §2,
  **schema to be finalized when the upstream payload is provided** — §2 is the
  expected/working contract until then).

Records are keyed by **stable unique IDs** (CUIDs, e.g.
`cmq7orcjr2w78x80tufd0nza4`). The post `id` is the join key: a post's comments are
fetched separately from the Comment API and joined to the post by that id. The
**unit of analysis remains "a post together with its comment thread"** — we just
**assemble** the thread by joining two API responses, instead of receiving it
pre-nested.

### Integration model — pull + our own separate database

```text
  ┌──────────────────────────┐        pull (poll/batch)        ┌───────────────────────────┐
  │  EXISTING PLATFORM        │  ── Post API  ───────────────▶  │  SMART LAYER               │
  │  (upstream, source of     │  ── Comment API ─────────────▶  │  ingestion → analysis →    │
  │   truth: scraping + DB)   │                                 │  OUR OWN DATABASE          │
  └──────────────────────────┘                                 │  (Postgres/ClickHouse/     │
                                                                │   Qdrant/Redis/object)     │
              ▲                                                 └──────────────┬─────────────┘
              │ NO write-back into the upstream DB                             │ serve
              └─────────────────  (read-only consumer)                ┌────────▼────────┐
                                                                      │ results / reports│
                                                                      │ / dashboard      │
                                                                      └─────────────────┘
```

- **Pull, not push.** The smart layer **reads** from the Post/Comment APIs
  (poll for new/updated records, or pull on demand for a campaign/time window).
  Upstream `status: NOT_ANALYZED` is a useful selector for "not yet pulled/analyzed
  by us," but it is **upstream state we read, not write**.
- **Separate database.** Everything we ingest and everything we produce lives in
  **our own datastore** (see [architecture.md](architecture.md) §2/§8). We do
  **not** write results back into the upstream platform's database. The upstream
  remains the scraping source of truth; we are an independent analytical copy.
- **Idempotent by upstream id.** We store records keyed by the upstream CUID, so
  re-pulling the same post/comment upserts rather than duplicates.

---

## 1. Post API — real schema

A `GET` against the Post API returns a **JSON array of post objects**. Every field
below is present on every record in the real sample
([social_posts.json](social_posts.json), 50 records). Types and example values are
taken from that sample.

| Field                   | Type                | Example / range                                  | Meaning & how we use it                                                                                       |
| ----------------------- | ------------------- | ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| `id`                    | string (CUID)       | `cmq7orcjr2w78x80tufd0nza4`                      | **Primary join key.** Maps to our `post_id`. Stable, unique, idempotency key.                                 |
| `campaignId`            | string (CUID)       | `cmpe1djj504zc4otgw94idx0v`                      | Monitoring campaign the post belongs to → our `campaign_id`; a primary filter/group dimension.                |
| `platformPostId`        | string              | `2064585098553434394`, `79077`                   | The native post id on the source platform → `platform_post_id`.                                               |
| `url`                   | string              | `https://x.com/.../status/...`                   | Canonical URL. **Platform is derived from the host** (see §3). Source handle/channel often in the path.       |
| `caption`               | string \| null      | Bangla / English / Banglish text, or `null`      | The post body → our analyzable **post text**. **`null` on ~half the sample (25/50)** — typically photo-only posts; for those, analyzable text comes from `photoOcrTexts`. Always handle a missing caption. |
| `photoUrls`             | string[]            | FB: `https://scontent.../...jpg`; TG: `posts/<campaignId>/<platformPostId>/x.jpg` | Image references — **format varies by platform**: Facebook returns full CDN URLs (`*.fbcdn.net`, with query strings), Telegram returns relative object-storage keys. Empty for text-only posts. |
| `photoOcrTexts`         | string[]            | OCR strings (Bangla/English)                     | **OCR already run upstream.** We append it to the analyzable text so image text feeds sentiment/NER/topics.   |
| `videoUrl`              | string \| null      | `null` in sample                                 | Video asset URL when present.                                                                                 |
| `postedAt`              | datetime (no tz)    | `2026-06-10T05:47:00`                            | When the post was published → our `created_at`. Treat as platform-local; normalize on ingest.                 |
| `scrapedAt`             | datetime (no tz)    | `2026-06-10T06:26:40.838`                        | When upstream scraped it → our `scraped_at` (ingest provenance).                                              |
| `commentCount`          | int                 | 0 – 1613                                         | Engagement count. **Also tells us how many comments to pull** from the Comment API (§2).                      |
| `shareCount`            | int                 | 0 – 1360                                         | Engagement.                                                                                                   |
| `totalReactions`        | int                 | 0 – 27720                                        | Engagement (reactions/likes).                                                                                 |
| `saves`                 | int                 | 0 in sample                                      | Engagement (often 0 — platform-dependent).                                                                    |
| `impressions`           | int                 | 0 in sample                                      | Engagement (often 0 — not exposed by all platforms).                                                          |
| `reach`                 | int                 | 0 in sample                                      | Engagement (often 0).                                                                                         |
| `sentiment`             | float               | −0.42 … 0.88                                     | **Upstream's coarse sentiment.** We keep it as **`baseline_sentiment`** and **recompute our own richer one** (see §4). |
| `viralPotential`        | float               | 0.1 … 0.89                                       | **Upstream's coarse virality score** → **`baseline_viral_potential`** (baseline/fallback).                    |
| `autoScrape`            | bool                | `false`                                          | Upstream scheduling flag. Provenance only.                                                                    |
| `aiAnalysisStatus`      | enum                | `COMPLETED`                                      | Upstream's own light analysis status (i.e. it already produced `sentiment`/`viralPotential`). Read-only.      |
| `status`                | enum                | `NOT_ANALYZED`                                   | Upstream lifecycle flag. We use it as a **pull selector**; we track *our* analysis status in our own DB.      |
| `viralMonitoringStatus` | enum                | `BASELINE_CREATED`, `FILTERED_UNSUPPORTED`       | Upstream viral-tracking state. `FILTERED_UNSUPPORTED` ⇒ upstream skipped it; we can deprioritize.             |
| `postType`              | enum                | `TEXT`, `PHOTO`, `PHOTO_TEXT`, `UNKNOWN`         | **Media type** → our **`media_type`** (NOT our semantic `post_type`; see naming note below).                  |
| `search_tsv`            | string (tsvector)   | `'11':42A '3.41':38A …`                          | Upstream Postgres full-text vector. We build our **own** index; informational only.                           |
| `caption_embedding`     | null                | `null` (always, in sample)                       | Upstream leaves embeddings empty. **We compute embeddings ourselves** into Qdrant (see [models.md](models.md)). |
| `createdAt` / `updatedAt` | datetime          | `2026-06-10T06:26:40.838`                        | Upstream row timestamps (ingest/update of the scrape record). Provenance.                                     |
| `badPostExpiresAt`, `isAutoScheduled`, `isViral`, `isViralCandidate`, `lastSnapshotAt`, `nextSnapshotAt`, `scheduledIntervalMinutes`, `hasVideo` | mixed | mostly `null`/`false` in sample | Upstream operational/monitoring flags. Carried as provenance; not required by analysis.                       |

> **Naming clash — read this.** Upstream **`postType`** describes the *media*
> (TEXT / PHOTO / PHOTO_TEXT / UNKNOWN). Our output **`post_type`** describes the
> *semantic kind* (complaint / promotion / news / opinion / …). They are different
> axes. We map upstream `postType` → **`media_type`** and reserve `post_type` for
> the classifier output. Do not conflate them.

### What the real sample tells us (50 records)

- **Platforms by URL host:** `www.facebook.com` ×40, `t.me` (Telegram) ×9,
  `x.com` ×1 — **not Facebook/Instagram only**. Treat platform as open-ended (§3).
- **`postType`:** PHOTO ×23, PHOTO_TEXT ×17, TEXT ×8, UNKNOWN ×2. ~80% carry an
  image → **OCR text matters**; 25/50 already have `photoOcrTexts`.
- **`caption` is `null` on 25/50 records** (mostly the PHOTO posts) — analyzable
  text for those comes from `photoOcrTexts`. Only **2 records** (both `UNKNOWN`,
  the `FILTERED_UNSUPPORTED` ones) have **no caption, no photo, and no OCR** — i.e.
  no analyzable content; deprioritize/skip them.
- **No author field.** Upstream does not return a post author; where useful we
  derive a **source handle/channel** from the URL path (e.g. `t.me/basherkella`).
- **Upstream pre-computes `sentiment` + `viralPotential`** and leaves
  `caption_embedding` null and `status: NOT_ANALYZED` — exactly the seam our
  service fills (recompute richer signal, add embeddings, summary, topics,
  entities, comment analysis).
- **Engagement is partial:** `impressions`/`reach`/`saves` are 0 in this sample;
  `totalReactions`/`commentCount`/`shareCount` carry the signal.

---

## 2. Comment API — expected schema (to be finalized)

> The Comment API payload has **not been provided yet**. This is the **working
> contract** we build against; confirm and adjust field names when the real
> response arrives. The principle is fixed: **comments are a separate API,
> fetched per post and joined by the post `id`.**

A `GET` for a post's comments is expected to return a **JSON array of comment
objects**, each with its own stable CUID and a reference back to the parent post:

| Field (expected)         | Type             | Meaning                                                                                  |
| ------------------------ | ---------------- | ---------------------------------------------------------------------------------------- |
| `id`                     | string (CUID)    | Comment's unique id → our `comment_id`.                                                   |
| `postId`                 | string (CUID)    | **Join key → the post's `id`.** This is how a comment attaches to its thread.            |
| `parentId`               | string \| null   | The parent comment id for replies; `null` for top-level → our `parent_id` (thread shape).|
| `caption` / `text`       | string           | Comment body → analyzable comment text (Bangla / English / Banglish).                    |
| `authorName`             | string \| null   | Commenter handle/name if exposed.                                                        |
| `totalReactions` / likes | int              | Comment engagement.                                                                      |
| `postedAt`               | datetime         | Comment time.                                                                            |
| `sentiment`              | float \| null    | If upstream provides a coarse comment sentiment, kept as `baseline_sentiment` (per §4).  |
| (media / OCR fields)     | as on posts      | If comments carry images, the same media/OCR handling as §1 applies.                     |

**Assembling the thread:** for each post we pull, we fetch its comments by
`postId == post.id`, order them, preserve `parentId` so reply structure is
recoverable, and analyze the **post + its full comment thread** as one unit (the
output schema's `comment_analysis` block — [architecture.md](architecture.md) §6).
`commentCount` from the post tells us how many to expect and lets us page the
Comment API.

---

## 3. Platform detection (multi-platform)

Platform is **derived from the `url` host**, not assumed. The service is
platform-agnostic — any source the upstream scrapes is supported.

| URL host pattern        | `platform`  |
| ----------------------- | ----------- |
| `facebook.com`          | `facebook`  |
| `t.me`                  | `telegram`  |
| `x.com` / `twitter.com` | `x`         |
| `instagram.com`         | `instagram` |
| (other)                 | `other` (host recorded verbatim) |

The source handle/channel is taken from the URL path where available
(`t.me/basherkella/79077` → channel `basherkella`).

---

## 4. Multimodal analysis — sentiment (text + image), summary, then comments

This is the **first target**, in priority order. A post is **multimodal**: the
`caption` text *and*, when present, one or more **images** (`photoUrls`, with
upstream `photoOcrTexts`). We analyze both modalities, fuse them, then summarize,
then bring in comments.

### Sentiment policy — recompute, keep upstream as baseline

The upstream stores a single coarse `sentiment` float (and `viralPotential`). We:

1. **Keep the upstream values** as `baseline_sentiment` and
   `baseline_viral_potential` (provenance + cheap fallback if our pipeline is
   degraded or skips an item).
2. **Recompute our own, richer sentiment** — per modality and fused (below). These
   are the authoritative fields downstream consumes.
3. **Never overwrite upstream.** `baseline_*` and our computed fields coexist in
   our own DB, always comparable (QA / calibration).

### Pipeline order (first target)

1. **Post text sentiment.** Normalize the `caption` (+ language/Banglish
   detection) and run **text sentiment** (+ emotion) → `text_sentiment`. Handle a
   **`null` caption** (25/50 records, §1): if there's no caption, text sentiment is
   skipped and the image carries the post.
2. **Image sentiment (when an image is present).** Run a **visual** model on the
   image itself (not just its OCR) → `image_sentiment`, per image and aggregated.
   This is a vision model ([models.md](models.md) §1), language-agnostic, run on
   every image post. OCR text (`photoOcrTexts`, already produced upstream) is also
   folded into the **text** path. `image_sentiment` is `null` for text-only posts.
3. **Fuse → post sentiment.** Combine `text_sentiment` and `image_sentiment` into
   the post-level `overall_sentiment` + `sentiment_score` (late fusion: caption
   present → text-weighted; **`null`-caption photo posts → image + OCR-weighted**).
   The fusion weighting is recorded so it stays auditable/tunable.
4. **Post summary (grounded on caption + image/OCR).** Generate `post_summary` in
   the post's own language from **all** post-level signal — caption text, OCR text,
   and the **image** (via a vision-language model or an image description), so a
   photo-only post still gets a meaningful summary. `post_summary_grounding` records
   which inputs informed it (`caption` / `ocr` / `image`).
5. **Comments.** Once a post's comments are pulled (§2), run the **same text
   sentiment** over each comment, aggregate into
   `comment_analysis.sentiment_breakdown` + themes, and fold the thread into the
   final summary/insight. Until the Comment API is wired, threads are post-only and
   `comment_analysis.analyzed = 0`.

Steps 1–4 run on the **post alone** and ship **now** (the data we have); step 5
follows when the Comment API lands — see [plan.md](plan.md). Cheap models do the
sentiment (steps 1–2) on every post; the LLM/VLM is selective for the summary
(step 4), per the hybrid routing in [architecture.md](architecture.md) §5.

---

## 5. Upstream field → smart-layer field mapping

How §1 inputs land in the canonical output ([architecture.md](architecture.md) §6):

| Upstream (post API)                    | Smart-layer output                                  |
| -------------------------------------- | --------------------------------------------------- |
| `id`                                   | `post_id` (and join key)                            |
| `campaignId`                           | `campaign_id`                                       |
| `platformPostId`                       | `platform_post_id`                                  |
| `url` (host)                           | `url`, `platform` (derived, §3)                     |
| `caption` (+ `photoOcrTexts`)          | analyzable **text** → `language`, `text_sentiment`, `topics`, `entities`, `keywords`, `emotion`, and (with the image) `post_summary` |
| `photoUrls` (the image itself)         | **visual** model input → `image_sentiment` + `image_analysis` (description/OCR), and a grounding input to `post_summary` |
| `photoOcrTexts` (upstream OCR)         | folded into the **text** path (above) and `image_analysis.ocr_text`              |
| fused `text_sentiment` + `image_sentiment` | `overall_sentiment` / `sentiment_score` (post-level)                         |
| `videoUrl` / `postType` / `hasVideo`   | `media` block + `media_type`                        |
| `postedAt`                             | `created_at`                                        |
| `scrapedAt`                            | `scraped_at`                                        |
| `commentCount`/`shareCount`/`totalReactions`/`saves`/`impressions`/`reach` | `engagement` |
| `sentiment` (upstream)                 | `baseline_sentiment` (we add our own `overall_sentiment`/`sentiment_score`) |
| `viralPotential` (upstream)            | `baseline_viral_potential`                          |
| `status`/`aiAnalysisStatus`/`viralMonitoringStatus` | `upstream_status` (provenance; our own analysis status is separate) |
| Comment API records (§2)               | `comment_analysis` block                            |

Fields with **no upstream source** (`author`) become optional/derived; fields the
upstream leaves empty (`caption_embedding`) are **computed by us**.
