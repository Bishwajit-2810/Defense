/**
 * Search results have to be openable.
 *
 * Every search hit already carries the whole canonical result in `result` — the
 * same object the Posts tab hands to PostModal — but the page rendered the
 * server's 200-character `snippet` and dropped the rest, so the post you had
 * just found by id could not be read or opened.
 */
import React from 'react';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { vi } from 'vitest';

import Search from './Search.jsx';

const apiCall = vi.fn();

vi.mock('../utils/api.js', () => ({
  API_BASE: 'http://api.test',
  getAuthHeaders: () => ({}),
  apiCall: (...args) => apiCall(...args),
}));

// Stand-in for the real modal (which pulls in chart.js): it prints what it was
// handed, so these tests assert the wiring rather than the modal's internals.
vi.mock('../components/PostModal', () => ({
  default: ({ post }) => post ? (
    <div data-testid="post-modal">
      <span data-testid="modal-post-id">{post.post_id}</span>
      <span data-testid="modal-summary">{post.post_summary}</span>
      <span data-testid="modal-campaign">{post.campaign_id}</span>
      <span data-testid="modal-analysed">{post.comment_analysis?.analyzed}</span>
    </div>
  ) : null,
}));

const POST_ID = 'cmoq9a2b40mjpl0k5xk4qi4g3';
// 353 characters in the real corpus; the API truncates `snippet` at 200.
const FULL_SUMMARY = 'তারেক রহমানের সামনে দুইটা পথ আছে; ' + 'ক'.repeat(300) + ' শেষ';

const HIT = {
  post_id: POST_ID,
  campaign_id: 'cmold6pt601ebfu22bfn6utvl',
  score: 1.0,
  snippet: FULL_SUMMARY.slice(0, 200),
  result: {
    post_id: POST_ID,
    post_summary: FULL_SUMMARY,
    overall_sentiment: 'negative',
    platform: 'facebook',
    language: 'bn',
    comment_analysis: { analyzed: 2857, coverage_label: '2857/6567 stored' },
  },
};

const doSearch = async (q = POST_ID) => {
  await act(async () => {
    fireEvent.change(screen.getByPlaceholderText(/Post id, platform id/i), { target: { value: q } });
  });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /^Search$/i })); });
};

beforeEach(() => {
  apiCall.mockReset();
  apiCall.mockResolvedValue({
    query: POST_ID, semantic: false, total: 1,
    match_type: 'exact_id', id_lookup_missed: false, results: [HIT],
  });
});

describe('Search result details', () => {
  it('renders the full summary, not the truncated snippet', async () => {
    render(<Search />);
    await doSearch();

    // The whole thing is on screen — the 200-char cut is the API's transport
    // limit, not what the operator is allowed to read.
    expect(await screen.findByText(FULL_SUMMARY)).toBeInTheDocument();
    expect(FULL_SUMMARY.length).toBeGreaterThan(HIT.snippet.length);
  });

  it('offers a way into the full post and passes the whole result to it', async () => {
    render(<Search />);
    await doSearch();

    expect(screen.queryByTestId('post-modal')).toBeNull();
    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: /View full post & analysis/i }));
    });

    await waitFor(() => expect(screen.getByTestId('post-modal')).toBeInTheDocument());
    expect(screen.getByTestId('modal-post-id')).toHaveTextContent(POST_ID);
    expect(screen.getByTestId('modal-summary')).toHaveTextContent(FULL_SUMMARY);
    // Campaign and the comment block travel too — the modal is not re-fetching
    // the analysis, only the comment pages.
    expect(screen.getByTestId('modal-campaign')).toHaveTextContent(HIT.campaign_id);
    expect(screen.getByTestId('modal-analysed')).toHaveTextContent('2857');
  });

  it('opens from clicking the card as well as the button', async () => {
    render(<Search />);
    await doSearch();

    const card = (await screen.findByText(POST_ID)).closest('div.border');
    await act(async () => { fireEvent.click(card); });

    expect(await screen.findByTestId('post-modal')).toBeInTheDocument();
  });

  it('does not open a detail view for a hit with no post id', async () => {
    apiCall.mockResolvedValue({
      query: 'x', semantic: false, total: 1, match_type: 'keyword',
      results: [{ post_id: '', campaign_id: 'c1', snippet: 'orphan row' }],
    });
    render(<Search />);
    await doSearch('something');

    expect(await screen.findByText('orphan row')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /View full post/i })).toBeNull();
  });

  it('still labels an exact identifier match as such', async () => {
    render(<Search />);
    await doSearch();

    expect(await screen.findByText(/Exact identifier match/i)).toBeInTheDocument();
    // …and does not print a similarity for it.
    expect(screen.queryByText(/Similarity Score/i)).toBeNull();
  });
});
