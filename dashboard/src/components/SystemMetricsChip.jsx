import React from 'react';
import { Cpu, HardDrive, Zap } from 'lucide-react';
import { useSystemMetrics } from '../hooks/useSystemMetrics';

/**
 * Formats a metric value with appropriate fallback and rounding.
 */
function formatPercent(val) {
  if (val === null || val === undefined || isNaN(val)) return '--';
  return `${Math.round(val)}%`;
}

/**
 * Returns color class based on load intensity.
 */
function getLoadColor(val) {
  if (val === null || val === undefined || isNaN(val)) return 'text-slate-500 dark:text-zinc-400';
  if (val >= 90) return 'text-rose-600 dark:text-rose-400 font-extrabold';
  if (val >= 70) return 'text-amber-600 dark:text-amber-400 font-bold';
  return 'text-slate-800 dark:text-zinc-200 font-semibold';
}

/**
 * Small popped chip component displaying real-time CPU, GPU, and RAM usages.
 * Can be rendered vertically (pinned/floating on the right side) or horizontally.
 * Clicking the chip triggers `onOpen` to launch the full System Monitor drawer.
 */
export default function SystemMetricsChip({
  onOpen,
  orientation = 'vertical',
  customMetrics,
  className = '',
  showShortcut = true,
  ariaLabel,
}) {
  // Use telemetry hook unless customMetrics are provided
  const { metrics: hookMetrics, isLive } = useSystemMetrics({ enabled: !customMetrics });
  const metrics = customMetrics || hookMetrics;

  const cpuText = formatPercent(metrics.cpu);
  const gpuText =
    metrics.gpuAvailable === false && metrics.gpu === null
      ? 'N/A'
      : formatPercent(metrics.gpu);
  const ramText = formatPercent(metrics.ram);

  const handleClick = (e) => {
    e.preventDefault();
    if (onOpen) onOpen();
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      if (onOpen) onOpen();
    }
  };

  const accessibleLabel =
    ariaLabel ||
    `System Telemetry: CPU ${cpuText}, GPU ${gpuText}, RAM ${ramText}. Click or press Alt+M to open System Monitor.`;

  const tooltipTitle = `System Telemetry (Alt+M)\nCPU: ${cpuText}\nGPU: ${gpuText}\nRAM: ${ramText}${
    metrics.ramUsed ? ` (${metrics.ramUsed} / ${metrics.ramTotal})` : ''
  }\nClick to open full System Monitor`;

  // 1. Vertical Orientation (Right-Side Popped Vertical Chip)
  if (orientation === 'vertical') {
    return (
      <div
        role="button"
        tabIndex={0}
        onClick={handleClick}
        onKeyDown={handleKeyDown}
        title={tooltipTitle}
        aria-label={accessibleLabel}
        data-testid="system-metrics-chip-vertical"
        className={`fixed right-3 sm:right-4 top-1/2 -translate-y-1/2 z-40
          flex flex-col items-center gap-2 p-2 sm:p-2.5 rounded-2xl
          bg-white/95 dark:bg-[#121214]/95 hover:bg-white dark:hover:bg-[#18181b]
          border border-slate-200/90 dark:border-zinc-800/90 hover:border-brand-500/60 dark:hover:border-brand-500/60
          shadow-xl shadow-slate-900/10 dark:shadow-black/50 hover:shadow-2xl hover:-translate-x-1
          backdrop-blur-md transition-all duration-200 cursor-pointer select-none group
          active:scale-95 focus:outline-none focus:ring-2 focus:ring-brand-500/40 focus:ring-offset-2 dark:focus:ring-offset-black ${className}`}
      >
        {/* Top Live Status Indicator */}
        <div className="flex items-center justify-center py-0.5" aria-hidden="true">
          <span className="relative flex h-2 w-2">
            <span
              className={`animate-ping absolute inline-flex h-full w-full rounded-full opacity-75 ${
                isLive ? 'bg-emerald-400' : 'bg-amber-400'
              }`}
            />
            <span
              className={`relative inline-flex rounded-full h-2 w-2 ${
                isLive ? 'bg-emerald-500' : 'bg-amber-500'
              }`}
            />
          </span>
        </div>

        {/* Separator */}
        <span className="w-4 h-px bg-slate-200 dark:bg-zinc-800" aria-hidden="true" />

        {/* CPU Metric Column */}
        <div
          className="flex flex-col items-center text-center gap-0.5 group-hover:text-amber-500 transition-colors"
          data-testid="chip-cpu"
        >
          <Cpu size={14} className="text-amber-500 flex-shrink-0" />
          <span className="text-[8px] font-sans font-bold text-slate-400 dark:text-zinc-500 uppercase tracking-wider">
            CPU
          </span>
          <span className={`text-[11px] sm:text-xs font-mono ${getLoadColor(metrics.cpu)}`}>
            {cpuText}
          </span>
        </div>

        {/* Separator */}
        <span className="w-4 h-px bg-slate-200 dark:bg-zinc-800" aria-hidden="true" />

        {/* GPU Metric Column */}
        <div
          className="flex flex-col items-center text-center gap-0.5 group-hover:text-emerald-500 transition-colors"
          data-testid="chip-gpu"
        >
          <Zap size={14} className="text-emerald-500 flex-shrink-0" />
          <span className="text-[8px] font-sans font-bold text-slate-400 dark:text-zinc-500 uppercase tracking-wider">
            GPU
          </span>
          <span className={`text-[11px] sm:text-xs font-mono ${getLoadColor(metrics.gpu)}`}>
            {gpuText}
          </span>
        </div>

        {/* Separator */}
        <span className="w-4 h-px bg-slate-200 dark:bg-zinc-800" aria-hidden="true" />

        {/* RAM Metric Column */}
        <div
          className="flex flex-col items-center text-center gap-0.5 group-hover:text-indigo-500 transition-colors"
          data-testid="chip-ram"
        >
          <HardDrive size={14} className="text-indigo-500 flex-shrink-0" />
          <span className="text-[8px] font-sans font-bold text-slate-400 dark:text-zinc-500 uppercase tracking-wider">
            RAM
          </span>
          <span className={`text-[11px] sm:text-xs font-mono ${getLoadColor(metrics.ram)}`}>
            {ramText}
          </span>
        </div>

        {/* Bottom Shortcut Badge */}
        {showShortcut && (
          <>
            <span className="w-4 h-px bg-slate-200 dark:bg-zinc-800" aria-hidden="true" />
            <span className="text-[8px] font-mono px-1 py-0.5 rounded bg-slate-100 dark:bg-zinc-800/80 text-slate-400 dark:text-zinc-500 border border-slate-200 dark:border-zinc-700/60 group-hover:text-brand-500 group-hover:border-brand-500/40 transition-colors">
              Alt+M
            </span>
          </>
        )}
      </div>
    );
  }

  // 2. Horizontal Orientation
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={handleClick}
      onKeyDown={handleKeyDown}
      title={tooltipTitle}
      aria-label={accessibleLabel}
      data-testid="system-metrics-chip-horizontal"
      className={`group relative inline-flex items-center gap-1.5 sm:gap-2 px-2.5 sm:px-3 py-1.5 rounded-full text-xs font-medium 
        bg-white/90 dark:bg-[#121214]/90 hover:bg-slate-50 dark:hover:bg-zinc-800/90
        border border-slate-200/90 dark:border-zinc-800/90 hover:border-brand-500/50 dark:hover:border-brand-500/50
        shadow-sm hover:shadow-md transition-all duration-150 cursor-pointer select-none
        active:scale-[0.98] focus:outline-none focus:ring-2 focus:ring-brand-500/40 focus:ring-offset-1 dark:focus:ring-offset-zinc-900 ${className}`}
    >
      {/* Live Status Pulse Dot */}
      <span className="relative flex h-2 w-2 flex-shrink-0" aria-hidden="true">
        <span
          className={`animate-ping absolute inline-flex h-full w-full rounded-full opacity-75 ${
            isLive ? 'bg-emerald-400' : 'bg-amber-400'
          }`}
        />
        <span
          className={`relative inline-flex rounded-full h-2 w-2 ${
            isLive ? 'bg-emerald-500' : 'bg-amber-500'
          }`}
        />
      </span>

      {/* Metrics Container */}
      <div className="flex items-center gap-1.5 sm:gap-2 font-mono leading-none">
        {/* CPU Metric */}
        <div
          className="flex items-center gap-1 hover:text-amber-500 transition-colors"
          data-testid="chip-cpu"
        >
          <Cpu size={12} className="text-amber-500 flex-shrink-0" />
          <span className="text-[10px] font-sans font-semibold text-slate-500 dark:text-zinc-400 uppercase hidden sm:inline">
            CPU
          </span>
          <span className={`text-xs ${getLoadColor(metrics.cpu)}`}>
            {cpuText}
          </span>
        </div>

        {/* Separator */}
        <span className="w-px h-3 bg-slate-200 dark:bg-zinc-800" aria-hidden="true" />

        {/* GPU Metric */}
        <div
          className="flex items-center gap-1 hover:text-emerald-500 transition-colors"
          data-testid="chip-gpu"
        >
          <Zap size={12} className="text-emerald-500 flex-shrink-0" />
          <span className="text-[10px] font-sans font-semibold text-slate-500 dark:text-zinc-400 uppercase hidden sm:inline">
            GPU
          </span>
          <span className={`text-xs ${getLoadColor(metrics.gpu)}`}>
            {gpuText}
          </span>
        </div>

        {/* Separator */}
        <span className="w-px h-3 bg-slate-200 dark:bg-zinc-800" aria-hidden="true" />

        {/* RAM Metric */}
        <div
          className="flex items-center gap-1 hover:text-indigo-500 transition-colors"
          data-testid="chip-ram"
        >
          <HardDrive size={12} className="text-indigo-500 flex-shrink-0" />
          <span className="text-[10px] font-sans font-semibold text-slate-500 dark:text-zinc-400 uppercase hidden sm:inline">
            RAM
          </span>
          <span className={`text-xs ${getLoadColor(metrics.ram)}`}>
            {ramText}
          </span>
        </div>
      </div>

      {/* Shortcut Indicator Badge */}
      {showShortcut && (
        <span className="hidden md:inline-flex items-center text-[9px] font-mono px-1.5 py-0.5 rounded bg-slate-100 dark:bg-zinc-800 text-slate-500 dark:text-zinc-400 border border-slate-200/70 dark:border-zinc-700/70 group-hover:border-brand-500/30 group-hover:text-brand-600 dark:group-hover:text-brand-400 transition-colors ml-0.5">
          Alt+M
        </span>
      )}
    </div>
  );
}
