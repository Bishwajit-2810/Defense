import React, { useState } from 'react';
import { Search as SearchIcon } from 'lucide-react';
import { apiCall } from '../utils/api.js';

export default function Search() {
  const [query, setQuery] = useState('');
  const [campaignId, setCampaignId] = useState('');
  const [semantic, setSemantic] = useState(false);
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);

  const handleSearch = async () => {
    if (!query.trim()) return;
    setLoading(true);
    try {
      let url = `/v1/search?q=${encodeURIComponent(query)}`;
      if (campaignId.trim()) url += `&campaign_id=${encodeURIComponent(campaignId.trim())}`;
      if (semantic) url += `&semantic=true`;
      
      const data = await apiCall(url);
      setResults(data.results || (Array.isArray(data) ? data : []));
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

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">Database Search</h2>
        <p className="text-slate-500 dark:text-zinc-400">Keyword or semantic search over analyzed posts (pgvector + ClickHouse)</p>
      </div>

      <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm p-6">
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
              placeholder="Search by keyword, topic, entity, or phrase..." 
              className="block w-full pl-10 pr-3 py-3 border border-slate-200 dark:border-zinc-800 rounded-lg leading-5 bg-white dark:bg-[#121214] placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 transition-shadow"
            />
          </div>
          <input 
            type="text" 
            placeholder="campaign (optional)"
            value={campaignId}
            onChange={(e) => setCampaignId(e.target.value)}
            className="w-48 px-3 py-3 border border-slate-200 dark:border-zinc-800 rounded-lg bg-white dark:bg-[#121214] placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 transition-shadow"
          />
          <button 
            onClick={handleSearch}
            disabled={loading}
            className="px-6 py-3 bg-brand-500 hover:bg-brand-600 text-white rounded-lg font-medium transition-colors h-full">
            {loading ? 'Searching...' : 'Search'}
          </button>
        </div>
        
        <div className="mt-4 flex items-center gap-2">
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" checked={semantic} onChange={(e) => setSemantic(e.target.checked)} className="rounded text-brand-500" />
            Semantic search (vector-based, pgvector)
          </label>
          <span className="text-xs text-slate-500 ml-2">Off = keyword / full-text; On = vector similarity via pgvector — works after posts are analyzed</span>
        </div>
      </div>

      <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl shadow-sm p-6">
        {!hasSearched ? (
          <div className="text-center text-slate-500 dark:text-zinc-500 py-8">
            Enter a query and press Search.
          </div>
        ) : results.length === 0 ? (
          <div className="text-center text-slate-500 dark:text-zinc-500 py-8">
            No results found for "{query}".
          </div>
        ) : (
          <div className="space-y-4">
            {results.map((result, i) => {
              const pid = result.post_id || result.id || `Result ${i + 1}`;
              const title = result.post_text || result.content || result.snippet || 'No text';
              const campaign = result.campaign_id || 'Unknown';
              const sentiment = result.overall_sentiment || result.sentiment || 'neutral';
              
              return (
                <div key={pid} className="border border-slate-200 dark:border-zinc-800 rounded-xl p-4 hover:bg-slate-50 dark:hover:bg-[#121214] transition-colors">
                  <div className="flex justify-between items-start mb-2">
                    <span className="font-mono text-xs text-slate-500">{pid}</span>
                    <span className={`px-2 py-1 rounded-full text-xs font-medium ${
                        sentiment === 'positive' ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400' :
                        sentiment === 'negative' ? 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400' :
                        'bg-slate-100 text-slate-700 dark:bg-zinc-800 dark:text-zinc-300'
                      }`}>
                        {sentiment}
                    </span>
                  </div>
                  <p className="text-slate-800 dark:text-slate-200 text-sm mb-2">{title}</p>
                  <div className="flex gap-2">
                    <span className="inline-block px-2 py-1 bg-slate-100 dark:bg-zinc-800 text-xs rounded text-slate-500">Campaign: {campaign}</span>
                    {result.score && <span className="inline-block px-2 py-1 bg-brand-50 dark:bg-brand-900/20 text-brand-600 dark:text-brand-400 text-xs rounded">Score: {Number(result.score).toFixed(4)}</span>}
                    {result.distance !== undefined && <span className="inline-block px-2 py-1 bg-brand-50 dark:bg-brand-900/20 text-brand-600 dark:text-brand-400 text-xs rounded">Distance: {Number(result.distance).toFixed(4)}</span>}
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
