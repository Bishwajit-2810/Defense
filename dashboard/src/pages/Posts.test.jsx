/**
 * Post search on the Posts page.
 *
 * The page had no post search at all — only a "Filter: campaign id" box that
 * changed state and nothing else, so it did nothing until you also pressed
 * Refresh. These tests cover the search that replaced it, and in particular the
 * distinction the old empty state got wrong: "not on this page", "no such post"
 * and "nothing uploaded yet" are three different answers.
 */
import React from 'react';
import { render, screen, waitFor, within, fireEvent, act } from '@testing-library/react';
import { vi } from 'vitest';

import Posts from './Posts.jsx';
import { getSnapshot as getTraceSnapshot, resetTraceSession } from '../utils/traceSession.js';

const apiCall = vi.fn();

vi.mock('../utils/api.js', () => ({
  API_BASE: 'http://api.test',
  getAuthHeaders: () => ({}),
  apiCall: (...args) => apiCall(...args),
}));

// PostModal pulls in chart.js. Stand in for it, but print what it was handed —
// a post found by the server fallback has to be openable too, and that means the
// merged row must carry the analysis, not just the id.
vi.mock('../components/PostModal', () => ({
  default: ({ post }) => post ? (
    <div data-testid="post-modal">
      <span data-testid="modal-post-id">{post.post_id}</span>
      <span data-testid="modal-summary">{post.post_summary}</span>
    </div>
  ) : null,
}));

const post = (over = {}) => ({
  post_id: 'cmp58e24s04pgwglq7g9u9jz0',
  campaign_id: 'cmold8r5301u8fu22m7flh3pc',
  platform: 'facebook',
  language: 'bn',
  overall_sentiment: 'negative',
  post_summary: 'একটি ক্ষুব্ধ মতামত পোস্ট',
  platform_post_id: '122162468462710684',
  ...over,
});

const LOADED = post();
const OTHER = post({ post_id: 'cmoldbhw0026ffu22lt3nzj33', post_summary: 'অন্য একটি পোস্ট', platform_post_id: '999' });

const type = async (el, value) => {
  await act(async () => { fireEvent.change(el, { target: { value } }); });
};
const searchBox = () => screen.getByPlaceholderText(/Search posts/i);

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  apiCall.mockReset();
  apiCall.mockImplementation(async (path) => {
    if (path.startsWith('/v1/analysis/latest')) return { results: [LOADED, OTHER] };
    return { results: [] };
  });
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue() } });
  resetTraceSession();
});
afterEach(() => { vi.useRealTimers(); });

const flush = async (ms = 500) => {
  await act(async () => { vi.advanceTimersByTime(ms); await Promise.resolve(); });
};

