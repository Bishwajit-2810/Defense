import React, { useState, useEffect, useRef } from 'react';
import { Play, FileText, CheckCircle2, Clock, AlertCircle, Download, Square, Trash2, RotateCw } from 'lucide-react';
import { apiCall, API_BASE, getAuthHeaders, getSseQueryAsync } from '../utils/api.js';

// Wording for the shared confirm dialog. `dismiss` is per-action because a
// button reading "Cancel" beside "Stop Job" is ambiguous — cancelling is also
// the name of what Stop does. The stop and delete bodies state what
// the backend actually guarantees — a stop cannot recall the posts already
// inside a stage, and a delete removes the job record, not the posts' analyses.
const CONFIRM_COPY = {
  rerun: {
    title: 'Confirm Re-run',
    body: 'Re-analyses the same posts as a new job. The existing results are overwritten as the new ones land.',
    confirm: 'Re-run Job',
    dismiss: 'Cancel',
    btnClass: 'bg-brand-500 hover:bg-brand-600',
  },
  stop: {
    title: 'Stop this job?',
    body: 'No further post is analysed. Posts already inside a pipeline stage finish and are saved, so progress may tick up once or twice more. The job is marked cancelled and can be re-run later.',
    confirm: 'Stop Job',
    dismiss: 'Keep Running',
    btnClass: 'bg-amber-500 hover:bg-amber-600',
  },
  delete: {
    title: 'Delete this job?',
    body: 'Removes the job record, its progress counters and its live trace. If it is still running it is stopped first. The posts and their analysis results are kept — delete a post from the Posts tab to remove those.',
    confirm: 'Delete Job',
    dismiss: 'Keep Job',
    btnClass: 'bg-rose-600 hover:bg-rose-700',
  },
};

// Mirrors `_STALE_JOB_SECONDS` in routers/analysis.py. A job that stops writing
// progress for this long is shown as stalled rather than running — after a power
// cut the row keeps saying "running" forever, and that is the single most
// misleading thing this table can display. The API applies the same threshold, so
// a Resume offered on a stalled row is one the API will accept.
const STALE_AFTER_MS = 5 * 60 * 1000;

// The list endpoint returns `id`; the run endpoint returns `analysis_id` and
// older rows have carried `job_id`. Read them through one helper so the stop and
// delete calls can never address a different job than the row they sit in.
const jobIdOf = (job) => job?.id || job?.job_id || job?.analysis_id || '';

