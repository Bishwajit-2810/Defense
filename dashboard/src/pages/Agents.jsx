import React, { useState, useEffect } from 'react';
import { apiCall } from '../utils/api.js';

export default function Agents() {
  const [agentType, setAgentType] = useState('analyst');
  const [question, setQuestion] = useState('');
  const [campaignId, setCampaignId] = useState('');
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(false);
  const [answerHtml, setAnswerHtml] = useState(null);

  useEffect(() => {
    fetchRuns();
    
    const handleAutoRefresh = () => fetchRuns();
    window.addEventListener('auto-refresh', handleAutoRefresh);
    return () => window.removeEventListener('auto-refresh', handleAutoRefresh);
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
    const timer = setInterval(async () => {
      try {
        const run = await apiCall(`/v1/agents/${encodeURIComponent(runId)}`);
        if (run.status !== 'running') {
          clearInterval(timer);
          renderAnswer(run);
          fetchRuns();
        } else {
          setAnswerHtml(
            `<div class="alert alert-info p-4 bg-blue-50 dark:bg-blue-900/20 text-blue-800 dark:text-blue-300 rounded-lg">
              Run ${String(runId).slice(0, 8)} is still working — polling for the answer...
            </div>`
          );
        }
      } catch (err) {
        clearInterval(timer);
        setAnswerHtml(`<div class="alert alert-error p-4 bg-red-50 dark:bg-red-900/20 text-red-800 dark:text-red-300 rounded-lg">Polling failed: ${err.message}</div>`);
        setLoading(false);
      }
    }, 3000);
  };

  const renderAnswer = (result) => {
    if (!result) return;
    let html = '';
    
    if (result.status === 'failed') {
      html = `<div class="alert alert-error p-4 bg-red-50 dark:bg-red-900/20 text-red-800 dark:text-red-300 rounded-lg">Run failed: ${result.error || 'unknown error'}</div>`;
    } else {
      let finalAns = result.result || result.answer || '';
      
      html = `<div class="agent-answer-box p-6 bg-slate-50 dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 rounded-lg shadow-sm">
        <h4 class="font-bold mb-4 text-lg">Answer</h4>
        <div class="prose dark:prose-invert max-w-none text-sm">${finalAns.replace(/\\n/g, '<br/>')}</div>
      </div>`;
      
      if (result.traces && result.traces.length > 0) {
        html += '<div class="mt-4"><h5 class="font-semibold text-sm mb-2 text-slate-500">Tool Calls:</h5><ul class="text-xs font-mono space-y-1">';
        result.traces.forEach(t => {
          if (t.tool_name) {
            html += `<li class="bg-slate-100 dark:bg-zinc-800 p-2 rounded text-slate-600 dark:text-zinc-400">
              <span class="font-bold text-brand-500">${t.tool_name}</span>(${JSON.stringify(t.tool_input || {})})
            </li>`;
          }
        });
        html += '</ul></div>';
      }
    }
    setAnswerHtml(html);
    setLoading(false);
  };

  const handleAsk = async () => {
    if (!question.trim()) {
      alert('Please enter a question.');
      return;
    }
    setLoading(true);
    setAnswerHtml(
      `<div class="loading-overlay p-4 bg-slate-50 dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 rounded-lg text-slate-500 flex items-center gap-2">
        <div class="w-4 h-4 rounded-full border-2 border-brand-500 border-t-transparent animate-spin"></div>
        The ${agentType} agent is thinking (uses LLM tool-calling — may take up to ~30 s)...
      </div>`
    );
    
    try {
      const body = {
        query: question.trim(),
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
        renderAnswer(result);
        fetchRuns();
      }
    } catch (err) {
      setAnswerHtml(`<div class="alert alert-error p-4 bg-red-50 dark:bg-red-900/20 text-red-800 dark:text-red-300 rounded-lg">Agent query failed: ${err.message}<br><span class="text-xs">Is the agent layer running? Start it with \`uv run run_all.py --with-agents\`.</span></div>`);
      setLoading(false);
    }
  };

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">Ask an Agent</h2>
        <p className="text-slate-500 dark:text-zinc-400">Natural-language Q&A over the analyzed corpus</p>
      </div>

      <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl p-6 shadow-sm">
        <div className="flex flex-wrap gap-4 items-end">
          <div className="flex flex-col">
            <label className="text-xs font-semibold text-slate-500 mb-1">Agent</label>
            <select 
              value={agentType}
              onChange={(e) => setAgentType(e.target.value)}
              className="px-3 py-3 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg w-32"
            >
              <option value="analyst">Analyst</option>
              <option value="coverage">Coverage</option>
              <option value="alerting">Alerting</option>
            </select>
          </div>
          <div className="flex flex-col flex-grow">
            <label className="text-xs font-semibold text-slate-500 mb-1">Question</label>
            <input 
              type="text" 
              placeholder="e.g. What are people saying about fuel prices this week?"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleAsk()}
              className="px-3 py-3 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg w-full"
            />
          </div>
          <div className="flex flex-col">
            <label className="text-xs font-semibold text-slate-500 mb-1">Campaign (optional)</label>
            <input 
              type="text" 
              placeholder="all"
              value={campaignId}
              onChange={(e) => setCampaignId(e.target.value)}
              className="px-3 py-3 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg w-32"
            />
          </div>
          <button 
            onClick={handleAsk}
            disabled={loading}
            className="px-6 py-3 bg-brand-500 hover:bg-brand-600 text-white rounded-lg font-medium transition-colors h-[46px]">
            {loading ? 'Asking...' : 'Ask'}
          </button>
        </div>
        
        {answerHtml && (
          <div className="mt-6" dangerouslySetInnerHTML={{ __html: answerHtml }}></div>
        )}
      </div>

      <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm p-6">
        <div className="flex justify-between items-center mb-4">
          <div>
            <h3 className="font-semibold text-lg">Recent Agent Runs</h3>
            <p className="text-sm text-slate-500">History of agent queries and their results</p>
          </div>
          <button onClick={fetchRuns} className="px-3 py-1.5 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg hover:bg-slate-100 dark:hover:bg-zinc-800">Refresh</button>
        </div>

        {runs.length === 0 ? (
          <div className="text-center text-slate-500 dark:text-zinc-500 py-8 border border-dashed border-slate-200 dark:border-zinc-800 rounded-lg">
            No runs loaded yet.
          </div>
        ) : (
          <div className="space-y-4">
            {runs.map((run, i) => {
              const rid = run.id || run.run_id || `Run ${i}`;
              return (
                <div key={rid} className="border border-slate-200 dark:border-zinc-800 rounded-xl p-4 hover:bg-slate-50 dark:hover:bg-[#121214] transition-colors cursor-pointer" onClick={() => renderAnswer(run)}>
                  <div className="flex justify-between items-start mb-2">
                    <h4 className="font-medium text-slate-800 dark:text-slate-200">{run.query || 'Unknown Query'}</h4>
                    <span className={`px-2 py-1 rounded-full text-xs font-medium ${
                        run.status === 'completed' ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400' :
                        run.status === 'failed' ? 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400' :
                        run.status === 'running' ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400' :
                        'bg-slate-100 text-slate-700 dark:bg-zinc-800 dark:text-zinc-300'
                      }`}>
                        {run.status}
                    </span>
                  </div>
                  <div className="flex gap-2">
                    <span className="inline-block px-2 py-1 bg-slate-100 dark:bg-zinc-800 text-xs rounded text-slate-500 font-mono">{String(rid).slice(0,8)}</span>
                    <span className="inline-block px-2 py-1 bg-slate-100 dark:bg-zinc-800 text-xs rounded text-slate-500">{run.agent_type || 'analyst'}</span>
                    <span className="inline-block px-2 py-1 bg-slate-100 dark:bg-zinc-800 text-xs rounded text-slate-500">{run.created_at ? new Date(run.created_at).toLocaleString() : ''}</span>
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
