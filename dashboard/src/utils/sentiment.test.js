import { describe, it, expect } from 'vitest';
import {
  SENTIMENT_COLORS,
  UNKNOWN_SENTIMENT_COLOR,
  orderedBreakdown,
  sentimentColors,
} from './sentiment';

const RED = SENTIMENT_COLORS.negative;
const GREEN = SENTIMENT_COLORS.positive;
const GREY = SENTIMENT_COLORS.neutral;

describe('sentimentColors', () => {
  it('paints red on negative whatever position it holds', () => {
    // The corpus doughnut receives the API's order: positive, negative,
    // neutral, mixed. With a POSITIONAL palette the red landed on `neutral`
    // and negative came out grey — the chart claimed hostility that was not
    // in the data.
    const apiOrder = ['positive', 'negative', 'neutral', 'mixed'];
    const colors = sentimentColors(apiOrder);

    expect(colors[apiOrder.indexOf('negative')]).toBe(RED);
    expect(colors[apiOrder.indexOf('positive')]).toBe(GREEN);
    expect(colors[apiOrder.indexOf('neutral')]).toBe(GREY);
    expect(colors[apiOrder.indexOf('neutral')]).not.toBe(RED);
  });

  it('is order-independent', () => {
    const a = ['negative', 'positive'];
    const b = ['positive', 'negative'];
    expect(sentimentColors(a)).toEqual([RED, GREEN]);
    expect(sentimentColors(b)).toEqual([GREEN, RED]);
  });

  it('matches labels regardless of case or padding', () => {
    expect(sentimentColors(['Negative', ' NEUTRAL '])).toEqual([RED, GREY]);
  });

  it('gives uncertain its own colour, not the neutral grey', () => {
    // An abstention is not a neutral verdict — see libs/ensemble.py.
    expect(SENTIMENT_COLORS.uncertain).not.toBe(GREY);
    expect(sentimentColors(['uncertain'])).toEqual([SENTIMENT_COLORS.uncertain]);
  });

  it('falls back visibly for an unknown label rather than reusing a meaning', () => {
    expect(sentimentColors(['banana'])).toEqual([UNKNOWN_SENTIMENT_COLOR]);
  });
});

describe('orderedBreakdown', () => {
  it('always keeps the three core buckets, even at zero', () => {
    const { labels, values } = orderedBreakdown({ positive: 3 });
    expect(labels).toEqual(['positive', 'negative', 'neutral']);
    expect(values).toEqual([3, 0, 0]);
  });

  it('shows uncertain only once something abstained', () => {
    expect(orderedBreakdown({ positive: 1, uncertain: 0 }).labels).not.toContain('uncertain');
    expect(orderedBreakdown({ positive: 1, uncertain: 2 }).labels).toContain('uncertain');
  });

  it('keeps labels and values aligned', () => {
    const { labels, values } = orderedBreakdown({
      positive: 5, negative: 2, neutral: 1, uncertain: 4,
    });
    const byLabel = Object.fromEntries(labels.map((l, i) => [l, values[i]]));
    expect(byLabel).toEqual({ positive: 5, negative: 2, neutral: 1, uncertain: 4 });
  });

  it('does not drop a bucket the pipeline invents later', () => {
    expect(orderedBreakdown({ positive: 1, surprise: 7 }).labels).toContain('surprise');
  });

  it('handles an empty breakdown without throwing', () => {
    expect(orderedBreakdown({}).values).toEqual([0, 0, 0]);
    expect(orderedBreakdown().values).toEqual([0, 0, 0]);
  });
});
