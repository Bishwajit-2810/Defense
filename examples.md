# Worked Examples — Input Thread → Output JSON

Two real scraped threads run through the smart layer, showing the exact input the
scraper sends and the structured JSON the service returns. These match the
canonical schema in [architecture.md](architecture.md) §6 and the API contracts in
[api_design.md](api_design.md).

The unit of analysis is always a **post + its comment thread**. `post_summary` is
written **in the post's own language** (Bangla post → Bangla summary; English post
→ English summary). Banglish (romanized Bangla, e.g. "Green garden e vat 25 taka")
is detected and folded into the dominant language for the summary.

---

## Example 1 — Bangla complaint thread (campus food prices)

A Bangla post complaining about Green Garden / campus-transport food prices, with
a thread of mostly Banglish comments agreeing and calling for a boycott.

### Input (abridged)

```json
{
  "post_id": "fb_greengarden_001",
  "platform": "facebook",
  "author": "TalentedOstrich6332",
  "text": "সাধারণত দেখা যায় যে গ্রিণ গার্ডেন এ খাবারের দাম অনেক বেশি... আজকে ট্রান্সপোর্টে একটা সিংগারা ২০ টাকা চাইল। এটা তো জুলুম। এই বিষয়ে কথা বলা প্রয়োজন মনে হয়েছে।",
  "created_at": "2026-06-01T09:00:00Z",
  "engagement": { "reactions": 48, "comment_count": 12 },
  "comments": [
    { "comment_id": "c1", "parent_id": null, "author": "GenuineJackfruit1970",
      "text": "Green garden e sudhu polao 100 taka baire 30-40 takai e paua jay. Quality same. Mone hoy gold dhuya pani diye ranna kore" },
    { "comment_id": "c2", "parent_id": null, "author": "Anonymous participant 558",
      "text": "নুনুর গার্ডেনে এক প্লেট ভাতের দাম ২০ টাকা 🤣 বাইরে ৫ টাকা একই চাল" },
    { "comment_id": "c3", "parent_id": null, "author": "AuthenticDragon4378",
      "text": "Eder boycott koray uttom karon era kokokhnoi apnake value korbe na" },
    { "comment_id": "c4", "parent_id": null, "author": "Munam Mira",
      "text": "Even 50 taka lekha Ice-cream gula naki 150! Ajob kahini." },
    { "comment_id": "c5", "parent_id": null, "author": "StunningDolphin9938",
      "text": "ট্রান্সপোর্টের ওই ভাইয়ার দোকান ভাড়াও নাই... ১০০% প্রফিট করছে উনি।" }
  ]
}
```

### Output JSON

```json
{
  "post_id": "fb_greengarden_001",
  "platform": "facebook",
  "author": "TalentedOstrich6332",
  "language": "bn",
  "language_mix": ["bn", "banglish", "en"],
  "language_confidence": 0.97,
  "post_type": "complaint",
  "post_summary": "পোস্টদাতা অভিযোগ করছেন গ্রিন গার্ডেন ও ক্যাম্পাস ট্রান্সপোর্টে খাবারের দাম বাইরের তুলনায় অনেক বেশি (একটি সিঙ্গারা ২০ টাকা), যা তিনি অন্যায্য মনে করছেন এবং কেনা বন্ধ করার ডাক দিয়েছেন। মন্তব্যকারীরা একমত — একই মানের খাবার বাইরে অনেক সস্তা — এবং অনেকে বয়কটের প্রস্তাব দিয়েছেন।",
  "post_summary_lang": "bn",
  "overall_sentiment": "negative",
  "sentiment_score": -0.64,
  "emotion": "anger",
  "intents": ["complaint", "call_to_action"],
  "topics": ["food pricing", "campus transport", "boycott"],
  "entities": [
    { "type": "organization", "value": "Green Garden", "confidence": 0.94 },
    { "type": "product", "value": "singara", "confidence": 0.82 },
    { "type": "product", "value": "polao", "confidence": 0.79 }
  ],
  "brand_mentions": [
    { "name": "Green Garden", "sentiment": "negative", "mentions": 9 }
  ],
  "keywords": ["দাম", "সিঙ্গারা", "polao", "boycott", "transport"],
  "toxicity_score": 0.08,
  "hate_speech_score": 0.01,
  "engagement": { "reactions": 48, "comment_count": 12 },
  "comment_analysis": {
    "analyzed": 12,
    "sentiment_breakdown": { "positive": 1, "negative": 9, "neutral": 2 },
    "themes": [
      "prices far above outside market",
      "same quality cheaper elsewhere",
      "calls to boycott",
      "transport vendor over-charging"
    ],
    "representative_comments": [
      { "author": "GenuineJackfruit1970", "lang": "banglish", "sentiment": "negative",
        "text": "Green garden e sudhu polao 100 taka baire 30-40 takai e paua jay" },
      { "author": "AuthenticDragon4378", "lang": "banglish", "sentiment": "negative",
        "text": "Eder boycott koray uttom" }
    ]
  },
  "post_summary_source": "llm",
  "confidence": 0.92,
  "processing": { "unit": "post+thread", "stage1_ms": 61, "llm_used": true, "llm_model": "LLM-A" },
  "created_at": "2026-06-01T09:00:00Z"
}
```

**What did the work:** Stage-1 NLP detected language/Banglish, per-comment
sentiment, entities (Green Garden), and toxicity cheaply. The router sent the
thread to **LLM-A** (the fast per-post model) only for the Bangla `post_summary`
and the comment `themes` (generative fields) — everything else is small-model
output.

---

