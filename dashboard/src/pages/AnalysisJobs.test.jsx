/**
 * Stop / Delete on the Analysis Jobs tab.
 *
 * These click the real buttons and assert on the request the page makes, because
 * the two ways this feature breaks are both invisible to the build: addressing
 * the wrong job (the list returns `id`, the run endpoint returns `analysis_id`)
 * and offering Stop on a job that has nothing to stop.
 */
import React from 'react';
import { render, screen, waitFor, within, fireEvent, act } from '@testing-library/react';
import { vi } from 'vitest';

import AnalysisJobs from './AnalysisJobs.jsx';

const apiCall = vi.fn();

vi.mock('../utils/api.js', () => ({
  API_BASE: 'http://api.test',
  getAuthHeaders: () => ({}),
  getSseQueryAsync: async () => '',
  apiCall: (...args) => apiCall(...args),
}));

// The Job ID cell renders only `id.split('-')[0]`, so the fixtures' FIRST
// dash-segments have to be distinct — `job-running-…`/`job-done-…` would both
// render as "job..." and a row lookup would match two rows.
const RUNNING = { id: 'running-1111-aaaa', type: 'analysis_run', status: 'running', campaign_id: 'c1', total: 10, completed: 3 };
const DONE = { id: 'done-2222-bbbb', type: 'analysis_run', status: 'done', campaign_id: 'c1', total: 10, completed: 10 };

function mockJobs(jobs) {
  apiCall.mockReset();
  apiCall.mockImplementation(async (path) => {
    if (path.startsWith('/v1/analysis?limit')) return { jobs, total: jobs.length };
    return {};
  });
}

beforeEach(() => {
  // The component opens an SSE stream when a job is tracked; jsdom has no
  // EventSource, and a stop must not depend on one being open.
  global.EventSource = class {
    constructor() { this.readyState = 0; }
    addEventListener() {}
    close() {}
  };
});

// `@testing-library/user-event` is not a dependency of this project, and adding
// one for two clicks is not worth it — fireEvent drives the same handlers.
const click = async (el) => { await act(async () => { fireEvent.click(el); }); };

const rowFor = async (job) => {
  const cell = await screen.findByText(`${job.id.split('-')[0]}...`);
  return cell.closest('tr');
};

describe('AnalysisJobs stop and delete', () => {
  it('offers Stop only on a job that is still running', async () => {
    mockJobs([RUNNING, DONE]);
    render(<AnalysisJobs />);

    const running = await rowFor(RUNNING);
    const done = await rowFor(DONE);

    expect(within(running).getByRole('button', { name: /stop/i })).toBeInTheDocument();
    expect(within(done).queryByRole('button', { name: /stop/i })).toBeNull();
    // Delete is offered on both — a finished job's record is the main thing an
    // operator wants out of the list.
    expect(within(running).getByRole('button', { name: /delete/i })).toBeInTheDocument();
    expect(within(done).getByRole('button', { name: /delete/i })).toBeInTheDocument();
  });

  it('posts to the cancel endpoint for the row that was clicked', async () => {
    mockJobs([RUNNING, DONE]);
    apiCall.mockImplementation(async (path) => {
      if (path.startsWith('/v1/analysis?limit')) return { jobs: [RUNNING, DONE], total: 2 };
      if (path.endsWith('/cancel')) return { analysis_id: RUNNING.id, status: 'cancelled' };
      return {};
    });
    render(<AnalysisJobs />);

    const running = await rowFor(RUNNING);
    await click(within(running).getByRole('button', { name: /stop/i }));

    // Confirms first — stopping a job is not an accident-friendly action.
    expect(await screen.findByText(/Stop this job\?/i)).toBeInTheDocument();
    expect(apiCall).not.toHaveBeenCalledWith(expect.stringContaining('/cancel'), expect.anything());

    await click(screen.getByRole('button', { name: /^Stop Job$/i }));

    await waitFor(() =>
      expect(apiCall).toHaveBeenCalledWith(`/v1/analysis/${RUNNING.id}/cancel`, { method: 'POST' })
    );
    // …and says what actually happened, not that the pipeline halted instantly.
    expect(await screen.findByText(/in-flight posts will finish/i)).toBeInTheDocument();
  });

  it('deletes with DELETE and drops the row from the table', async () => {
    apiCall.mockReset();
    let jobs = [RUNNING, DONE];
    apiCall.mockImplementation(async (path, opts) => {
      if (path.startsWith('/v1/analysis?limit')) return { jobs, total: jobs.length };
      if (opts?.method === 'DELETE') {
        jobs = jobs.filter(j => j.id !== DONE.id);
        return { analysis_id: DONE.id, deleted: { jobs: 1, redis_keys: 5 }, stopped: false };
      }
      return {};
    });
    render(<AnalysisJobs />);

    const done = await rowFor(DONE);
    await click(within(done).getByRole('button', { name: /delete/i }));

    // The dialog has to be explicit that the posts' analyses survive.
    expect(await screen.findByText(/analysis results are kept/i)).toBeInTheDocument();
    await click(screen.getByRole('button', { name: /^Delete Job$/i }));

    await waitFor(() =>
      expect(apiCall).toHaveBeenCalledWith(`/v1/analysis/${DONE.id}`, { method: 'DELETE' })
    );
    await waitFor(() =>
      expect(screen.queryByText(`${DONE.id.split('-')[0]}...`)).toBeNull()
    );
    // The other job is untouched.
    expect(screen.getByText(`${RUNNING.id.split('-')[0]}...`)).toBeInTheDocument();
  });

  it('surfaces a refused stop instead of pretending it worked', async () => {
    apiCall.mockReset();
    apiCall.mockImplementation(async (path) => {
      if (path.startsWith('/v1/analysis?limit')) return { jobs: [RUNNING], total: 1 };
      throw new Error("Job 'running-1111-aaaa' is already in a final state (status: done) — nothing to stop");
    });
    render(<AnalysisJobs />);

    const running = await rowFor(RUNNING);
    await click(within(running).getByRole('button', { name: /stop/i }));
    await click(screen.getByRole('button', { name: /^Stop Job$/i }));

    expect(await screen.findByText(/nothing to stop/i)).toBeInTheDocument();
  });

  it('addresses a job that reports itself as analysis_id, not id', async () => {
    const odd = { analysis_id: 'odd-3333-cccc', status: 'queued', total: 5, completed: 0 };
    apiCall.mockReset();
    apiCall.mockImplementation(async (path) => {
      if (path.startsWith('/v1/analysis?limit')) return { jobs: [odd], total: 1 };
      return { status: 'cancelled' };
    });
    render(<AnalysisJobs />);

    const row = (await screen.findByText('odd...')).closest('tr');
    await click(within(row).getByRole('button', { name: /stop/i }));
    await click(screen.getByRole('button', { name: /^Stop Job$/i }));

    await waitFor(() =>
      expect(apiCall).toHaveBeenCalledWith('/v1/analysis/odd-3333-cccc/cancel', { method: 'POST' })
    );
  });
});

