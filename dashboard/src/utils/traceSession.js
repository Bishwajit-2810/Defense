/**
 * The Trace tab's session — held in this module, not in the component.
 *
 * The Trace page used to keep the whole trace (job id, layer rail, event tape,
 * canonical result) in `useState`, and `App` renders tabs as
 * `{activeTab === 'trace' && <Trace />}`. Switching to any other tab therefore
 * *unmounted* the page: React threw the state away and the cleanup closed the
 * EventSource, so coming back showed "Nothing traced yet" and — worse — a trace
 * that was still running had been silently abandoned mid-flight.
 *
 * Holding the session here inverts that. The stream is owned by the module, so
 * it keeps consuming frames while the operator is reading Posts or Logs, and the
 * page re-renders from the store when it mounts again. The component becomes a
 * view over a session that outlives it.
 *
 * A small slice (post id, job id, status) is mirrored into `sessionStorage`, so
 * a full page reload can re-attach to a job still in flight: the API replays the
 * buffered stage events on connect (`job:{id}:stage_events`, 1 h TTL), which is
 * what makes re-attaching produce a complete rail rather than whatever happens
 * to arrive next. The tape and result are deliberately *not* persisted — they
 * are rebuilt from that replay and from `GET /v1/analysis/{id}`.
 */
import { apiCall, API_BASE, getSseQueryAsync } from './api.js';

const STORAGE_KEY = 'defense.trace.session';

// Stage keys, in pipeline order. Mirrors libs/progress.py STAGES.
export const TRACE_LAYERS = [
  { key: 'ingest', label: 'Ingestion', desc: 'Consumes raw data from the stream.' },
  { key: 'stage1', label: 'Stage 1 (NLP & LLM)', desc: 'Extracts entities, metrics, and generates heavy LLM summarization.' },
  { key: 'router', label: 'Router', desc: 'Filters emoji-only spam and routes valid comments to Stage 2.' },
  { key: 'stage2', label: 'Stage 2 (Parallel)', desc: 'Runs LLM, XLM-R, and DistilBERT concurrently on valid comments. Checks Watchlist Alerts.' },
  { key: 'assembler', label: 'Assembler', desc: 'Merges partial results and writes final JSON to the database.' },
];

// A trace is running while the status is one of these — the button stays
// disabled and a reload will try to re-attach.
const RUNNING = new Set(['starting', 'live']);
export const isRunning = (status) => RUNNING.has(status);

const blank = () => ({
  status: 'idle',        // idle | starting | live | done | failed | cancelled | timeout
  postId: '',
  wantSummary: true,
  jobId: null,
  layers: {},            // stage key -> { status, detail, ms }
  tape: [],
  result: null,
  error: null,
  startedAt: null,
});

let state = blank();
let stream = null;
let seenSeq = new Set();
const listeners = new Set();

// ---------------------------------------------------------------------------
// Store plumbing
// ---------------------------------------------------------------------------

function persist() {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({
      postId: state.postId,
      wantSummary: state.wantSummary,
      jobId: state.jobId,
      status: state.status,
    }));
  } catch { /* private mode / storage disabled — the session just won't survive a reload */ }
}

