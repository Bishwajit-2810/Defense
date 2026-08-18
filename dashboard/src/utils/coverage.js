// Comment-scrape coverage, in one place because two views show it and they must
// not drift: the Posts table column and the post-detail modal.
//
// The distinction that matters, and that the raw numbers hide:
//   `stored_comments` — comment rows the scraper actually delivered.
//   `comment_count`   — how many comments the platform claims the post has.
// Most posts in the corpus store ~87 rows against reported totals of 138–5,644,
// so the shortfall is comments that were NEVER FETCHED, not comments the
// pipeline declined to analyse. Showing only one of the two numbers reads as a
// coverage claim neither can support.

// Three different numbers, and conflating any two of them overstates coverage:
//   analysed — comments a Stage-2 model actually read (`analysed_by_models`).
//              Smaller than `stored` whenever a top-N cap, the near-duplicate
//              filter or the min-words gate cut the set; null on rows written
//              before the ensemble recorded itself, and then simply not shown.
//   stored   — comment rows the scraper delivered.
//   total    — what the platform claims the post has.
export function commentScrape(post) {
  const eng = post?.engagement || {};
  const ca = post?.comment_analysis || {};
  const stored = Number(eng.stored_comments) || Number(ca.analyzed) || 0;
  const total = Number(eng.comment_count) || 0;
  const rawAnalysed = Number(ca.analysed_by_models);
  const analysed = Number.isFinite(rawAnalysed) ? rawAnalysed : null;
  if (!stored && !total) return null;
  // No reported total to compare against — say so rather than implying 100%.
  if (!total) return { analysed, stored, total, pct: null, state: 'unknown' };
  // Five posts in the corpus store MORE rows than the platform reports (87 vs 58).
  // An upstream inconsistency, surfaced instead of rendered as "150% scraped".
  if (stored > total) return { analysed, stored, total, pct: null, state: 'mismatch' };
  return { analysed, stored, total, pct: Math.round((stored / total) * 100), state: stored >= total ? 'full' : 'partial' };
}

export const SCRAPE_TAG = {
  full: {
    cls: 'bg-emerald-50 border-emerald-200 text-emerald-700 dark:bg-emerald-900/30 dark:border-emerald-800 dark:text-emerald-400',
    text: (c) => `✓ fully scraped · ${c.stored.toLocaleString()} of ${c.total.toLocaleString()}`,
    short: () => '✓ fully scraped',
    title: (c) => `Every comment the platform reports (${c.total.toLocaleString()}) was scraped and analysed.`,
  },
  partial: {
    cls: 'bg-amber-50 border-amber-200 text-amber-700 dark:bg-amber-900/30 dark:border-amber-800 dark:text-amber-400',
    text: (c) => `⚠ not fully scraped · ${c.stored.toLocaleString()} of ${c.total.toLocaleString()} (${c.pct}%)`,
    short: (c) => `⚠ not fully scraped · ${c.pct}%`,
    title: (c) => `The scrape stored ${c.stored.toLocaleString()} comment rows; the platform reports ${c.total.toLocaleString()}. `
      + `The other ${(c.total - c.stored).toLocaleString()} were never fetched, so no model has seen them — `
      + `this is a scrape-depth limit, not an analysis cap. Every stored comment was analysed.`,
  },
  mismatch: {
    cls: 'bg-orange-50 border-orange-200 text-orange-700 dark:bg-orange-900/30 dark:border-orange-800 dark:text-orange-400',
    text: (c) => `⚠ ${c.stored.toLocaleString()} scraped · platform reports only ${c.total.toLocaleString()}`,
    short: () => '⚠ upstream count mismatch',
    title: () => 'More comment rows were stored than the platform reports having. An upstream count inconsistency — '
      + 'coverage is clamped to 100% rather than shown above it.',
  },
  unknown: {
    cls: 'bg-slate-100 border-slate-200 text-slate-600 dark:bg-zinc-800 dark:border-zinc-700 dark:text-zinc-300',
    text: (c) => `${c.stored.toLocaleString()} scraped · platform total unknown`,
    short: () => 'platform total unknown',
    title: () => 'The upstream payload carried no commentCount, so the share of the thread that was scraped cannot be computed.',
  },
};

// Explains the triple in one string, so the table cell and the modal metric can
// never describe the same numbers differently.
export function scrapeTooltip(scrape, platform) {
  if (!scrape) return 'No comment data';
  const parts = [];
  if (scrape.analysed !== null) {
    parts.push(`${scrape.analysed.toLocaleString()} read by a Stage-2 model`);
  }
  parts.push(`${scrape.stored.toLocaleString()} comment rows scraped`);
  parts.push(scrape.total
    ? `${scrape.total.toLocaleString()} reported by ${platform || 'the platform'}`
    : 'platform total unknown');
  let out = parts.join(' · ');
  if (scrape.analysed !== null && scrape.analysed < scrape.stored) {
    out += `. The ${(scrape.stored - scrape.analysed).toLocaleString()} stored comments no model read are `
      + 'near-duplicates (they reuse their twin\'s verdict), textless (emoji/links only), or were cut by a cap '
      + 'or the min-words gate.';
  }
  return out;
}
