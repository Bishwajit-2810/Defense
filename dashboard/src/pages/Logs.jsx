import React, { useState, useEffect, useRef } from 'react';
import { Terminal, Play, Square, Trash2 } from 'lucide-react';
import { API_BASE, getSseQueryAsync } from '../utils/api';

// The Redis log sink strips ANSI before storing, but `run_all.py` tees worker
// stdout through the same buffer, so a colourised line can still arrive. Matching
// ESC by its escape is the point of this regex, hence the rule exemption; a
// \u001b escape trips the same rule, so a disable is the only way to say
// "intentional".
// oxlint-disable-next-line no-control-regex
const stripAnsi = (str) => (typeof str === 'string' ? str.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '') : str);

// Identity of a log line, for dropping a repeat.
//
// The stream is *designed* to overlap: `/v1/logs/stream` replays `backfill`
// lines from `logs:recent` and then tails `logs:live`, subscribing before it
// reads so no line can fall between the two — which means the line straddling
// the join arrives twice by construction. Timestamp + level + message is enough
// to identify it; `ts` is a float from `record.created`, so two genuinely
// distinct lines would have to share a microsecond AND their text.
const lineKey = (e) => `${e?.ts ?? e?.time ?? ''}|${e?.level ?? ''}|${e?.message ?? ''}`;

