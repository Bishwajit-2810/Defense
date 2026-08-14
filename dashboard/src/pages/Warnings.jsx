import React, { useState, useEffect } from 'react';
import { apiCall, API_BASE, getAuthHeaders } from '../utils/api.js';
import { formatAlertReason } from '../utils/sentiment.js';
import PostModal from '../components/PostModal';

export default function Warnings() {
  const [posts, setPosts] = useState([]);
  const [scanned, setScanned] = useState(0);
  const [loading, setLoading] = useState(false);
  const [selectedPost, setSelectedPost] = useState(null);

  useEffect(() => {
    fetchPosts();
    
    const handleAutoRefresh = () => fetchPosts();
    window.addEventListener('auto-refresh', handleAutoRefresh);
    return () => window.removeEventListener('auto-refresh', handleAutoRefresh);
  }, []);

  const fetchPosts = async () => {
    try {
      setLoading(true);
      // 500 is the API's maximum page. This page therefore covers the most
      // recent 500 analysed posts, not the whole corpus — `scanned` is shown so
      // an empty list reads as "none in the last N" rather than "none, ever".
      let url = '/v1/analysis/latest?limit=500&include=results';
      const data = await apiCall(url);
      const allPosts = data.results || (Array.isArray(data) ? data : []);
      setScanned(allPosts.length);
      setPosts(allPosts.filter(p => p.watchlist_alert));
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };
  
  const downloadZip = async () => {
    try {
      // The export is bounded server-side (rendering N PDFs inside one request).
      // Ask for the maximum page and tell the user when it was truncated rather
      // than handing over a partial ZIP that looks complete.
      const res = await fetch(`${API_BASE}/v1/analysis/export?only_warnings=true&limit=200`, {
        headers: getAuthHeaders()
      });
      if (!res.ok) throw new Error('Download failed');
      const count = Number(res.headers.get('X-Export-Count'));
      const limit = Number(res.headers.get('X-Export-Limit'));
      if (Number.isFinite(count) && Number.isFinite(limit) && count >= limit) {
        alert(`This ZIP holds the ${count} most recent alerts (the per-request maximum). Use offset= to fetch older ones.`);
      }
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `defense_warnings.zip`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (err) {
      console.error(err);
      alert('Failed to download ZIP');
    }
  };

  const renderToxicityBar = (score) => {
    const p = Math.round(score * 100);
    const color = score < 0.33 ? '#10b981' : score < 0.66 ? '#f59e0b' : '#ef4444';
    return (
      <div className="flex items-center gap-2">
        <div className="w-16 h-2 bg-slate-200 dark:bg-zinc-700 rounded-full overflow-hidden">
          <div className="h-full" style={{ width: `${p}%`, backgroundColor: color }}></div>
        </div>
        <span className="text-xs text-slate-500">{p}%</span>
      </div>
    );
  };

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div>
        <h2 className="text-2xl font-bold tracking-tight text-rose-600 dark:text-rose-500 flex items-center gap-2">
          ⚠️ Watchlist alerts
        </h2>
        <p className="text-slate-500 dark:text-zinc-400">
          Posts matching a watchlist rule: any mention of an <code className="font-mono text-xs">always</code> target,
          or a comment opposing a <code className="font-mono text-xs">favored</code> one. Each alert states which.
        </p>
      </div>

      <div className="bg-white dark:bg-[#09090b] border border-rose-200 dark:border-rose-900/50 rounded-xl overflow-hidden shadow-sm">
        <div className="p-4 border-b border-rose-100 dark:border-rose-900/30 bg-rose-50/50 dark:bg-rose-900/10 flex justify-between items-center flex-wrap gap-4">
          <div>
            <h3 className="font-semibold text-rose-900 dark:text-rose-300">Alert Results</h3>
            <p className="text-xs text-rose-600/70 dark:text-rose-400/70">{posts.length} active alerts · scanned the {scanned} most recent analysed posts</p>
          </div>
          <div className="flex gap-2 items-center">
            <button 
              onClick={downloadZip}
              className="px-3 py-1.5 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg hover:bg-slate-50 dark:hover:bg-zinc-800"
            >
              Export ZIP
            </button>
            <button 
              onClick={fetchPosts} 
              disabled={loading}
              className="px-3 py-1.5 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg hover:bg-slate-50 dark:hover:bg-zinc-800 disabled:opacity-50"
            >
              {loading ? 'Refreshing...' : 'Refresh'}
            </button>
          </div>
        </div>
        
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm whitespace-nowrap">
            <thead className="bg-slate-50 dark:bg-[#121214] text-slate-500 dark:text-zinc-400">
              <tr>
                <th className="px-4 py-3 font-medium">#</th>
                <th className="px-4 py-3 font-medium">Post ID</th>
                <th className="px-4 py-3 font-medium">Platform</th>
                <th className="px-4 py-3 font-medium">Language</th>
                <th className="px-4 py-3 font-medium">Why it alerted</th>
                <th className="px-4 py-3 font-medium">Sentiment</th>
                <th className="px-4 py-3 font-medium">Toxicity</th>
                <th className="px-4 py-3 font-medium">Summary</th>
                <th className="px-4 py-3 font-medium">Created At</th>
                <th className="px-4 py-3 font-medium text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-200 dark:divide-zinc-800">
              {posts.length === 0 ? (
                <tr><td colSpan="10" className="px-4 py-8 text-center text-slate-500">No alerts found. Everything looks clear.</td></tr>
              ) : posts.map((post, i) => {
                const sentiment = post.overall_sentiment || 'neutral';
                const toxScore = typeof post.toxicity_score === 'number' ? post.toxicity_score : null;
                const dateStr = post.created_at ? new Date(post.created_at).toLocaleString() : '—';
                let langStr = post.language || '—';
                if (post.language_mix && post.language_mix.length > 1) {
                  langStr += ` +${post.language_mix.length - 1}`;
                }
                const summary = post.post_summary || '—';

                return (
                  <tr key={post.post_id || i} className="hover:bg-rose-50 dark:hover:bg-rose-900/20 cursor-pointer transition-colors bg-rose-50/30 dark:bg-rose-900/10" onClick={() => setSelectedPost(post)}>
                    <td className="px-4 py-3">{i + 1}</td>
                    <td className="px-4 py-3 font-mono text-xs text-rose-700 dark:text-rose-400">
                      {String(post.post_id || '').slice(0,8)}...
                    </td>
                    <td className="px-4 py-3">{post.platform || '—'}</td>
                    <td className="px-4 py-3">{langStr}</td>
                    {/* The rule that fired, not a blanket "under attack": an
                        `always` target alerts on a plain mention. */}
                    <td className="px-4 py-3 max-w-[220px] truncate text-xs text-rose-700 dark:text-rose-400"
                        title={formatAlertReason(post.watchlist_alert_reason) || ''}>
                      {formatAlertReason(post.watchlist_alert_reason) || '—'}
                    </td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-1 rounded-full text-xs font-medium ${
                        sentiment === 'positive' ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400' :
                        sentiment === 'negative' ? 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400' :
                        'bg-slate-100 text-slate-700 dark:bg-zinc-800 dark:text-zinc-300'
                      }`}>
                        {sentiment}
                      </span>
                    </td>
                    <td className="px-4 py-3">{toxScore !== null ? renderToxicityBar(toxScore) : '—'}</td>
                    <td className="px-4 py-3 max-w-[200px] truncate" title={summary}>{summary}</td>
                    <td className="px-4 py-3 text-xs">{dateStr}</td>
                    <td className="px-4 py-3 text-right">
                      <button 
                        onClick={(e) => { e.stopPropagation(); setSelectedPost(post); }}
                        className="px-3 py-1 bg-white dark:bg-zinc-800 border border-rose-200 dark:border-rose-700/50 rounded text-xs font-medium hover:bg-rose-50 dark:hover:bg-rose-900/30 text-rose-600 dark:text-rose-400 transition-colors"
                      >
                        Details
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <PostModal post={selectedPost} onClose={() => setSelectedPost(null)} />
    </div>
  );
}
