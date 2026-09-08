import React, { useState, useEffect } from 'react';
import { Moon, Sun, Shield, Activity } from 'lucide-react';
import { apiCall } from '../utils/api';

export default function Header({ authStatus, onLogout, onOpenSystemMonitor }) {
  const [isDark, setIsDark] = useState(
    document.documentElement.classList.contains('dark')
  );
  
  const [llmConfig, setLlmConfig] = useState(null);
  const [nlpConfig, setNlpConfig] = useState(null);

  useEffect(() => {
    if (authStatus) {
      apiCall('/v1/config/llm').then(setLlmConfig).catch(() => {});
      apiCall('/v1/config/nlp').then(setNlpConfig).catch(() => {});
    }
  }, [authStatus]);

  const toggleTheme = () => {
    const root = document.documentElement;
    if (root.classList.contains('dark')) {
      root.classList.remove('dark');
      localStorage.theme = 'light';
      setIsDark(false);
    } else {
      root.classList.add('dark');
      localStorage.theme = 'dark';
      setIsDark(true);
    }
  };

  const getLlmLabel = (backend) => {
    if (backend === 'groq') return 'Groq (Llama-3)';
    if (backend === 'local') return 'Local (vLLM)';
    if (backend === 'auto') return 'Auto (Groq/Local fallback)';
    if (backend === 'openai') return 'OpenAI (GPT-4o)';
    return backend;
  };

  return (
    <header className="sticky top-0 z-50 backdrop-blur-md bg-white/80 dark:bg-[#09090b]/80 border-b border-slate-200 dark:border-zinc-800 p-4 flex items-center justify-between">
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-lg bg-brand-500 flex items-center justify-center text-white shadow-lg shadow-brand-500/20">
          <Shield size={24} />
        </div>
        <div>
          <h1 className="font-bold text-lg leading-tight">Selective Intelligence</h1>
          <div className="text-xs text-slate-500 dark:text-zinc-400 font-medium tracking-wide uppercase">Cost-Aware Analysis Layer</div>
        </div>
      </div>

      <div className="flex items-center gap-2 sm:gap-3 md:gap-4">
        {/* System Monitor Trigger Button */}
        <button
          onClick={onOpenSystemMonitor}
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-semibold bg-brand-500/10 hover:bg-brand-500/20 text-brand-600 dark:text-brand-400 border border-brand-500/20 shadow-sm transition-all duration-150"
          title="Open Backend System Monitor (Alt+M)"
          aria-label="Open Backend System Monitor"
        >
          <Activity size={14} className="animate-pulse text-brand-500" />
          <span className="hidden sm:inline">System Monitor</span>
        </button>

        {llmConfig && llmConfig.backend && (
          <button className="hidden lg:inline-flex px-3 py-1 rounded-full text-xs font-semibold bg-indigo-500/10 text-indigo-600 dark:text-indigo-400 border border-indigo-500/20 hover:bg-indigo-500/20 transition-colors" title="LLM Backend">
            LLM: {getLlmLabel(llmConfig.backend)}
          </button>
        )}
        
        {nlpConfig && nlpConfig.sentiment_model && (
          <button className="hidden lg:inline-flex px-3 py-1 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/20 hover:bg-emerald-500/20 transition-colors" title="NLP Backend">
            NLP: {nlpConfig.sentiment_model}
          </button>
        )}

        <button 
          onClick={toggleTheme}
          className="p-2 rounded-full hover:bg-slate-100 dark:hover:bg-zinc-800 transition-colors text-slate-600 dark:text-zinc-300"
          title="Toggle Theme"
        >
          {isDark ? <Sun size={20} /> : <Moon size={20} />}
        </button>

        <div className="hidden sm:inline-flex px-3 py-1 rounded-full text-xs font-semibold bg-brand-500/10 text-brand-600 dark:text-brand-400 border border-brand-500/20">
          Authenticated
        </div>

        <button onClick={onLogout} className="px-4 py-2 text-sm font-medium rounded-lg border border-slate-200 dark:border-zinc-700 hover:bg-slate-50 dark:hover:bg-zinc-800 transition-colors">
          Sign Out
        </button>
      </div>
    </header>
  );
}
