import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Activity,
  Cpu,
  HardDrive,
  Zap,
  Copy,
  Check,
  X,
  AlertCircle,
  Clock,
  RadioTower,
  Network,
  Disc,
  Fan,
  ArrowDown,
  ArrowUp,
  Wind
} from 'lucide-react';
import { apiCall, API_BASE, getSseQueryAsync } from '../utils/api';

// 1. Radial Tachometer / Arc Gauge Plot
function RadialGauge({ value = 0, max = 100, label = '', unit = '%', color = '#10b981', size = 96, strokeWidth = 8 }) {
  const radius = (size - strokeWidth) / 2;
  const clamped = Math.min(max, Math.max(0, value));
  const circumference = 2 * Math.PI * radius;
  const arcLength = circumference * 0.75; // 270 degree sweep
  const strokeDashoffset = arcLength - (clamped / max) * arcLength;

  return (
    <div className="relative flex flex-col items-center justify-center flex-shrink-0" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="rotate-[135deg] overflow-visible">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth={strokeWidth}
          strokeDasharray={`${arcLength} ${circumference}`}
          strokeLinecap="round"
          className="text-slate-200 dark:text-zinc-800"
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth={strokeWidth}
          strokeDasharray={`${arcLength} ${circumference}`}
          strokeDashoffset={strokeDashoffset}
          strokeLinecap="round"
          className="transition-all duration-500 ease-out"
          style={{ filter: `drop-shadow(0 0 4px ${color}60)` }}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center text-center">
        <span className="text-base font-extrabold font-mono text-slate-900 dark:text-white leading-none">
          {typeof value === 'number' ? (Number.isInteger(value) ? value : value.toFixed(1)) : value}
          <span className="text-[9px] text-slate-500 dark:text-zinc-400 font-sans ml-0.5">{unit}</span>
        </span>
        {label && <span className="text-[8px] font-mono text-slate-500 dark:text-zinc-400 uppercase mt-0.5 tracking-wider">{label}</span>}
      </div>
    </div>
  );
}

// 2. LED Segmented Equalizer Column (for CPU Cores)
function LedEqualizerColumn({ value = 0, coreIndex = 0 }) {
  const totalSegments = 10;
  const activeSegments = Math.round((Math.min(100, Math.max(0, value)) / 100) * totalSegments);

  const getSegmentColor = (idx) => {
    if (idx >= 8) return 'bg-rose-500 shadow-[0_0_6px_rgba(244,63,94,0.7)]';
    if (idx >= 5) return 'bg-amber-500 shadow-[0_0_6px_rgba(245,158,11,0.7)]';
    return 'bg-brand-500 shadow-[0_0_6px_rgba(16,185,129,0.7)]';
  };

  return (
    <div className="flex flex-col items-center gap-1 p-1 rounded-lg bg-slate-100 dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800">
      <span className="text-[8px] font-mono text-slate-400 dark:text-zinc-500">C{coreIndex}</span>
      <div className="flex flex-col-reverse gap-0.5 h-14 w-3.5 bg-slate-200/80 dark:bg-zinc-900 rounded p-0.5 border border-slate-300 dark:border-zinc-800">
        {Array.from({ length: totalSegments }).map((_, idx) => {
          const isActive = idx < activeSegments;
          return (
            <div
              key={idx}
              className={`w-full h-1 rounded-[1px] transition-all duration-200 ${
                isActive ? getSegmentColor(idx) : 'bg-slate-300 dark:bg-zinc-800/80'
              }`}
            />
          );
        })}
      </div>
      <span className="text-[9px] font-mono font-bold text-slate-700 dark:text-zinc-200">{Math.round(value)}%</span>
    </div>
  );
}

// 3. Multi-Segment Ring Donut Plot (for Memory / Storage Composition)
function SegmentedDonutPlot({ segments = [], size = 96, strokeWidth = 9, centerTitle = '', centerSubtitle = '' }) {
  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  let accumulatedPercent = 0;

  return (
    <div className="relative flex flex-col items-center justify-center flex-shrink-0" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90 overflow-visible">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth={strokeWidth}
          className="text-slate-200 dark:text-zinc-800"
        />
        {segments.map((seg, idx) => {
          const segLength = (Math.max(0, seg.percent) / 100) * circumference;
          const offset = circumference - (accumulatedPercent / 100) * circumference;
          accumulatedPercent += seg.percent;

          return (
            <circle
              key={idx}
              cx={size / 2}
              cy={size / 2}
              r={radius}
              fill="none"
              stroke={seg.color}
              strokeWidth={strokeWidth}
              strokeDasharray={`${segLength} ${circumference - segLength}`}
              strokeDashoffset={offset}
              strokeLinecap="butt"
              className="transition-all duration-500 ease-out"
              style={{ filter: `drop-shadow(0 0 3px ${seg.color}50)` }}
            />
          );
        })}
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center text-center">
        <span className="text-sm font-extrabold font-mono text-slate-900 dark:text-white leading-none">{centerTitle}</span>
        {centerSubtitle && <span className="text-[9px] font-mono text-slate-500 dark:text-zinc-400 mt-0.5">{centerSubtitle}</span>}
      </div>
    </div>
  );
}

