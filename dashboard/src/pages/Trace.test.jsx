/**
 * Trace tab — the post list and its mount-only load.
 *
 * Written *before* stabilising `fetchPosts` for `exhaustive-deps`, to pin the
 * behaviour the empty dependency array was protecting: the list is fetched once,
 * the first post is preselected, and **picking a different post must not refetch
 * the list**. Naively declaring `fetchPosts` as a dependency would have done
 * exactly that, because it closes over `selectedPostId`.
 */
import React from 'react';
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react';
import { vi } from 'vitest';

import Trace from './Trace.jsx';

const apiCall = vi.fn();
vi.mock('../utils/api.js', () => ({
  API_BASE: 'http://api.test',
  getAuthHeaders: () => ({}),
  getSseQueryAsync: async () => '',
  apiCall: (...a) => apiCall(...a),
}));
vi.mock('../utils/api', () => ({
  API_BASE: 'http://api.test',
  getAuthHeaders: () => ({}),
  getSseQueryAsync: async () => '',
  apiCall: (...a) => apiCall(...a),
}));

class FakeEventSource {
  static instances = [];
  constructor(url) { this.url = url; this.closed = false; this.listeners = {}; FakeEventSource.instances.push(this); }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  close() { this.closed = true; }
  static get open() { return FakeEventSource.instances.filter(i => !i.closed); }
  static reset() { FakeEventSource.instances = []; }
}

const POSTS = [
  { post_id: 'cmfirst00000000000000001', post_summary: 'first post' },
  { post_id: 'cmsecond0000000000000002', post_summary: 'second post' },
];

const listCalls = () =>
  apiCall.mock.calls.filter(([p]) => typeof p === 'string' && p.includes('/v1/analysis/latest')).length;

// Waiting on `listCalls()` only proves the request was issued; the options are
// rendered a tick later. Every interaction below waits for the preselection,
// which is the observable proof that the list landed.
const listLoaded = async () => {
  await waitFor(() => expect(screen.getByRole('combobox').value).toBe(POSTS[0].post_id));
};

beforeEach(() => {
  apiCall.mockReset();
  apiCall.mockImplementation(async (path) => {
    if (String(path).includes('/v1/analysis/latest')) return { results: POSTS };
    // Run Trace bails with "No job ID in response" unless this carries one.
    if (String(path).includes('/v1/analysis/run')) return { analysis_id: 'job-trace-1' };
    return {};
  });
  FakeEventSource.reset();
  global.EventSource = FakeEventSource;
  global.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
  Element.prototype.scrollIntoView = vi.fn();
});

describe('Trace post list', () => {
  it('loads the list once on mount and preselects the first post', async () => {
    render(<Trace />);

    await waitFor(() => expect(listCalls()).toBe(1));
    const select = screen.getByRole('combobox');
    await waitFor(() => expect(select.value).toBe(POSTS[0].post_id));
  });

  it('does not refetch the list when a different post is picked', async () => {
    render(<Trace />);
    await listLoaded();

    await act(async () => {
      fireEvent.change(screen.getByRole('combobox'), { target: { value: POSTS[1].post_id } });
    });

    expect(screen.getByRole('combobox').value).toBe(POSTS[1].post_id);
    // The whole point of the empty dependency array.
    expect(listCalls()).toBe(1);
  });

  it('does not overwrite a selection the operator already made', async () => {
    render(<Trace />);
    await listLoaded();

    await act(async () => {
      fireEvent.change(screen.getByRole('combobox'), { target: { value: POSTS[1].post_id } });
    });
    // Any later list refresh must not snap the dropdown back to the first row.
    await act(async () => { window.dispatchEvent(new Event('auto-refresh')); });

    expect(screen.getByRole('combobox').value).toBe(POSTS[1].post_id);
  });

  it('survives a failing list request', async () => {
    // The component handles this by logging, so spy on `console.error` and
    // assert it — rather than letting a deliberate 502 dump a stack trace into
    // an otherwise green run, where it reads as a real failure.
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
    apiCall.mockRejectedValue(new Error('API error 502'));

    render(<Trace />);

    await waitFor(() => expect(listCalls()).toBe(1));
    // Still standing, and the failure was reported rather than swallowed.
    expect(screen.getByRole('combobox')).toBeInTheDocument();
    await waitFor(() =>
      expect(logged).toHaveBeenCalledWith('Failed to load trace posts', expect.any(Error))
    );
    logged.mockRestore();
  });

  it('closes an open trace stream on unmount', async () => {
    // `handleRunTrace` bails with an alert when nothing is selected, so this
    // selects explicitly rather than relying on the mount-time preselect having
    // landed — waiting on that made the test flaky under load, and a flaky test
    // is worse than no test.
    const alerted = vi.fn();
    vi.stubGlobal('alert', alerted);

    const { unmount } = render(<Trace />);
    await listLoaded();
    await act(async () => {
      fireEvent.change(screen.getByRole('combobox'), { target: { value: POSTS[0].post_id } });
    });
    expect(screen.getByRole('combobox').value).toBe(POSTS[0].post_id);

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Run Trace/i }));
    });

    // If the guard fired, say so — otherwise the stream assertion below reports
    // "0 streams" and hides the reason.
    expect(alerted).not.toHaveBeenCalled();
    await waitFor(
      () => expect(apiCall).toHaveBeenCalledWith('/v1/analysis/run', expect.objectContaining({ method: 'POST' })),
      { timeout: 4000 }
    );
    await waitFor(() => expect(FakeEventSource.instances.length).toBeGreaterThan(0), { timeout: 4000 });

    await act(async () => { unmount(); });

    expect(FakeEventSource.open.length).toBe(0);
    vi.unstubAllGlobals();
  });
});
