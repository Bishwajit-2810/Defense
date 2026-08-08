import React, { useState, useEffect, useRef } from 'react';
import { Database, Brain, Cpu, MessageSquare, Save, Activity, ChevronRight, Terminal, CheckCircle2 } from 'lucide-react';
import { API_BASE, apiCall, getSseQueryAsync } from '../utils/api.js';

export default function Pipeline() {
  const [logs, setLogs] = useState([]);
  const [isFollowing, setIsFollowing] = useState(true);
  const [pipelineStats, setPipelineStats] = useState(null);
  const [liveStatus, setLiveStatus] = useState('connecting');
  const logsEndRef = useRef(null);
  const logStreamRef = useRef(null);
  const pipelineStreamRef = useRef(null);
  const pollTimerRef = useRef(null);

  const connectLogStream = async () => {
    if (logStreamRef.current) return;
    const qs = await getSseQueryAsync();
    const ampersand = qs ? '&' : '?';
    const es = new EventSource(`${API_BASE}/v1/logs/stream${qs}${ampersand}level=DEBUG&format=json`);
    
    es.addEventListener('log', (e) => {
      try {
        const parsed = JSON.parse(e.data);
        setLogs(prev => [...prev.slice(-99), parsed]);
      } catch (err) {}
    });
    
    logStreamRef.current = es;
  };

  const connectPipelineStream = async () => {
    if (pipelineStreamRef.current || pollTimerRef.current) return;
    setLiveStatus('connecting');

    const qs = await getSseQueryAsync();
    const es = new EventSource(`${API_BASE}/v1/pipeline/stream${qs}`);
    
    es.addEventListener('connected', () => setLiveStatus('live'));
    es.addEventListener('stats', (e) => {
      try {
        setPipelineStats(JSON.parse(e.data));
        setLiveStatus('live');
      } catch (err) {}
    });
    
    es.addEventListener('error', () => {
      if (es.readyState === EventSource.CLOSED) {
        es.close();
        pipelineStreamRef.current = null;
        startPolling();
      }
    });

    pipelineStreamRef.current = es;
  };

  const startPolling = () => {
    if (pollTimerRef.current) return;
    const tick = async () => {
      try {
        const stats = await apiCall('/v1/pipeline/stats');
        setPipelineStats(stats);
        setLiveStatus('polling');
      } catch (e) {
        setLiveStatus('offline');
      }
    };
    tick();
    pollTimerRef.current = setInterval(tick, 2000);
  };

  const cleanup = () => {
    if (logStreamRef.current) { logStreamRef.current.close(); logStreamRef.current = null; }
    if (pipelineStreamRef.current) { pipelineStreamRef.current.close(); pipelineStreamRef.current = null; }
    if (pollTimerRef.current) { clearInterval(pollTimerRef.current); pollTimerRef.current = null; }
  };

  useEffect(() => {
    connectLogStream();
    connectPipelineStream();
    return cleanup;
  }, []);

  useEffect(() => {
    if (isFollowing && logsEndRef.current) {
      logsEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logs, isFollowing]);

  const defaultNodes = [
    { label: 'Ingestion' },
    { label: 'Stage 1 (NLP)' },
    { label: 'Router' },
    { label: 'Stage 2 (LLM)' },
    { label: 'Assembler' }
  ];

  const stages = pipelineStats?.stages || defaultNodes;

  return (
    <div className="space-y-8 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">Intelligence Pipeline</h2>
        <p className="text-slate-500 dark:text-zinc-400">Live data flow architecture and log stream.</p>
      </div>

      <div className="bg-white dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 rounded-2xl p-8 shadow-sm">
        <div className="flex justify-between items-center mb-8">
          <h3 className="text-sm font-semibold uppercase tracking-wider text-slate-500 dark:text-zinc-500 flex items-center gap-2">
            <Activity size={16} className={liveStatus === 'live' ? 'text-brand-500 animate-pulse' : 'text-slate-400'} />
            Active Data Flow
          </h3>
          <span className={`px-2 py-1 rounded text-xs font-medium uppercase tracking-wider ${
            liveStatus === 'live' ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400' :
            liveStatus === 'polling' ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400' :
            liveStatus === 'offline' ? 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400' :
            'bg-slate-100 text-slate-700 dark:bg-zinc-800 dark:text-zinc-400'
          }`}>
            {liveStatus}
          </span>
        </div>
        
        <div className="flex flex-col lg:flex-row items-center justify-between gap-4 lg:gap-2">
          {stages.map((stage, i) => {
            const active = (stage.in_flight || 0) > 0;
            const waiting = (stage.backlog || 0) > 0;
            const hasDlq = (stage.dlq || 0) > 0;
            const next = stages[i + 1];
            const flowing = active || waiting || (next && (next.in_flight || 0) > 0);

            return (
              <React.Fragment key={i}>
                <div className={`relative flex flex-col p-4 rounded-xl w-40 min-h-[120px] transition-all duration-500 border ${
                  active ? 'border-brand-500/50 bg-brand-50/50 dark:bg-brand-900/10 shadow-sm' : 
                  'border-slate-200 dark:border-zinc-800 bg-slate-50 dark:bg-[#09090b]'
                }`}>
                  <div className={`font-bold text-sm text-center mb-3 ${active ? 'text-brand-600 dark:text-brand-400' : 'text-slate-700 dark:text-zinc-300'}`}>
                    {stage.label}
                  </div>
                  <div className="flex flex-col gap-1 mt-auto text-xs">
                    <div className="flex justify-between items-center text-brand-600 dark:text-brand-400">
                      <span>In Flight:</span>
                      <span className="font-mono font-bold">{stage.in_flight || 0}</span>
                    </div>
                    <div className="flex justify-between items-center text-slate-500">
                      <span>Backlog:</span>
                      <span className="font-mono">{stage.backlog || 0}</span>
                    </div>
                    {hasDlq && (
                      <div className="flex justify-between items-center text-rose-500 mt-1 pt-1 border-t border-rose-100 dark:border-rose-900/30">
                        <span>Failed:</span>
                        <span className="font-mono font-bold">{stage.dlq}</span>
                      </div>
                    )}
                  </div>
                </div>
                
                <div className="hidden lg:flex items-center">
                  <ChevronRight size={24} className={`transition-colors duration-500 ${flowing ? 'text-brand-500' : 'text-slate-200 dark:text-zinc-800'}`} />
                </div>
                <div className="lg:hidden h-8 border-l-2 border-dashed border-slate-300 dark:border-zinc-700"></div>
              </React.Fragment>
            );
          })}

          <div className="relative flex flex-col items-center justify-center p-4 rounded-xl w-32 min-h-[120px] border border-emerald-200 dark:border-emerald-900/50 bg-emerald-50 dark:bg-emerald-900/10 shadow-sm text-emerald-700 dark:text-emerald-400">
            <CheckCircle2 size={24} className="mb-2" />
            <div className="font-bold text-sm text-center mb-1">Completed</div>
            <div className="font-mono font-bold text-lg">{pipelineStats?.completed || 0}</div>
          </div>
        </div>
      </div>

      <div className="bg-[#0a0a0c] border border-brand-500/30 rounded-2xl overflow-hidden shadow-2xl shadow-brand-500/10 flex flex-col h-[400px] relative group">
        <div className="absolute inset-0 bg-gradient-to-b from-transparent to-brand-500/5 pointer-events-none"></div>
        
        <div className="bg-[#0f1115] border-b border-brand-500/20 px-4 py-3 flex justify-between items-center z-10">
          <div className="flex items-center gap-2">
            <Terminal size={16} className="text-brand-500" />
            <span className="text-brand-500 font-mono text-xs font-bold tracking-widest uppercase">System Execution Trace</span>
          </div>
          <button 
            onClick={() => setIsFollowing(!isFollowing)}
            className={`text-[10px] font-mono px-2 py-1 rounded border transition-colors ${isFollowing ? 'bg-brand-500/20 border-brand-500/50 text-brand-400' : 'bg-transparent border-zinc-700 text-zinc-500 hover:text-zinc-300'}`}
          >
            {isFollowing ? 'AUTO-SCROLL ON' : 'AUTO-SCROLL OFF'}
          </button>
        </div>
        
        <div className="flex-1 overflow-y-auto p-4 font-mono text-[11px] leading-relaxed z-10 custom-scrollbar">
          {logs.length === 0 ? (
            <div className="h-full flex items-center justify-center text-brand-500/30 animate-pulse">
              Awaiting data stream...
            </div>
          ) : (
            logs.map((log, i) => (
              <div key={i} className="flex gap-4 hover:bg-white/5 px-2 -mx-2 rounded transition-colors py-0.5">
                <span className="text-brand-500/40 shrink-0 select-none">
                  {(() => {
                    try {
                      const t = log.time || log.timestamp;
                      if (!t) return '';
                      const d = new Date(typeof t === 'number' && t < 1e12 ? t * 1000 : t);
                      return d.toISOString().split('T')[1].replace('Z', '');
                    } catch (e) {
                      return '';
                    }
                  })()}
                </span>
                <span className={`shrink-0 w-12 text-center select-none font-bold ${log.level === 'ERROR' ? 'text-red-500' : log.level === 'WARNING' ? 'text-amber-500' : log.level === 'INFO' ? 'text-brand-500' : 'text-zinc-500'}`}>
                  {log.level}
                </span>
                <span className="text-zinc-500 shrink-0 w-24 truncate select-none">
                  [{log.name}]
                </span>
                <span className={`break-words flex-1 ${log.level === 'ERROR' ? 'text-red-400' : log.level === 'WARNING' ? 'text-amber-400' : log.level === 'INFO' ? 'text-brand-300/80 shadow-brand-500' : 'text-zinc-400'}`}>
                  {log.message}
                </span>
              </div>
            ))
          )}
          <div ref={logsEndRef} />
        </div>
      </div>
      
    </div>
  );
}