## Example 2 — English brand-page thread (Fabrilife jerseys)

An English promotional post from a clothing brand, with a large thread of
product-availability and price inquiries (many Banglish), plus the brand's own
replies. The summary is requested in English.

### Input (abridged — the real thread has 100+ comments)

```json
{
  "post_id": "fb_fabrilife_001",
  "platform": "facebook",
  "author": "Fabrilife",
  "text": "Unlock Your Confidence! Fabrilife, Bangladesh's fastest growing clothing brand, brings you premium quality comfort. Shop Now and discover your new favorite piece of confidence!",
  "created_at": "2026-05-28T08:00:00Z",
  "engagement": { "reactions": 76000, "comment_count": 2000 },
  "comments": [
    { "comment_id": "c1", "parent_id": null, "author": "Kamrul Islam",
      "text": "ইরান, তুরস্কের জার্সি আনেন!" },
    { "comment_id": "c2", "parent_id": null, "author": "Sayeed Islam",
      "text": "আর্জেন্টিনা জার্সি নিতে চাচ্ছি।" },
    { "comment_id": "c3", "parent_id": "c2", "author": "Fabrilife",
      "text": "The offer price of Argentina 2026 World Cup Home Jersey is 1290 taka..." },
    { "comment_id": "c4", "parent_id": null, "author": "Monir Zaman",
      "text": "৪ বছরের বাচ্চাদের jersey হবে।" },
    { "comment_id": "c5", "parent_id": "c4", "author": "Fabrilife",
      "text": "We are sorry, Kids Jersey is not available." },
    { "comment_id": "c6", "parent_id": null, "author": "Mohammad Zahirul Haque",
      "text": "Eta copy naki original?" },
    { "comment_id": "c7", "parent_id": "c6", "author": "Fabrilife",
      "text": "All of our jerseys are imported from Thailand and are high-quality 1:1 replicas." }
  ]
}
```

### Output JSON

```json
{
  "post_id": "fb_fabrilife_001",
  "platform": "facebook",
  "author": "Fabrilife",
  "language": "en",
  "language_mix": ["en", "bn", "banglish"],
  "language_confidence": 0.96,
  "post_type": "promotion",
  "post_summary": "A promotional post from Fabrilife (a Bangladeshi clothing brand) marketing premium, comfortable clothing. The comment thread is dominated by customer inquiries about World Cup football jerseys — availability of specific national teams (Argentina, Portugal, Germany, Brazil, England), prices (~1270–1290 taka), kids' sizes, outlet locations, and whether the jerseys are original. The brand actively replies with prices, order links, and outlet addresses; kids' jerseys and several teams are out of stock.",
  "post_summary_lang": "en",
  "overall_sentiment": "positive",
  "sentiment_score": 0.34,
  "emotion": "interest",
  "intents": ["promotion", "product_inquiry", "price_inquiry", "availability_inquiry"],
  "topics": ["football jerseys", "world cup 2026", "pricing", "product availability", "outlet locations"],
  "entities": [
    { "type": "organization", "value": "Fabrilife", "confidence": 0.98 },
    { "type": "product", "value": "World Cup jersey", "confidence": 0.9 },
    { "type": "location", "value": "Argentina", "confidence": 0.7 },
    { "type": "location", "value": "Portugal", "confidence": 0.7 }
  ],
  "brand_mentions": [
    { "name": "Fabrilife", "sentiment": "positive", "mentions": 60 }
  ],
  "keywords": ["jersey", "argentina", "portugal", "price", "available", "outlet"],
  "toxicity_score": 0.01,
  "hate_speech_score": 0.0,
  "engagement": { "reactions": 76000, "comment_count": 2000 },
  "comment_analysis": {
    "analyzed": 2000,
    "sentiment_breakdown": { "positive": 420, "negative": 110, "neutral": 1470 },
    "themes": [
      "which national-team jerseys are available",
      "price requests (mostly answered: ~1290 taka)",
      "kids' jersey availability (out of stock)",
      "original vs replica questions",
      "outlet / showroom locations across cities"
    ],
    "top_intents": [
      { "intent": "price_inquiry", "count": 540 },
      { "intent": "availability_inquiry", "count": 430 },
      { "intent": "location_inquiry", "count": 180 }
    ]
  },
  "post_summary_source": "llm",
  "confidence": 0.9,
  "processing": { "unit": "post+thread", "stage1_ms": 240, "llm_used": true, "llm_model": "LLM-B" },
  "created_at": "2026-05-28T08:00:00Z"
}
```

**What did the work:** with 2,000 comments, the router does **not** send every
comment to the LLM. Stage-1 NLP classifies each comment's language, sentiment, and
intent cheaply; comments are **clustered by embedding**, and only cluster
representatives + the post go to **LLM-B** for the English `post_summary` and
`themes`. This is the cost lever that keeps a 2,000-comment thread to a single
cluster-level LLM call instead of thousands — see [architecture.md](architecture.md)
§5 and [models.md](models.md).

---

## How these map to the owner's request

The owner asked for `{ post_summary, sentiment_analysis, "and something like
that" }`. The schema delivers:

- **`post_summary`** — in the original language (`post_summary_lang` records it).
- **`sentiment_analysis`** — split into post-level (`overall_sentiment` +
  `sentiment_score`) and thread-level
  (`comment_analysis.sentiment_breakdown` = positive / negative / neutral counts).
- **"something like that"** — `post_type`, `intents`, `topics`, `entities`,
  `brand_mentions`, `comment_analysis.themes`/`top_intents`, toxicity, and
  engagement, so downstream projects get rich structured signal, not just two
  fields.