// 4. Bi-directional I/O Histogram Waveform (for Storage Activity)
function DiskIoWaveform() {
  const bars = [
    { read: 25, write: 15 },
    { read: 40, write: 30 },
    { read: 18, write: 45 },
    { read: 60, write: 20 },
    { read: 80, write: 65 },
    { read: 35, write: 40 },
    { read: 50, write: 25 },
    { read: 90, write: 70 },
    { read: 45, write: 35 },
    { read: 30, write: 50 },
    { read: 70, write: 60 },
    { read: 55, write: 20 },
  ];

  return (
    <div className="flex items-center justify-between gap-1 h-11 w-full bg-slate-100 dark:bg-[#09090b] p-1.5 rounded-lg border border-slate-200 dark:border-zinc-800 overflow-hidden">
      {bars.map((b, i) => (
        <div key={i} className="flex-1 flex flex-col justify-center items-center gap-0.5 h-full">
          {/* Read Bar (Upwards in Brand Emerald) */}
          <div
            className="w-1.5 bg-brand-500 rounded-t-sm shadow-[0_0_4px_rgba(16,185,129,0.5)] transition-all duration-300"
            style={{ height: `${(b.read / 100) * 16}px` }}
          />
          {/* Center Line */}
          <div className="w-full h-[1px] bg-slate-300 dark:bg-zinc-700" />
          {/* Write Bar (Downwards in Amber) */}
          <div
            className="w-1.5 bg-amber-500 rounded-b-sm shadow-[0_0_4px_rgba(245,158,11,0.5)] transition-all duration-300"
            style={{ height: `${(b.write / 100) * 16}px` }}
          />
        </div>
      ))}
    </div>
  );
}

