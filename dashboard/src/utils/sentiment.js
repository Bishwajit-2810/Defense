/**
 * One place that decides what colour a sentiment label is drawn in.
 *
 * Charts used to carry a POSITIONAL palette — `['#10b981', '#64748b', '#ef4444']`
 * lined up against whatever order the API happened to return labels in. The
 * corpus doughnut receives `{positive, negative, neutral, mixed}`, so negative
 * was drawn grey and neutral was drawn red: the chart said the corpus was
 * hostile when it wasn't. A colour that means something has to be keyed on the
 * label, never on its index.
 */

export const SENTIMENT_COLORS = {
  positive: '#10b981',   // emerald
  negative: '#ef4444',   // red
  neutral: '#64748b',    // slate
  mixed: '#f59e0b',      // amber — both sides present
  // An ABSTENTION: the labellers split and no verdict was claimed. Deliberately
  // not the neutral grey — "we could not label this" is not "we judged this
  // neutral" (see libs/ensemble.py).
  uncertain: '#fbbf24',
};

/** Fallback for a label the palette does not know — never silently reused. */
export const UNKNOWN_SENTIMENT_COLOR = '#cbd5e1';

/**
 * Colours aligned to `labels`, matched by name (case/whitespace-insensitive).
 * Pass the chart's own label array so the two can never drift apart.
 */
export function sentimentColors(labels = []) {
  return labels.map(
    (l) => SENTIMENT_COLORS[String(l).trim().toLowerCase()] ?? UNKNOWN_SENTIMENT_COLOR
  );
}

/** Display order when we control it: the two poles, then the non-verdicts. */
export const SENTIMENT_ORDER = ['positive', 'negative', 'neutral', 'uncertain', 'mixed'];

/**
 * Turn a `{label: count}` breakdown into ordered {labels, values} for a chart,
 * dropping buckets that are empty AND not one of the three core labels — so
 * `uncertain` appears only once something actually abstained, but positive /
 * negative / neutral always hold their place.
 */
export function orderedBreakdown(breakdown = {}) {
  const core = new Set(['positive', 'negative', 'neutral']);
  const known = SENTIMENT_ORDER.filter(
    (k) => core.has(k) || Number(breakdown[k]) > 0
  );
  const extra = Object.keys(breakdown).filter(
    (k) => !SENTIMENT_ORDER.includes(String(k).toLowerCase())
  );
  const labels = [...known, ...extra];
  return {
    labels,
    values: labels.map((k) => Number(breakdown[k]) || 0),
  };
}