function commit(patch) {
  state = { ...state, ...patch };
  persist();
  // Copied first: a listener that unsubscribes during the notify pass (a page
  // unmounting on the same frame) must not shorten the set being iterated.
  for (const fn of Array.from(listeners)) fn();
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// `useSyncExternalStore` compares snapshots by identity, so this must return the
// same object until something actually changes — never a fresh copy.
export function getSnapshot() {
  return state;
}

export function setPostId(postId) {
  commit({ postId: String(postId || '') });
}

export function setWantSummary(wantSummary) {
  commit({ wantSummary: !!wantSummary });
}

/** Test seam: drop the session and any open stream. */
export function resetTraceSession() {
  closeStream();
  seenSeq = new Set();
  state = blank();
  try { sessionStorage.removeItem(STORAGE_KEY); } catch { /* ignore */ }
  for (const fn of Array.from(listeners)) fn();
}

// ---------------------------------------------------------------------------
// Stream
// ---------------------------------------------------------------------------

export function closeStream() {
  if (stream) {
    stream.close();
    stream = null;
  }
}

const idleLayers = () =>
  TRACE_LAYERS.reduce((acc, layer) => ({ ...acc, [layer.key]: { status: 'idle' } }), {});

async function fetchFinalResult(jobId) {
  try {
    const data = await apiCall(`/v1/analysis/${jobId}?include=results`);
    const finalRes = (data.results && data.results[0]) ? data.results[0] : data;
    commit({ result: finalRes });
  } catch { /* the rail and the tape still stand without it */ }
}

function openStream(jobId) {
  return getSseQueryAsync().then((qs) => {
    const es = new EventSource(`${API_BASE}/v1/analysis/${jobId}/stream${qs}`);
    stream = es;

    // Every handler checks it is still the current stream: starting a second
    // trace replaces `stream`, and a late frame from the abandoned one must not
    // write over the new session.
    const current = () => stream === es;

    const onFrame = (e) => {
      if (!current()) return;
      let data;
      try { data = JSON.parse(e.data); } catch { return; }

      // The replay buffer and live pub/sub overlap by exactly one frame; `seq`
      // is the server's monotonic per-job counter for dropping it.
      if (data.seq != null) {
        if (seenSeq.has(data.seq)) return;
        seenSeq.add(data.seq);
      }

      const patch = { tape: [...state.tape, data] };
      if (data.stage) {
        patch.layers = {
          ...state.layers,
          [data.stage]: { status: data.status, detail: data.detail || {}, ms: data.ms },
        };
      }
      commit(patch);
    };

    const finish = (status) => {
      if (!current()) return;
      closeStream();
      commit({ status });
      if (status === 'done') fetchFinalResult(jobId);
    };

    es.addEventListener('progress', onFrame);
    es.addEventListener('stage', onFrame);
    es.addEventListener('done', (e) => { onFrame(e); finish('done'); });
    es.addEventListener('cancelled', () => finish('cancelled'));
    // The API caps a stream at 5 minutes and says so before closing. Reported as
    // its own state: the job is very likely still running, and calling that
    // "failed" is a lie the operator would act on.
    es.addEventListener('timeout', () => finish('timeout'));
    es.addEventListener('error', (e) => {
      if (!current()) return;
      // Both a server-sent `event: error` frame and a plain transport failure
      // arrive here under the same name. Only the first carries `data`; the
      // second is left to `onerror` below, which knows not to cry failure over
      // a stream the server closed because it was finished.
      if (!e || typeof e.data !== 'string') return;
      let detail = 'The pipeline reported an error on this job.';
      try { detail = JSON.parse(e.data).error || detail; } catch { /* keep the default */ }
      closeStream();
      commit({ status: 'failed', error: detail });
    });
    es.onerror = () => {
      // Transport-level drop. Only meaningful while we still expect frames —
      // the browser also fires this when the server closes a finished stream.
      if (!current() || !isRunning(state.status)) return;
      closeStream();
      commit({ status: 'failed', error: 'Lost the progress stream.' });
    };

    return es;
  });
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

/**
 * Run one post through the real pipeline and follow it.
 *
 * `postId` may be anything the operator can get hold of — a row from the recent
 * list, or an id pasted from a ticket for a post far outside it.
 */
export async function startTrace({ postId, wantSummary } = {}) {
  const id = String(postId ?? state.postId ?? '').trim();
  const want = wantSummary === undefined ? state.wantSummary : !!wantSummary;
  if (!id) {
    const err = new Error('Pick a post, or paste a post id, to trace.');
    commit({ status: 'failed', error: err.message });
    throw err;
  }

  closeStream();
  seenSeq = new Set();
  commit({
    postId: id,
    wantSummary: want,
    status: 'starting',
    jobId: null,
    layers: idleLayers(),
    tape: [],
    result: null,
    error: null,
    startedAt: Date.now(),
  });

  let jobId;
  try {
    const resp = await apiCall('/v1/analysis/run', {
      method: 'POST',
      body: JSON.stringify({ post_ids: [id], options: { want_summary: want } }),
    });
    jobId = resp.analysis_id || resp.id || resp.job_id;
    if (!jobId) throw new Error('No job id in response');
  } catch (err) {
    commit({ status: 'failed', error: err.message });
    throw err;
  }

  commit({ status: 'live', jobId });
  await openStream(jobId);
  return jobId;
}

/**
 * Re-attach to a job whose stream we are not holding — after a page reload.
 * The buffered stage events are replayed on connect, so the rail rebuilds.
 */
export async function reattach(jobId = state.jobId) {
  if (!jobId || stream) return null;
  seenSeq = new Set();
  commit({ status: 'live', jobId, layers: idleLayers(), tape: [], error: null });
  return openStream(jobId);
}

export function clearTrace() {
  closeStream();
  seenSeq = new Set();
  commit({
    ...blank(),
    // The selection is configuration, not trace output: clearing the trace
    // should not also make the operator find their post again.
    postId: state.postId,
    wantSummary: state.wantSummary,
  });
}

// ---------------------------------------------------------------------------
// Rehydrate on load
// ---------------------------------------------------------------------------

try {
  const saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || 'null');
  if (saved && typeof saved === 'object') {
    state = {
      ...blank(),
      postId: saved.postId || '',
      wantSummary: saved.wantSummary !== false,
      // A job that was in flight when the page went away is re-attachable; the
      // page asks for that explicitly on mount rather than opening a stream as
      // a module import side effect.
      jobId: isRunning(saved.status) ? (saved.jobId || null) : null,
      status: 'idle',
    };
  }
} catch { /* nothing to restore */ }

/** True when a reload left a job we can still follow. */
export function hasResumableJob() {
  return Boolean(state.jobId) && !isRunning(state.status) && !stream;
}