describe('Posts search', () => {
  it('finds a loaded post by its full id without a request', async () => {
    render(<Posts />);
    await screen.findByText(/cmp58e24\.\.\./);

    await type(searchBox(), LOADED.post_id);

    expect(screen.getByText(/cmp58e24\.\.\./)).toBeInTheDocument();
    expect(screen.queryByText(/cmoldbhw\.\.\./)).toBeNull();
    // Tier 1 is local: a post already on screen must not cost a round trip.
    expect(apiCall).not.toHaveBeenCalledWith(expect.stringContaining('/v1/search'), undefined);
  });

  it('finds a loaded post by the 8-char prefix the table displays', async () => {
    render(<Posts />);
    await screen.findByText(/cmp58e24\.\.\./);

    await type(searchBox(), 'cmp58e24');

    expect(screen.getByText(/cmp58e24\.\.\./)).toBeInTheDocument();
    expect(screen.queryByText(/cmoldbhw\.\.\./)).toBeNull();
  });

  it('also searches platform id and caption text', async () => {
    render(<Posts />);
    await screen.findByText(/cmp58e24\.\.\./);

    await type(searchBox(), '122162468462710684');
    expect(screen.getByText(/cmp58e24\.\.\./)).toBeInTheDocument();

    await type(searchBox(), 'অন্য');
    expect(screen.getByText(/cmoldbhw\.\.\./)).toBeInTheDocument();
    expect(screen.queryByText(/cmp58e24\.\.\./)).toBeNull();
  });

  it('falls back to the server for a post that is not on this page', async () => {
    const older = post({ post_id: 'cmzold000000000000000000x', post_summary: 'পুরনো পোস্ট' });
    apiCall.mockImplementation(async (path) => {
      if (path.startsWith('/v1/analysis/latest')) return { results: [LOADED, OTHER] };
      if (path.startsWith('/v1/search')) {
        return { results: [{ post_id: older.post_id, campaign_id: older.campaign_id, result: older }],
                 match_type: 'exact_id', id_lookup_missed: false };
      }
      return {};
    });
    render(<Posts />);
    await screen.findByText(/cmp58e24\.\.\./);

    await type(searchBox(), older.post_id);
    await flush();

    await waitFor(() =>
      expect(apiCall).toHaveBeenCalledWith(expect.stringContaining(`/v1/search?q=${encodeURIComponent(older.post_id)}`))
    );
    expect(await screen.findByText(/cmzold00\.\.\./)).toBeInTheDocument();
    // …and it says the row came from the whole corpus, not this page.
    expect(screen.getByText(/Exact identifier match from the full corpus/i)).toBeInTheDocument();
  });

  it('opens the details of a post that came from the server fallback', async () => {
    const older = post({ post_id: 'cmzold000000000000000000x', post_summary: 'পুরনো পোস্ট বিশ্লেষণ' });
    apiCall.mockImplementation(async (path) => {
      if (path.startsWith('/v1/analysis/latest')) return { results: [LOADED, OTHER] };
      if (path.startsWith('/v1/search')) {
        return { results: [{ post_id: older.post_id, campaign_id: older.campaign_id, result: older }],
                 match_type: 'exact_id' };
      }
      return {};
    });
    render(<Posts />);
    await screen.findByText(/cmp58e24\.\.\./);

    await type(searchBox(), older.post_id);
    await flush();
    const row = (await screen.findByText(/cmzold00\.\.\./)).closest('tr');
    await act(async () => { fireEvent.click(row); });

    // The merged row is the full analysis, not a search stub — otherwise the
    // modal opens empty for exactly the post the operator went looking for.
    expect(await screen.findByTestId('post-modal')).toBeInTheDocument();
    expect(screen.getByTestId('modal-post-id')).toHaveTextContent(older.post_id);
    expect(screen.getByTestId('modal-summary')).toHaveTextContent('পুরনো পোস্ট বিশ্লেষণ');
  });

  it('says "no post has that id" rather than "upload posts to get started"', async () => {
    apiCall.mockImplementation(async (path) => {
      if (path.startsWith('/v1/analysis/latest')) return { results: [LOADED, OTHER] };
      if (path.startsWith('/v1/search')) return { results: [], match_type: 'keyword', id_lookup_missed: true };
      return {};
    });
    render(<Posts />);
    await screen.findByText(/cmp58e24\.\.\./);

    await type(searchBox(), 'cmzzzz9999zzzz9999zzzz999');
    await flush();

    expect(await screen.findByText(/No post has the id/i)).toBeInTheDocument();
    expect(screen.queryByText(/Upload posts to get started/i)).toBeNull();
  });

  it('distinguishes "nothing matches" from "no such post"', async () => {
    apiCall.mockImplementation(async (path) => {
      if (path.startsWith('/v1/analysis/latest')) return { results: [LOADED, OTHER] };
      if (path.startsWith('/v1/search')) return { results: [], match_type: 'keyword', id_lookup_missed: false };
      return {};
    });
    render(<Posts />);
    await screen.findByText(/cmp58e24\.\.\./);

    await type(searchBox(), 'zzzznotaword');
    await flush();

    expect(await screen.findByText(/Nothing matches/i)).toBeInTheDocument();
    expect(screen.queryByText(/No post has the id/i)).toBeNull();
  });

  it('refetches when the campaign filter changes, with no Refresh click', async () => {
    render(<Posts />);
    await screen.findByText(/cmp58e24\.\.\./);
    apiCall.mockClear();

    await type(screen.getByPlaceholderText(/Filter: campaign id/i), 'cmold8r5301u8fu22m7flh3pc');
    await flush();

    await waitFor(() =>
      expect(apiCall).toHaveBeenCalledWith(expect.stringContaining('campaign_id=cmold8r5301u8fu22m7flh3pc'))
    );
  });

  it('copies the full post id, which the 8-char cell cannot show', async () => {
    render(<Posts />);
    const row = (await screen.findByText(/cmp58e24\.\.\./)).closest('tr');

    await act(async () => { fireEvent.click(within(row).getByLabelText('Copy post id')); });

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(LOADED.post_id);
  });

  it('clearing the box restores every row', async () => {
    render(<Posts />);
    await screen.findByText(/cmp58e24\.\.\./);

    await type(searchBox(), 'cmp58e24');
    expect(screen.queryByText(/cmoldbhw\.\.\./)).toBeNull();

    await act(async () => { fireEvent.click(screen.getByLabelText('Clear search')); });
    expect(screen.getByText(/cmoldbhw\.\.\./)).toBeInTheDocument();
  });
});

