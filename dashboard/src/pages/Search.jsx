import React, { useState } from 'react';
import { Search as SearchIcon, Sparkles, Database, Info, AlertCircle } from 'lucide-react';
import { apiCall } from '../utils/api.js';

export default function Search() {
  const [query, setQuery] = useState('');
  const [campaignId, setCampaignId] = useState('');
  const [semantic, setSemantic] = useState(false);
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const [searchMeta, setSearchMeta] = useState(null);

  const handleSearch = async () => {
    if (!query.trim()) return;
    setLoading(true);
    try {
      let url = `/v1/search?q=${encodeURIComponent(query)}`;
      if (campaignId.trim()) url += `&campaign_id=${encodeURIComponent(campaignId.trim())}`;
      if (semantic) url += `&semantic=true`;
      
      const data = await apiCall(url);
      const resList = data.results || (Array.isArray(data) ? data : []);
      setResults(resList);
      setSearchMeta({
        query: data.query || query,
        semantic: Boolean(data.semantic ?? semantic),
        total: data.total ?? resList.length,
      });
      setHasSearched(true);
    } catch (err) {
      console.error(err);
      alert("Error searching: " + err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter') {
      handleSearch();
    }
  };

  const hasStubResult = results.some((r) => r.embedding_is_stub);

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">Database & Vector Search</h2>
        <p className="text-slate-500 dark:text-zinc-400">
          Dual-mode retrieval: PostgreSQL JSONB keyword indexing &amp; pgvector 768-dim multilingual semantic search
        </p>
      </div>

      <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm p-6 space-y-4">
        <div className="flex gap-4 items-center">
          <div className="relative flex-grow">
            <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
              <SearchIcon className="h-5 w-5 text-slate-400" />
            </div>
            <input 
              type="text" 
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Search by keyword, topic, entity, or multilingual query in Bangla/English..." 
              className="block w-full pl-10 pr-3 py-3 border border-slate-200 dark:border-zinc-800 rounded-lg leading-5 bg-white dark:bg-[#121214] placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 transition-shadow text-sm"
            />
          </div>
          <input 
            type="text" 
            placeholder="campaign (optional)"
            value={campaignId}
            onChange={(e) => setCampaignId(e.target.value)}
            className="w-48 px-3 py-3 border border-slate-200 dark:border-zinc-800 rounded-lg bg-white dark:bg-[#121214] placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 transition-shadow text-sm"
          />
          <button 
            onClick={handleSearch}
            disabled={loading}
            className="px-6 py-3 bg-brand-500 hover:bg-brand-600 disabled:opacity-50 text-white rounded-lg font-medium transition-colors h-full flex items-center gap-2">
            {loading ? 'Searching...' : 'Search'}
          </button>
        </div>
        
        <div className="flex flex-wrap items-center justify-between gap-3 pt-1 border-t border-slate-100 dark:border-zinc-800/80">
          <label className="flex items-center gap-2.5 text-sm cursor-pointer select-none">
            <input 
              type="checkbox" 
              checked={semantic} 
              onChange={(e) => setSemantic(e.target.checked)} 
              className="rounded text-brand-500 focus:ring-brand-500 w-4 h-4" 
            />
            <span className="font-medium text-slate-800 dark:text-slate-200 flex items-center gap-1.5">
              <Sparkles className="w-4 h-4 text-purple-500" />
              Semantic Search (pgvector cosine similarity)
            </span>
          </label>
          <span className="text-xs text-slate-500">
            {semantic ? (
              <span className="text-purple-600 dark:text-purple-400 font-medium">
                Active Model: paraphrase-multilingual-mpnet-base-v2 (768-dim)
              </span>
            ) : (
              <span>Active Mode: Case-insensitive PostgreSQL JSONB full-text scan</span>
            )}
          </span>
        </div>
      </div>

      {hasStubResult && semantic && (
        <div className="p-4 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-xl text-amber-800 dark:text-amber-300 text-xs flex items-start gap-2.5">
          <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
          <div>
            <span className="font-semibold">Stub Vectors Notice:</span> Some results contain hash-based stub vectors (system running in non-ML mode). Cosine distances reflect deterministic unit vectors rather than contextual semantic meanings.
          </div>
        </div>
      )}

      <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl shadow-sm p-6">
        {!hasSearched ? (
          <div className="text-center text-slate-500 dark:text-zinc-500 py-10">
            Enter a search term above and execute keyword or vector similarity search.
          </div>
        ) : results.length === 0 ? (
          <div className="text-center text-slate-500 dark:text-zinc-500 py-10">
            No results found matching "{query}".
          </div>
        ) : (
          <div className="space-y-4">
            <div className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
              Found {results.length} results ({searchMeta?.semantic ? 'Semantic Cosine Rank' : 'Keyword Match'}):
            </div>
            {results.map((result, i) => {
              const pid = result.post_id || result.id || `Result ${i + 1}`;
              const title = result.snippet || result.post_text || result.content || (result.result?.post_summary) || 'No snippet';
              const campaign = result.campaign_id || 'Unknown';
              const sentiment = result.overall_sentiment || result.sentiment || result.result?.overall_sentiment || 'neutral';
              const isStub = Boolean(result.embedding_is_stub);
              
              return (
                <div key={pid} className="border border-slate-200 dark:border-zinc-800 rounded-xl p-4 hover:bg-slate-50 dark:hover:bg-[#121214] transition-colors space-y-2">
                  <div className="flex justify-between items-start">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs text-slate-500 font-semibold">{pid}</span>
                      {isStub && (
                        <span className="px-1.5 py-0.5 text-[10px] rounded bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">
                          stub-vector
                        </span>
                      )}
                    </div>
                    <span className={`px-2.5 py-0.5 rounded-full text-xs font-medium ${
                        sentiment === 'positive' ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400' :
                        sentiment === 'negative' ? 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400' :
                        'bg-slate-100 text-slate-700 dark:bg-zinc-800 dark:text-zinc-300'
                      }`}>
                        {sentiment}
                    </span>
                  </div>
                  <p className="text-slate-800 dark:text-slate-200 text-sm">{title}</p>
                  <div className="flex flex-wrap items-center gap-2 pt-1">
                    <span className="inline-block px-2 py-0.5 bg-slate-100 dark:bg-zinc-800 text-xs rounded text-slate-500">
                      Campaign: {campaign}
                    </span>
                    {result.score !== undefined && result.score !== null && (
                      <span className="inline-block px-2 py-0.5 bg-brand-50 dark:bg-brand-900/20 text-brand-600 dark:text-brand-400 text-xs rounded font-mono">
                        Similarity Score: {Number(result.score).toFixed(4)}
                      </span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

