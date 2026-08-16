import React, { useState, useEffect, useRef } from 'react';
import {
  MessageSquare, Send, Bot, User, Trash2, Wrench, Server, CheckCircle2,
  XCircle, Loader2, ChevronDown, ChevronRight, Plus, Pencil, ArrowRight, CloudOff
} from 'lucide-react';
import { apiCall, API_BASE, getAuthHeaders } from '../utils/api.js';
import MarkdownView from '../components/MarkdownView.jsx';

// How often to re-poll a running agent run. The trace is persisted as each tool
// call starts and finishes, so this is the resolution at which tools appear.
const POLL_MS = 1200;

export default function Chat() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [backend, setBackend] = useState('auto');
  const [campaignId, setCampaignId] = useState('');
  const [loading, setLoading] = useState(false);

  const [conversations, setConversations] = useState([]);
  const [conversationId, setConversationId] = useState(null);
  const [historyError, setHistoryError] = useState(null);

  const messagesEndRef = useRef(null);
  const pollTimerRef = useRef(null);
  // handleSend needs the conversation id it just created, in the same tick.
  const conversationIdRef = useRef(null);

  useEffect(() => {
    fetchConversations();
    return () => {
      if (pollTimerRef.current) clearInterval(pollTimerRef.current);
    };
  }, []);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  const setConversation = (id) => {
    conversationIdRef.current = id;
    setConversationId(id);
  };

  // ------------------------------------------------------------------ history
  const fetchConversations = async () => {
    try {
      const data = await apiCall('/v1/chat/conversations?limit=50');
      setConversations(Array.isArray(data) ? data : []);
      setHistoryError(null);
    } catch (err) {
      // Chat still works without history; say so once, quietly.
      setHistoryError(err.message);
    }
  };

  const openConversation = async (id) => {
    if (loading) return;
    try {
      const convo = await apiCall(`/v1/chat/conversations/${encodeURIComponent(id)}`);
      setMessages((convo.messages || []).map(m => ({
        role: m.role,
        content: m.content,
        meta: m.meta || undefined,
      })));
      setConversation(id);
    } catch (err) {
      setHistoryError(err.message);
    }
  };

  const newConversation = () => {
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    setMessages([]);
    setConversation(null);
    setLoading(false);
  };

  const deleteConversation = async (id, e) => {
    e?.stopPropagation();
    try {
      await apiCall(`/v1/chat/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
      setConversations(prev => prev.filter(c => c.id !== id));
      if (conversationIdRef.current === id) newConversation();
    } catch (err) {
      setHistoryError(err.message);
    }
  };

  const renameConversation = async (convo, e) => {
    e?.stopPropagation();
    const title = window.prompt('Rename conversation', convo.title);
    if (!title || !title.trim()) return;
    try {
      await apiCall(`/v1/chat/conversations/${encodeURIComponent(convo.id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ title: title.trim() })
      });
      setConversations(prev => prev.map(c => (c.id === convo.id ? { ...c, title: title.trim() } : c)));
    } catch (err) {
      setHistoryError(err.message);
    }
  };

  /** Persist a turn. Never throws: losing history must not lose the answer. */
  const persist = async (turns) => {
    try {
      if (!conversationIdRef.current) {
        const created = await apiCall('/v1/chat/conversations', {
          method: 'POST',
          body: JSON.stringify({ messages: turns })
        });
        setConversation(created.id);
      } else {
        await apiCall(`/v1/chat/conversations/${encodeURIComponent(conversationIdRef.current)}/messages`, {
          method: 'POST',
          body: JSON.stringify(turns)
        });
      }
      setHistoryError(null);
      fetchConversations();
    } catch (err) {
      setHistoryError(err.message);
    }
  };

  // Only role/content goes back to the model — the provenance we attach is ours.
  const wireTurns = (msgs) => msgs.map(({ role, content }) => ({ role, content }));

  const patchLast = (patch) => {
    setMessages(prev => {
      const updated = [...prev];
      const last = updated[updated.length - 1];
      if (!last || last.role !== 'assistant') return prev;
      updated[updated.length - 1] = { ...last, ...patch, meta: { ...last.meta, ...patch.meta } };
      return updated;
    });
  };

  /** Which agent answered most recently — what a switch is measured against. */
  const lastAgent = () => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role === 'assistant' && m.meta?.agent) return m.meta.agent;
    }
    return null;
  };

  // ---------------------------------------------------------------- plain chat
  const runPlainChat = async (history, meta) => {
    const res = await fetch(`${API_BASE}/v1/chat/stream`, {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify({ messages: wireTurns(history), backend })
    });
    if (!res.ok) throw new Error(`Chat error ${res.status}`);

    setMessages([...history, { role: 'assistant', content: '', meta: { ...meta, mode: 'chat' } }]);

    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let assistantContent = '';
    let finalMeta = { ...meta, mode: 'chat' };
    let buffer = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const dataStr = line.slice(6);
        if (dataStr === '[DONE]') break;
        try {
          const data = JSON.parse(dataStr);
          if (typeof data.content === 'string') {
            assistantContent += data.content;
            patchLast({ content: assistantContent });
          }
          if (data.model || data.backend) {
            finalMeta = { ...finalMeta, model: data.model, backend: data.backend };
            patchLast({ meta: { model: data.model, backend: data.backend } });
          }
        } catch { /* partial frame — the buffer picks it up next read */ }
      }
    }
    return { content: assistantContent, meta: finalMeta };
  };

  // -------------------------------------------------------------------- agents
  const runMeta = (run) => ({
    mode: 'agent',
    run_id: run.run_id,
    status: run.status,
    tools: run.tools_used || [],
    backend: run.llm_backend,
    model: run.llm_model,
    citations: run.citations || [],
    unverified: run.unverified_citations || [],
    error: run.error,
  });

  const pollRun = (runId, baseMeta) => new Promise((resolve) => {
    if (pollTimerRef.current) clearInterval(pollTimerRef.current);
    const timer = setInterval(async () => {
      try {
        const run = await apiCall(`/v1/agents/${encodeURIComponent(runId)}`);
        const meta = { ...baseMeta, ...runMeta(run) };
        patchLast({ content: run.answer || '', meta });
        if (run.status !== 'running') {
          clearInterval(timer);
          pollTimerRef.current = null;
          resolve({ content: run.answer || '', meta });
        }
      } catch (err) {
        clearInterval(timer);
        pollTimerRef.current = null;
        const meta = { ...baseMeta, status: 'failed' };
        const content = `Could not follow the agent run: ${err.message}`;
        patchLast({ content, meta });
        resolve({ content, meta });
      }
    }, POLL_MS);
    pollTimerRef.current = timer;
  });

  const handleSend = async () => {
    if (!input.trim() || loading) return;

    const userMsg = { role: 'user', content: input.trim() };
    const history = [...messages, userMsg];
    const previousAgent = lastAgent();
    setMessages(history);
    setInput('');
    setLoading(true);

    persist([{ role: 'user', content: userMsg.content }]);

    let outcome = null;
    try {
      const routed = await apiCall('/v1/chat/agent', {
        method: 'POST',
        body: JSON.stringify({
          messages: wireTurns(history),
          campaign_id: campaignId.trim() || undefined,
          previous_agent: previousAgent || undefined,
          backend
        })
      });

      if (routed.mode !== 'agent') {
        outcome = await runPlainChat(history, {
          agent: null,
          reason: routed.reason,
          degraded: routed.degraded,
          switched_from: routed.switched_from || null,
        });
      } else {
        const baseMeta = {
          agent: routed.agent,
          reason: routed.reason,
          switched_from: routed.switched_from || null,
        };
        const meta = { ...baseMeta, ...runMeta(routed) };
        setMessages([...history, { role: 'assistant', content: routed.answer || '', meta }]);
        outcome = (routed.status === 'running' && routed.run_id)
          ? await pollRun(routed.run_id, baseMeta)
          : { content: routed.answer || '', meta };
      }
    } catch (err) {
      console.error(err);
      try {
        const fallback = await apiCall('/v1/chat', {
          method: 'POST',
          body: JSON.stringify({ messages: wireTurns(history), backend })
        });
        outcome = {
          content: fallback.reply || 'No response',
          meta: { mode: 'chat', backend: fallback.backend, model: fallback.model }
        };
      } catch (fallbackErr) {
        outcome = { content: `Error: ${fallbackErr.message}`, meta: { status: 'failed' } };
      }
      setMessages([...history, { role: 'assistant', content: outcome.content, meta: outcome.meta }]);
    } finally {
      setLoading(false);
    }

    if (outcome) {
      await persist([{ role: 'assistant', content: outcome.content, meta: outcome.meta }]);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    // The page heading spans the full width and the two columns sit under it,
    // so the history rail and the chat panel share a top and a bottom edge.
    // With the heading inside the right-hand column they did not: the rail
    // started level with the title and overshot the chat card at both ends,
    // which is what made a correctly-styled sidebar look misaligned and raw.
    <div className="flex flex-col h-[calc(100vh-140px)] animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="mb-4 flex flex-wrap justify-between items-end gap-4">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">AI Chat Assistant</h2>
          <p className="text-slate-500 dark:text-zinc-400">
            Corpus questions are routed to an MCP agent automatically — you'll see which tools it uses.
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <input
            value={campaignId}
            onChange={e => setCampaignId(e.target.value)}
            placeholder="Campaign ID (all)"
            title="Optional campaign scope for agent runs"
            className="px-3 py-1.5 text-xs w-36 bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg"
          />
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
        </div>
      </div>

      <div className="flex-1 flex gap-4 min-h-0">
      {/* ------------------------------------------------------------ sidebar */}
      <aside className="w-72 shrink-0 hidden lg:flex flex-col bg-white dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 rounded-xl shadow-sm overflow-hidden">
        <div className="p-3 border-b border-slate-200 dark:border-zinc-800">
          <button
            onClick={newConversation}
            className="w-full flex items-center justify-center gap-2 px-3 py-2.5 text-sm font-semibold bg-brand-500 hover:bg-brand-600 active:bg-brand-700 text-white rounded-lg shadow-sm transition-colors"
          >
            <Plus size={16} strokeWidth={2.5} /> New chat
          </button>
        </div>

        <div className="px-3 pt-3 pb-1.5 flex items-center justify-between">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400 dark:text-zinc-600">
            History
          </span>
          {conversations.length > 0 && (
            <span className="text-[11px] font-mono text-slate-400 dark:text-zinc-600">
              {conversations.length}
            </span>
          )}
        </div>

        <div className="flex-1 overflow-y-auto px-2 pb-2 space-y-1 flex flex-col">
          {conversations.length === 0 ? (
            // Centred, not pinned to the top: an empty box hugging the top of a
            // full-height rail is what makes the column read as dead space.
            <div className="flex-1 flex flex-col items-center justify-center text-center px-4">
              <MessageSquare size={22} className="mb-2.5 text-slate-300 dark:text-zinc-700" />
              <p className="text-xs text-slate-400 dark:text-zinc-600 leading-relaxed">
                {historyError ? 'History unavailable' : 'Your conversations will appear here'}
              </p>
            </div>
          ) : conversations.map(c => {
            const active = c.id === conversationId;
            return (
              <div
                key={c.id}
                onClick={() => openConversation(c.id)}
                title={c.title}
                className={`group relative flex items-start gap-2.5 pl-3 pr-2 py-2.5 rounded-lg cursor-pointer border transition-colors ${
                  active
                    ? 'bg-brand-50 dark:bg-brand-900/20 border-brand-200 dark:border-brand-800'
                    : 'border-transparent hover:bg-slate-50 dark:hover:bg-zinc-800/60'
                }`}
              >
                {active && (
                  <span className="absolute left-0 top-2.5 bottom-2.5 w-0.5 rounded-full bg-brand-500" />
                )}
                <MessageSquare
                  size={14}
                  className={`shrink-0 mt-0.5 ${active ? 'text-brand-600 dark:text-brand-400' : 'text-slate-400 dark:text-zinc-600'}`}
                />
                <div className="min-w-0 flex-1">
                  <p className={`text-sm truncate leading-snug ${
                    active
                      ? 'font-semibold text-brand-800 dark:text-brand-200'
                      : 'font-medium text-slate-700 dark:text-zinc-300'
                  }`}>
                    {c.title}
                  </p>
                  <div className="flex items-center gap-2 mt-1 text-[11px] text-slate-400 dark:text-zinc-600">
                    <span>{relativeTime(c.updated_at)}</span>
                    <span className="w-1 h-1 rounded-full bg-slate-300 dark:bg-zinc-700" />
                    <span>{c.message_count} turn{c.message_count === 1 ? '' : 's'}</span>
                  </div>
                </div>
                <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
                  <button
                    onClick={(e) => renameConversation(c, e)}
                    className="p-1 rounded text-slate-400 hover:text-brand-600 hover:bg-white dark:hover:bg-zinc-800"
                    title="Rename"
                  >
                    <Pencil size={13} />
                  </button>
                  <button
                    onClick={(e) => deleteConversation(c.id, e)}
                    className="p-1 rounded text-slate-400 hover:text-red-500 hover:bg-white dark:hover:bg-zinc-800"
                    title="Delete"
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              </div>
            );
          })}
        </div>

        {historyError && (
          <div className="px-3 py-2.5 border-t border-slate-200 dark:border-zinc-800 bg-amber-50 dark:bg-amber-900/10 text-[11px] text-amber-700 dark:text-amber-400 flex items-start gap-1.5">
            <CloudOff size={12} className="mt-0.5 shrink-0" />
            <span className="leading-relaxed">Not saving history: {historyError}</span>
          </div>
        )}
      </aside>

      {/* --------------------------------------------------------------- main */}
      <div className="flex-1 flex flex-col min-w-0">
        <div className="flex-1 bg-white dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm flex flex-col min-h-0">
          <div className="flex-1 p-6 overflow-y-auto flex flex-col">
            {messages.length === 0 ? (
              <div className="flex-1 flex flex-col items-center justify-center text-slate-500 dark:text-zinc-500 text-center">
                <MessageSquare size={48} className="mb-4 opacity-20" />
                <h3 className="font-semibold text-lg mb-2">Ask me anything</h3>
                <p className="text-sm max-w-md">
                  General questions are answered directly. Questions about your corpus are handed to
                  the agent that fits them, and you'll see every MCP tool it calls as it calls them.
                </p>
              </div>
            ) : (
              <div className="space-y-6">
                {messages.map((msg, i) => (
                  <React.Fragment key={i}>
                    <SwitchMarker meta={msg.meta} />
                    <div className={`flex gap-4 ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
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
                        {msg.role === 'assistant' ? (
                          <>
                            <ProvenanceBar meta={msg.meta} />
                            {msg.content ? <MarkdownView content={msg.content} /> : <ThinkingDots />}
                            <Citations meta={msg.meta} />
                          </>
                        ) : (
                          <div className="whitespace-pre-wrap text-sm leading-relaxed">{msg.content}</div>
                        )}
                      </div>
                      {msg.role === 'user' && (
                        <div className="w-8 h-8 rounded-full bg-slate-200 dark:bg-zinc-700 text-slate-600 dark:text-zinc-300 flex items-center justify-center shrink-0 mt-1">
                          <User size={16} />
                        </div>
                      )}
                    </div>
                  </React.Fragment>
                ))}
                {loading && messages[messages.length - 1]?.role !== 'assistant' && (
                  <div className="flex gap-4 justify-start">
                    <div className="w-8 h-8 rounded-full bg-brand-500/10 text-brand-600 flex items-center justify-center shrink-0 mt-1">
                      <Bot size={16} />
                    </div>
                    <div className="bg-slate-100 dark:bg-zinc-800 rounded-2xl rounded-tl-none px-5 py-3">
                      <ThinkingDots />
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
      </div>
    </div>
  );
}

/** "3m ago" / "yesterday" — a sidebar has no room for a timestamp. */
function relativeTime(iso) {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const mins = Math.floor((Date.now() - then) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return 'yesterday';
  if (days < 7) return `${days}d ago`;
  return new Date(then).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function ThinkingDots() {
  return (
    <div className="flex items-center gap-2 py-1">
      <div className="w-2 h-2 rounded-full bg-slate-400 animate-bounce"></div>
      <div className="w-2 h-2 rounded-full bg-slate-400 animate-bounce" style={{ animationDelay: '150ms' }}></div>
      <div className="w-2 h-2 rounded-full bg-slate-400 animate-bounce" style={{ animationDelay: '300ms' }}></div>
    </div>
  );
}

/** A handover mid-conversation: a different analyst, with different tools, took over. */
function SwitchMarker({ meta }) {
  if (!meta?.switched_from) return null;
  const to = meta.agent || 'direct chat';
  return (
    <div className="flex items-center justify-center gap-2 text-xs text-slate-500 dark:text-zinc-400">
      <div className="h-px flex-1 bg-slate-200 dark:bg-zinc-800" />
      <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-amber-500/10 text-amber-700 dark:text-amber-400 whitespace-nowrap">
        <span className="font-mono">{meta.switched_from}</span>
        <ArrowRight size={12} />
        <span className="font-mono font-semibold">{to}</span>
        {meta.reason && <span className="italic opacity-80">· {meta.reason}</span>}
      </span>
      <div className="h-px flex-1 bg-slate-200 dark:bg-zinc-800" />
    </div>
  );
}

/** Who answered, and — for an agent — every MCP tool it touched, live. */
function ProvenanceBar({ meta }) {
  const [open, setOpen] = useState(true);
  if (!meta) return null;

  const tools = meta.tools || [];
  const isAgent = meta.mode === 'agent';

  return (
    <div className="mb-2 pb-2 border-b border-slate-200 dark:border-zinc-700/60">
      <div className="flex items-center gap-2 flex-wrap text-xs">
        {isAgent ? (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-brand-500/10 text-brand-600 dark:text-brand-400 font-semibold">
            <Bot size={12} /> {meta.agent}
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-slate-200/70 dark:bg-zinc-700/70 text-slate-600 dark:text-zinc-300 font-medium">
            <MessageSquare size={12} /> direct chat
          </span>
        )}
        {(meta.backend || meta.model) && (
          <span className="text-slate-400 dark:text-zinc-500">
            {meta.backend}{meta.model ? ` · ${meta.model}` : ''}
          </span>
        )}
        {meta.status === 'running' && (
          <span className="inline-flex items-center gap-1 text-amber-600 dark:text-amber-400">
            <Loader2 size={12} className="animate-spin" /> working
          </span>
        )}
        {meta.reason && !meta.switched_from && (
          <span className="text-slate-400 dark:text-zinc-500 italic truncate max-w-[280px]" title={meta.reason}>
            {meta.reason}
          </span>
        )}
        {tools.length > 0 && (
          <button
            onClick={() => setOpen(o => !o)}
            className="inline-flex items-center gap-1 text-slate-500 dark:text-zinc-400 hover:text-brand-600"
          >
            {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            <Wrench size={12} /> {tools.length} MCP call{tools.length === 1 ? '' : 's'}
          </button>
        )}
      </div>

      {open && tools.length > 0 && (
        <div className="mt-2 space-y-1">
          {tools.map((t, idx) => <ToolChip key={t.tool_call_id || idx} tool={t} />)}
        </div>
      )}

      {meta.degraded && (
        <div className="mt-2 text-xs text-amber-600 dark:text-amber-400">
          Agent layer unavailable — answered without corpus tools.
        </div>
      )}
      {meta.error && <div className="mt-2 text-xs text-red-500">{meta.error}</div>}
    </div>
  );
}

function ToolChip({ tool }) {
  const state = tool.status || (tool.error ? 'error' : 'ok');
  const icon = state === 'running'
    ? <Loader2 size={12} className="animate-spin text-amber-500" />
    : state === 'error'
      ? <XCircle size={12} className="text-red-500" />
      : <CheckCircle2 size={12} className="text-emerald-500" />;

  return (
    <div className="flex items-center gap-2 text-xs font-mono bg-white/60 dark:bg-zinc-900/60 border border-slate-200 dark:border-zinc-700 rounded-md px-2 py-1">
      {icon}
      {tool.mcp_server && (
        <span className="inline-flex items-center gap-1 text-slate-500 dark:text-zinc-400">
          <Server size={11} />{tool.mcp_server}
        </span>
      )}
      <span className="text-slate-800 dark:text-zinc-200 font-semibold">{tool.tool_name}</span>
      {typeof tool.result_size === 'number' && (
        <span className="text-slate-400">{tool.result_size} rows</span>
      )}
      {typeof tool.duration_ms === 'number' && (
        <span className="text-slate-400">{tool.duration_ms}ms</span>
      )}
      {tool.error && (
        <span className="text-red-500 truncate max-w-[220px]" title={tool.error}>{tool.error}</span>
      )}
    </div>
  );
}

function Citations({ meta }) {
  if (!meta) return null;
  const cites = meta.citations || [];
  const unverified = meta.unverified || [];
  if (!cites.length && !unverified.length) return null;

  return (
    <div className="mt-3 pt-2 border-t border-slate-200 dark:border-zinc-700/60 text-xs space-y-1">
      {cites.length > 0 && (
        <div className="text-slate-500 dark:text-zinc-400">
          <span className="font-semibold">Grounded post IDs:</span>{' '}
          <span className="font-mono">{cites.slice(0, 12).join(', ')}</span>
          {cites.length > 12 && <span> +{cites.length - 12} more</span>}
        </div>
      )}
      {unverified.length > 0 && (
        <div className="text-amber-600 dark:text-amber-400">
          <span className="font-semibold">Unverified:</span>{' '}
          <span className="font-mono">{unverified.join(', ')}</span> — asserted but not returned by any tool.
        </div>
      )}
    </div>
  );
}