export default function Logs() {
  const [logs, setLogs] = useState([]);
  const [isFollowing, setIsFollowing] = useState(true);
  const [isConnected, setIsConnected] = useState(false);
  const [filterLevel, setFilterLevel] = useState('DEBUG');
  const logsEndRef = useRef(null);
  const eventSourceRef = useRef(null);
  // Keys already rendered, so a replayed line is dropped instead of appended.
  // A ref rather than state: it must be read inside the event handler without
  // re-subscribing, and it is not rendered.
  const seenRef = useRef(new Set());

  // Open ONE stream for the current filter level, and close exactly the one this
  // effect opened.
  //
  // This used to be an `async connectStream()` guarded by `if
  // (eventSourceRef.current) return` and called from an effect. The guard cannot
  // work: it runs before the `await getSseQueryAsync()`, so under StrictMode's
  // double-mount both invocations pass it while the ref is still null, and two
  // EventSources end up open — the ref keeps the second, the first is orphaned
  // and never closed. Every line then arrived twice, and so did the stream's
  // 60-line backfill, which is what made the log view read as if the backend
  // were doing everything twice. Changing the filter leaked another one.
  useEffect(() => {
    let cancelled = false;
    let es = null;

    (async () => {
      const qs = await getSseQueryAsync();
      // The effect was torn down while we were awaiting the ticket — do not
      // open a socket nobody will close.
      if (cancelled) return;

      const ampersand = qs ? '&' : '?';
      es = new EventSource(`${API_BASE}/v1/logs/stream${qs}${ampersand}level=${filterLevel}&format=json`);
      eventSourceRef.current = es;

      es.addEventListener('log', (e) => {
        try {
          const parsed = JSON.parse(e.data);
          if (parsed && parsed.message) {
            parsed.message = stripAnsi(parsed.message);
          }
          // Belt and braces on top of the single-stream fix: the backfill/live
          // join legitimately repeats a line, and a reconnect replays the whole
          // backfill again.
          const key = lineKey(parsed);
          if (seenRef.current.has(key)) return;
          seenRef.current.add(key);
          if (seenRef.current.size > 4000) {
            // Bounded: keep the newest half rather than growing without limit.
            seenRef.current = new Set(Array.from(seenRef.current).slice(-2000));
          }
          setLogs(prev => [...prev.slice(-999), parsed]);
        } catch {
          // ignore malformed
        }
      });

      es.onopen = () => setIsConnected(true);
      es.onerror = () => setIsConnected(false);
    })();

    return () => {
      cancelled = true;
      if (es) es.close();
      if (eventSourceRef.current === es) eventSourceRef.current = null;
      setIsConnected(false);
    };
  }, [filterLevel]);

  useEffect(() => {
    if (isFollowing && logsEndRef.current) {
      logsEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logs, isFollowing]);

  // Clearing has to forget the keys too, or a reconnect's backfill would be
  // deduped away and the view would stay empty.
  const clearLogs = () => {
    seenRef.current = new Set();
    setLogs([]);
  };

  const getLevelColor = (level) => {
    switch(level) {
      case 'ERROR': return 'text-red-500 dark:text-red-400 bg-red-500/10';
      case 'WARNING': return 'text-amber-500 dark:text-amber-400 bg-amber-500/10';
      case 'INFO': return 'text-brand-600 dark:text-brand-400 bg-brand-500/10';
      case 'DEBUG': return 'text-slate-500 dark:text-zinc-400 bg-slate-500/10';
      default: return 'text-slate-500 dark:text-zinc-400 bg-slate-500/10';
    }
  };

  return (
    <div className="flex flex-col h-[calc(100vh-140px)] animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-6">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">System Logs</h2>
          <p className="text-slate-500 dark:text-zinc-400 flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${isConnected ? 'bg-brand-500 animate-pulse' : 'bg-red-500'}`}></span>
            {isConnected ? 'Live streaming' : 'Disconnected'}
          </p>
        </div>
        
        <div className="flex flex-wrap items-center gap-2">
          <select 
            value={filterLevel}
            onChange={(e) => setFilterLevel(e.target.value)}
            className="px-3 py-2 bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-800 rounded-lg text-sm"
          >
            <option value="DEBUG">Debug & Above</option>
            <option value="INFO">Info & Above</option>
            <option value="WARNING">Warnings & Errors</option>
            <option value="ERROR">Errors Only</option>
          </select>
          
          <button 
            onClick={() => setIsFollowing(!isFollowing)}
            className={`flex items-center gap-2 px-3 py-2 border rounded-lg text-sm transition-colors ${
              isFollowing 
                ? 'bg-brand-500 border-brand-500 text-white' 
                : 'bg-white dark:bg-zinc-900 border-slate-200 dark:border-zinc-800 hover:bg-slate-50 dark:hover:bg-zinc-800'
            }`}
          >
            {isFollowing ? <Play size={16} /> : <Square size={16} />}
            Auto-scroll
          </button>
          
          <button 
            onClick={clearLogs}
            className="flex items-center gap-2 px-3 py-2 bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-800 rounded-lg text-sm hover:bg-red-50 hover:text-red-600 hover:border-red-200 dark:hover:bg-red-900/20 dark:hover:text-red-400 dark:hover:border-red-900/50 transition-colors"
          >
            <Trash2 size={16} />
            Clear
          </button>
        </div>
      </div>

      <div className="flex-1 bg-white dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-inner flex flex-col font-mono text-xs">
        <div className="flex-1 overflow-y-auto p-4 space-y-1">
          {logs.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-slate-400 dark:text-zinc-600">
              <Terminal size={48} className="mb-4 opacity-50" />
              <p>Waiting for logs...</p>
            </div>
          ) : (
            logs.map((log, i) => (
              <div key={i} className="flex gap-4 py-1 hover:bg-slate-50 dark:hover:bg-zinc-900/50 px-2 -mx-2 rounded transition-colors group">
                <span className="text-slate-400 dark:text-zinc-500 shrink-0 select-none">
                  {new Date((log.ts || log.time || Date.now() / 1000) * 1000).toLocaleTimeString([], { hour12: false, fractionalSecondDigits: 3 })}
                </span>
                <span className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase shrink-0 w-16 text-center select-none ${getLevelColor(log.level)}`}>
                  {log.level}
                </span>
                <span className="text-slate-500 dark:text-zinc-400 shrink-0 w-32 truncate select-none" title={log.service || log.module || log.name}>
                  {log.service !== '-' ? log.service : (log.module || log.name)}
                </span>
                <span className="text-slate-800 dark:text-zinc-300 whitespace-pre-wrap break-words flex-1">
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
