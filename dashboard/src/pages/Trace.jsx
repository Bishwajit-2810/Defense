import React, { useState, useEffect, useRef } from 'react';
import { Activity, GitCommit, Play, Square } from 'lucide-react';
import { Chart as ChartJS, ArcElement, Tooltip, Legend, CategoryScale, LinearScale, BarElement, Title } from 'chart.js';
import { Doughnut, Bar } from 'react-chartjs-2';
import { apiCall, API_BASE, getSseQueryAsync } from '../utils/api.js';
import { orderedBreakdown, sentimentColors } from '../utils/sentiment';

ChartJS.register(ArcElement, Tooltip, Legend, CategoryScale, LinearScale, BarElement, Title);

const TRACE_LAYERS = [
  { key: 'ingest', label: 'Ingestion', desc: 'Consumes raw data from the stream.' },
  { key: 'stage1', label: 'Stage 1 (NLP & LLM)', desc: 'Extracts entities, metrics, and generates heavy LLM summarization.' },
  { key: 'router', label: 'Router', desc: 'Filters emoji-only spam and routes valid comments to Stage 2.' },
  { key: 'stage2', label: 'Stage 2 (Parallel)', desc: 'Runs LLM, XLM-R, and DistilBERT concurrently on valid comments. Checks Watchlist Alerts.' },
  { key: 'assembler', label: 'Assembler', desc: 'Merges partial results and writes final JSON to the database.' }
];

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
  const [selectedPostId, setSelectedPostId] = useState('');
  const [wantSummary, setWantSummary] = useState(true);
  const [status, setStatus] = useState('idle');
  const [jobId, setJobId] = useState(null);
  const [traceState, setTraceState] = useState({});
  const [tape, setTape] = useState([]);
  const [result, setResult] = useState(null);
  const eventSourceRef = useRef(null);

  useEffect(() => {
    fetchPosts();
    return () => stopTrace();
  }, []);

  const fetchPosts = async () => {
    try {
      const data = await apiCall('/v1/analysis/latest?limit=50&include=results');
      const results = (data && data.results) || (Array.isArray(data) ? data : []);
      setPosts(results);
      if (results.length > 0 && !selectedPostId) {
        setSelectedPostId(results[0].post_id || results[0].id);
      }
    } catch (err) {
      console.error('Failed to load trace posts', err);
    }
  };

  const stopTrace = () => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
  };

  const handleRunTrace = async () => {
    if (!selectedPostId) {
      alert('Pick a post to trace first.');
      return;
    }

    stopTrace();
    setTraceState(
      TRACE_LAYERS.reduce((acc, layer) => ({ ...acc, [layer.key]: { status: 'idle' } }), {})
    );
    setTape([]);
    setResult(null);
    setStatus('starting');

    try {
      const resp = await apiCall('/v1/analysis/run', {
        method: 'POST',
        body: JSON.stringify({
          post_ids: [selectedPostId],
          options: { want_summary: wantSummary }
        })
      });

      const newJobId = resp.analysis_id || resp.id || resp.job_id;
      if (!newJobId) throw new Error('No job ID in response');
      
      setJobId(newJobId);
      setStatus('live');
      
      const qs = await getSseQueryAsync();
      const es = new EventSource(`${API_BASE}/v1/analysis/${newJobId}/stream${qs}`);
      eventSourceRef.current = es;

      const handleEvent = (e) => {
        try {
          const data = JSON.parse(e.data);
          setTape(prev => [...prev, data]);
          
          if (data.status === 'completed' || data.status === 'failed') {
            setStatus(data.status === 'completed' ? 'done' : 'failed');
            es.close();
            if (data.status === 'completed') fetchFinalResult(newJobId);
          } else if (data.stage) {
            setTraceState(prev => ({
              ...prev,
              [data.stage]: { status: data.status, detail: data.detail || {} }
            }));
          }
        } catch (err) {}
      };

      es.addEventListener('progress', handleEvent);
      es.addEventListener('stage', handleEvent);
      
      es.addEventListener('done', (e) => {
        setStatus('done');
        es.close();
        fetchFinalResult(newJobId);
      });

      es.addEventListener('error', (e) => {
        setStatus('failed');
        es.close();
      });

      es.onerror = () => {
        // Fallback for network drops
        setStatus('failed');
        es.close();
      };
    } catch (err) {
      setStatus('failed');
      alert('Trace failed to start: ' + err.message);
    }
  };

  const fetchFinalResult = async (id) => {
    try {
      if (!id) return;
      const data = await apiCall(`/v1/analysis/${id}?include=results`);
      const finalRes = (data.results && data.results[0]) ? data.results[0] : data;
      setResult(finalRes);
    } catch (e) {}
  };

  const clearTrace = () => {
    stopTrace();
    setStatus('idle');
    setTape([]);
    setTraceState({});
    setResult(null);
    setJobId(null);
  };

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
            <p className="text-slate-500 text-sm">Re-runs a stored post through the real pipeline and follows it live.</p>
          </div>
          <span className={`px-2 py-1 rounded-full text-xs font-medium uppercase tracking-wider ${
            status === 'live' ? 'bg-brand-500 text-white animate-pulse' :
            status === 'done' ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400' :
            status === 'failed' ? 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400' :
            'bg-slate-100 text-slate-700 dark:bg-zinc-800 dark:text-zinc-400'
          }`}>
            {status}
          </span>
        </div>
        
        <div className="flex flex-wrap items-end gap-4">
          <div className="flex flex-col flex-grow min-w-[200px]">
            <label className="text-xs font-semibold text-slate-500 mb-1">Post</label>
            <select 
              value={selectedPostId}
              onChange={(e) => setSelectedPostId(e.target.value)}
              className="px-3 py-3 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg w-full"
            >
              {posts.length === 0 ? (
                <option value="">Loading stored posts...</option>
              ) : (
                posts.map((p, i) => {
                  const pid = p.post_id || p.id || `Post ${i}`;
                  const label = p.post_text || p.title || p.platform_post_id || pid;
                  return <option key={pid} value={pid}>{label.slice(0, 60)}{label.length > 60 ? '...' : ''}</option>;
                })
              )}
            </select>
          </div>
          <label className="flex items-center gap-2 text-sm cursor-pointer mb-3">
            <input type="checkbox" checked={wantSummary} onChange={(e) => setWantSummary(e.target.checked)} className="rounded text-brand-500" />
            Request summary
          </label>
          <button 
            onClick={handleRunTrace}
            disabled={status === 'live'}
            className="px-6 py-3 bg-brand-500 hover:bg-brand-600 text-white rounded-lg font-medium transition-colors"
          >
            {status === 'live' ? 'Tracing...' : 'Run Trace'}
          </button>
          <button 
            onClick={clearTrace}
            className="px-6 py-3 bg-slate-100 dark:bg-zinc-800 hover:bg-slate-200 dark:hover:bg-zinc-700 text-slate-700 dark:text-slate-300 rounded-lg font-medium transition-colors"
          >
            Clear
          </button>
        </div>
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
              const state = traceState[layer.key] || { status: 'idle' };
              return (
                <div key={layer.key} className="flex items-center gap-4">
                  <div className={`w-32 text-right font-medium text-sm cursor-help ${
                    state.status === 'completed' ? 'text-emerald-600 dark:text-emerald-400' :
                    state.status === 'processing' ? 'text-brand-600 dark:text-brand-400' :
                    'text-slate-400 dark:text-zinc-600'
                  }`} title={layer.desc}>
                    {layer.label}
                  </div>
                  <div className="flex-grow bg-slate-50 dark:bg-[#121214] rounded px-4 py-2 text-sm font-mono text-slate-600 dark:text-slate-400">
                    {state.status === 'idle' ? 'waiting...' :
                     state.status === 'processing' ? 'processing...' :
                     JSON.stringify(state.detail)}
                  </div>
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

      {(result || (traceState && traceState.stage1)) && (
        <TraceVisuals result={result} traceState={traceState} />
      )}

      {tape.length > 0 && (
        <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm p-6">
          <h3 className="font-semibold text-lg mb-4">Event Tape</h3>
          <div className="space-y-2">
            {tape.map((t, i) => (
              <div key={i} className="bg-slate-50 dark:bg-[#121214] p-2 rounded text-xs font-mono flex gap-4">
                <span className="text-slate-400 w-24 shrink-0">{t.stage || t.status}</span>
                <span className="text-slate-600 dark:text-slate-400 truncate">{JSON.stringify(t.detail || t)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
