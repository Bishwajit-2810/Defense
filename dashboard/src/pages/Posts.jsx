import React, { useState, useEffect, useMemo, useRef, useCallback } from 'react';
import { Search as SearchIcon, X, Copy, Check, Play, Activity, Loader2 } from 'lucide-react';
import { apiCall, API_BASE, getAuthHeaders } from '../utils/api.js';
import { commentScrape, scrapeTooltip } from '../utils/coverage';
import { formatAlertReason } from '../utils/sentiment.js';
import { setPostId as setTracePostId } from '../utils/traceSession.js';
import PostModal from '../components/PostModal';

// Every field an operator might paste or type. `post_id` and `platform_post_id`
// are first because that is what the table shows and what gets copied out of a
// ticket; the text fields make the same box work as a content search, so there is
// one input rather than a filter per field.
const SEARCHABLE = (post) => [
  post.post_id,
  post.result?.platform_post_id ?? post.platform_post_id,
  post.result?.url ?? post.url,
  post.campaign_id,
  post.platform,
  post.post_summary,
  post.post_text,
  post.language,
  post.overall_sentiment,
  ...(post.topics || []),
  ...(post.keywords || []),
];

const matchesQuery = (post, needle) =>
  SEARCHABLE(post).some(v => v && String(v).toLowerCase().includes(needle));