/**
 * Re-running one post, and handing one post to the Trace tab.
 *
 * The row that prompted this had a Summary cell reading "—": the post was
 * analysed without `want_summary`, and the only remedy the page offered was
 * re-uploading the whole JSON file.
 */
describe('Posts single-post actions', () => {
  const rowFor = async (prefix) => (await screen.findByText(new RegExp(prefix + '\\.\\.\\.'))).closest('tr');

  it('re-runs exactly one post, asking for the summary', async () => {
    apiCall.mockImplementation(async (path) => {
      if (path.startsWith('/v1/analysis/latest')) return { results: [LOADED, OTHER] };
      if (path === '/v1/analysis/run') return { analysis_id: 'job-rerun-1' };
      return { results: [] };
    });

    render(<Posts />);
    const row = await rowFor('cmp58e24');

    await act(async () => { fireEvent.click(within(row).getByRole('button', { name: /Re-run/i })); });

    const [, opts] = apiCall.mock.calls.find(([path]) => path === '/v1/analysis/run');
    const body = JSON.parse(opts.body);
    // One post — not the campaign, not the page.
    expect(body.post_ids).toEqual([LOADED.post_id]);
    expect(body.options.want_summary).toBe(true);
    expect(await screen.findByText(/Queued cmp58e24/)).toBeInTheDocument();
  });

  it('says so when the re-run cannot be queued', async () => {
    apiCall.mockImplementation(async (path) => {
      if (path.startsWith('/v1/analysis/latest')) return { results: [LOADED, OTHER] };
      if (path === '/v1/analysis/run') throw new Error('No posts matched the selector');
      return { results: [] };
    });

    render(<Posts />);
    const row = await rowFor('cmp58e24');

    await act(async () => { fireEvent.click(within(row).getByRole('button', { name: /Re-run/i })); });

    expect(await screen.findByText(/Re-run failed: No posts matched the selector/)).toBeInTheDocument();
  });

  it('flags a row that has no summary', async () => {
    apiCall.mockImplementation(async (path) => {
      if (path.startsWith('/v1/analysis/latest')) {
        return { results: [post({ post_id: 'cmor32gy000000000000000a', post_summary: null })] };
      }
      return { results: [] };
    });

    render(<Posts />);

    // A bare dash reads as "nothing to summarise"; this is a fixable state.
    expect(await screen.findByText('no summary')).toBeInTheDocument();
  });

  it('hands the post id to the Trace tab and navigates there', async () => {
    const navigated = vi.fn();
    window.addEventListener('dashboard-navigate', navigated);

    render(<Posts />);
    const row = await rowFor('cmp58e24');

    await act(async () => { fireEvent.click(within(row).getByRole('button', { name: /Trace/i })); });

    expect(getTraceSnapshot().postId).toBe(LOADED.post_id);
    expect(navigated).toHaveBeenCalled();
    expect(navigated.mock.calls[0][0].detail).toEqual({ tab: 'trace' });
    window.removeEventListener('dashboard-navigate', navigated);
  });
});
