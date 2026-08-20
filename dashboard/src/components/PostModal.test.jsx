/**
 * The comment-error branch of the post detail modal.
 *
 * `PostModal` renders `<AlertCircle>` when the comments request fails — but
 * `AlertCircle` was never in its import list. So the one path that exists to
 * *show* the operator an error was itself a `ReferenceError`: open a post whose
 * comment page 404s or times out, and the modal blew up instead of saying so.
 * Nothing caught it because the build does not resolve JSX identifiers and no
 * test opened the modal on a failing request.
 */
import React from 'react';
import { render, screen, waitFor, act } from '@testing-library/react';
import { vi } from 'vitest';

import PostModal from './PostModal.jsx';

const apiCall = vi.fn();
vi.mock('../utils/api', () => ({ apiCall: (...a) => apiCall(...a) }));

// jsdom has no canvas context, and chart.js additionally throws when it tries to
// resize a detached canvas on rerender. The charts are not what these tests are
// about, so stub the renderer rather than the browser.
vi.mock('react-chartjs-2', () => ({
  Bar: () => <div data-testid="chart-bar" />,
  Doughnut: () => <div data-testid="chart-doughnut" />,
}));
vi.mock('chart.js', () => ({
  Chart: { register: () => {} },
  ArcElement: {}, Tooltip: {}, Legend: {}, CategoryScale: {},
  LinearScale: {}, BarElement: {}, Title: {},
}));

const POST = {
  post_id: 'cmp58e24s04pgwglq7g9u9jz0',
  campaign_id: 'c1',
  platform: 'facebook',
  language: 'bn',
  overall_sentiment: 'negative',
  sentiment_score: -0.8,
  post_summary: 'একটি ক্ষুব্ধ মতামত পোস্ট',
  engagement: { reactions: 100, comment_count: 10, share_count: 2 },
  comment_analysis: { analyzed: 0, coverage_label: '0/10 stored' },
};

beforeEach(() => {
  apiCall.mockReset();
  // jsdom implements neither of these and PostModal's charts touch them.
  Element.prototype.scrollIntoView = vi.fn();
  global.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
});

describe('PostModal comment errors', () => {
  it('shows the error instead of crashing when the comments request fails', async () => {
    apiCall.mockRejectedValue(new Error('API error 502'));

    render(<PostModal post={POST} onClose={() => {}} />);

    // The message reaches the operator…
    expect(await screen.findByText(/API error 502/)).toBeInTheDocument();
    // …and the modal is still standing (this is what the missing import broke).
    expect(screen.getByText(POST.post_id)).toBeInTheDocument();
  });

  it('renders nothing when there is no post', () => {
    const { container } = render(<PostModal post={null} onClose={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('requests the comment page for the post it was given', async () => {
    apiCall.mockResolvedValue({ comments: [], total: 0 });

    render(<PostModal post={POST} onClose={() => {}} />);

    await waitFor(() =>
      expect(apiCall).toHaveBeenCalledWith(
        expect.stringContaining(`/v1/analysis/post/${encodeURIComponent(POST.post_id)}/comments`)
      )
    );
  });
});

// These pin the effect's identity rules before `loadComments` is stabilised for
// `exhaustive-deps`. Getting it wrong is not subtle: declare an unstable
// `loadComments` as a dependency and an open modal re-requests page 1 of the
// comments on *every* render — up to 2,000 rows per keystroke elsewhere on the
// page.
describe('PostModal comment loading', () => {
  const commentCalls = () =>
    apiCall.mock.calls.filter(([p]) => String(p).includes('/comments')).length;

  beforeEach(() => apiCall.mockResolvedValue({ comments: [], total: 0 }));

  it('loads page 1 exactly once when the modal opens', async () => {
    render(<PostModal post={POST} onClose={() => {}} />);
    await waitFor(() => expect(commentCalls()).toBe(1));
    expect(apiCall).toHaveBeenCalledWith(expect.stringContaining('offset=0'));
  });

  it('does not reload when the modal re-renders with the same post', async () => {
    const { rerender } = render(<PostModal post={POST} onClose={() => {}} />);
    await waitFor(() => expect(commentCalls()).toBe(1));

    // A new object with the same id — what a parent refetch produces.
    await act(async () => {
      rerender(<PostModal post={{ ...POST }} onClose={() => {}} />);
    });

    expect(commentCalls()).toBe(1);
  });

  it('reloads when a different post is opened', async () => {
    const { rerender } = render(<PostModal post={POST} onClose={() => {}} />);
    await waitFor(() => expect(commentCalls()).toBe(1));

    await act(async () => {
      rerender(<PostModal post={{ ...POST, post_id: 'cmother00000000000000009' }} onClose={() => {}} />);
    });

    await waitFor(() => expect(commentCalls()).toBe(2));
    expect(apiCall).toHaveBeenLastCalledWith(expect.stringContaining('cmother00000000000000009'));
  });
});
