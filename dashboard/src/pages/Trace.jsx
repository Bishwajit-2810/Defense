import React, { useState, useEffect, useCallback, useSyncExternalStore } from 'react';
import { GitCommit, Loader2 } from 'lucide-react';
import { Chart as ChartJS, ArcElement, Tooltip, Legend, CategoryScale, LinearScale, BarElement, Title } from 'chart.js';
import { Doughnut, Bar } from 'react-chartjs-2';
import { apiCall } from '../utils/api.js';
import { orderedBreakdown, sentimentColors } from '../utils/sentiment';
import {
  TRACE_LAYERS,
  subscribe,
  getSnapshot,
  setPostId,
  setWantSummary,
  startTrace,
  clearTrace,
  reattach,
  hasResumableJob,
  isRunning,
} from '../utils/traceSession.js';

ChartJS.register(ArcElement, Tooltip, Legend, CategoryScale, LinearScale, BarElement, Title);

// The pipeline publishes `running` / `done` (libs/progress.py); the rail used to
// colour on `processing` / `completed`, which nothing ever sends — so every layer
// stayed grey however far the post got. Both vocabularies are accepted here so
// the rail cannot silently stop lighting up again.
const LAYER_DONE = new Set(['done', 'completed', 'ok']);
const LAYER_ACTIVE = new Set(['running', 'processing', 'started']);

function TraceVisuals({ result, traceState }) {
  if (!result && (!traceState || !traceState.stage1)) return null;

  const breakdown = traceState?.stage1?.detail?.comment_breakdown
    || result?.comment_analysis?.sentiment_breakdown;
  const hasBreakdown = !!breakdown && Object.values(breakdown).some((v) => Number(v) > 0);

  // Built from the breakdown's own keys so a bucket the pipeline reports —
  // `uncertain`, added when the labellers abstain — cannot be dropped by a
  // hardcoded three-slice chart, and every colour is keyed on its label.
  const sentSlices = orderedBreakdown(hasBreakdown ? breakdown : {});
  const sentimentData = {
    labels: sentSlices.labels,
    datasets: [{
      data: sentSlices.values,
      backgroundColor: sentimentColors(sentSlices.labels),
      borderWidth: 0
    }]
  };

  const chartOptions = {
    plugins: { legend: { position: 'bottom', labels: { color: '#64748b' } } },
    cutout: '70%',
    maintainAspectRatio: false
  };

  const stage1Ms = result?.stage1_ms || traceState?.assembler?.detail?.stage1_ms || 0;
  const stage2Ms = result?.stage2_ms || traceState?.assembler?.detail?.stage2_ms || 0;

  const timeData = {
    labels: ['Stage 1 (NLP)', 'Stage 2 (LLM)'],
    datasets: [{
      label: 'Processing Time (ms)',
      data: [stage1Ms, stage2Ms],
      backgroundColor: ['#3b82f6', '#8b5cf6'],
      borderRadius: 4
    }]
  };

  const barOptions = {
    indexAxis: 'y',
    plugins: { legend: { display: false } },
    scales: { 
      x: { grid: { color: '#33415520' }, ticks: { color: '#64748b' } },
      y: { grid: { display: false }, ticks: { color: '#64748b' } }
    },
    maintainAspectRatio: false
  };

  if (!hasBreakdown && stage1Ms === 0 && stage2Ms === 0) return null;

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
      {hasBreakdown && (
        <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl p-6 shadow-sm">
          <h3 className="font-semibold text-lg mb-4">Comment Sentiment</h3>
          <div className="h-48 relative">
            <Doughnut data={sentimentData} options={chartOptions} />
          </div>
        </div>
      )}
      {(stage1Ms > 0 || stage2Ms > 0) && (
        <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl p-6 shadow-sm">
          <h3 className="font-semibold text-lg mb-4">Processing Latency</h3>
          <div className="h-48 relative">
            <Bar data={timeData} options={barOptions} />
          </div>
        </div>
      )}
    </div>
  );
}

