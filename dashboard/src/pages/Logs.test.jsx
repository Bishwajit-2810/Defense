/**
 * The Logs tab must open exactly one stream.
 *
 * Every line was rendered twice, and so was the stream's 60-line backfill, which
 * made the log view read as if the backend were doing everything twice. The cause
 * was an `async connectStream()` guarded by `if (eventSourceRef.current) return`:
 * the guard runs before `await getSseQueryAsync()`, so under StrictMode's
 * double-mount both invocations passed it while the ref was still null. Two
 * EventSources ended up open, the ref kept the second, and the first was orphaned
 * — never closed, streaming duplicates forever. Changing the filter leaked
 * another one.
 */
import React, { StrictMode } from 'react';
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react';
import { vi } from 'vitest';

import Logs from './Logs.jsx';

let ticketResolvers = [];

vi.mock('../utils/api', () => ({
  API_BASE: 'http://api.test',
  // Deliberately a real promise: the bug lived in the window between the guard
  // and the await resolving, so a synchronous mock would hide it.
  getSseQueryAsync: () => new Promise(res => { ticketResolvers.push(() => res('?ticket=t')); }),
}));

class FakeEventSource {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.closed = false;
    this.listeners = {};
    FakeEventSource.instances.push(this);
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  close() { this.closed = true; }
  emit(type, data) {
    (this.listeners[type] || []).forEach(fn => fn({ data: JSON.stringify(data) }));
  }
  static get open() { return FakeEventSource.instances.filter(i => !i.closed); }
  static reset() { FakeEventSource.instances = []; }
}

const line = (over = {}) => ({
  ts: 1787252924.13, level: 'INFO', service: 'api',
  message: 'http_request path=/v1/pipeline/stats status=200', module: 'main:1', ...over,
});

const settleTickets = async () => {
  await act(async () => {
    ticketResolvers.forEach(r => r());
    ticketResolvers = [];
    await Promise.resolve();
  });
};

beforeEach(() => {
  ticketResolvers = [];
  FakeEventSource.reset();
  global.EventSource = FakeEventSource;
  // jsdom has no scrollIntoView.
  Element.prototype.scrollIntoView = vi.fn();
});

describe('Logs stream', () => {
  it('opens exactly one stream under StrictMode double-mount', async () => {
    render(<StrictMode><Logs /></StrictMode>);
    await settleTickets();

    // The ticket promise resolves for both mounts; only one socket may survive.
    await waitFor(() => expect(FakeEventSource.open.length).toBe(1));
    // Nothing orphaned: any extra instance created must have been closed.
    expect(FakeEventSource.instances.filter(i => !i.closed).length).toBe(1);
  });

  it('renders a line once, not twice', async () => {
    render(<StrictMode><Logs /></StrictMode>);
    await settleTickets();
    await waitFor(() => expect(FakeEventSource.open.length).toBe(1));

    await act(async () => { FakeEventSource.open[0].emit('log', line()); });

    expect(screen.getAllByText(/http_request path=/)).toHaveLength(1);
  });

  it('drops the repeat where the backfill meets the live tail', async () => {
    // The API replays `backfill` lines and then tails, subscribing first so no
    // line is lost — which means the straddling line arrives twice by design.
    render(<StrictMode><Logs /></StrictMode>);
    await settleTickets();
    await waitFor(() => expect(FakeEventSource.open.length).toBe(1));
    const es = FakeEventSource.open[0];

    await act(async () => {
      es.emit('log', { ...line(), backfill: true });
      es.emit('log', line());
    });

    expect(screen.getAllByText(/http_request path=/)).toHaveLength(1);
  });

  it('keeps two genuinely different lines', async () => {
    render(<StrictMode><Logs /></StrictMode>);
    await settleTickets();
    await waitFor(() => expect(FakeEventSource.open.length).toBe(1));
    const es = FakeEventSource.open[0];

    await act(async () => {
      es.emit('log', line({ ts: 1787252924.13 }));
      es.emit('log', line({ ts: 1787252925.44 }));
    });

    expect(screen.getAllByText(/http_request path=/)).toHaveLength(2);
  });

  it('closes the old stream when the level filter changes', async () => {
    render(<StrictMode><Logs /></StrictMode>);
    await settleTickets();
    await waitFor(() => expect(FakeEventSource.open.length).toBe(1));

    await act(async () => {
      fireEvent.change(screen.getByRole('combobox'), { target: { value: 'ERROR' } });
    });
    await settleTickets();

    await waitFor(() => expect(FakeEventSource.open.length).toBe(1));
    expect(FakeEventSource.open[0].url).toContain('level=ERROR');
  });

  it('closes the stream on unmount', async () => {
    const { unmount } = render(<StrictMode><Logs /></StrictMode>);
    await settleTickets();
    await waitFor(() => expect(FakeEventSource.open.length).toBe(1));

    await act(async () => { unmount(); });

    expect(FakeEventSource.open.length).toBe(0);
  });

  it('Clear forgets the dedupe keys, so a reconnect can repopulate', async () => {
    render(<StrictMode><Logs /></StrictMode>);
    await settleTickets();
    await waitFor(() => expect(FakeEventSource.open.length).toBe(1));
    const es = FakeEventSource.open[0];

    await act(async () => { es.emit('log', line()); });
    expect(screen.getAllByText(/http_request path=/)).toHaveLength(1);

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Clear/i })); });
    expect(screen.queryByText(/http_request path=/)).toBeNull();

    // Same line again — must come back, not be swallowed as a duplicate.
    await act(async () => { es.emit('log', line()); });
    expect(screen.getAllByText(/http_request path=/)).toHaveLength(1);
  });
});
