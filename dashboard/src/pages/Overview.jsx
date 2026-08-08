import React, { useState, useEffect } from 'react';
import { Activity, Database, Zap, HardDrive, Cpu, DollarSign, RefreshCw, BarChart2, MessageCircle, Percent } from 'lucide-react';
import { apiCall } from '../utils/api';
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

  const sentimentData = {
    labels: sentDist.labels.length ? sentDist.labels : ['Positive', 'Neutral', 'Negative'],
    datasets: [{
      data: sentDist.values.length ? sentDist.values : [0, 0, 0],
      backgroundColor: ['#10b981', '#64748b', '#ef4444'],
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
          subtext={`≈ $${Number(usage?.estimated_cost_usd || 0).toFixed(4)}`}
        />
        <StatCard 
          title="LLM Use (Posts)" 
          value={`${panel.posts_with_llm || 0}`} 
          icon={MessageCircle} 
          subtext={`Out of ${panel.total_posts || 0} total posts`}
        />
        <StatCard 
          title="Cost split" 
          value={usage?.lane_split?.post?.calls ? "Available" : "None"} 
          icon={DollarSign} 
          subtext={usage?.lane_split?.comment?.calls ? `${Math.round(((usage.lane_split.comment.calls) / ((usage.lane_split.post.calls || 0) + (usage.lane_split.comment.calls || 0) + (usage.lane_split.stage1?.calls || 0))) * 100)}% of pipeline calls are comment-level` : 'No pipeline LLM calls yet'}
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
    </div>
  );
}