describe('AnalysisJobs resume', () => {
  // A power-cut job: the row still says "running" but nothing has written
  // progress for hours. This is the state the Resume button exists for.
  const STALLED = {
    id: 'stalled-4444-dddd', status: 'running', campaign_id: 'c1',
    total: 300, completed: 30,
    updated_at: new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString(),
  };
  const LIVE = {
    id: 'live-5555-eeee', status: 'running', campaign_id: 'c1',
    total: 300, completed: 120, updated_at: new Date().toISOString(),
  };

  it('shows a quiet job as stalled rather than running', async () => {
    mockJobs([STALLED, LIVE]);
    render(<AnalysisJobs />);

    const stalled = await rowFor(STALLED);
    const live = await rowFor(LIVE);
    expect(within(stalled).getByText('stalled')).toBeInTheDocument();
    expect(within(live).getByText('running')).toBeInTheDocument();
  });

  it('resumes in one tap and reports what was re-queued, not just "resumed"', async () => {
    apiCall.mockReset();
    apiCall.mockImplementation(async (path) => {
      if (path.startsWith('/v1/analysis?limit')) return { jobs: [STALLED], total: 1 };
      if (path.endsWith('/resume')) {
        return { resumed: true, status: 'running', progress: { total: 300, completed: 30, remaining: 270 } };
      }
      return {};
    });
    render(<AnalysisJobs />);

    const row = await rowFor(STALLED);
    await click(within(row).getByRole('button', { name: /resume/i }));

    await waitFor(() =>
      expect(apiCall).toHaveBeenCalledWith(`/v1/analysis/${STALLED.id}/resume`, { method: 'POST' })
    );
    expect(await screen.findByText(/30\/300 already done, 270 re-queued/)).toBeInTheDocument();
  });

  it('surfaces the refusal when the job turns out to be alive', async () => {
    apiCall.mockReset();
    apiCall.mockImplementation(async (path) => {
      if (path.startsWith('/v1/analysis?limit')) return { jobs: [LIVE], total: 1 };
      throw new Error("Job 'live-5555-eeee' is still making progress (last update 4s ago) — stop it first, then resume");
    });
    render(<AnalysisJobs />);

    const row = await rowFor(LIVE);
    await click(within(row).getByRole('button', { name: /resume/i }));

    expect(await screen.findByText(/stop it first, then resume/i)).toBeInTheDocument();
  });

  it('says so when there was nothing left to resume', async () => {
    apiCall.mockReset();
    apiCall.mockImplementation(async (path) => {
      if (path.startsWith('/v1/analysis?limit')) return { jobs: [STALLED], total: 1 };
      return { resumed: false, status: 'done', reason: 'every post in the selector already has a result from this job' };
    });
    render(<AnalysisJobs />);

    const row = await rowFor(STALLED);
    await click(within(row).getByRole('button', { name: /resume/i }));

    expect(await screen.findByText(/Nothing to resume/i)).toBeInTheDocument();
  });

  it('does not offer Resume on a job that completed', async () => {
    mockJobs([DONE]);
    render(<AnalysisJobs />);
    const done = await rowFor(DONE);
    expect(within(done).queryByRole('button', { name: /resume/i })).toBeNull();
  });
});

