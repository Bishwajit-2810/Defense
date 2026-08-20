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
