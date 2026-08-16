import React, { useState, useEffect, useRef } from 'react';
import { 
  Bot, 
  Target, 
  Scale, 
  AlertTriangle, 
  Compass, 
  CheckCircle2, 
  FileText, 
  RefreshCw, 
  Bell, 
  Sparkles, 
  Cpu, 
  ExternalLink,
  ShieldCheck,
  ChevronDown,
  ChevronUp,
  Clock,
  Coins,
  Search,
  Trash2,
  XCircle
} from 'lucide-react';
import { apiCall } from '../utils/api.js';
import MarkdownView from '../components/MarkdownView.jsx';

const AGENT_CONFIGS = {
  analyst: {
    label: 'Analyst',
    badge: 'Corpus Q&A',
    icon: Bot,
    color: 'text-blue-500 bg-blue-50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800',
    description: 'Natural-language reasoning over campaign metrics, sentiment trends, and top posts.',
    tools: ['trend_query', 'sentiment_over_time', 'top_posts', 'reaction_mix', 'semantic_search', 'get_post', 'get_thread', 'representative_comments'],
    prompts: [
      'What are the most active discussions and sentiment trends across all posts?',
      'Which posts had the highest reaction volume and what was the public reaction?',
      'Summarize positive vs negative reaction breakdowns across top posts.'
    ]
  },
  stance: {
    label: 'Stance Intelligence',
    badge: 'Target Stance',
    icon: Target,
    color: 'text-purple-500 bg-purple-50 dark:bg-purple-900/20 border-purple-200 dark:border-purple-800',
    description: 'Deep-dive into target-dependent stance patterns over 3-script watchlist entities.',
    tools: ['stance_by_target', 'stance_over_time', 'semantic_search', 'get_post', 'get_thread', 'representative_comments'],
    prompts: [
      'Who are people most opposing and supporting in the comments and posts?',
      'Analyze stance asymmetry between political entities in this campaign.',
      'Show stance over time for the primary political figures.'
    ]
  },
  comparator: {
    label: 'Comparative',
    badge: 'Cross-Analysis',
    icon: Scale,
    color: 'text-amber-500 bg-amber-50 dark:bg-amber-900/20 border-amber-200 dark:border-amber-800',
    description: 'Side-by-side comparison across campaigns, time windows, and engagement segments.',
    tools: ['trend_query', 'sentiment_over_time', 'top_posts', 'reaction_mix', 'semantic_search'],
    prompts: [
      'Compare sentiment distributions and engagement across top posts.',
      'What is the delta in toxicity between viral posts and standard posts?',
      'Compare comment sentiment distribution with post caption tone.'
    ]
  },
  toxicity: {
    label: 'Toxicity & Harm',
    badge: 'Content Safety',
    icon: AlertTriangle,
    color: 'text-rose-500 bg-rose-50 dark:bg-rose-900/20 border-rose-200 dark:border-rose-800',
    description: 'Detect hate speech clusters, targeted harassment, and toxic comment hotspots.',
    tools: ['top_posts', 'get_thread', 'representative_comments', 'trend_query', 'semantic_search'],
    prompts: [
      'Identify the posts and comment threads with highest toxicity scores.',
      'Are toxic comments concentrated around specific targets or general discourse?',
      'Find representative examples of hostile or toxic comments.'
    ]
  },
  narrative: {
    label: 'Narrative Discovery',
    badge: 'Embedding Clusters',
    icon: Compass,
    color: 'text-indigo-500 bg-indigo-50 dark:bg-indigo-900/20 border-indigo-200 dark:border-indigo-800',
    description: 'Discover emerging narratives and topic clusters via pgvector embedding similarity.',
    tools: ['get_clusters', 'semantic_search', 'get_post', 'trend_query', 'top_posts'],
    prompts: [
      'What are the main emerging topic clusters discovered by embedding similarity?',
      'Summarize key narrative themes and their dominant sentiments.',
      'Which narrative clusters have the highest negative sentiment?'
    ]
  },
  quality: {
    label: 'Data Quality & Audit',
    badge: 'Honesty & Provenance',
    icon: CheckCircle2,
    color: 'text-emerald-500 bg-emerald-50 dark:bg-emerald-900/20 border-emerald-200 dark:border-emerald-800',
    description: 'Audit comment coverage, ensemble agreement rates, and model provenance transparency.',
    tools: ['coverage_stats', 'agreement_stats', 'top_posts', 'get_post'],
    prompts: [
      'Audit comment coverage, ensemble agreement rates, and model provenance.',
      'Are there any coverage anomalies or unverified stub vectors in this campaign?',
      'What fraction of comment labels came from LLM escalation vs cheap voters?'
    ]
  },
  reporter: {
    label: 'Report Drafting',
    badge: 'Executive Briefings',
    icon: FileText,
    color: 'text-teal-500 bg-teal-50 dark:bg-teal-900/20 border-teal-200 dark:border-teal-800',
    description: 'Synthesize full structured executive briefings with verified data citations.',
    tools: ['trend_query', 'sentiment_over_time', 'top_posts', 'reaction_mix', 'semantic_search', 'get_post', 'representative_comments'],
    prompts: [
      'Draft an executive analytical briefing summarizing sentiment, top themes, and notable posts.',
      'Generate a structured campaign report with data citations and key findings.',
      'Draft a crisis alert briefing highlighting spikes in public opposition.'
    ]
  },
  coverage: {
    label: 'Coverage Deep-Dive',
    badge: 'Data Collection',
    icon: RefreshCw,
    color: 'text-cyan-500 bg-cyan-50 dark:bg-cyan-900/20 border-cyan-200 dark:border-cyan-800',
    description: 'Find under-covered viral posts (analyzed/total < 10%) and trigger comment collection.',
    tools: ['top_posts', 'get_post', 'fetch_more_comments'],
    prompts: [
      'Identify viral posts with low comment coverage that need deeper comment fetching.',
      'Check coverage ratios across top engaged posts in this campaign.'
    ]
  },
  alerting: {
    label: 'Spike Alerting',
    badge: 'Monitoring',
    icon: Bell,
    color: 'text-orange-500 bg-orange-50 dark:bg-orange-900/20 border-orange-200 dark:border-orange-800',
    description: 'Detect sudden negative sentiment spikes (>50% in 24h) and toxicity threshold breaches.',
    tools: ['trend_query', 'sentiment_over_time', 'top_posts'],
    prompts: [
      'Check for sudden spikes in negative sentiment (>50% neg) or elevated toxicity.',
      'Are there any viral posts with high engagement that require immediate analysis?'
    ]
  }
};