/**
 * Progress must not invent a number it does not have.
 *
 * A `done` job with `completed: 0` against `total: 1` rendered a confident
 * **0%** — observed live on 29 Aug 2026, on a job whose completion had been
 * recorded outside the assembler so no counter was ever written. The mirror
 * image was just as wrong: with `total` missing the page fell back to
 * `isFinished ? 100 : 0` and drew a confident **100%**. Two fabrications for the
 * same absence of data, both standing next to a status that contradicted them.
 */
describe('AnalysisJobs progress', () => {
  const progressCell = (row) => row.querySelectorAll('td')[5];

  it('renders a real ratio when both counters are present', async () => {
    mockJobs([{ ...RUNNING, total: 10, completed: 3 }]);
    render(<AnalysisJobs />);

    const row = await rowFor(RUNNING);
    expect(progressCell(row).textContent).toContain('30%');
  });

  // The two counters are separate Redis keys with a 24 h TTL and they expire
  // INDEPENDENTLY. Both halves were live on 29 Aug 2026: one finished job held
  // `:total` without `:completed`, another held `:completed` without `:total` —
  // two finished jobs, two different wrong answers, from the same missing data.
  // `status` lives in Postgres and does not expire, and `done` means every post
  // landed, so it is the durable source when the counters cannot answer.

  it('completes a done job whose completed counter expired', async () => {
    const job = { id: 'nocount-1111-aaaa', type: 'analysis_run', status: 'done', total: 1, completed: null };
    mockJobs([job]);
    render(<AnalysisJobs />);

    const row = await rowFor(job);
    expect(progressCell(row).textContent).toContain('100%');
    // Not a standalone 0% — that was the missing counter coerced to zero.
    // ("100%" contains "0%", so this has to match on the boundary.)
    expect(progressCell(row).textContent).not.toMatch(/(^|[^\d])0%/);
  });

  it('completes a done job whose total counter expired', async () => {
    const job = { id: 'nototal-1111-aaaa', type: 'analysis_run', status: 'done', total: null, completed: 50 };
    mockJobs([job]);
    render(<AnalysisJobs />);

    const row = await rowFor(job);
    expect(progressCell(row).textContent).toContain('100%');
  });

  it('marks a derived 100% as derived', async () => {
    const job = { id: 'derived-1111-aaaa', type: 'analysis_run', status: 'done', total: null, completed: null };
    mockJobs([job]);
    render(<AnalysisJobs />);

    const row = await rowFor(job);
    // Same number, weaker evidence — read off the status, not counted.
    expect(progressCell(row).textContent).toContain('100%*');
    expect(progressCell(row).querySelector('[aria-label="derived from status"]')).not.toBeNull();
  });

  it('does not claim a cancelled job is complete', async () => {
    const job = { id: 'stopped-1111-aaaa', type: 'analysis_run', status: 'cancelled', total: null, completed: null };
    mockJobs([job]);
    render(<AnalysisJobs />);

    const row = await rowFor(job);
    // Stopped is not finished. How much landed is simply no longer recorded.
    expect(progressCell(row).textContent).toContain('—');
    expect(progressCell(row).textContent).not.toContain('100%');
  });

  it('does not claim a failed job is complete', async () => {
    const job = { id: 'broken-1111-aaaa', type: 'analysis_run', status: 'failed', total: null, completed: null };
    mockJobs([job]);
    render(<AnalysisJobs />);

    const row = await rowFor(job);
    expect(progressCell(row).textContent).not.toContain('100%');
  });

  it('shows unknown for a running job with no counters yet', async () => {
    const job = { id: 'fresh-1111-aaaa', type: 'analysis_run', status: 'running', total: null, completed: null };
    mockJobs([job]);
    render(<AnalysisJobs />);

    const row = await rowFor(job);
    expect(progressCell(row).textContent).toContain('—');
  });

  it('trusts real counters over the status', async () => {
    const job = { id: 'partial-1111-aaaa', type: 'analysis_run', status: 'done', total: 10, completed: 7 };
    mockJobs([job]);
    render(<AnalysisJobs />);

    const row = await rowFor(job);
    // A measurement beats an inference, even a contradictory one — and the
    // contradiction is worth seeing rather than smoothing over.
    expect(progressCell(row).textContent).toContain('70%');
  });

  it('keeps a genuine zero distinct from a missing one', async () => {
    const job = { id: 'zero-1111-aaaa', type: 'analysis_run', status: 'running', total: 8, completed: 0 };
    mockJobs([job]);
    render(<AnalysisJobs />);

    const row = await rowFor(job);
    // Nothing has landed yet, and the counters say so — that IS 0%.
    expect(progressCell(row).textContent).toContain('0%');
  });
});