export default function Trace() {
  const [posts, setPosts] = useState([]);
  const [notice, setNotice] = useState(null);

  // The trace itself lives in `utils/traceSession`, outside React. Tabs are
  // rendered as `{activeTab === 'trace' && <Trace />}`, so this component is
  // unmounted the moment the operator looks at anything else — holding the trace
  // in `useState` is what made the page come back blank, and what abandoned a
  // still-running trace on the way out.
  const session = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  const { postId, wantSummary, status, jobId, layers, tape, result, error } = session;
  const running = isRunning(status);

  // Loading the list and choosing a default are two concerns, and mixing them is
  // what made this un-declarable as a dependency: the fetch closed over
  // `selectedPostId`, so any honest dependency array refetched all 50 posts
  // every time the operator picked a different one from the dropdown. Split, the
  // fetch depends on nothing and the preselect is a separate effect over `posts`.
  const fetchPosts = useCallback(async () => {
    try {
      const data = await apiCall('/v1/analysis/latest?limit=50&include=results');
      const results = (data && data.results) || (Array.isArray(data) ? data : []);
      setPosts(results);
    } catch (err) {
      console.error('Failed to load trace posts', err);
    }
  }, []);

  // No cleanup that closes the stream: the session owns it, and a tab switch is
  // not a reason to stop following a post through the pipeline.
  useEffect(() => { fetchPosts(); }, [fetchPosts]);

  // Default to the first post, and only while nothing is selected. The store
  // keeps the operator's choice across mounts, so this fires once per session.
  useEffect(() => {
    if (posts.length > 0 && !getSnapshot().postId) {
      setPostId(posts[0].post_id || posts[0].id || '');
    }
  }, [posts]);

  // A reload drops the EventSource but not the job. Re-attaching replays the
  // buffered stage events, so the rail rebuilds instead of starting mid-post.
  useEffect(() => {
    if (hasResumableJob()) reattach();
  }, []);

  const handleRunTrace = async () => {
    setNotice(null);
    try {
      await startTrace({ postId, wantSummary });
    } catch (err) {
      setNotice(err.message);
    }
  };

  // The id may be one the recent-50 list does not contain — pasted from a ticket,
  // or carried over from the Posts tab. The dropdown then carries an extra option
  // for it rather than snapping the selection to something the operator did not
  // pick, which is what a plain `<select>` does with an unknown value.
  const known = posts.some(p => (p.post_id || p.id) === postId);
  const statusLabel = status === 'live' ? (jobId ? 'live' : 'starting') : status;

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">Trace One Post</h2>
        <p className="text-slate-500 dark:text-zinc-400">Deep inspection of individual LLM reasoning steps.</p>
      </div>

      <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm p-6">
        <div className="flex justify-between items-start mb-4">
          <div>
            <h3 className="font-semibold text-lg">Trace Configuration</h3>
            <p className="text-slate-500 text-sm">Re-runs a stored post through the real pipeline and follows it live. The trace keeps running while you are on another tab.</p>
          </div>
          <span className={`px-2 py-1 rounded-full text-xs font-medium uppercase tracking-wider ${
            running ? 'bg-brand-500 text-white animate-pulse' :
            status === 'done' ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400' :
            status === 'failed' ? 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400' :
            status === 'timeout' || status === 'cancelled' ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400' :
            'bg-slate-100 text-slate-700 dark:bg-zinc-800 dark:text-zinc-400'
          }`}>
            {statusLabel}
          </span>
        </div>
        
        <div className="flex flex-wrap items-end gap-4">
          <div className="flex flex-col flex-grow min-w-[200px]">
            <label className="text-xs font-semibold text-slate-500 mb-1" htmlFor="trace-post-select">Post</label>
            <select 
              id="trace-post-select"
              value={postId}
              onChange={(e) => setPostId(e.target.value)}
              className="px-3 py-3 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg w-full"
            >
              {/* The id carried in from the Posts tab, or pasted below, may not
                  be one of the recent 50 — and it is still selected. Without an
                  option to hold it the browser falls back to index -1 and the
                  control reads as empty while the trace is about to run on it. */}
              {postId && !known && (
                <option value={postId}>{postId} (by id)</option>
              )}
              {posts.length === 0 && !postId && (
                <option value="">Loading stored posts...</option>
              )}
              {posts.map((p, i) => {
                const pid = p.post_id || p.id || `Post ${i}`;
                const label = p.post_text || p.title || p.platform_post_id || pid;
                return <option key={pid} value={pid}>{label.slice(0, 60)}{label.length > 60 ? '...' : ''}</option>;
              })}
            </select>
          </div>
          <div className="flex flex-col min-w-[260px]">
            {/* The dropdown only holds the latest 50. An id out of a ticket, a
                log line or the Posts tab has to be traceable without hunting for
                it in a list it may not be in at all. */}
            <label className="text-xs font-semibold text-slate-500 mb-1" htmlFor="trace-post-id">Or paste a post id</label>
            <input
              id="trace-post-id"
              type="text"
              value={postId}
              spellCheck={false}
              placeholder="cmor32gy…"
              onChange={(e) => setPostId(e.target.value.trim())}
              className="px-3 py-3 text-sm font-mono bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg w-full"
            />
          </div>
          <label className="flex items-center gap-2 text-sm cursor-pointer mb-3" title="Forces the post through Stage 2 so a missing post summary is generated.">
            <input type="checkbox" checked={wantSummary} onChange={(e) => setWantSummary(e.target.checked)} className="rounded text-brand-500" />
            Request summary
          </label>
          <button 
            onClick={handleRunTrace}
            disabled={running || !postId}
            className="px-6 py-3 bg-brand-500 hover:bg-brand-600 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg font-medium transition-colors flex items-center gap-2"
          >
            {running && <Loader2 size={16} className="animate-spin" />}
            {running ? 'Tracing...' : 'Run Trace'}
          </button>
          <button 
            onClick={() => { setNotice(null); clearTrace(); }}
            className="px-6 py-3 bg-slate-100 dark:bg-zinc-800 hover:bg-slate-200 dark:hover:bg-zinc-700 text-slate-700 dark:text-slate-300 rounded-lg font-medium transition-colors"
          >
            Clear
          </button>
        </div>

        {(notice || error) && (
          <p role="alert" className="mt-4 text-sm text-rose-600 dark:text-rose-400">{notice || error}</p>
        )}
        {jobId && (
          <p className="mt-4 text-xs text-slate-500 dark:text-zinc-500 font-mono">job {jobId}</p>
        )}
      </div>

      <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm p-6">
        <h3 className="font-semibold text-lg mb-4">Layers</h3>
        {status === 'idle' ? (
          <div className="text-center text-slate-500 dark:text-zinc-500 py-8">
            <GitCommit size={48} className="mx-auto mb-4 opacity-20" />
            <p>Nothing traced yet — pick a post and press Run Trace.</p>
          </div>
        ) : (
          <div className="space-y-4">
            {TRACE_LAYERS.map(layer => {
              const state = layers[layer.key] || { status: 'idle' };
              const done = LAYER_DONE.has(state.status);
              const active = LAYER_ACTIVE.has(state.status);
              return (
                <div key={layer.key} className="flex items-center gap-4">
                  <div className={`w-32 text-right font-medium text-sm cursor-help ${
                    done ? 'text-emerald-600 dark:text-emerald-400' :
                    active ? 'text-brand-600 dark:text-brand-400' :
                    'text-slate-400 dark:text-zinc-600'
                  }`} title={layer.desc}>
                    {layer.label}
                  </div>
                  <div className="flex-grow bg-slate-50 dark:bg-[#121214] rounded px-4 py-2 text-sm font-mono text-slate-600 dark:text-slate-400 break-all">
                    {state.status === 'idle' ? 'waiting...' :
                     active && !state.detail ? 'processing...' :
                     JSON.stringify(state.detail)}
                  </div>
                  {typeof state.ms === 'number' && (
                    <div className="w-20 text-right text-xs text-slate-400 dark:text-zinc-500 shrink-0">{Math.round(state.ms)} ms</div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {result && (
        <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm p-6">
          <h3 className="font-semibold text-lg mb-4">Canonical Result</h3>
          <pre className="bg-slate-50 dark:bg-[#121214] p-4 rounded-lg overflow-x-auto text-xs font-mono text-slate-700 dark:text-slate-300">
            {JSON.stringify(result, null, 2)}
          </pre>
        </div>
      )}

      {(result || layers.stage1) && (
        <TraceVisuals result={result} traceState={layers} />
      )}

      {tape.length > 0 && (
        <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm p-6">
          <h3 className="font-semibold text-lg mb-4">Event Tape</h3>
          <div className="space-y-2">
            {tape.map((t, i) => (
              <div key={t.seq ?? `frame-${i}`} className="bg-slate-50 dark:bg-[#121214] p-2 rounded text-xs font-mono flex gap-4">
                <span className="text-slate-400 w-24 shrink-0">{t.stage || t.event || t.status}</span>
                <span className="text-slate-600 dark:text-slate-400 truncate">{JSON.stringify(t.detail || t)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