export default function Posts() {
  const [posts, setPosts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [campaignId, setCampaignId] = useState('');
  const [query, setQuery] = useState('');
  // Posts the loaded page does not contain, fetched from /v1/search. The table
  // holds the latest 100; an id from an older post is not in it, and a filter
  // over what happens to be loaded would report "not found" for a post that
  // exists — which is the failure this page had.
  const [serverHits, setServerHits] = useState(null);
  const [searching, setSearching] = useState(false);
  const [copied, setCopied] = useState(null);
  const searchSeq = useRef(0);
  const [wantSummary, setWantSummary] = useState(true);
  const [selectedPost, setSelectedPost] = useState(null);
  // Which row is mid-enqueue, and the outcome of the last single-post re-run.
  const [rerunning, setRerunning] = useState(null);
  const [rerunNote, setRerunNote] = useState(null);

  // useCallback so the two effects below can name it as a dependency. Without
  // it the identity changes every render, so listing it would refetch in a loop
  // and omitting it is a lint warning that hides a real class of stale-closure
  // bug — a fetch that captured an old `campaignId`.
  const fetchPosts = useCallback(async () => {
    try {
      let url = '/v1/analysis/latest?limit=100&include=results';
      if (campaignId) url += '&campaign_id=' + encodeURIComponent(campaignId);
      const data = await apiCall(url);
      setPosts(data.results || (Array.isArray(data) ? data : []));
    } catch (err) {
      console.error(err);
    }
  }, [campaignId]);
  
  useEffect(() => {
    const handleAutoRefresh = () => fetchPosts();
    window.addEventListener('auto-refresh', handleAutoRefresh);
    return () => window.removeEventListener('auto-refresh', handleAutoRefresh);
  }, [fetchPosts]);

  // The campaign box used to change state and nothing else: it only took effect
  // when you also pressed Refresh, so it read as a filter that did not work.
  // Debounced so typing an id is not one request per keystroke.
  useEffect(() => {
    const t = setTimeout(fetchPosts, campaignId ? 350 : 0);
    return () => clearTimeout(t);
  }, [fetchPosts, campaignId]);

  // Two tiers, in this order:
  //   1. filter the rows already on screen — instant, no request;
  //   2. only if that finds nothing, ask the server, which searches the whole
  //      corpus and (since the id fix) answers an identifier exactly.
  // The second tier is what makes an id search trustworthy: "no match here" and
  // "no such post" are different answers and the page now distinguishes them.
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return posts;
    return posts.filter(p => matchesQuery(p, needle));
  }, [posts, query]);

  useEffect(() => {
    const needle = query.trim();
    setServerHits(null);
    if (needle.length < 3 || filtered.length > 0) return;

    const seq = ++searchSeq.current;
    const t = setTimeout(async () => {
      setSearching(true);
      try {
        const data = await apiCall(`/v1/search?q=${encodeURIComponent(needle)}&limit=50`);
        if (seq !== searchSeq.current) return;  // a newer query has taken over
        setServerHits({
          rows: (data.results || []).map(r => ({ ...(r.result || {}), ...r })),
          matchType: data.match_type,
          idLookupMissed: Boolean(data.id_lookup_missed),
        });
      } catch (err) {
        if (seq === searchSeq.current) setServerHits({ rows: [], error: err.message });
      } finally {
        if (seq === searchSeq.current) setSearching(false);
      }
    }, 400);
    return () => clearTimeout(t);
  }, [query, filtered.length]);

  const copyId = async (id) => {
    try {
      await navigator.clipboard.writeText(id);
      setCopied(id);
      setTimeout(() => setCopied(c => (c === id ? null : c)), 1200);
    } catch { /* clipboard blocked — the full id is in the title tooltip */ }
  };

  // Re-analyse exactly one row.
  //
  // The table has rows whose Summary cell reads "—": the post went through the
  // pipeline without `want_summary`, or the router bypassed Stage 2, so no
  // summary was ever generated. Until now the only remedy on this page was to
  // re-upload the whole file. `POST /v1/analysis/run` takes `post_ids`, so a
  // single post can be re-normalized from its stored raw_payload and pushed back
  // through Stage 1 — and `want_summary` forces it through Stage 2, which is
  // what actually produces the missing summary.
  const rerunPost = async (postId) => {
    if (!postId || rerunning) return;
    setRerunning(postId);
    setRerunNote(null);
    try {
      const resp = await apiCall('/v1/analysis/run', {
        method: 'POST',
        body: JSON.stringify({
          post_ids: [postId],
          options: { tasks: ['all'], want_summary: true, summary_lang: 'auto', llm_backend: 'auto' },
        }),
      });
      const jobId = resp.analysis_id || resp.id || resp.job_id;
      setRerunNote({
        postId,
        kind: 'ok',
        text: `Queued ${postId.slice(0, 8)}… as job ${String(jobId || '').slice(0, 8)}… — the row updates when it lands.`,
      });
      // The job is asynchronous: nothing has changed in the table yet. The
      // 15-second auto-refresh picks the result up; this is the nudge that makes
      // a fast run show up without waiting for it.
      setTimeout(fetchPosts, 4000);
    } catch (err) {
      setRerunNote({ postId, kind: 'error', text: `Re-run failed: ${err.message}` });
    } finally {
      setRerunning(null);
    }
  };

  // Hand the row to the Trace tab. The post id goes into the trace session
  // directly rather than through an event, because Trace is not mounted yet —
  // the navigation below is what mounts it, and it reads the store on the way in.
  const tracePost = (postId) => {
    if (!postId) return;
    setTracePostId(postId);
    window.dispatchEvent(new CustomEvent('dashboard-navigate', { detail: { tab: 'trace' } }));
  };

  const downloadZip = async () => {
    try {
      const res = await fetch(`${API_BASE}/v1/analysis/export?only_warnings=false`, {
        headers: getAuthHeaders()
      });
      if (!res.ok) throw new Error('Download failed');
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `defense_all_analyses.zip`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (err) {
      console.error(err);
      alert('Failed to download ZIP');
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

  const downloadReport = async () => {
    try {
      const campaign = campaignId.trim() || 'all';
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
      a.download = `mass_reaction_report_${campaign}.pdf`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (err) {
      alert('Error generating report: ' + err.message);
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

  // What the table renders: the local filter, or the server's answer when the
  // local filter came up empty.
  const fromServer = Boolean(query.trim() && filtered.length === 0 && serverHits?.rows?.length);
  const rows = fromServer ? serverHits.rows : filtered;

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
        {posts.some(p => p.watchlist_alert) && (
          <div className="p-4 bg-rose-50 dark:bg-rose-900/20 border-b border-rose-200 dark:border-rose-800">
            <h3 className="text-rose-700 dark:text-rose-400 font-bold flex items-center gap-2">
              ⚠️ Watchlist alerts
            </h3>
            <p className="text-sm text-rose-600 dark:text-rose-300">
              {posts.filter(p => p.watchlist_alert).length} post(s) matched a watchlist rule — open one to see which target and why.
            </p>
          </div>
        )}
        <div className="p-4 border-b border-slate-200 dark:border-zinc-800 bg-slate-50 dark:bg-[#121214] flex justify-between items-center flex-wrap gap-4">
          <div>
            <h3 className="font-semibold">Analysis Results</h3>
            <p className="text-xs text-slate-500">
              {query.trim()
                ? `${rows.length} of ${posts.length} loaded${fromServer ? ' · from server search' : ''}`
                : `${posts.length} results loaded`}
            </p>
          </div>
          <div className="flex gap-2 items-center flex-wrap">
            <div className="relative">
              <SearchIcon className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400 pointer-events-none" />
              <input
                type="text"
                placeholder="Search posts — id, platform id, caption, topic…"
                value={query}
                onChange={e => setQuery(e.target.value)}
                className="pl-8 pr-8 py-1.5 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg w-80"
              />
              {query && (
                <button
                  onClick={() => setQuery('')}
                  aria-label="Clear search"
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 dark:hover:text-slate-200"
                >
                  <X className="w-4 h-4" />
                </button>
              )}
            </div>
            <input 
              type="text" 
              placeholder="Filter: campaign id" 
              value={campaignId}
              onChange={e => setCampaignId(e.target.value)}
              className="px-3 py-1.5 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg"
            />
            <button onClick={downloadReport} className="px-3 py-1.5 text-sm bg-brand-500 hover:bg-brand-600 text-white rounded-lg font-medium transition-colors">Download Analysis Report</button>
            <button onClick={downloadZip} className="px-3 py-1.5 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg hover:bg-slate-100 dark:hover:bg-zinc-800">Export ZIP</button>
            <button onClick={fetchPosts} className="px-3 py-1.5 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg hover:bg-slate-100 dark:hover:bg-zinc-800">Refresh</button>
          </div>
        </div>
        
        {rerunNote && (
          <div className={`px-4 py-2 border-b text-xs flex items-center justify-between gap-4 ${
            rerunNote.kind === 'ok'
              ? 'bg-emerald-50 dark:bg-emerald-900/10 border-emerald-200 dark:border-emerald-900/40 text-emerald-700 dark:text-emerald-400'
              : 'bg-rose-50 dark:bg-rose-900/10 border-rose-200 dark:border-rose-900/40 text-rose-700 dark:text-rose-400'
          }`}>
            <span className="font-mono">{rerunNote.text}</span>
            <button onClick={() => setRerunNote(null)} aria-label="Dismiss" className="shrink-0 opacity-70 hover:opacity-100">
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        )}

        {fromServer && (
          <div className="px-4 py-2 bg-brand-50 dark:bg-brand-900/10 border-b border-brand-200 dark:border-brand-900/40 text-xs text-brand-700 dark:text-brand-400">
            {serverHits.matchType === 'exact_id'
              ? <>Exact identifier match from the full corpus — this <strong>is</strong> the post you asked for.</>
              : serverHits.matchType === 'id_prefix'
                ? <>Identifier prefix match from the full corpus ({serverHits.rows.length}).</>
                : <>Not on this page — {serverHits.rows.length} match(es) found in the full corpus by {serverHits.matchType || 'keyword'} search.</>}
          </div>
        )}

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
                <th className="px-4 py-3 font-medium" title="Comments (analysed / scraped / platform total): Comments read by Stage-2 models / rows delivered by scraper / total reported by platform">Comments</th>
                <th className="px-4 py-3 font-medium">Created At</th>
                <th className="px-4 py-3 font-medium text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-200 dark:divide-zinc-800">
              {rows.length === 0 ? (
                <tr><td colSpan="10" className="px-4 py-8 text-center text-slate-500">
                  {/* Three different situations, three different answers. They
                      used to be one line reading "No results found. Upload posts
                      to get started." — which is wrong advice for a search that
                      simply matched nothing, and actively misleading for a post
                      id that does not exist. */}
                  {searching ? 'Searching the full corpus…'
                    : !query.trim() ? 'No results found. Upload posts to get started.'
                    : serverHits?.error ? `Search failed: ${serverHits.error}`
                    : serverHits?.idLookupMissed
                      ? <>No post has the id <span className="font-mono">{query.trim()}</span> — checked the whole corpus, not just this page.</>
                      : <>Nothing matches <span className="font-mono">{query.trim()}</span>. Searched this page and the full corpus (id, platform id, URL, campaign, caption, summary, topics, keywords).</>}
                </td></tr>
              ) : rows.map((post, i) => {
                const sentiment = post.overall_sentiment || 'neutral';
                const toxScore = typeof post.toxicity_score === 'number' ? post.toxicity_score : null;
                const coverage = (post.comment_analysis && post.comment_analysis.coverage_label) || '—';
                const scrape = commentScrape(post);
                const dateStr = post.created_at ? new Date(post.created_at).toLocaleString() : '—';
                let langStr = post.language || '—';
                if (post.language_mix && post.language_mix.length > 1) {
                  langStr += ` +${post.language_mix.length - 1}`;
                }
                const summary = post.post_summary || '—';
                const hasSummary = Boolean(post.post_summary);
                const busy = rerunning === post.post_id;

                return (
                  <tr key={post.post_id || i} className={`hover:bg-slate-50 dark:hover:bg-zinc-900/50 cursor-pointer transition-colors ${post.watchlist_alert ? 'bg-rose-50/30 dark:bg-rose-900/10' : ''}`} onClick={() => setSelectedPost(post)}>
                    <td className="px-4 py-3">{i + 1}</td>
                    {/* The cell shows 8 of ~25 characters, which is why an id
                        search had to work on a prefix — and why the full value
                        needs to be reachable without opening the row. */}
                    <td className="px-4 py-3 font-mono text-xs">
                      {post.watchlist_alert && <span title={formatAlertReason(post.watchlist_alert_reason) || 'Watchlist alert'} className="mr-2 text-rose-500">⚠️</span>}
                      <span title={post.post_id || ''}>{String(post.post_id || '').slice(0,8)}...</span>
                      {post.post_id && (
                        <button
                          onClick={(e) => { e.stopPropagation(); copyId(post.post_id); }}
                          title={`Copy ${post.post_id}`}
                          aria-label="Copy post id"
                          className="ml-1.5 align-middle text-slate-400 hover:text-brand-500 transition-colors"
                        >
                          {copied === post.post_id
                            ? <Check className="w-3.5 h-3.5 text-emerald-500" />
                            : <Copy className="w-3.5 h-3.5" />}
                        </button>
                      )}
                    </td>
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
                    {/* A missing summary is a state the operator can fix from
                        this row, so it says so instead of showing a bare dash
                        that reads like "this post has nothing to summarise". */}
                    <td className="px-4 py-3 max-w-[200px] truncate" title={hasSummary ? summary : 'No summary was generated for this post — use Re-run to generate one.'}>
                      {hasSummary
                        ? summary
                        : <span className="text-amber-600 dark:text-amber-500">no summary</span>}
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap" title={`${scrapeTooltip(scrape, post.platform)}\n\n${coverage}`}>
                      {/* Numbers only. The scrape-state tag lives on the post
                          detail, not in a 50-row list where it repeats on almost
                          every line and says nothing the three numbers don't; the
                          cell tooltip still carries the full explanation. */}
                      {scrape ? (
                        <div>
                          {/* Analysed first: it is the only one of the three that
                              says what the models covered. Omitted, rather than
                              filled in with the stored count, when unknown. */}
                          {scrape.analysed !== null && (
                            <span className={scrape.analysed < scrape.stored ? 'font-semibold text-amber-600 dark:text-amber-500' : 'font-semibold'}>
                              {scrape.analysed.toLocaleString()}
                              <span className="text-slate-400 dark:text-zinc-500 font-normal"> / </span>
                            </span>
                          )}
                          <span className="font-medium">{scrape.stored.toLocaleString()}</span>
                          <span className="text-slate-400 dark:text-zinc-500"> / {scrape.total ? scrape.total.toLocaleString() : '?'}</span>
                        </div>
                      ) : '—'}
                    </td>
                    <td className="px-4 py-3 text-xs">{dateStr}</td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-1.5">
                        {/* Re-analyse this post and nothing else. The whole-file
                            re-upload above is the wrong tool for one row with a
                            missing summary. */}
                        <button
                          onClick={(e) => { e.stopPropagation(); rerunPost(post.post_id); }}
                          disabled={!post.post_id || Boolean(rerunning)}
                          title={hasSummary
                            ? 'Re-analyse only this post through the real pipeline (asks for an LLM summary)'
                            : 'Re-analyse only this post and generate the missing summary'}
                          className={`px-2.5 py-1 rounded text-xs font-medium border transition-colors flex items-center gap-1 disabled:opacity-40 disabled:cursor-not-allowed ${
                            hasSummary
                              ? 'bg-white dark:bg-zinc-800 border-slate-200 dark:border-zinc-700 hover:bg-slate-50 dark:hover:bg-zinc-700'
                              : 'bg-amber-50 dark:bg-amber-900/20 border-amber-300 dark:border-amber-800 text-amber-700 dark:text-amber-400 hover:bg-amber-100 dark:hover:bg-amber-900/40'
                          }`}
                        >
                          {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Play className="w-3 h-3" />}
                          {busy ? 'Queuing' : 'Re-run'}
                        </button>
                        <button
                          onClick={(e) => { e.stopPropagation(); tracePost(post.post_id); }}
                          disabled={!post.post_id}
                          title="Open the Trace tab with this post id, to watch it move layer by layer"
                          className="px-2.5 py-1 bg-white dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded text-xs font-medium hover:bg-slate-50 dark:hover:bg-zinc-700 transition-colors flex items-center gap-1 disabled:opacity-40"
                        >
                          <Activity className="w-3 h-3" />
                          Trace
                        </button>
                        <button 
                          onClick={(e) => { e.stopPropagation(); setSelectedPost(post); }}
                          className="px-3 py-1 bg-white dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded text-xs font-medium hover:bg-slate-50 dark:hover:bg-zinc-700 transition-colors"
                        >
                          Details
                        </button>
                      </div>
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
