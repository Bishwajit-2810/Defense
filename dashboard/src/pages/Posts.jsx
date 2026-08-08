import React, { useState, useEffect } from 'react';
import { apiCall } from '../utils/api.js';
import PostModal from '../components/PostModal';

export default function Posts() {
  const [posts, setPosts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [campaignId, setCampaignId] = useState('');
  const [wantSummary, setWantSummary] = useState(true);
  const [selectedPost, setSelectedPost] = useState(null);

  useEffect(() => {
    fetchPosts();
    
    const handleAutoRefresh = () => fetchPosts();
    window.addEventListener('auto-refresh', handleAutoRefresh);
    return () => window.removeEventListener('auto-refresh', handleAutoRefresh);
  }, []);

  const fetchPosts = async () => {
    try {
      let url = '/v1/analysis/latest?limit=100&include=results';
      if (campaignId) url += '&campaign_id=' + encodeURIComponent(campaignId);
      const data = await apiCall(url);
      setPosts(data.results || (Array.isArray(data) ? data : []));
    } catch (err) {
      console.error(err);
    }
  };

  const handleFileUpload = async (event) => {
    const files = event.target.files;
    if (!files.length) return;
    
    setLoading(true);
    
    try {
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        const text = await file.text();
        const parsed = JSON.parse(text);
        const postsArray = Array.isArray(parsed) ? parsed : [parsed];
        
        const body = {
          source: 'inline',
          posts: postsArray,
          options: { tasks: ['all'], want_summary: wantSummary, summary_lang: 'auto', llm_backend: 'auto' }
        };

        await apiCall('/v1/posts/upload', {
          method: 'POST',
          body: JSON.stringify(body)
        });
      }
      fetchPosts();
    } catch (err) {
      console.error("Upload Error:", err);
      alert("Failed to upload: " + err.message);
    } finally {
      setLoading(false);
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
        <h2 className="text-2xl font-bold tracking-tight">Posts Analysis</h2>
        <p className="text-slate-500 dark:text-zinc-400">Upload and inspect posts for automated threat intelligence.</p>
      </div>

      <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm p-6">
        <h3 className="font-semibold text-lg mb-2">Upload Posts</h3>
        <p className="text-slate-500 text-sm mb-4">Upload a JSON file containing post-with-details records for analysis</p>
        
        <div className="border-2 border-dashed border-slate-300 dark:border-zinc-700 rounded-xl p-8 text-center bg-slate-50 dark:bg-[#121214] hover:bg-slate-100 dark:hover:bg-zinc-900/50 transition-colors cursor-pointer relative mb-4">
          <input 
            type="file" 
            accept=".json,application/json"
            multiple 
            onChange={handleFileUpload} 
            className="absolute inset-0 w-full h-full opacity-0 cursor-pointer" 
            disabled={loading}
          />
          <div className="mx-auto w-12 h-12 bg-brand-500/10 text-brand-500 rounded-full flex items-center justify-center mb-2">
            <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
            </svg>
          </div>
          <h3 className="text-sm font-semibold mb-1">Click to select or drag & drop a .json file</h3>
        </div>
        
        <div className="flex items-center gap-4">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={wantSummary} onChange={e => setWantSummary(e.target.checked)} className="rounded text-brand-500" />
            LLM summaries
          </label>
          <button className="px-4 py-2 bg-brand-500 hover:bg-brand-600 text-white rounded-lg text-sm font-medium transition-colors" disabled={loading}>
            {loading ? 'Uploading...' : 'Upload Posts'}
          </button>
        </div>
      </div>

      <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm">
        <div className="p-4 border-b border-slate-200 dark:border-zinc-800 bg-slate-50 dark:bg-[#121214] flex justify-between items-center flex-wrap gap-4">
          <div>
            <h3 className="font-semibold">Analysis Results</h3>
            <p className="text-xs text-slate-500">{posts.length} results loaded</p>
          </div>
          <div className="flex gap-2 items-center">
            <input 
              type="text" 
              placeholder="Filter: campaign id" 
              value={campaignId}
              onChange={e => setCampaignId(e.target.value)}
              className="px-3 py-1.5 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg"
            />
            <button onClick={fetchPosts} className="px-3 py-1.5 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg hover:bg-slate-100 dark:hover:bg-zinc-800">Refresh</button>
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
                <th className="px-4 py-3 font-medium">Sentiment</th>
                <th className="px-4 py-3 font-medium">Toxicity</th>
                <th className="px-4 py-3 font-medium">Summary</th>
                <th className="px-4 py-3 font-medium">Comment Coverage</th>
                <th className="px-4 py-3 font-medium">Created At</th>
                <th className="px-4 py-3 font-medium text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-200 dark:divide-zinc-800">
              {posts.length === 0 ? (
                <tr><td colSpan="10" className="px-4 py-8 text-center text-slate-500">No results found. Upload posts to get started.</td></tr>
              ) : posts.map((post, i) => {
                const sentiment = post.overall_sentiment || 'neutral';
                const toxScore = typeof post.toxicity_score === 'number' ? post.toxicity_score : null;
                const coverage = (post.comment_analysis && post.comment_analysis.coverage_label) || '—';
                const dateStr = post.created_at ? new Date(post.created_at).toLocaleString() : '—';
                let langStr = post.language || '—';
                if (post.language_mix && post.language_mix.length > 1) {
                  langStr += ` +${post.language_mix.length - 1}`;
                }
                const summary = post.post_summary || '—';

                return (
                  <tr key={post.post_id || i} className="hover:bg-slate-50 dark:hover:bg-zinc-900/50 cursor-pointer transition-colors" onClick={() => setSelectedPost(post)}>
                    <td className="px-4 py-3">{i + 1}</td>
                    <td className="px-4 py-3 font-mono text-xs">{String(post.post_id || '').slice(0,8)}...</td>
                    <td className="px-4 py-3">{post.platform || '—'}</td>
                    <td className="px-4 py-3">{langStr}</td>
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
                    <td className="px-4 py-3">{coverage}</td>
                    <td className="px-4 py-3 text-xs">{dateStr}</td>
                    <td className="px-4 py-3 text-right">
                      <button 
                        onClick={(e) => { e.stopPropagation(); setSelectedPost(post); }}
                        className="px-3 py-1 bg-white dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded text-xs font-medium hover:bg-slate-50 dark:hover:bg-zinc-700 transition-colors"
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
