import React, { useState, useEffect } from 'react';
import { Activity, Database, Zap, Cpu, DollarSign, RefreshCw, MessageCircle, Percent } from 'lucide-react';
import { apiCall } from '../utils/api';
import { sentimentColors } from '../utils/sentiment';
import { Chart as ChartJS, ArcElement, Tooltip, Legend, CategoryScale, LinearScale, BarElement, Title } from 'chart.js';
import { Doughnut, Bar } from 'react-chartjs-2';

ChartJS.register(ArcElement, Tooltip, Legend, CategoryScale, LinearScale, BarElement, Title);

const parseDist = (data) => {
  if (!data) return { labels: [], values: [] };
  if (Array.isArray(data)) {
    return {
      labels: data.map(d => d.label || d.topic || d.language || d.emotion || Object.values(d)[0]),
      values: data.map(d => Number(d.value || d.count || Object.values(d)[1]))
    };
  }
  return {
    labels: Object.keys(data),
    values: Object.values(data)
  };
};

export default function Overview({ isActive }) {
  const [usage, setUsage] = useState(null);
  const [overview, setOverview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const loadOverview = async () => {
    setLoading(true);
    setError(null);
    try {
      const [usageData, overviewData] = await Promise.all([
        apiCall('/v1/usage').catch(() => ({})),
        apiCall('/v1/analysis/overview').catch(() => ({}))
      ]);
      setUsage(usageData);
      setOverview(overviewData);
    } catch (err) {
      setError(err.message);
    }
    setLoading(false);
  };

  useEffect(() => {
    if (isActive) loadOverview();
    
    const handleAutoRefresh = () => {
      if (isActive) loadOverview();
    };
    
    window.addEventListener('auto-refresh', handleAutoRefresh);
    return () => window.removeEventListener('auto-refresh', handleAutoRefresh);
  }, [isActive]);

  const StatCard = ({ title, value, subtext, icon: Icon }) => (
    <div className="bg-slate-50 dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 rounded-xl p-6 shadow-sm hover:shadow-md transition-shadow">
      <div className="flex items-start justify-between mb-4">
        <div className="p-2 bg-brand-500/10 text-brand-600 dark:text-brand-400 rounded-lg">
          <Icon size={24} />
        </div>
      </div>
      <div className="text-3xl font-bold mb-1 text-slate-900 dark:text-white">
        {value}
      </div>
      <div className="text-sm font-medium text-slate-500 dark:text-zinc-400">
        {title}
      </div>
      {subtext && (
        <div className="text-xs text-slate-400 dark:text-zinc-500 mt-2">
          <span dangerouslySetInnerHTML={{ __html: subtext }}></span>
        </div>
      )}
    </div>
  );

  const sentDist = parseDist(overview?.sentiment_distribution);
  const topicDist = parseDist(overview?.top_topics);
  const langDist = parseDist(overview?.language_distribution);
  const emoDist = parseDist(overview?.comment_emotion_distribution);

  // Colours are keyed on the LABEL, not its position. The API returns
  // {positive, negative, neutral, mixed} in that order, so the old positional
  // palette [green, grey, red] painted negative grey and neutral red.
  const sentimentLabels = sentDist.labels.length
    ? sentDist.labels
    : ['positive', 'negative', 'neutral'];
  const sentimentData = {
    labels: sentimentLabels,
    datasets: [{
      data: sentDist.values.length ? sentDist.values : [0, 0, 0],
      backgroundColor: sentimentColors(sentimentLabels),
      borderWidth: 0,
      hoverOffset: 4
    }]
  };

  const topicsData = {
    labels: topicDist.labels,
    datasets: [{
      label: 'Frequency',
      data: topicDist.values,
      backgroundColor: '#3b82f6',
      borderRadius: 4
    }]
  };

  const langData = {
    labels: langDist.labels,
    datasets: [{
      label: 'Posts',
      data: langDist.values,
      backgroundColor: '#f59e0b',
      borderRadius: 4
    }]
  };

  const emoData = {
    labels: emoDist.labels,
    datasets: [{
      label: 'Comments',
      data: emoDist.values,
      backgroundColor: '#8b5cf6',
      borderRadius: 4
    }]
  };

  const chartOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { position: 'bottom', labels: { color: '#888' } }
    }
  };

  const barOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: false }
    },
    scales: {
      y: { ticks: { color: '#888' }, grid: { color: '#33333333' } },
      x: { ticks: { color: '#888' }, grid: { display: false } }
    }
  };

  // Coverage
  const cc = overview?.corpus_coverage || {};
  let coverageHtml = 'No coverage data';
  if (cc.reported) {
    coverageHtml = `<b>${Math.round((Number(cc.coverage) || 0) * 100)}%</b> coverage (${(cc.analyzed || 0).toLocaleString()} of ${(cc.reported || 0).toLocaleString()} comments)`;
  }

  // LLM Panel
  const panel = overview?.llm_panel || {};

  // Dynamic Cost & Token Economics
  const lanes = usage?.lane_split || {};
  const postCalls = lanes.post?.calls || 0;
  const commentCalls = lanes.comment?.calls || 0;
  const stage1Calls = lanes.stage1?.calls || 0;
  // The interactive/agent lanes are not hoisted into consts: the lane table
  // below iterates LANE_HINTS and reads `lanes[laneKey]`, so it already renders
  // all five. Hoisting only the two nobody reads was leftover from before.

  const postTokens = lanes.post?.tokens || 0;
  const commentTokens = lanes.comment?.tokens || 0;

  const pipelineCalls = postCalls + commentCalls + stage1Calls;
  // Every lane the table below renders, not just the three pipeline ones: the
  // share column divided by pipelineCalls, so the interactive lane — which is
  // not a pipeline lane and is often the largest — reported 145%.
  const allLaneCalls = Object.values(lanes).reduce((n, l) => n + (l?.calls || 0), 0);
  const totalTokens = usage?.total_tokens || 0;
  const estimatedCost = Number(usage?.estimated_cost_usd || 0);

  // Frontier market price benchmark: $5.00 per 1M tokens ($0.005 per 1k tokens - e.g. GPT-4o / Claude 3.5)
  const marketPriceEquiv = Number(
    usage?.market_price_equivalent_usd ?? (totalTokens > 0 ? (totalTokens / 1000) * 0.005 : 0)
  );
  const costSavings = Number(
    usage?.cost_savings_usd ?? Math.max(0, marketPriceEquiv - estimatedCost)
  );

  const byModel = usage?.tokens_by_backend_model || {};
  const costByModel = usage?.cost_by_backend_model || {};
  const modelKeys = Object.keys(byModel);
  const isAllLocal = modelKeys.length === 0 || modelKeys.every(k => k.startsWith('local:'));

  const LANE_HINTS = {
    post: 'summary / post_type / insight',
    comment: 'comment stance, thread summary',
    stage1: "Stage-1's own LLM path",
    interactive: 'chat, reports, interactive analysis',
    agent: 'agent tool-calling turns'
  };
  
  const commentMarketCost = (commentTokens / 1000) * 0.005;
  const postMarketCost = (postTokens / 1000) * 0.005;

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500 pb-12">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">System Overview</h2>
          <p className="text-slate-500 dark:text-zinc-400">Live telemetry and pipeline statistics.</p>
        </div>
        <button 
          onClick={loadOverview}
          disabled={loading}
          className="px-4 py-2 bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-800 rounded-lg hover:bg-slate-50 dark:hover:bg-zinc-800 transition-colors flex items-center gap-2"
        >
          <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
          Refresh
        </button>
      </div>

      {error && (
        <div className="p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-900/50 rounded-lg text-red-600 dark:text-red-400">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-6">
        <StatCard 
          title="Posts Analyzed" 
          value={usage?.posts_analyzed?.toLocaleString() || '0'} 
          icon={Database} 
          subtext="Total items processed through pipeline"
        />
        <StatCard 
          title="LLM-routed posts" 
          value={usage?.llm_calls?.toLocaleString() || '0'} 
          icon={Activity} 
          subtext={`${Math.round((Number(usage?.llm_routing_rate) || 0) * 100)}% routing rate`}
        />
        <StatCard 
          title="LLM API Calls" 
          value={usage?.llm_api_calls?.toLocaleString() || '0'} 
          icon={Zap} 
          subtext={`${usage?.cache_hits || 0} cache hits globally`}
        />
        <StatCard 
          title="Corpus Coverage" 
          value={cc.reported ? `${Math.round((Number(cc.coverage) || 0) * 100)}%` : '0%'} 
          icon={Percent} 
          subtext={coverageHtml}
        />
        <StatCard 
          title="Pipeline tokens" 
          value={usage?.pipeline_tokens?.toLocaleString() || '0'} 
          icon={Cpu} 
          subtext="post + comment + stage-1 lanes"
        />
        <StatCard 
          title="Tokens used (all)" 
          value={usage?.total_tokens?.toLocaleString() || '0'} 
          icon={Database} 
          subtext={isAllLocal || estimatedCost === 0 ? `Local Cost: <b>$0.00</b> · Market: ~<b>$${marketPriceEquiv.toFixed(4)}</b>` : `Incurred: <b>$${estimatedCost.toFixed(4)}</b> (Saved ~<b>$${costSavings.toFixed(4)}</b>)`}
        />
        <StatCard 
          title="LLM Use (Posts)" 
          value={`${panel.posts_with_llm || 0}`} 
          icon={MessageCircle} 
          subtext={`Out of ${panel.total_posts || 0} total posts`}
        />
        <StatCard 
          title="Cost: Free Now vs Paid API" 
          value={
            <div className="flex items-baseline gap-2">
              <span className="text-emerald-600 dark:text-emerald-400 font-bold">$0.00</span>
              <span className="text-sm font-normal text-slate-400 dark:text-zinc-500 line-through">
                ~${marketPriceEquiv.toFixed(4)}
              </span>
            </div>
          } 
          icon={DollarSign} 
          subtext={pipelineCalls ? `<b>Now (Local):</b> $0.00 Free · <b>If Paid (Cloud):</b> ~$${marketPriceEquiv.toFixed(4)} (Comments: ~$${commentMarketCost.toFixed(3)}, Posts: ~$${postMarketCost.toFixed(3)})` : 'Now: $0.00 Free · Commercial Cloud API: $0.50/1M tokens'}
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        
        <div className="bg-slate-50 dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 rounded-xl p-6 shadow-sm flex flex-col">
          <h3 className="text-lg font-semibold mb-6">Sentiment (Corpus)</h3>
          <div className="flex-1 relative min-h-[250px]">
            <Doughnut data={sentimentData} options={chartOptions} />
          </div>
        </div>

        <div className="lg:col-span-2 bg-slate-50 dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 rounded-xl p-6 shadow-sm flex flex-col">
          <h3 className="text-lg font-semibold mb-6">Top Topics</h3>
          <div className="flex-1 relative min-h-[250px]">
            {topicDist.labels.length > 0 ? (
              <Bar data={topicsData} options={barOptions} />
            ) : (
              <div className="h-full flex items-center justify-center text-slate-400">
                No topics discovered yet
              </div>
            )}
          </div>
        </div>

        <div className="lg:col-span-2 bg-slate-50 dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 rounded-xl p-6 shadow-sm flex flex-col">
          <h3 className="text-lg font-semibold mb-6">Comment Emotions</h3>
          <div className="flex-1 relative min-h-[250px]">
            {emoDist.labels.length > 0 ? (
              <Bar data={emoData} options={barOptions} />
            ) : (
              <div className="h-full flex items-center justify-center text-slate-400">
                No emotions discovered yet
              </div>
            )}
          </div>
        </div>

        <div className="bg-slate-50 dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 rounded-xl p-6 shadow-sm flex flex-col">
          <h3 className="text-lg font-semibold mb-6">Pipeline / LLM</h3>
          <div className="flex-1 flex flex-col gap-4 text-sm">
            <div className="flex justify-between items-center border-b border-slate-200 dark:border-zinc-800 pb-2">
              <span className="text-slate-500">Posts with LLM output</span>
              <span className="font-semibold text-slate-700 dark:text-slate-300">{panel.posts_with_llm || 0} / {panel.total_posts || 0}</span>
            </div>
            <div className="flex justify-between items-center border-b border-slate-200 dark:border-zinc-800 pb-2">
              <span className="text-slate-500">Posts with summaries</span>
              <span className="font-semibold text-slate-700 dark:text-slate-300">{panel.posts_with_summaries || 0}</span>
            </div>
            <div className="flex flex-col gap-2 pt-2">
              <span className="text-slate-500">Backends seen</span>
              <div className="flex flex-wrap gap-2">
                {(panel.backends_seen || []).map((b, i) => (
                  <span key={i} className="px-2 py-1 bg-brand-500/10 text-brand-600 dark:text-brand-400 rounded text-xs font-medium">
                    {b.label} ({b.count})
                  </span>
                ))}
                {!(panel.backends_seen || []).length && <span className="text-slate-400">None</span>}
              </div>
            </div>
          </div>
        </div>

        <div className="lg:col-span-2 bg-slate-50 dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 rounded-xl p-6 shadow-sm flex flex-col">
          <h3 className="text-lg font-semibold mb-6">Languages</h3>
          <div className="flex-1 relative min-h-[250px]">
            {langDist.labels.length > 0 ? (
              <Bar data={langData} options={{...barOptions, indexAxis: 'y'}} />
            ) : (
              <div className="h-full flex items-center justify-center text-slate-400">
                No languages discovered yet
              </div>
            )}
          </div>
        </div>

      </div>

      {/* Dynamic LLM Cost & Token Economics Panel */}
      <div className="bg-slate-50 dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 rounded-xl p-6 shadow-sm space-y-6">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-slate-200 dark:border-zinc-800 pb-4">
          <div>
            <h3 className="text-lg font-semibold flex items-center gap-2">
              <DollarSign size={20} className="text-emerald-500" />
              LLM Token & Cost Economics
            </h3>
            <p className="text-xs text-slate-500 dark:text-zinc-400 mt-1">
              Dynamic spend attribution across pipeline lanes, models, and market benchmarks.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="px-2.5 py-1 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/20 rounded-full text-xs font-semibold">
              Local Backend: $0.00 / token (Self-Hosted)
            </span>
            <span className="px-2.5 py-1 bg-purple-500/10 text-purple-600 dark:text-purple-400 border border-purple-500/20 rounded-full text-xs font-medium">
              Frontier Benchmark: $5.00 / 1M tok ($0.005/1K - GPT-4o)
            </span>
          </div>
        </div>

        {/* Cost Comparison Summary Widget */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div className="p-4 bg-white dark:bg-[#18181b] border border-slate-200 dark:border-zinc-800 rounded-lg">
            <div className="text-xs font-medium text-slate-500 dark:text-zinc-400">Actual Incurred Cost</div>
            <div className="text-2xl font-bold mt-1 text-slate-900 dark:text-white">
              ${estimatedCost.toFixed(4)}
            </div>
            <div className="text-[11px] text-emerald-600 dark:text-emerald-400 mt-1 font-medium">
              {isAllLocal ? '✓ 100% Local GPU Execution ($0 token spend)' : `Priced across ${modelKeys.length} active models`}
            </div>
          </div>

          <div className="p-4 bg-white dark:bg-[#18181b] border border-slate-200 dark:border-zinc-800 rounded-lg">
            <div className="text-xs font-medium text-slate-500 dark:text-zinc-400">Frontier Cloud Market Value</div>
            <div className="text-2xl font-bold mt-1 text-slate-900 dark:text-white">
              ~${marketPriceEquiv.toFixed(4)}
            </div>
            <div className="text-[11px] text-slate-400 dark:text-zinc-500 mt-1">
              Equivalent cost on frontier APIs ({totalTokens.toLocaleString()} tokens @ $5.00/1M)
            </div>
          </div>

          <div className="p-4 bg-white dark:bg-[#18181b] border border-emerald-500/30 bg-emerald-50/20 dark:bg-emerald-950/10 rounded-lg">
            <div className="text-xs font-medium text-emerald-700 dark:text-emerald-400">Self-Hosted Cost Savings</div>
            <div className="text-2xl font-bold mt-1 text-emerald-600 dark:text-emerald-400">
              +${costSavings.toFixed(4)}
            </div>
            <div className="text-[11px] text-emerald-700/80 dark:text-emerald-400/80 mt-1 font-medium">
              {totalTokens > 0 ? `${Math.round((costSavings / Math.max(0.0001, marketPriceEquiv)) * 100)}% savings vs commercial cloud LLM APIs` : '0 tokens spent'}
            </div>
          </div>
        </div>

        {/* Spend by Lane & Model Details */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Lane Split Table */}
          <div className="space-y-3">
            <h4 className="text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-zinc-400 flex items-center justify-between">
              <span>Spend by Pipeline & Interactive Lane</span>
              <span className="font-mono text-[10px] text-slate-400">{allLaneCalls} total calls</span>
            </h4>
            <div className="bg-white dark:bg-[#18181b] border border-slate-200 dark:border-zinc-800 rounded-lg overflow-hidden">
              <table className="w-full text-xs text-left">
                <thead className="bg-slate-100 dark:bg-zinc-800/60 text-slate-500 dark:text-zinc-400 border-b border-slate-200 dark:border-zinc-800">
                  <tr>
                    <th className="py-2.5 px-3 font-semibold">Lane</th>
                    <th className="py-2.5 px-3 font-semibold text-right">Calls (Share)</th>
                    <th className="py-2.5 px-3 font-semibold text-right">Tokens (Share)</th>
                    <th className="py-2.5 px-3 font-semibold text-right">Local Cost</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-zinc-800/50">
                  {Object.entries(LANE_HINTS).map(([laneKey, laneHint]) => {
                    const l = lanes[laneKey] || { calls: 0, tokens: 0 };
                    // The API computes both shares over the same denominator it
                    // reports; prefer them, and fall back to the lane totals.
                    const callPct = Math.round(
                      (l.call_share ?? (allLaneCalls ? (l.calls || 0) / allLaneCalls : 0)) * 100
                    );
                    const tokPct = Math.round(
                      (l.token_share ?? (totalTokens ? (l.tokens || 0) / totalTokens : 0)) * 100
                    );
                    return (
                      <tr key={laneKey} className="hover:bg-slate-50/50 dark:hover:bg-zinc-800/30 transition-colors">
                        <td className="py-2.5 px-3">
                          <div className="font-semibold text-slate-800 dark:text-zinc-200 capitalize">{laneKey}</div>
                          <div className="text-[10px] text-slate-400 dark:text-zinc-500">{laneHint}</div>
                        </td>
                        <td className="py-2.5 px-3 text-right font-mono">
                          <span className="font-semibold text-slate-700 dark:text-zinc-300">{(l.calls || 0).toLocaleString()}</span>
                          <span className="text-slate-400 text-[10px] ml-1">({callPct}%)</span>
                        </td>
                        <td className="py-2.5 px-3 text-right font-mono">
                          <span className="font-semibold text-slate-700 dark:text-zinc-300">{(l.tokens || 0).toLocaleString()}</span>
                          <span className="text-slate-400 text-[10px] ml-1">({tokPct}%)</span>
                        </td>
                        <td className="py-2.5 px-3 text-right font-mono text-emerald-600 dark:text-emerald-400 font-semibold">
                          $0.00
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>

          {/* Tokens by Backend & Model */}
          <div className="space-y-3">
            <h4 className="text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-zinc-400 flex items-center justify-between">
              <span>Tokens by Backend & Model</span>
              <span className="font-mono text-[10px] text-slate-400">{modelKeys.length} registered models</span>
            </h4>
            <div className="bg-white dark:bg-[#18181b] border border-slate-200 dark:border-zinc-800 rounded-lg overflow-hidden">
              {modelKeys.length === 0 ? (
                <div className="p-6 text-center text-slate-400 dark:text-zinc-500 text-xs">
                  <Cpu size={24} className="mx-auto mb-2 opacity-40" />
                  No per-model token counters recorded yet. Local models will record at $0.00/token upon execution.
                </div>
              ) : (
                <table className="w-full text-xs text-left">
                  <thead className="bg-slate-100 dark:bg-zinc-800/60 text-slate-500 dark:text-zinc-400 border-b border-slate-200 dark:border-zinc-800">
                    <tr>
                      <th className="py-2.5 px-3 font-semibold">Backend / Model</th>
                      <th className="py-2.5 px-3 font-semibold text-right">Tokens</th>
                      <th className="py-2.5 px-3 font-semibold text-right">Unit Rate</th>
                      <th className="py-2.5 px-3 font-semibold text-right">Cost</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 dark:divide-zinc-800/50">
                    {modelKeys.map((key) => {
                      const toks = byModel[key] || 0;
                      const c = Number(costByModel[key] || 0);
                      const isLocal = key.startsWith('local:');
                      return (
                        <tr key={key} className="hover:bg-slate-50/50 dark:hover:bg-zinc-800/30 transition-colors">
                          <td className="py-2.5 px-3">
                            <div className="font-semibold text-slate-800 dark:text-zinc-200 font-mono text-[11px]">{key}</div>
                            <div className="text-[10px] text-slate-400">
                              {isLocal ? 'Self-hosted vLLM GPU' : 'Cloud API'}
                            </div>
                          </td>
                          <td className="py-2.5 px-3 text-right font-mono text-slate-700 dark:text-zinc-300">
                            {toks.toLocaleString()}
                          </td>
                          <td className="py-2.5 px-3 text-right font-mono text-[11px] text-slate-500">
                            {isLocal ? '$0.00 / 1K' : 'Cloud / 1K'}
                          </td>
                          <td className="py-2.5 px-3 text-right font-mono font-bold">
                            {isLocal ? (
                              <span className="text-emerald-600 dark:text-emerald-400">$0.00 (Free)</span>
                            ) : (
                              <span className="text-slate-900 dark:text-white">${c.toFixed(4)}</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </div>

        {/* Paid vs Free Scale Economics Table */}
        <div className="pt-4 border-t border-slate-200 dark:border-zinc-800 space-y-3">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
            <h4 className="text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-zinc-400">
              Paid vs Free Cost Projection at Scale
            </h4>
            <span className="text-[11px] text-slate-400 dark:text-zinc-500">
              Benchmark: Frontier Cloud API (GPT-4o / Claude 3.5 @ $5.00 / 1M tokens) vs Local vLLM ($0.00)
            </span>
          </div>

          <div className="bg-white dark:bg-[#18181b] border border-slate-200 dark:border-zinc-800 rounded-lg overflow-hidden">
            <table className="w-full text-xs text-left">
              <thead className="bg-slate-100 dark:bg-zinc-800/60 text-slate-500 dark:text-zinc-400 border-b border-slate-200 dark:border-zinc-800">
                <tr>
                  <th className="py-2.5 px-3 font-semibold">Scale / Batch Size</th>
                  <th className="py-2.5 px-3 font-semibold text-right">Est. Token Volume</th>
                  <th className="py-2.5 px-3 font-semibold text-right">Now (Local vLLM)</th>
                  <th className="py-2.5 px-3 font-semibold text-right">If Paid Frontier API</th>
                  <th className="py-2.5 px-3 font-semibold text-right">Total Net Savings</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-zinc-800/50">
                {[
                  { scale: 'Current Corpus Run', tokens: totalTokens, paid: marketPriceEquiv, free: estimatedCost },
                  { scale: '1,000 Posts (~1,000 threads)', tokens: 1000000, paid: 5.00, free: 0 },
                  { scale: '10,000 Posts (~10,000 threads)', tokens: 10000000, paid: 50.00, free: 0 },
                  { scale: '100,000 Posts (Enterprise)', tokens: 100000000, paid: 500.00, free: 0 },
                  { scale: '1,000,000 Posts (Full Volume)', tokens: 1000000000, paid: 5000.00, free: 0 },
                ].map((row, idx) => (
                  <tr key={idx} className={idx === 0 ? 'bg-brand-50/30 dark:bg-brand-950/20 font-medium' : 'hover:bg-slate-50/50 dark:hover:bg-zinc-800/30'}>
                    <td className="py-2.5 px-3">
                      <div className="font-semibold text-slate-800 dark:text-zinc-200">{row.scale}</div>
                    </td>
                    <td className="py-2.5 px-3 text-right font-mono text-slate-600 dark:text-zinc-400">
                      {row.tokens.toLocaleString()} tok
                    </td>
                    <td className="py-2.5 px-3 text-right font-mono font-bold text-emerald-600 dark:text-emerald-400">
                      ${row.free.toFixed(2)} (Free)
                    </td>
                    <td className="py-2.5 px-3 text-right font-mono text-slate-700 dark:text-zinc-300">
                      ${row.paid.toFixed(2)}
                    </td>
                    <td className="py-2.5 px-3 text-right font-mono font-bold text-emerald-600 dark:text-emerald-400">
                      +${(row.paid - row.free).toFixed(2)} (100% Saved)
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
