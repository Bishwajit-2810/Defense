import React, { useState, useEffect, useRef } from 'react';
import { MessageSquare, Send, Bot, User, Trash2 } from 'lucide-react';
import { apiCall, API_BASE, getAuthHeaders } from '../utils/api.js';

export default function Chat() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [backend, setBackend] = useState('auto');
  const [model, setModel] = useState('');
  const [availableModels, setAvailableModels] = useState([]);
  const [loading, setLoading] = useState(false);
  const messagesEndRef = useRef(null);

  useEffect(() => {
    fetchModels();
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, loading]);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  const fetchModels = async () => {
    try {
      const data = await apiCall('/v1/chat/models');
      const formatted = Object.entries(data.backends || {}).map(([backendKey, info]) => ({
        backend: backendKey,
        models: info.models.map(m => ({ id: m }))
      }));
      if (data.active_backend && data.backends[data.active_backend]) {
        formatted.push({
          backend: 'auto',
          models: data.backends[data.active_backend].models.map(m => ({ id: m }))
        });
      }
      setAvailableModels(formatted);
    } catch (err) {
      console.error("Failed to fetch models", err);
    }
  };

  const handleSend = async () => {
    if (!input.trim() || loading) return;
    
    const userMsg = { role: 'user', content: input.trim() };
    const newMessages = [...messages, userMsg];
    setMessages(newMessages);
    setInput('');
    setLoading(true);

    try {
      const body = {
        messages: newMessages,
        backend: backend,
        model: model || undefined
      };

      const headers = getAuthHeaders();

      const res = await fetch(`${API_BASE}/v1/chat/stream`, {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(body)
      });

      if (!res.ok) {
        throw new Error(`Chat error ${res.status}`);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      
      setMessages([...newMessages, { role: 'assistant', content: '' }]);
      
      let assistantContent = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        
        const chunk = decoder.decode(value, { stream: true });
        const lines = chunk.split('\n');
        
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const dataStr = line.slice(6);
            if (dataStr === '[DONE]') break;
            try {
              const data = JSON.parse(dataStr);
              if (typeof data.content === 'string') {
                assistantContent += data.content;
                setMessages(prev => {
                  const updated = [...prev];
                  updated[updated.length - 1].content = assistantContent;
                  return updated;
                });
              }
            } catch (e) {}
          }
        }
      }
    } catch (err) {
      console.error(err);
      // Fallback to non-streaming
      try {
        const fallbackRes = await apiCall('/v1/chat', {
          method: 'POST',
          body: JSON.stringify({ messages: newMessages, backend, model: model || undefined })
        });
        const reply = fallbackRes.reply || 'No response';
        setMessages([...newMessages, { role: 'assistant', content: reply }]);
      } catch (fallbackErr) {
        setMessages([...newMessages, { role: 'assistant', content: `Error: ${fallbackErr.message}` }]);
      }
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const currentBackendModels = Array.isArray(availableModels) 
    ? availableModels.find(m => m.backend === backend)?.models || []
    : [];

  return (
    <div className="flex flex-col h-[calc(100vh-140px)] animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="mb-4 flex flex-wrap justify-between items-end gap-4">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">AI Chat Assistant</h2>
          <p className="text-slate-500 dark:text-zinc-400">Query the pipeline LLM using natural language.</p>
        </div>
        <div className="flex items-center gap-2">
          <div className="bg-slate-100 dark:bg-zinc-800 p-1 rounded-lg flex items-center">
            {['auto', 'local', 'groq'].map(b => (
              <button 
                key={b}
                onClick={() => setBackend(b)}
                className={`px-3 py-1.5 text-xs font-medium rounded-md capitalize ${backend === b ? 'bg-white dark:bg-zinc-700 shadow-sm text-brand-600 dark:text-brand-400' : 'text-slate-600 dark:text-zinc-400 hover:text-slate-900 dark:hover:text-slate-200'}`}
              >
                {b}
              </button>
            ))}
          </div>
          <select 
            value={model} 
            onChange={e => setModel(e.target.value)}
            className="px-3 py-1.5 text-xs bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg max-w-[150px]"
          >
            <option value="">Default model</option>
            {currentBackendModels.map(m => (
              <option key={m.id} value={m.id}>{m.id}</option>
            ))}
          </select>
          <button 
            onClick={() => setMessages([])} 
            className="p-1.5 text-slate-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg transition-colors"
            title="Clear conversation"
          >
            <Trash2 size={18} />
          </button>
        </div>
      </div>

      <div className="flex-1 bg-white dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm flex flex-col">
        <div className="flex-1 p-6 overflow-y-auto flex flex-col">
          {messages.length === 0 ? (
            <div className="flex-1 flex flex-col items-center justify-center text-slate-500 dark:text-zinc-500 text-center">
              <MessageSquare size={48} className="mb-4 opacity-20" />
              <h3 className="font-semibold text-lg mb-2">Ask me anything</h3>
              <p className="text-sm max-w-md">
                A general-purpose assistant powered by the same LLM as the pipeline. It doesn't see your analyzed posts — use the <strong>Agents</strong> tab for questions about your data.
              </p>
            </div>
          ) : (
            <div className="space-y-6">
              {messages.map((msg, i) => (
                <div key={i} className={`flex gap-4 ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                  {msg.role === 'assistant' && (
                    <div className="w-8 h-8 rounded-full bg-brand-500/10 text-brand-600 flex items-center justify-center shrink-0 mt-1">
                      <Bot size={16} />
                    </div>
                  )}
                  <div className={`max-w-[80%] rounded-2xl px-5 py-3 ${
                    msg.role === 'user' 
                      ? 'bg-brand-500 text-white rounded-tr-none' 
                      : 'bg-slate-100 dark:bg-zinc-800 text-slate-800 dark:text-slate-200 rounded-tl-none'
                  }`}>
                    <div className="whitespace-pre-wrap text-sm leading-relaxed">{msg.content}</div>
                  </div>
                  {msg.role === 'user' && (
                    <div className="w-8 h-8 rounded-full bg-slate-200 dark:bg-zinc-700 text-slate-600 dark:text-zinc-300 flex items-center justify-center shrink-0 mt-1">
                      <User size={16} />
                    </div>
                  )}
                </div>
              ))}
              {loading && messages[messages.length - 1]?.role !== 'assistant' && (
                <div className="flex gap-4 justify-start">
                  <div className="w-8 h-8 rounded-full bg-brand-500/10 text-brand-600 flex items-center justify-center shrink-0 mt-1">
                    <Bot size={16} />
                  </div>
                  <div className="bg-slate-100 dark:bg-zinc-800 rounded-2xl rounded-tl-none px-5 py-3 flex items-center gap-2">
                    <div className="w-2 h-2 rounded-full bg-slate-400 animate-bounce"></div>
                    <div className="w-2 h-2 rounded-full bg-slate-400 animate-bounce" style={{animationDelay: '150ms'}}></div>
                    <div className="w-2 h-2 rounded-full bg-slate-400 animate-bounce" style={{animationDelay: '300ms'}}></div>
                  </div>
                </div>
              )}
              <div ref={messagesEndRef} />
            </div>
          )}
        </div>
        
        <div className="p-4 bg-slate-50 dark:bg-[#09090b] border-t border-slate-200 dark:border-zinc-800">
          <div className="relative flex items-end">
            <textarea 
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Message the assistant..." 
              className="block w-full pl-4 pr-12 py-3 border border-slate-300 dark:border-zinc-700 rounded-xl bg-white dark:bg-[#121214] text-slate-900 dark:text-white placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 shadow-sm min-h-[50px] max-h-[200px] resize-y"
              rows="1"
            />
            <button 
              onClick={handleSend}
              disabled={loading || !input.trim()}
              className="absolute right-2 bottom-2 p-2 bg-brand-500 hover:bg-brand-600 text-white rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
              <Send size={18} />
            </button>
          </div>
          <div className="text-center text-xs text-slate-400 mt-2">
            Enter to send · Shift+Enter for a new line
          </div>
        </div>
      </div>
    </div>
  );
}