export default function AnalysisJobs() {
  const [jobs, setJobs] = useState([]);
  const [campaignId, setCampaignId] = useState('');
  const [wantSummary, setWantSummary] = useState(true);
  const [loading, setLoading] = useState(false);
  const [liveProgress, setLiveProgress] = useState(null);
  
  // Modals state. `action` is 'rerun' | 'stop' | 'delete' — one confirm dialog
  // for all three, since they differ only in wording and the call they make.
  const [confirmModal, setConfirmModal] = useState({ isOpen: false, job: null, action: 'rerun' });
  const [resultModal, setResultModal] = useState({ isOpen: false, data: null });
  const [busyJob, setBusyJob] = useState(null);

  // What the last stop/delete/resume did. Kept separate from `liveProgress`
  // because that line belongs to the SSE stream and is rewritten by every frame
  // — resuming a job subscribes to its stream immediately, which used to wipe
  // the "30/300 already done, 270 re-queued" summary before it could be read.
  const [notice, setNotice] = useState(null);

  // Open EventSources by job id. Stopping a job has to close its stream here:
  // the server sends one terminating `cancelled` frame and then goes quiet, and
  // a stream left open would keep the row's "Track" state alive for 5 minutes.
  const streamsRef = useRef({});

  useEffect(() => {
    fetchJobs();
    
    const handleAutoRefresh = () => fetchJobs();
    window.addEventListener('auto-refresh', handleAutoRefresh);
    return () => {
      window.removeEventListener('auto-refresh', handleAutoRefresh);
      Object.values(streamsRef.current).forEach(es => { try { es.close(); } catch (e) {} });
      streamsRef.current = {};
    };
  }, []);

  const fetchJobs = async () => {
    try {
      const data = await apiCall('/v1/analysis?limit=25');
      let jobsArr = [];
      if (Array.isArray(data)) jobsArr = data;
      else if (data && Array.isArray(data.jobs)) jobsArr = data.jobs;
      else if (data && Array.isArray(data.results)) jobsArr = data.results;
      
      setJobs(jobsArr);
    } catch (err) {
      console.error(err);
      // Fallback if /v1/analysis/jobs fails, maybe it's /v1/analysis
      try {
        const data = await apiCall('/v1/analysis?limit=25');
        let jobsArr = [];
        if (Array.isArray(data)) jobsArr = data;
        else if (data && Array.isArray(data.jobs)) jobsArr = data.jobs;
        else if (data && Array.isArray(data.results)) jobsArr = data.results;
        setJobs(jobsArr);
      } catch(e) {}
    }
  };

  const handleRunAnalysis = async () => {
    setLoading(true);
    setLiveProgress('Starting job...');
    try {
      const body = {
        options: { tasks: ['all'], want_summary: wantSummary, summary_lang: 'auto', llm_backend: 'auto' }
      };
      if (campaignId.trim()) body.campaign_id = campaignId.trim();

      const data = await apiCall('/v1/analysis/run', {
        method: 'POST',
        body: JSON.stringify(body)
      });
      
      const jobId = data.job_id || data.analysis_id;
      if (jobId) {
        setLiveProgress(`Job ${jobId} started. Connecting to stream...`);
        subscribeToJobProgress(jobId);
      } else {
        setLiveProgress('Job started, but no job_id returned.');
      }
      fetchJobs();
    } catch (err) {
      setLiveProgress('Error: ' + err.message);
    } finally {
      setLoading(false);
    }
  };

  const askConfirm = (job, action) => {
    setNotice(null);
    setConfirmModal({ isOpen: true, job, action });
  };

  const closeStream = (jobId) => {
    const es = streamsRef.current[jobId];
    if (es) {
      try { es.close(); } catch (e) {}
      delete streamsRef.current[jobId];
    }
  };

  const handleStopJob = async (job) => {
    const jid = jobIdOf(job);
    setBusyJob(jid);
    try {
      const res = await apiCall(`/v1/analysis/${jid}/cancel`, { method: 'POST' });
      closeStream(jid);
      // Posts already inside a stage finish, so this is "no new work", not
      // "everything halted this instant" — say so rather than implying a
      // guarantee the pipeline cannot make.
      setNotice({ kind: 'ok', text: `Job ${jid.split('-')[0]}... stopped — in-flight posts will finish, the rest are skipped.` });
      setJobs(prev => prev.map(j => (jobIdOf(j) === jid ? { ...j, status: res.status || 'cancelled' } : j)));
    } catch (err) {
      setNotice({ kind: 'error', text: err.message });
    } finally {
      setBusyJob(null);
      fetchJobs();
    }
  };

  const handleResumeJob = async (job) => {
    const jid = jobIdOf(job);
    setBusyJob(jid);
    setNotice({ kind: 'ok', text: `Resuming job ${jid.split('-')[0]}...` });
    try {
      const res = await apiCall(`/v1/analysis/${jid}/resume`, { method: 'POST' });
      const p = res.progress || {};
      if (res.resumed) {
        // Report what it actually re-queued. "Resumed" alone would leave the
        // operator unable to tell a 270-post continuation from a no-op.
        setNotice({ kind: 'ok', text:
          `Resumed — ${p.completed}/${p.total} already done, ${p.remaining} re-queued.` });
        subscribeToJobProgress(jid);
      } else {
        setNotice({ kind: 'ok', text: `Nothing to resume — ${res.reason || 'the job is already complete'}.` });
      }
    } catch (err) {
      setNotice({ kind: 'error', text: err.message });
    } finally {
      setBusyJob(null);
      fetchJobs();
    }
  };

  const handleDeleteJob = async (job) => {
    const jid = jobIdOf(job);
    setBusyJob(jid);
    try {
      const res = await apiCall(`/v1/analysis/${jid}`, { method: 'DELETE' });
      closeStream(jid);
      setJobs(prev => prev.filter(j => jobIdOf(j) !== jid));
      setNotice({ kind: 'ok', text: res.stopped
        ? `Job ${jid.split('-')[0]}... stopped and deleted. Post analyses are kept.`
        : `Job ${jid.split('-')[0]}... deleted. Post analyses are kept.` });
    } catch (err) {
      setNotice({ kind: 'error', text: err.message });
      fetchJobs();
    } finally {
      setBusyJob(null);
    }
  };

  const handleConfirm = async () => {
    const { job, action } = confirmModal;
    setConfirmModal({ isOpen: false, job: null, action: 'rerun' });
    if (!job) return;
    if (action === 'stop') return handleStopJob(job);
    if (action === 'delete') return handleDeleteJob(job);
    return handleRerunJob(job);
  };

  const handleRerunJob = async (job) => {
    if (!job) return;
    
    setLoading(true);
    setLiveProgress(`Re-running job ${jobIdOf(job)}...`);
    window.scrollTo({top: 0, behavior: 'smooth'});
    try {
      const body = {
        options: { tasks: ['all'], want_summary: wantSummary, summary_lang: 'auto', llm_backend: 'auto' }
      };
      if (job.campaign_id) body.campaign_id = job.campaign_id;
      if (job.post_ids && job.post_ids.length > 0) body.post_ids = job.post_ids;

      const data = await apiCall('/v1/analysis/run', {
        method: 'POST',
        body: JSON.stringify(body)
      });
      
      const jobId = data.job_id || data.analysis_id;
      if (jobId) {
        setLiveProgress(`Job ${jobId} started. Connecting to stream...`);
        subscribeToJobProgress(jobId);
      } else {
        setLiveProgress('Job started, but no job_id returned.');
      }
      fetchJobs();
    } catch (err) {
      setLiveProgress('Error: ' + err.message);
    } finally {
      setLoading(false);
    }
  };

  const fetchFinalResult = async (id) => {
    try {
      const data = await apiCall(`/v1/analysis/${id}?include=results`);
      const finalRes = (data.results && data.results[0]) ? data.results[0] : data;
      setResultModal({ isOpen: true, data: finalRes });
    } catch (e) {
      console.error(e);
    }
  };

  const downloadJobReport = async (campaignId) => {
    try {
      const campaign = campaignId || 'all';
      const res = await fetch(`${API_BASE}/v1/reports/export_latest?campaign_id=${encodeURIComponent(campaign)}&format=pdf`, {
        headers: getAuthHeaders()
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || 'Report export failed');
      }
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `analysis_report_${campaign}.pdf`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (err) {
      alert('Error downloading report: ' + err.message);
    }
  };

  const subscribeToJobProgress = async (jobId) => {
    setLiveProgress(`Job ${jobId} tracking. Connecting to stream...`);
    const qs = await getSseQueryAsync();
    closeStream(jobId);
    const es = new EventSource(`${API_BASE}/v1/analysis/${jobId}/stream${qs}`);
    streamsRef.current[jobId] = es;

    const handleEvent = (e) => {
      try {
        const data = JSON.parse(e.data);
        
        // Update job state if it's related to this job
        setJobs(prev => prev.map(job => {
          if (job.id === jobId || job.job_id === jobId) {
            return { 
              ...job, 
              status: data.status || job.status, 
              error: data.error || data.detail?.error || job.error,
              completed: data.completed !== undefined ? data.completed : job.completed,
              total: data.total !== undefined ? data.total : job.total
            };
          }
          return job;
        }));
        
        if (e.type === 'progress' && data.total > 0) {
          const p = Math.round((data.completed / data.total) * 100);
          setLiveProgress(`Progress: ${p}% (${data.completed}/${data.total}) - ${data.status || 'running'}`);
        } else if (e.type === 'stage') {
          setLiveProgress(`Progress: 0% Stage updated: ${data.stage || data.status || 'processing'}`);
        } else if (e.type === 'done') {
          setLiveProgress(`Progress: 100% Job complete!`);
          fetchFinalResult(jobId);
        } else if (e.type === 'cancelled') {
          setLiveProgress(`Job stopped — ${data.completed || 0}/${data.total ?? '?'} posts had finished.`);
          closeStream(jobId);
          fetchJobs();
        } else if (e.type === 'error') {
          setLiveProgress(`Error: ${data.error || 'Unknown error occurred'}`);
        } else if (e.type === 'connected') {
          setLiveProgress(`Progress: 0% Connected. Waiting for progress...`);
        }
      } catch (err) {}
    };

    es.addEventListener('progress', handleEvent);
    es.addEventListener('stage', handleEvent);
    es.addEventListener('done', handleEvent);
    es.addEventListener('cancelled', handleEvent);
    es.addEventListener('error', handleEvent);
    es.addEventListener('connected', handleEvent);

    es.onerror = () => {
      // Transport-level failure (not the `error` event above). This used to call
      // a `pollStatus` that does not exist in this file, so a dropped stream
      // threw a ReferenceError instead of falling back to anything.
      closeStream(jobId);
      fetchJobs();
    };
  };

  const renderProgressBar = (progress) => {
    return (
      <div className="flex items-center gap-2">
        <div className="w-24 h-2 bg-slate-200 dark:bg-zinc-700 rounded-full overflow-hidden">
          <div className="h-full bg-brand-500" style={{ width: `${progress}%` }}></div>
        </div>
        <span className="text-xs text-slate-500">{progress}%</span>
      </div>
    );
  };

  const renderLiveProgress = () => {
    if (!liveProgress) return null;
    let text = liveProgress;
    let percent = undefined;
    
    // Parse "Progress: 10% (1/10) - running"
    const match = text.match(/^Progress: (\d+)%\s*(.*)$/);
    if (match) {
      percent = parseInt(match[1], 10);
      text = `Processing... ${match[2]}`;
    }

    return (
      <div className="mt-4 p-4 bg-brand-50 dark:bg-brand-900/10 border border-brand-200 dark:border-brand-900/50 rounded-lg flex flex-col gap-3 transition-all duration-300">
        <div className="flex justify-between items-center text-brand-700 dark:text-brand-400 text-sm font-semibold">
          <span>{text}</span>
          {percent !== undefined && <span>{percent}%</span>}
        </div>
        <div className="w-full h-2 bg-brand-200/50 dark:bg-brand-900/30 rounded-full overflow-hidden">
          <div 
            className={`h-full bg-brand-500 rounded-full transition-all duration-500 ${percent === undefined && !text.startsWith('Error') ? 'w-full animate-pulse' : (text.startsWith('Error') ? 'w-full bg-rose-500' : '')}`}
            style={{ width: percent !== undefined ? `${percent}%` : '100%' }}
          ></div>
        </div>
      </div>
    );
  };

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">Analysis Jobs</h2>
        <p className="text-slate-500 dark:text-zinc-400">Trigger (re-)analysis and monitor active jobs.</p>
      </div>

      <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm p-6">
        <h3 className="font-semibold text-lg mb-2">Run Analysis</h3>
        <p className="text-slate-500 text-sm mb-4">Trigger (re-)analysis for already-ingested posts by campaign — progress streams live below</p>
        
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex flex-col">
            <label className="text-xs font-semibold text-slate-500 mb-1">Campaign ID</label>
            <input 
              type="text" 
              placeholder="e.g. cmoldmxzr02..." 
              value={campaignId}
              onChange={e => setCampaignId(e.target.value)}
              className="px-3 py-2 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg w-64"
            />
          </div>
          <div className="flex items-center gap-2 mt-5">
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input type="checkbox" checked={wantSummary} onChange={e => setWantSummary(e.target.checked)} className="rounded text-brand-500" />
              LLM summaries
            </label>
            <button 
              onClick={handleRunAnalysis}
              disabled={loading}
              className="px-4 py-2 bg-brand-500 hover:bg-brand-600 text-white rounded-lg text-sm font-medium transition-colors"
            >
              Run Analysis
            </button>
          </div>
        </div>
        
        {notice && (
          <div className={`mt-4 p-3 rounded-lg text-sm flex items-start justify-between gap-3 border ${
            notice.kind === 'error'
              ? 'bg-rose-50 dark:bg-rose-900/10 border-rose-200 dark:border-rose-900/50 text-rose-700 dark:text-rose-400'
              : 'bg-emerald-50 dark:bg-emerald-900/10 border-emerald-200 dark:border-emerald-900/50 text-emerald-700 dark:text-emerald-400'
          }`}>
            <span>{notice.text}</span>
            <button onClick={() => setNotice(null)} className="opacity-60 hover:opacity-100 shrink-0" aria-label="Dismiss">✕</button>
          </div>
        )}

        {renderLiveProgress()}
      </div>

      <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm">
        <div className="p-4 border-b border-slate-200 dark:border-zinc-800 bg-slate-50 dark:bg-[#121214] flex justify-between items-center">
          <h3 className="font-semibold">Jobs</h3>
          <button onClick={fetchJobs} className="px-3 py-1.5 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg hover:bg-slate-100 dark:hover:bg-zinc-800">Refresh</button>
        </div>
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-200 dark:divide-zinc-800 text-left">
            <thead className="bg-slate-50 dark:bg-[#121214]">
              <tr>
                <th scope="col" className="px-6 py-3 text-xs font-medium text-slate-500 uppercase tracking-wider">Job ID</th>
                <th scope="col" className="px-6 py-3 text-xs font-medium text-slate-500 uppercase tracking-wider">Type</th>
                <th scope="col" className="px-6 py-3 text-xs font-medium text-slate-500 uppercase tracking-wider">Campaign ID</th>
                <th scope="col" className="px-6 py-3 text-xs font-medium text-slate-500 uppercase tracking-wider">Posts</th>
                <th scope="col" className="px-6 py-3 text-xs font-medium text-slate-500 uppercase tracking-wider">Status</th>
                <th scope="col" className="px-6 py-3 text-xs font-medium text-slate-500 uppercase tracking-wider">Progress</th>
                <th scope="col" className="px-6 py-3 text-xs font-medium text-slate-500 uppercase tracking-wider">Started</th>
                <th scope="col" className="px-6 py-3 text-xs font-medium text-slate-500 uppercase tracking-wider">Actions</th>
              </tr>
            </thead>
            <tbody className="bg-white dark:bg-[#09090b] divide-y divide-slate-200 dark:divide-zinc-800">
              {jobs.length === 0 ? (
                <tr>
                  <td colSpan="8" className="px-6 py-12 text-center text-slate-500 dark:text-zinc-500">
                    No jobs loaded yet.
                  </td>
                </tr>
              ) : (
                jobs.map((job, i) => {
                  const jid = job.job_id || job.id || job.analysis_id || `Job ${i + 1}`;
                  const type = job.job_type || job.type || 'analysis';
                  const status = job.status || 'unknown';
                  const completed = job.completed || 0;
                  const total = job.total || 0;
                  const isFinished = status === 'completed' || status === 'done';
                  // Only a job that is actually still moving can be stopped or
                  // tracked — a cancelled or failed one has nothing to stop.
                  const isRunning = !isFinished && status !== 'failed' && status !== 'cancelled';
                  // A job whose row says "running" but has written nothing for
                  // five minutes is what a power cut leaves behind.
                  const lastUpdate = job.updated_at ? Date.parse(job.updated_at) : NaN;
                  const isStalled = isRunning && !Number.isNaN(lastUpdate)
                    && (Date.now() - lastUpdate) > STALE_AFTER_MS;
                  // Resume is offered on anything unfinished. A job that turns
                  // out to be alive is refused by the API with a 409 that says
                  // to stop it first, which is surfaced verbatim.
                  const canResume = !isFinished;
                  const progress = total > 0 ? Math.round((completed / total) * 100) : (isFinished ? 100 : 0);
                  const busy = busyJob === jid;
                  
                  return (
                    <tr key={jid} className="hover:bg-slate-50 dark:hover:bg-zinc-900/50 transition-colors">
                      <td className="px-6 py-4 whitespace-nowrap text-sm font-medium font-mono text-xs text-slate-500">{jid.split('-')[0] + '...'}</td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm"><span className="px-2 py-1 rounded bg-slate-100 dark:bg-zinc-800 text-xs">{type}</span></td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm font-medium font-mono text-xs">
                        {job.campaign_id ? (
                          <button 
                            onClick={() => { setCampaignId(job.campaign_id); window.scrollTo({top: 0, behavior: 'smooth'}); }}
                            className="hover:text-brand-500 transition-colors title='Click to use this Campaign ID'"
                          >
                            {job.campaign_id}
                          </button>
                        ) : '-'}
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-slate-500">{job.total || job.post_count || '-'}</td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm">
                        <span className={`px-2 py-1 rounded-full text-xs font-medium ${
                          isFinished ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400' :
                          status === 'failed' ? 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400' :
                          status === 'cancelled' ? 'bg-zinc-200 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400' :
                          isStalled ? 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400' :
                          status === 'processing' || status === 'running' ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400' :
                          'bg-slate-100 text-slate-700 dark:bg-zinc-800 dark:text-zinc-300'
                        }`}>
                          {isStalled ? 'stalled' : status}
                        </span>
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm">
                        {renderProgressBar(progress)}
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap text-xs text-slate-500">
                        {job.started_at ? new Date(job.started_at).toLocaleString() : (job.created_at ? new Date(job.created_at).toLocaleString() : '-')}
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm">
                        <div className="flex flex-col gap-2">
                          {isRunning && (
                            <button onClick={() => subscribeToJobProgress(jid)} className="text-brand-500 hover:text-brand-600 text-xs font-semibold text-left">Track</button>
                          )}
                          {isRunning && (
                            <button
                              onClick={() => askConfirm(job, 'stop')}
                              disabled={busy}
                              className="text-amber-600 dark:text-amber-500 hover:text-amber-700 dark:hover:text-amber-400 text-xs font-semibold text-left flex items-center gap-1 disabled:opacity-50"
                            >
                              <Square size={12} /> Stop
                            </button>
                          )}
                          {canResume && (
                            <button
                              onClick={() => handleResumeJob(job)}
                              disabled={busy}
                              title="Re-queue only the posts this job never finished"
                              className="text-emerald-600 dark:text-emerald-500 hover:text-emerald-700 dark:hover:text-emerald-400 text-xs font-semibold text-left flex items-center gap-1 disabled:opacity-50"
                            >
                              <RotateCw size={12} /> Resume
                            </button>
                          )}
                          {isFinished && (
                            <button onClick={() => downloadJobReport(job.campaign_id)} className="text-brand-500 hover:text-brand-600 text-xs font-semibold text-left flex items-center gap-1">
                              <Download size={12} /> Download Report
                            </button>
                          )}
                          <button onClick={() => askConfirm(job, 'rerun')} className="text-slate-500 hover:text-brand-500 text-xs font-semibold text-left">Re-run</button>
                          <button
                            onClick={() => askConfirm(job, 'delete')}
                            disabled={busy}
                            className="text-rose-600 dark:text-rose-500 hover:text-rose-700 dark:hover:text-rose-400 text-xs font-semibold text-left flex items-center gap-1 disabled:opacity-50"
                          >
                            <Trash2 size={12} /> Delete
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Confirm Re-run / Stop / Delete Modal */}
      {confirmModal.isOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm animate-in fade-in duration-200">
          <div className="bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-xl shadow-xl w-full max-w-md p-6">
            <h3 className="text-lg font-bold mb-4">{CONFIRM_COPY[confirmModal.action].title}</h3>
            <p className="text-slate-600 dark:text-zinc-400 mb-2">
              Job <span className="font-mono text-brand-500">{jobIdOf(confirmModal.job).split('-')[0]}...</span>
            </p>
            <p className="text-slate-600 dark:text-zinc-400 mb-6 text-sm">
              {CONFIRM_COPY[confirmModal.action].body}
            </p>
            <div className="flex justify-end gap-3">
              <button 
                onClick={() => setConfirmModal({ isOpen: false, job: null, action: 'rerun' })}
                className="px-4 py-2 bg-slate-100 hover:bg-slate-200 dark:bg-zinc-800 dark:hover:bg-zinc-700 rounded-lg text-sm font-medium transition-colors"
              >
                {CONFIRM_COPY[confirmModal.action].dismiss}
              </button>
              <button 
                onClick={handleConfirm}
                className={`px-4 py-2 text-white rounded-lg text-sm font-medium transition-colors ${CONFIRM_COPY[confirmModal.action].btnClass}`}
              >
                {CONFIRM_COPY[confirmModal.action].confirm}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Result Display Modal */}
      {resultModal.isOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm animate-in fade-in duration-200 p-4">
          <div className="bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-xl shadow-xl w-full max-w-3xl max-h-[80vh] flex flex-col">
            <div className="flex justify-between items-center p-6 border-b border-slate-200 dark:border-zinc-800">
              <h3 className="text-lg font-bold">Analysis Result</h3>
              <button onClick={() => setResultModal({ isOpen: false, data: null })} className="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200">
                <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
              </button>
            </div>
            <div className="p-6 overflow-y-auto">
              {resultModal.data?.summary || resultModal.data?.analysis ? (
                <div className="space-y-4">
                  {resultModal.data.summary && (
                    <div>
                      <h4 className="font-semibold text-brand-500 mb-2">Campaign Summary</h4>
                      <p className="text-sm whitespace-pre-wrap">{resultModal.data.summary}</p>
                    </div>
                  )}
                  {resultModal.data.analysis && (
                    <div>
                      <h4 className="font-semibold text-brand-500 mb-2">Detailed Analysis</h4>
                      <pre className="text-xs p-4 bg-slate-100 dark:bg-zinc-950 rounded-lg overflow-x-auto whitespace-pre-wrap">
                        {typeof resultModal.data.analysis === 'string' ? resultModal.data.analysis : JSON.stringify(resultModal.data.analysis, null, 2)}
                      </pre>
                    </div>
                  )}
                </div>
              ) : (
                <pre className="text-xs p-4 bg-slate-100 dark:bg-zinc-950 rounded-lg overflow-x-auto">
                  {JSON.stringify(resultModal.data, null, 2)}
                </pre>
              )}
            </div>
            <div className="p-4 border-t border-slate-200 dark:border-zinc-800 bg-slate-50 dark:bg-zinc-950 flex justify-between items-center">
              <button 
                onClick={() => downloadJobReport(resultModal.data?.campaign_id)}
                className="px-4 py-2 bg-slate-100 dark:bg-zinc-800 hover:bg-slate-200 dark:hover:bg-zinc-700 text-slate-700 dark:text-zinc-200 border border-slate-200 dark:border-zinc-700 rounded-lg text-sm font-medium flex items-center gap-2 transition-colors"
              >
                <Download size={15} /> Download Mass Reaction Report (PDF)
              </button>
              <button 
                onClick={() => setResultModal({ isOpen: false, data: null })}
                className="px-4 py-2 bg-brand-500 hover:bg-brand-600 text-white rounded-lg text-sm font-medium transition-colors"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