export default function Agents() {
  const [agentType, setAgentType] = useState('analyst');
  const [question, setQuestion] = useState('');
  const [campaignId, setCampaignId] = useState('');
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(false);
  const [currentRun, setCurrentRun] = useState(null);
  const [errorMsg, setErrorMsg] = useState(null);
  const [showTools, setShowTools] = useState(false);
  const pollTimerRef = useRef(null);

  useEffect(() => {
    fetchRuns();
    
    const handleAutoRefresh = () => fetchRuns();
    window.addEventListener('auto-refresh', handleAutoRefresh);
    return () => {
      window.removeEventListener('auto-refresh', handleAutoRefresh);
      if (pollTimerRef.current) clearInterval(pollTimerRef.current);
    };
  }, []);

  const fetchRuns = async () => {
    try {
      const data = await apiCall('/v1/agents/runs?limit=25');
      let runsArr = [];
      if (Array.isArray(data)) runsArr = data;
      else if (data && Array.isArray(data.runs)) runsArr = data.runs;
      else if (data && Array.isArray(data.results)) runsArr = data.results;
      
      setRuns(runsArr);
    } catch (err) {
      console.error(err);
    }
  };

  const pollAgentRun = (runId) => {
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
    }
    const timer = setInterval(async () => {
      try {
        const run = await apiCall(`/v1/agents/${encodeURIComponent(runId)}`);
        if (run.status !== 'running') {
          clearInterval(timer);
          pollTimerRef.current = null;
          setCurrentRun(run);
          setLoading(false);
          fetchRuns();
        } else {
          setCurrentRun(run);
        }
      } catch (err) {
        clearInterval(timer);
        pollTimerRef.current = null;
        setErrorMsg(`Polling failed: ${err.message}`);
        setLoading(false);
      }
    }, 2500);
    pollTimerRef.current = timer;
  };

  const handleAsk = async (queryText = question) => {
    const q = queryText.trim();
    if (!q) {
      alert('Please enter a question.');
      return;
    }
    setLoading(true);
    setErrorMsg(null);
    setCurrentRun({ status: 'running', query: q, agent_type: agentType });
    
    try {
      const body = {
        query: q,
        agent_type: agentType
      };
      if (campaignId.trim()) body.campaign_id = campaignId.trim();

      const result = await apiCall('/v1/agents/query', {
        method: 'POST',
        body: JSON.stringify(body)
      });

      if (result.status === 'running' && result.run_id) {
        pollAgentRun(result.run_id);
      } else {
        setCurrentRun(result);
        setLoading(false);
        fetchRuns();
      }
    } catch (err) {
      setErrorMsg(`Agent query failed: ${err.message}. (Ensure agent layer is running via uv run run_all.py --with-agents)`);
      setLoading(false);
      setCurrentRun(null);
    }
  };

  const handleCancelRun = async (runId) => {
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    setLoading(false);
    const targetId = runId || currentRun?.run_id || currentRun?.id;
    if (targetId) {
      try {
        await apiCall(`/v1/agents/${encodeURIComponent(targetId)}`, { method: 'DELETE' });
        setRuns(prev => prev.filter(r => (r.run_id || r.id) !== targetId));
      } catch (e) {
        console.warn('Cancel run error:', e);
      }
    }
    setCurrentRun(null);
    fetchRuns();
  };

  const handleDeleteRun = async (runId, e) => {
    if (e) e.stopPropagation();
    if (currentRun && (currentRun.run_id || currentRun.id) === runId) {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
      setLoading(false);
      setCurrentRun(null);
    }
    try {
      await apiCall(`/v1/agents/${encodeURIComponent(runId)}`, { method: 'DELETE' });
      setRuns(prev => prev.filter(r => (r.run_id || r.id) !== runId));
    } catch (err) {
      console.error('Failed to delete run:', err);
      alert(`Could not delete run: ${err.message}`);
    }
  };

  const handleClearAllRuns = async () => {
    if (!window.confirm('Are you sure you want to clear all agent interaction history?')) {
      return;
    }
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    setLoading(false);
    try {
      await apiCall('/v1/agents', { method: 'DELETE' });
      setRuns([]);
      setCurrentRun(null);
    } catch (err) {
      console.error('Failed to clear runs:', err);
      alert(`Could not clear runs: ${err.message}`);
    }
  };

  const activeConfig = AGENT_CONFIGS[agentType] || AGENT_CONFIGS.analyst;
  const ActiveIcon = activeConfig.icon;

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      {/* Header Banner */}
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4 border-b border-slate-200 dark:border-zinc-800 pb-5">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <h2 className="text-2xl font-bold tracking-tight">Agentic RAG & Intelligence</h2>
            <span className="px-2.5 py-0.5 text-xs font-semibold rounded-full bg-brand-50 text-brand-600 dark:bg-brand-900/30 dark:text-brand-400 border border-brand-200 dark:border-brand-800">
              MCP Protocol
            </span>
          </div>
          <p className="text-sm text-slate-500 dark:text-zinc-400">
            Domain-specific multi-agent reasoning layer with prompt-injection hardening and grounded citation citations.
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-slate-500 dark:text-zinc-400 bg-slate-100 dark:bg-zinc-900 px-3 py-1.5 rounded-lg border border-slate-200 dark:border-zinc-800">
          <ShieldCheck className="w-4 h-4 text-emerald-500" />
          <span>Prompt-Injection Hardened &lt;tool_data&gt;</span>
        </div>
      </div>

      {/* Agent Selector Grid */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 xl:grid-cols-9 gap-2">
        {Object.entries(AGENT_CONFIGS).map(([key, cfg]) => {
          const Icon = cfg.icon;
          const isActive = agentType === key;
          return (
            <button
              key={key}
              onClick={() => {
                setAgentType(key);
                setCurrentRun(null);
                setErrorMsg(null);
              }}
              className={`flex flex-col items-center text-center p-3 rounded-xl border transition-all ${
                isActive
                  ? 'bg-brand-50/70 dark:bg-brand-900/20 border-brand-500 shadow-sm text-brand-700 dark:text-brand-300 font-semibold'
                  : 'bg-white dark:bg-[#09090b] border-slate-200 dark:border-zinc-800 hover:border-slate-300 dark:hover:border-zinc-700 text-slate-600 dark:text-zinc-400'
              }`}
            >
              <div className={`p-2 rounded-lg mb-1.5 ${isActive ? 'bg-brand-500 text-white' : 'bg-slate-100 dark:bg-zinc-800 text-slate-600 dark:text-zinc-400'}`}>
                <Icon className="w-4 h-4" />
              </div>
              <span className="text-xs font-medium leading-tight">{cfg.label}</span>
            </button>
          );
        })}
      </div>

      {/* Active Agent Info & Interactive Prompt Input */}
      <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl p-6 shadow-sm space-y-4">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-100 dark:border-zinc-800/80 pb-3">
          <div className="flex items-center gap-2.5">
            <div className={`p-2 rounded-lg border ${activeConfig.color}`}>
              <ActiveIcon className="w-5 h-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h3 className="font-semibold text-slate-900 dark:text-slate-100">{activeConfig.label} Agent</h3>
                <span className="text-[11px] font-mono px-2 py-0.5 rounded bg-brand-50 text-brand-700 dark:bg-brand-900/30 dark:text-brand-300 border border-brand-200 dark:border-brand-800">
                  role: agent (llama3.1:8b)
                </span>
              </div>
              <p className="text-xs text-slate-500 dark:text-zinc-400">{activeConfig.description}</p>
            </div>
          </div>
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-xs text-slate-400 mr-1">Tools:</span>
            {activeConfig.tools.slice(0, 4).map((t) => (
              <span key={t} className="text-[10px] font-mono px-2 py-0.5 rounded bg-slate-100 dark:bg-zinc-800 text-slate-600 dark:text-zinc-400">
                {t}
              </span>
            ))}
            {activeConfig.tools.length > 4 && (
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-slate-100 dark:bg-zinc-800 text-slate-400">
                +{activeConfig.tools.length - 4}
              </span>
            )}
          </div>
        </div>

        {/* Preset Prompt Chips */}
        <div>
          <label className="text-xs font-semibold text-slate-400 mb-1.5 flex items-center gap-1">
            <Sparkles className="w-3.5 h-3.5 text-amber-500" />
            <span>Suggested Inquiries for {activeConfig.label}:</span>
          </label>
          <div className="flex flex-wrap gap-2">
            {activeConfig.prompts.map((p, idx) => (
              <button
                key={idx}
                onClick={() => {
                  setQuestion(p);
                  handleAsk(p);
                }}
                className="text-left text-xs bg-slate-50 hover:bg-slate-100 dark:bg-zinc-900 dark:hover:bg-zinc-800 border border-slate-200 dark:border-zinc-800 rounded-lg px-3 py-1.5 text-slate-700 dark:text-zinc-300 transition-colors"
              >
                "{p}"
              </button>
            ))}
          </div>
        </div>

        {/* Input Form */}
        <div className="flex flex-wrap gap-3 items-end pt-2">
          <div className="flex flex-col flex-grow min-w-[280px]">
            <label className="text-xs font-semibold text-slate-500 mb-1">Question or Investigation Directive</label>
            <input 
              type="text" 
              placeholder={`Ask the ${activeConfig.label} agent...`}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleAsk()}
              className="px-4 py-3 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg w-full focus:outline-none focus:ring-2 focus:ring-brand-500"
            />
          </div>
          <div className="flex flex-col w-40">
            <label className="text-xs font-semibold text-slate-500 mb-1">Campaign Scope</label>
            <input 
              type="text" 
              placeholder="all campaigns"
              value={campaignId}
              onChange={(e) => setCampaignId(e.target.value)}
              className="px-3 py-3 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg w-full focus:outline-none focus:ring-2 focus:ring-brand-500"
            />
          </div>
          <button 
            onClick={() => handleAsk()}
            disabled={loading}
            className="px-6 py-3 bg-brand-500 hover:bg-brand-600 disabled:opacity-50 text-white rounded-lg font-medium transition-colors h-[46px] flex items-center gap-2 shadow-sm"
          >
            {loading ? (
              <>
                <div className="w-4 h-4 rounded-full border-2 border-white border-t-transparent animate-spin"></div>
                <span>Reasoning...</span>
              </>
            ) : (
              <>
                <Bot className="w-4 h-4" />
                <span>Ask Agent</span>
              </>
            )}
          </button>
        </div>

        {/* Error Alert */}
        {errorMsg && (
          <div className="p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 text-red-800 dark:text-red-300 rounded-lg text-sm">
            {errorMsg}
          </div>
        )}

        {/* Loading Banner */}
        {loading && (
          <div className="p-5 bg-slate-50 dark:bg-zinc-900/50 border border-slate-200 dark:border-zinc-800 rounded-xl flex flex-col sm:flex-row sm:items-center justify-between gap-4">
            <div className="flex items-center gap-3">
              <div className="w-5 h-5 rounded-full border-2 border-brand-500 border-t-transparent animate-spin flex-shrink-0"></div>
              <div>
                <h4 className="text-sm font-semibold text-slate-800 dark:text-slate-200">
                  {activeConfig.label} Agent is orchestrating MCP tools...
                </h4>
                <p className="text-xs text-slate-500">
                  Dynamically retrieving real metrics from ClickHouse & pgvector embeddings (budget limit: 10 calls).
                </p>
              </div>
            </div>
            <button
              onClick={() => handleCancelRun(currentRun?.run_id)}
              className="px-3.5 py-1.5 bg-rose-50 hover:bg-rose-100 dark:bg-rose-950/40 dark:hover:bg-rose-900/50 text-rose-700 dark:text-rose-300 border border-rose-200 dark:border-rose-800 rounded-lg text-xs font-medium flex items-center gap-1.5 transition-colors self-start sm:self-auto shadow-sm"
              title="Stop and cancel active agent task"
            >
              <Trash2 className="w-3.5 h-3.5" />
              <span>Cancel / Stop Task</span>
            </button>
          </div>
        )}

        {/* Active Result View */}
        {currentRun && currentRun.status !== 'running' && (
          <div className="mt-4 border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm">
            {/* Run Header / Telemetry Bar */}
            <div className="bg-slate-100 dark:bg-zinc-900 px-5 py-3 border-b border-slate-200 dark:border-zinc-800 flex flex-wrap items-center justify-between gap-3 text-xs">
              <div className="flex items-center gap-2">
                <span className="font-semibold text-slate-700 dark:text-slate-300">Run Output</span>
                <span className="font-mono text-slate-500 bg-white dark:bg-zinc-800 px-2 py-0.5 rounded border border-slate-200 dark:border-zinc-700">
                  {String(currentRun.run_id || currentRun.id || 'run').slice(0, 8)}
                </span>
                <span className={`px-2 py-0.5 rounded-full font-medium ${
                  currentRun.status === 'completed'
                    ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400'
                    : 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400'
                }`}>
                  {currentRun.status}
                </span>
              </div>
              <div className="flex items-center gap-4 text-slate-500 dark:text-zinc-400">
                {currentRun.llm_backend && (
                  <span className="flex items-center gap-1">
                    <Cpu className="w-3.5 h-3.5" />
                    <span>{currentRun.llm_backend} {currentRun.llm_model ? `(${currentRun.llm_model})` : ''}</span>
                  </span>
                )}
                {currentRun.usage?.total_tokens && (
                  <span className="flex items-center gap-1">
                    <Coins className="w-3.5 h-3.5" />
                    <span>{currentRun.usage.total_tokens} tokens</span>
                  </span>
                )}
              </div>
            </div>

            {/* Answer Body */}
            <div className="p-6 bg-white dark:bg-[#09090b] space-y-4">
              <MarkdownView content={currentRun.answer || currentRun.result || 'No output text returned.'} />

              {/* Citations */}
              {currentRun.citations && currentRun.citations.length > 0 && (
                <div className="pt-4 border-t border-slate-100 dark:border-zinc-800">
                  <h5 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2">
                    Verified Grounded Citations ({currentRun.citations.length} post IDs):
                  </h5>
                  <div className="flex flex-wrap gap-1.5">
                    {currentRun.citations.map((cid) => (
                      <span 
                        key={cid}
                        className="inline-flex items-center gap-1 font-mono text-[11px] bg-slate-100 dark:bg-zinc-800/80 text-slate-700 dark:text-zinc-300 px-2 py-1 rounded border border-slate-200 dark:border-zinc-700"
                      >
                        <span>{cid}</span>
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* Tool Execution Trace Toggle */}
              {((currentRun.tools_used && currentRun.tools_used.length > 0) || (currentRun.traces && currentRun.traces.length > 0)) && (
                <div className="pt-2 border-t border-slate-100 dark:border-zinc-800">
                  <button 
                    onClick={() => setShowTools(!showTools)}
                    className="flex items-center justify-between w-full text-xs font-semibold text-slate-500 hover:text-slate-700 dark:hover:text-zinc-300 py-1"
                  >
                    <span>MCP Tool Invocation Trace ({(currentRun.tools_used || currentRun.traces || []).length} calls)</span>
                    {showTools ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                  </button>
                  
                  {showTools && (
                    <div className="mt-2 space-y-2 font-mono text-xs">
                      {(currentRun.tools_used || currentRun.traces || []).map((t, idx) => {
                        const name = t.tool_name || t.name || 'tool';
                        const args = t.arguments || t.tool_input || {};
                        return (
                          <div key={idx} className="bg-slate-50 dark:bg-zinc-900 p-3 rounded-lg border border-slate-200 dark:border-zinc-800">
                            <div className="flex items-center justify-between text-brand-600 dark:text-brand-400 font-semibold mb-1">
                              <span>#{idx + 1} {name}</span>
                              {t.error && <span className="text-rose-500">Error: {t.error}</span>}
                            </div>
                            <pre className="text-[11px] text-slate-600 dark:text-zinc-400 overflow-x-auto">
                              {JSON.stringify(args, null, 2)}
                            </pre>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Recent Runs History */}
      <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm p-6">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4">
          <div>
            <h3 className="font-semibold text-lg">Agent Interaction History</h3>
            <p className="text-sm text-slate-500">Chronological ledger of analytical questions and tool-use verdicts</p>
          </div>
          <div className="flex items-center gap-2">
            {runs.length > 0 && (
              <button 
                onClick={handleClearAllRuns} 
                className="px-3 py-1.5 text-xs text-rose-600 dark:text-rose-400 bg-rose-50 dark:bg-rose-950/30 border border-rose-200 dark:border-rose-900 rounded-lg hover:bg-rose-100 dark:hover:bg-rose-900/50 flex items-center gap-1.5 transition-colors font-medium"
                title="Clear all agent run history"
              >
                <Trash2 className="w-3.5 h-3.5" />
                <span>Clear All</span>
              </button>
            )}
            <button 
              onClick={fetchRuns} 
              className="px-3 py-1.5 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg hover:bg-slate-100 dark:hover:bg-zinc-800 flex items-center gap-1.5"
            >
              <RefreshCw className="w-3.5 h-3.5" />
              <span>Refresh</span>
            </button>
          </div>
        </div>

        {runs.length === 0 ? (
          <div className="text-center text-slate-500 dark:text-zinc-500 py-10 border border-dashed border-slate-200 dark:border-zinc-800 rounded-lg">
            No agent runs logged yet. Pick an agent above and ask a question!
          </div>
        ) : (
          <div className="space-y-3">
            {runs.map((run, i) => {
              const rid = run.id || run.run_id || `Run ${i}`;
              const aType = run.agent_type || run.agent_name || 'analyst';
              const cfg = AGENT_CONFIGS[aType] || AGENT_CONFIGS.analyst;
              const Icon = cfg.icon;

              let displayQuery = run.query || run.question;
              if (displayQuery) {
                displayQuery = displayQuery.replace(/\n\n\[Date range context:[^\]]+\]/g, '').trim();
              }
              if (!displayQuery) {
                displayQuery = run.answer ? (run.answer.slice(0, 85) + '...') : `Agent Run ${String(rid).slice(0, 8)}`;
              }

              return (
                <div 
                  key={rid} 
                  className="group border border-slate-200 dark:border-zinc-800 rounded-xl p-4 hover:bg-slate-50 dark:hover:bg-[#121214] transition-all cursor-pointer flex flex-col md:flex-row md:items-center justify-between gap-3" 
                  onClick={() => {
                    setCurrentRun(run);
                    setAgentType(aType);
                  }}
                >
                  <div className="flex items-start gap-3 flex-1 min-w-0">
                    <div className="p-2 rounded-lg bg-slate-100 dark:bg-zinc-800 text-slate-600 dark:text-zinc-400 shrink-0 mt-0.5">
                      <Icon className="w-4 h-4" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <h4 className="font-medium text-slate-800 dark:text-slate-200 text-sm truncate">{displayQuery}</h4>
                      <div className="flex flex-wrap gap-2 mt-1.5 text-[11px] text-slate-500">
                        <span className="font-mono bg-slate-100 dark:bg-zinc-800 px-1.5 py-0.5 rounded">{String(rid).slice(0, 8)}</span>
                        <span className="bg-slate-100 dark:bg-zinc-800 px-1.5 py-0.5 rounded capitalize">{cfg.label}</span>
                        {run.campaign_id && <span className="bg-slate-100 dark:bg-zinc-800 px-1.5 py-0.5 rounded">Campaign: {run.campaign_id}</span>}
                        {run.created_at && (
                          <span className="flex items-center gap-1">
                            <Clock className="w-3 h-3" />
                            <span>{new Date(run.created_at * (run.created_at < 1e11 ? 1000 : 1)).toLocaleTimeString()}</span>
                          </span>
                        )}
                      </div>
                    </div>
                  </div>

                  <div className="flex items-center gap-2 self-end md:self-center shrink-0">
                    <span className={`px-2.5 py-1 rounded-full text-xs font-medium ${
                      run.status === 'completed' ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400' :
                      run.status === 'failed' ? 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400' :
                      run.status === 'running' ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400' :
                      'bg-slate-100 text-slate-700 dark:bg-zinc-800 dark:text-zinc-300'
                    }`}>
                      {run.status}
                    </span>
                    <button
                      onClick={(e) => handleDeleteRun(rid, e)}
                      className="p-1.5 text-slate-400 hover:text-rose-600 dark:hover:text-rose-400 hover:bg-rose-50 dark:hover:bg-rose-950/40 rounded-lg transition-colors"
                      title="Delete this run from history"
                      aria-label="Delete run"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
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

