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
import { resetTraceSession, getSnapshot } from '../utils/traceSession.js';

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
  // The trace session deliberately outlives the component, so it also outlives
  // a test — reset it or each case inherits the last one's job and rail.
  resetTraceSession();
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

  it('traces a post id that is not in the dropdown', async () => {
    render(<Trace />);
    await listLoaded();

    const pasted = 'cmor32gyffffffffffffffff';
    await act(async () => {
      fireEvent.change(screen.getByLabelText(/paste a post id/i), { target: { value: pasted } });
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Run Trace/i }));
    });

    // The id the operator typed is the id that runs — not the preselected row.
    await waitFor(() => expect(apiCall).toHaveBeenCalledWith(
      '/v1/analysis/run',
      expect.objectContaining({ body: expect.stringContaining(pasted) })
    ));
    // And the dropdown carries it rather than snapping back to a known row.
    expect(screen.getByRole('combobox').value).toBe(pasted);
  });
});

describe('Trace session', () => {
  // The bug this pins: tabs render as `{activeTab === 'trace' && <Trace />}`, so
  // leaving the tab unmounts the page. When the trace lived in `useState` that
  // threw away the rail and closed the stream — the page came back blank and a
  // running trace had been abandoned.
  const runATrace = async () => {
    const view = render(<Trace />);
    await listLoaded();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Run Trace/i }));
    });
    await waitFor(() => expect(FakeEventSource.instances.length).toBeGreaterThan(0), { timeout: 4000 });
    return { es: FakeEventSource.instances[0], unmount: view.unmount };
  };

  const emit = (es, type, payload) => act(async () => {
    (es.listeners[type] || []).forEach(fn => fn({ data: JSON.stringify(payload) }));
  });

  it('keeps the stream open when the tab is switched away', async () => {
    const { es, unmount } = await runATrace();

    await act(async () => { unmount(); });

    // The whole fix: leaving the tab must not abandon a post mid-pipeline.
    expect(es.closed).toBe(false);
    expect(FakeEventSource.open.length).toBe(1);
  });

  it('still shows the rail after unmounting and remounting', async () => {
    const { es, unmount } = await runATrace();
    await emit(es, 'stage', { event: 'stage', stage: 'stage1', status: 'done', seq: 1, detail: { engine: 'llm' } });
    await act(async () => { unmount(); });

    // Coming back to the tab: the rail is rebuilt from the session rather than
    // from an empty state, which is what used to show "Nothing traced yet".
    render(<Trace />);
    await waitFor(() => expect(screen.getAllByText(/"engine":"llm"/).length).toBeGreaterThan(0));
    expect(screen.queryAllByText(/Nothing traced yet/).length).toBe(0);
  });

  it('applies a frame that arrives while the tab is unmounted', async () => {
    const { es, unmount } = await runATrace();
    await act(async () => { unmount(); });

    await emit(es, 'stage', { event: 'stage', stage: 'router', status: 'done', seq: 3, detail: { use_llm: true } });

    render(<Trace />);
    await waitFor(() => expect(screen.getAllByText(/"use_llm":true/).length).toBeGreaterThan(0));
  });

  it('dedupes a frame that arrives replayed and live', async () => {
    const { es } = await runATrace();
    const frame = { event: 'stage', stage: 'ingest', status: 'done', seq: 7, detail: { platform: 'facebook' } };
    await emit(es, 'stage', { ...frame, replay: true });
    await emit(es, 'stage', frame);

    expect(getSnapshot().tape.filter(t => t.seq === 7).length).toBe(1);
  });

  it('closes the stream and empties the rail on Clear', async () => {
    const { es } = await runATrace();
    await act(async () => {
      fireEvent.click(screen.getAllByRole('button', { name: /^Clear$/i })[0]);
    });

    expect(es.closed).toBe(true);
    expect(getSnapshot().status).toBe('idle');
    expect(getSnapshot().tape).toEqual([]);
    // The selection is configuration, not output — Clear must not lose it.
    expect(getSnapshot().postId).toBe(POSTS[0].post_id);
  });
});