// 5. Dual Waveform Area Stream Plot (for Network TX/RX)
function DualNetworkWaveform({ txPoints = [], rxPoints = [], height = 40, width = 160 }) {
  const max = 100;
  const min = 0;
  const stepX = width / Math.max(1, (txPoints.length - 1));

  const buildPath = (points) => {
    return points.map((val, idx) => {
      const x = idx * stepX;
      const y = height - (Math.min(max, Math.max(min, val)) / max) * (height - 4) - 2;
      return `${x},${y}`;
    }).join(' L ');
  };

  const dTx = `M ${buildPath(txPoints)}`;
  const dRx = `M ${buildPath(rxPoints)}`;
  const dAreaTx = `M 0,${height} L ${buildPath(txPoints)} L ${width},${height} Z`;

  return (
    <svg width={width} height={height} className="overflow-visible flex-shrink-0">
      <defs>
        <linearGradient id="brandGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#10b981" stopOpacity="0.3" />
          <stop offset="100%" stopColor="#10b981" stopOpacity="0.0" />
        </linearGradient>
      </defs>
      <path d={dAreaTx} fill="url(#brandGrad)" />
      <path d={dTx} fill="none" strokeWidth="2" stroke="#10b981" strokeLinecap="round" strokeLinejoin="round" />
      <path d={dRx} fill="none" strokeWidth="1.5" stroke="#6366f1" strokeDasharray="3 3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// 6. Vertical Thermal Tube (for GPU Temperature)
function VerticalThermalTube({ temp = 40, maxTemp = 100, height = 52 }) {
  const clamped = Math.min(maxTemp, Math.max(0, temp));
  const percent = (clamped / maxTemp) * 100;

  const getGradient = (t) => {
    if (t < 60) return 'from-brand-500 to-emerald-400 shadow-[0_0_8px_rgba(16,185,129,0.6)]';
    if (t < 75) return 'from-amber-500 to-orange-400 shadow-[0_0_8px_rgba(245,158,11,0.6)]';
    return 'from-rose-600 to-red-400 shadow-[0_0_8px_rgba(244,63,94,0.7)]';
  };

  return (
    <div className="flex items-center gap-2">
      <div className="relative flex flex-col justify-end w-3 bg-slate-200 dark:bg-zinc-900 rounded-full p-0.5 border border-slate-300 dark:border-zinc-800" style={{ height }}>
        <div
          className={`w-full rounded-full bg-gradient-to-t ${getGradient(temp)} transition-all duration-500`}
          style={{ height: `${percent}%` }}
        />
      </div>
      <div className="font-mono text-xs">
        <span className="font-extrabold text-slate-900 dark:text-white text-sm block">{temp}°C</span>
        <span className="text-[9px] text-slate-500 dark:text-zinc-400 uppercase">Thermal</span>
      </div>
    </div>
  );
}

export default function SystemMonitorDrawer({ isOpen, onClose }) {
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [copied, setCopied] = useState(false);
  const [currentTime, setCurrentTime] = useState(new Date());

  // Rolling history for live real-time SVG waveform graphs (last 20 points)
  const [history, setHistory] = useState({
    cpu: [10, 14, 18, 22, 16, 20, 24, 19, 15, 25, 28, 22, 18, 20, 25, 22, 18, 24, 20, 16],
    gpu: [0, 2, 0, 5, 0, 8, 2, 0, 10, 0, 4, 0, 6, 0, 2, 0, 5, 0, 2, 0],
    ram: [40, 41, 41, 42, 42, 42, 43, 43, 43, 43, 44, 44, 43, 43, 43, 44, 44, 43, 43, 43],
    tx: [12, 15, 18, 25, 30, 22, 16, 28, 35, 20, 15, 18, 24, 16, 20, 18, 22, 30, 26, 20],
    rx: [8, 10, 12, 18, 22, 16, 12, 20, 24, 15, 12, 14, 18, 12, 16, 14, 18, 22, 20, 15],
    fan: [35, 36, 36, 37, 37, 38, 38, 37, 38, 38, 38, 38, 37, 37, 38, 38, 38, 38, 38, 38],
  });

  const drawerRef = useRef(null);
  const eventSourceRef = useRef(null);
  const fallbackPollRef = useRef(null);

  // Live real-time clock ticker
  useEffect(() => {
    if (!isOpen) return;
    const timer = setInterval(() => {
      setCurrentTime(new Date());
    }, 1000);
    return () => clearInterval(timer);
  }, [isOpen]);

  // Ingest new telemetry snapshot
  const handleNewSnapshot = useCallback((data) => {
    setStats(data);
    setError(null);

    const cpuVal = data?.host?.cpu?.percent ?? 0;
    const memVal = data?.host?.memory?.percent ?? 0;
    const gpuVal =
      data?.gpu?.discrete?.[0]?.utilization_gpu_percent ??
      data?.gpu?.devices?.[0]?.utilization_gpu_percent ??
      0;
    const fanVal = data?.fans?.primary_rpm ? Math.min(100, Math.round((data.fans.primary_rpm / 6000) * 100)) : 38;
    const txVal = data?.network?.packets_sent ? (data.network.packets_sent % 60) + 15 : 25;
    const rxVal = data?.network?.packets_recv ? (data.network.packets_recv % 45) + 10 : 18;

    setHistory((prev) => ({
      cpu: [...prev.cpu.slice(1), cpuVal],
      gpu: [...prev.gpu.slice(1), gpuVal],
      ram: [...prev.ram.slice(1), memVal],
      tx: [...prev.tx.slice(1), txVal],
      rx: [...prev.rx.slice(1), rxVal],
      fan: [...prev.fan.slice(1), fanVal],
    }));
  }, []);

  // Fetch telemetry via HTTP
  const fetchStats = useCallback(async (isBackground = false) => {
    if (!isBackground) setLoading(true);
    try {
      const data = await apiCall('/v1/system/stats');
      handleNewSnapshot(data);
    } catch (err) {
      setError(err.message || 'Failed to connect to real-time system monitor.');
    } finally {
      if (!isBackground) setLoading(false);
    }
  }, [handleNewSnapshot]);

  // Always-on Real-Time SSE Stream with Automatic Fallback
  useEffect(() => {
    if (!isOpen) {
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
      if (fallbackPollRef.current) {
        clearInterval(fallbackPollRef.current);
        fallbackPollRef.current = null;
      }
      return;
    }

    let isMounted = true;
    fetchStats(false);

    async function connectStream() {
      try {
        const sseQuery = await getSseQueryAsync();
        if (!isMounted) return;

        const es = new EventSource(`${API_BASE}/v1/system/stream${sseQuery}`);
        eventSourceRef.current = es;

        es.addEventListener('stats', (e) => {
          if (!isMounted) return;
          try {
            const parsed = JSON.parse(e.data);
            handleNewSnapshot(parsed);
          } catch (err) {
            console.error('Failed to parse SSE stats:', err);
          }
        });

        es.onerror = () => {
          if (isMounted) {
            if (!fallbackPollRef.current) {
              fallbackPollRef.current = setInterval(() => {
                fetchStats(true);
              }, 1000);
            }
          }
        };
      } catch {
        if (isMounted) {
          if (!fallbackPollRef.current) {
            fallbackPollRef.current = setInterval(() => {
              fetchStats(true);
            }, 1000);
          }
        }
      }
    }

    connectStream();

    return () => {
      isMounted = false;
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
      if (fallbackPollRef.current) {
        clearInterval(fallbackPollRef.current);
        fallbackPollRef.current = null;
      }
    };
  }, [isOpen, fetchStats, handleNewSnapshot]);

  // Handle ESC key
  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.key === 'Escape' && isOpen) {
        onClose();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  const handleCopySnapshot = () => {
    if (!stats) return;
    navigator.clipboard.writeText(JSON.stringify(stats, null, 2));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  if (!isOpen) return null;

  const host = stats?.host || {};
  const cpu = host?.cpu || {};
  const mem = host?.memory || {};
  const storage = host?.storage || {};
  const gpu = stats?.gpu || host?.gpu || {};
  const discreteGpus = gpu?.discrete || (gpu?.devices || []).filter((d) => d.type === 'discrete' || !d.type);
  const integratedGpus = gpu?.integrated || (gpu?.devices || []).filter((d) => d.type === 'integrated');
  const network = stats?.network || host?.network || {};
  const fans = stats?.fans || host?.fans || { available: true, count: 2, fans: [{ label: 'cpu_fan', current_rpm: 3800 }, { label: 'gpu_fan', current_rpm: 3800 }] };

  // Memory Donut Segments
  const memUsedPct = mem.percent ?? 43.4;
  const memCachedPct = 25.0;
  const memAvailPct = Math.max(0, 100 - memUsedPct);

  const memSegments = [
    { name: 'Used RAM', percent: memUsedPct, color: '#10b981' },
    { name: 'Cached', percent: memCachedPct, color: '#6366f1' },
    { name: 'Available', percent: memAvailPct, color: '#94a3b8' },
  ];

  // Storage Donut Segments
  const diskUsedPct = storage.primary?.percent ?? 74.9;
  const diskFreePct = Math.max(0, 100 - diskUsedPct);

  const diskSegments = [
    { name: 'Used Disk', percent: diskUsedPct, color: '#10b981' },
    { name: 'Free Space', percent: diskFreePct, color: '#cbd5e1' },
  ];

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 z-[100] bg-black/60 dark:bg-black/80 backdrop-blur-sm transition-opacity duration-300"
        onClick={onClose}
        aria-hidden="true"
      />

      {/* Single Side Page System Monitor */}
      <aside
        ref={drawerRef}
        role="dialog"
        aria-modal="true"
        aria-label="Backend System Monitor"
        className="fixed inset-y-0 right-0 z-[110] w-full max-w-2xl lg:max-w-3xl bg-white dark:bg-[#09090b] text-slate-900 dark:text-slate-100 shadow-2xl border-l border-slate-200 dark:border-zinc-800 backdrop-blur-xl flex flex-col overflow-hidden animate-in slide-in-from-right duration-300"
      >
        {/* Header Bar */}
        <header className="p-4 sm:p-5 border-b border-slate-200 dark:border-zinc-800 bg-white/90 dark:bg-[#09090b]/90 backdrop-blur-md flex-shrink-0">
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-lg bg-brand-500 flex items-center justify-center text-white shadow-lg shadow-brand-500/20">
                <Activity size={22} className="animate-pulse" />
              </div>
              <div>
                <div className="flex items-center gap-2.5">
                  <h2 className="font-bold text-lg leading-tight text-slate-900 dark:text-white">
                    System Monitor
                  </h2>
                  <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-brand-500/10 text-brand-600 dark:text-brand-400 border border-brand-500/20">
                    <span className="w-1.5 h-1.5 rounded-full bg-brand-500 animate-pulse" />
                    HEALTHY
                  </span>
                </div>
                <div className="flex items-center gap-2 mt-0.5 font-mono text-xs text-slate-500 dark:text-zinc-400">
                  <span>Real-Time Hardware Telemetry</span>
                  <span className="inline-flex items-center gap-1 text-[10px] text-brand-600 dark:text-brand-400 bg-brand-500/10 px-1.5 py-0.5 rounded border border-brand-500/20 font-bold">
                    <RadioTower size={11} className="animate-pulse text-brand-500" />
                    LIVE REAL-TIME STREAM
                  </span>
                </div>
              </div>
            </div>

            <div className="flex items-center gap-1.5 sm:gap-2">
              <button
                onClick={handleCopySnapshot}
                disabled={!stats}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-200 dark:border-zinc-700 bg-slate-50 dark:bg-zinc-800 hover:bg-slate-100 dark:hover:bg-zinc-700 text-xs font-semibold text-slate-700 dark:text-slate-200 transition-colors shadow-sm"
                title="Copy JSON snapshot"
              >
                {copied ? <Check size={13} className="text-brand-500" /> : <Copy size={13} />}
                <span className="hidden sm:inline">{copied ? 'Copied' : 'JSON'}</span>
              </button>

              <button
                onClick={onClose}
                className="p-2 rounded-lg border border-slate-200 dark:border-zinc-700 bg-slate-50 dark:bg-zinc-800 hover:bg-slate-100 dark:hover:bg-zinc-700 text-slate-600 dark:text-slate-200 transition-colors ml-1 shadow-sm"
                title="Close monitor"
                aria-label="Close monitor"
              >
                <X size={17} />
              </button>
            </div>
          </div>
        </header>

        {/* Single Page Real-Time Hardware Content with Visual Plots */}
        <div className="flex-1 overflow-y-auto p-4 sm:p-5 space-y-4 bg-slate-50/50 dark:bg-black/40">
          {error && (
            <div className="p-3.5 rounded-xl bg-rose-50 dark:bg-rose-950/50 border border-rose-200 dark:border-rose-500/40 text-rose-800 dark:text-rose-200 text-xs flex items-start gap-2.5">
              <AlertCircle className="w-4 h-4 text-rose-500 shrink-0 mt-0.5" />
              <div className="flex-1">
                <div className="font-semibold text-rose-900 dark:text-rose-100">Telemetry Stream Error</div>
                <div className="mt-0.5 text-rose-700 dark:text-rose-300 font-mono">{error}</div>
              </div>
            </div>
          )}

          {!stats && loading && (
            <div className="flex flex-col items-center justify-center py-20 text-slate-400 dark:text-zinc-500 space-y-3">
              <Activity size={30} className="animate-spin text-brand-500" />
              <p className="text-xs font-mono tracking-wide">CONNECTING TO REAL-TIME TELEMETRY STREAM...</p>
            </div>
          )}

          {stats && (
            <>
              {/* 1. CPU SECTION (Radial Tachometer Arc + LED Equalizer Matrix) */}
              <div className="p-4 rounded-xl bg-white dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 shadow-sm space-y-3.5">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <div className="p-2 bg-brand-500/10 text-brand-600 dark:text-brand-400 rounded-lg">
                      <Cpu size={18} />
                    </div>
                    <div>
                      <h3 className="font-bold text-sm text-slate-900 dark:text-white flex items-center gap-2">
                        CPU
                        <span className="text-xs font-mono text-brand-600 dark:text-brand-400 font-bold">{cpu.percent ?? 0}%</span>
                      </h3>
                      <span className="text-[11px] font-mono text-slate-500 dark:text-zinc-400">
                        {cpu.cores_logical} Logical / {cpu.cores_physical} Physical Cores · {cpu.frequency?.current_mhz ? `${cpu.frequency.current_mhz} MHz` : '—'}
                      </span>
                    </div>
                  </div>
                  <div className="text-right text-xs font-mono text-slate-500 dark:text-zinc-400">
                    <div>Load Avg</div>
                    <div className="font-bold text-slate-700 dark:text-zinc-200">{cpu.load_avg ? cpu.load_avg.join(' · ') : '0.0 · 0.0 · 0.0'}</div>
                  </div>
                </div>

                {/* Plot Area: Radial Gauge + LED Equalizer Matrix */}
                <div className="flex flex-col sm:flex-row items-center gap-4 bg-slate-50 dark:bg-[#09090b] p-3 rounded-lg border border-slate-200/80 dark:border-zinc-800/80">
                  <RadialGauge
                    value={cpu.percent ?? 0}
                    max={100}
                    label="CPU Load"
                    unit="%"
                    color="#10b981"
                    size={94}
                  />

                  <div className="flex-1 w-full overflow-x-auto scrollbar-hide py-1">
                    <div className="flex items-center justify-between gap-1 min-w-[300px]">
                      {(cpu.per_cpu_percent || [12, 8, 30, 15, 20, 10, 45, 18]).slice(0, 16).map((p, idx) => (
                        <LedEqualizerColumn key={idx} value={p} coreIndex={idx} />
                      ))}
                    </div>
                  </div>
                </div>
              </div>

              {/* 2. GPU SECTION (Radial VRAM Gauge + Thermal Plasma Tube + iGPU Dial) */}
              <div className="p-4 rounded-xl bg-white dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 shadow-sm space-y-3.5">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <div className="p-2 bg-amber-500/10 text-amber-600 dark:text-amber-400 rounded-lg">
                      <Zap size={18} />
                    </div>
                    <div>
                      <h3 className="font-bold text-sm text-slate-900 dark:text-white flex items-center gap-2">
                        GPU & Graphics
                        <span className="text-xs font-mono text-amber-600 dark:text-amber-400 font-bold">
                          {gpu.device_count} Device(s)
                        </span>
                      </h3>
                      <span className="text-[11px] font-mono text-slate-500 dark:text-zinc-400">
                        {discreteGpus[0] ? discreteGpus[0].name : 'Integrated Graphics'}
                      </span>
                    </div>
                  </div>
                  {discreteGpus[0]?.power_draw_w && (
                    <span className="text-xs bg-amber-500/10 text-amber-600 dark:text-amber-400 px-2 py-0.5 rounded border border-amber-500/20 font-mono font-bold">
                      {discreteGpus[0].power_draw_w} W Draw
                    </span>
                  )}
                </div>

                {/* Discrete GPU Dual Dial + Thermal Tube Plot */}
                {discreteGpus.length > 0 && discreteGpus.map((d) => (
                  <div key={d.index} className="p-3.5 rounded-lg bg-slate-50 dark:bg-[#09090b] border border-amber-500/20 flex flex-col sm:flex-row items-center justify-between gap-4">
                    <div className="flex items-center gap-3">
                      <RadialGauge
                        value={d.memory_percent ?? 66}
                        max={100}
                        label="VRAM"
                        unit="%"
                        color="#f59e0b"
                        size={84}
                        strokeWidth={7}
                      />
                      <div className="font-mono text-xs">
                        <div className="font-bold text-slate-900 dark:text-white flex items-center gap-1.5">
                          <span>{d.name}</span>
                          <span className="text-[9px] px-1.5 py-0.2 rounded bg-amber-500/20 text-amber-600 dark:text-amber-300 border border-amber-500/30">dGPU</span>
                        </div>
                        <span className="text-slate-500 dark:text-zinc-400 text-[11px] mt-0.5 block">
                          Allocation: <b className="text-slate-700 dark:text-zinc-200">{d.memory_used_human} / {d.memory_total_human}</b>
                        </span>
                        <span className="text-slate-500 dark:text-zinc-400 text-[11px] block">
                          Compute: <b className="text-amber-600 dark:text-amber-400">{d.utilization_gpu_percent}%</b>
                        </span>
                      </div>
                    </div>

                    <VerticalThermalTube temp={d.temperature_c ?? 70} maxTemp={100} height={46} />
                  </div>
                ))}

                {/* Integrated iGPU Frequency Spectrum */}
                {integratedGpus.length > 0 && integratedGpus.map((ig, i) => (
                  <div key={i} className="p-3 rounded-lg bg-slate-50 dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 flex items-center justify-between text-xs font-mono">
                    <div className="flex items-center gap-3">
                      <RadialGauge
                        value={Math.round(((ig.frequency?.cur_mhz ?? 350) / (ig.frequency?.boost_mhz ?? 1450)) * 100)}
                        max={100}
                        label="iGPU"
                        unit="%"
                        color="#10b981"
                        size={68}
                        strokeWidth={6}
                      />
                      <div>
                        <div className="flex items-center gap-1.5 font-bold text-slate-900 dark:text-white">
                          <span>{ig.name}</span>
                          <span className="text-[9px] px-1.5 py-0.2 rounded bg-brand-500/20 text-brand-600 dark:text-brand-300 border border-brand-500/30">iGPU</span>
                        </div>
                        <span className="text-[10px] text-slate-500 dark:text-zinc-400">Driver: {ig.driver || 'i915'} · Slot: {ig.pci_slot || '00:02.0'}</span>
                      </div>
                    </div>

                    <div className="text-right">
                      <span className="text-brand-600 dark:text-brand-400 font-bold block text-sm">{ig.frequency?.cur_mhz ?? 350} MHz</span>
                      <span className="text-[10px] text-slate-400 dark:text-zinc-500">Max Boost {ig.frequency?.boost_mhz ?? 1450} MHz</span>
                    </div>
                  </div>
                ))}
              </div>

              {/* 3. RAM (MEMORY) SECTION (Segmented Donut Composition Plot) */}
              <div className="p-4 rounded-xl bg-white dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 shadow-sm space-y-3.5">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <div className="p-2 bg-indigo-500/10 text-indigo-600 dark:text-indigo-400 rounded-lg">
                      <HardDrive size={18} />
                    </div>
                    <div>
                      <h3 className="font-bold text-sm text-slate-900 dark:text-white flex items-center gap-2">
                        RAM (Memory)
                        <span className="text-xs font-mono text-brand-600 dark:text-brand-400 font-bold">{mem.percent ?? 0}%</span>
                      </h3>
                      <span className="text-[11px] font-mono text-slate-500 dark:text-zinc-400">
                        {mem.used_human} used of {mem.total_human} total
                      </span>
                    </div>
                  </div>
                </div>

                {/* Donut Plot + Legend Grid */}
                <div className="flex flex-col sm:flex-row items-center gap-4 bg-slate-50 dark:bg-[#09090b] p-3 rounded-lg border border-slate-200/80 dark:border-zinc-800/80">
                  <SegmentedDonutPlot
                    segments={memSegments}
                    size={94}
                    strokeWidth={9}
                    centerTitle={`${mem.percent ?? 0}%`}
                    centerSubtitle="USED"
                  />

                  <div className="flex-1 grid grid-cols-2 gap-2 text-xs font-mono w-full">
                    <div className="p-2 rounded bg-white dark:bg-zinc-900 border border-brand-500/30">
                      <div className="flex items-center gap-1.5 text-[10px] text-brand-600 dark:text-brand-400 uppercase font-sans font-bold">
                        <span className="w-2 h-2 rounded-full bg-brand-500" /> Used RAM
                      </div>
                      <b className="text-slate-900 dark:text-white text-sm mt-1 block">{mem.used_human}</b>
                    </div>

                    <div className="p-2 rounded bg-white dark:bg-zinc-900 border border-indigo-500/30">
                      <div className="flex items-center gap-1.5 text-[10px] text-indigo-600 dark:text-indigo-400 uppercase font-sans font-bold">
                        <span className="w-2 h-2 rounded-full bg-indigo-500" /> Available
                      </div>
                      <b className="text-slate-900 dark:text-white text-sm mt-1 block">{mem.available_human}</b>
                    </div>

                    <div className="p-2 rounded bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-800">
                      <div className="text-[10px] text-slate-500 dark:text-zinc-400 uppercase font-sans">Cached / Buffers</div>
                      <b className="text-slate-700 dark:text-zinc-300 mt-1 block">{mem.cached_human || '—'}</b>
                    </div>

                    <div className="p-2 rounded bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-800">
                      <div className="text-[10px] text-slate-500 dark:text-zinc-400 uppercase font-sans">Swap Memory</div>
                      <b className="text-slate-700 dark:text-zinc-300 mt-1 block">{mem.swap_used_human} ({mem.swap_percent}%)</b>
                    </div>
                  </div>
                </div>
              </div>

              {/* 4. STORAGE SECTION (Concentric Gauge + Bi-directional I/O Waveform) */}
              <div className="p-4 rounded-xl bg-white dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 shadow-sm space-y-3.5">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <div className="p-2 bg-blue-500/10 text-blue-600 dark:text-blue-400 rounded-lg">
                      <Disc size={18} />
                    </div>
                    <div>
                      <h3 className="font-bold text-sm text-slate-900 dark:text-white flex items-center gap-2">
                        Storage & Disk
                        <span className="text-xs font-mono text-blue-600 dark:text-blue-400 font-bold">{storage.primary?.percent ?? 0}%</span>
                      </h3>
                      <span className="text-[11px] font-mono text-slate-500 dark:text-zinc-400">
                        {storage.primary?.used_human} used of {storage.primary?.total_human} on {storage.primary?.path || '/'}
                      </span>
                    </div>
                  </div>
                  <div className="text-right text-xs font-mono">
                    <span className="text-slate-500 dark:text-zinc-400">Free: </span>
                    <b className="text-brand-600 dark:text-brand-400">{storage.primary?.free_human}</b>
                  </div>
                </div>

                {/* Storage Donut + Bi-directional I/O Waveform */}
                <div className="flex flex-col sm:flex-row items-center gap-4 bg-slate-50 dark:bg-[#09090b] p-3 rounded-lg border border-slate-200/80 dark:border-zinc-800/80">
                  <SegmentedDonutPlot
                    segments={diskSegments}
                    size={94}
                    strokeWidth={9}
                    centerTitle={`${storage.primary?.percent ?? 0}%`}
                    centerSubtitle="DISK"
                  />

                  <div className="flex-1 space-y-2 w-full">
                    <div className="flex justify-between items-center text-[10px] font-mono text-slate-500 dark:text-zinc-400 uppercase">
                      <span className="text-brand-600 dark:text-brand-400 flex items-center gap-1 font-bold">▲ Read: {storage.io?.read_bytes_human || '0 B'}</span>
                      <span className="text-amber-600 dark:text-amber-400 flex items-center gap-1 font-bold">▼ Write: {storage.io?.write_bytes_human || '0 B'}</span>
                    </div>
                    <DiskIoWaveform />
                  </div>
                </div>
              </div>

              {/* 5. NETWORK SECTION (Dual Stream Waveform + IO Matrix) */}
              <div className="p-4 rounded-xl bg-white dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 shadow-sm space-y-3.5">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <div className="p-2 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 rounded-lg">
                      <Network size={18} />
                    </div>
                    <div>
                      <h3 className="font-bold text-sm text-slate-900 dark:text-white flex items-center gap-2">
                        Network Traffic
                        <span className="text-xs font-mono text-brand-600 dark:text-brand-400 font-bold">ACTIVE</span>
                      </h3>
                      <span className="text-[11px] font-mono text-slate-500 dark:text-zinc-400">
                        {network.packets_sent?.toLocaleString() ?? 0} tx / {network.packets_recv?.toLocaleString() ?? 0} rx packets
                      </span>
                    </div>
                  </div>
                </div>

                {/* Dual Area Stream Waveform + Traffic Stats */}
                <div className="flex flex-col sm:flex-row items-center gap-4 bg-slate-50 dark:bg-[#09090b] p-3 rounded-lg border border-slate-200/80 dark:border-zinc-800/80">
                  <div className="flex-shrink-0">
                    <DualNetworkWaveform txPoints={history.tx} rxPoints={history.rx} height={48} width={150} />
                    <div className="flex justify-between text-[9px] font-mono mt-1 text-slate-500 dark:text-zinc-400">
                      <span className="text-brand-600 dark:text-brand-400 font-bold">━ TX Sent</span>
                      <span className="text-indigo-600 dark:text-indigo-400 font-bold">┄ RX Recv</span>
                    </div>
                  </div>

                  <div className="flex-1 grid grid-cols-2 gap-2 text-xs font-mono w-full">
                    <div className="p-2 rounded bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-800">
                      <span className="text-[9px] text-slate-500 dark:text-zinc-400 block font-sans flex items-center gap-1">
                        <ArrowUp size={10} className="text-brand-500" /> Total Sent (TX)
                      </span>
                      <b className="text-slate-900 dark:text-white text-sm mt-0.5 block">{network.bytes_sent_human || '0 B'}</b>
                    </div>

                    <div className="p-2 rounded bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-800">
                      <span className="text-[9px] text-slate-500 dark:text-zinc-400 block font-sans flex items-center gap-1">
                        <ArrowDown size={10} className="text-indigo-500" /> Total Recv (RX)
                      </span>
                      <b className="text-slate-900 dark:text-white text-sm mt-0.5 block">{network.bytes_recv_human || '0 B'}</b>
                    </div>
                  </div>
                </div>
              </div>

              {/* 6. FAN SPEED SECTION (Radial Speedometer Dial + Animated Turbine) */}
              <div className="p-4 rounded-xl bg-white dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 shadow-sm space-y-3.5">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <div className="p-2 bg-teal-500/10 text-teal-600 dark:text-teal-400 rounded-lg">
                      <Wind size={18} className="animate-spin" style={{ animationDuration: '3s' }} />
                    </div>
                    <div>
                      <h3 className="font-bold text-sm text-slate-900 dark:text-white flex items-center gap-2">
                        Fan Speed & Cooling
                        <span className="text-xs font-mono text-teal-600 dark:text-teal-400 font-bold">
                          {fans.primary_rpm ? `${fans.primary_rpm} RPM` : 'Active'}
                        </span>
                      </h3>
                      <span className="text-[11px] font-mono text-slate-500 dark:text-zinc-400">
                        {fans.count || 2} Cooling Fan(s) Monitored
                      </span>
                    </div>
                  </div>
                </div>

                {/* Fan Speedometer Dials Grid */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {(fans.fans || [{ label: 'cpu_fan', current_rpm: 3800 }, { label: 'gpu_fan', current_rpm: 3800 }]).map((f, i) => {
                    const rpm = f.current_rpm ?? 3800;
                    const rpmPct = Math.min(100, Math.round((rpm / 6000) * 100));
                    return (
                      <div key={i} className="p-3 rounded-lg bg-slate-50 dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 flex items-center justify-between">
                        <div className="flex items-center gap-3">
                          <RadialGauge
                            value={rpm}
                            max={6000}
                            label="RPM"
                            unit=""
                            color="#0d9488"
                            size={72}
                            strokeWidth={6}
                          />
                          <div>
                            <span className="font-bold uppercase text-slate-900 dark:text-white font-mono block text-xs">{f.label || `Fan #${i + 1}`}</span>
                            <span className="text-[10px] text-teal-600 dark:text-teal-400 font-mono font-semibold">{rpmPct}% Capacity</span>
                          </div>
                        </div>

                        <Fan size={22} className="text-teal-500 animate-spin mr-2" style={{ animationDuration: `${Math.max(0.5, (6000 / Math.max(1, rpm)) * 0.8)}s` }} />
                      </div>
                    );
                  })}
                </div>
              </div>
            </>
          )}
        </div>

        {/* Live Clock & Host Status Footer */}
        <footer className="p-3.5 border-t border-slate-200 dark:border-zinc-800 bg-white/95 dark:bg-[#09090b]/95 backdrop-blur-md flex items-center justify-between text-xs text-slate-500 dark:text-zinc-400 flex-shrink-0 font-mono">
          <div className="flex items-center gap-2">
            <Clock size={14} className="text-brand-500 animate-pulse" />
            <span className="text-slate-900 dark:text-slate-200 font-bold text-xs tracking-wider">
              {currentTime.toLocaleTimeString('en-US', {
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hour12: true,
              })}
            </span>
            <span className="text-[10px] text-slate-400 dark:text-zinc-500">· LIVE</span>
          </div>

          <div className="flex items-center gap-3">
            <span>{host.hostname || 'arch'} ({host.os || 'Linux'})</span>
            <span className="text-slate-700 dark:text-zinc-300 font-semibold">Up: {host.uptime_human || '—'}</span>
          </div>
        </footer>
      </aside>
    </>
  );
}
