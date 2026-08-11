import React from 'react';
import { LayoutDashboard, MessageSquare, Activity, FileText, Search, Bot, MessageCircle, GitCommit, Terminal, AlertTriangle } from 'lucide-react';

export default function NavTabs({ activeTab, setActiveTab }) {
  const tabs = [
    { id: 'overview', label: 'Overview', icon: LayoutDashboard },
    { id: 'posts', label: 'Posts', icon: MessageSquare },
    { id: 'jobs', label: 'Analysis Jobs', icon: Activity },
    { id: 'reports', label: 'Reports', icon: FileText },
    { id: 'search', label: 'Search', icon: Search },
    { id: 'agents', label: 'Agents', icon: Bot },
    { id: 'chat', label: 'Chat', icon: MessageCircle },
    { id: 'pipeline', label: 'Pipeline', icon: GitCommit },
    { id: 'trace', label: 'Trace', icon: GitCommit },
    { id: 'warnings', label: 'Warnings', icon: AlertTriangle },
    { id: 'logs', label: 'Logs', icon: Terminal, badge: 0 },
  ];

  return (
    <nav className="w-full md:w-64 flex-shrink-0 bg-slate-50 dark:bg-black md:border-r border-b md:border-b-0 border-slate-200 dark:border-zinc-800 p-2 md:p-4 flex md:flex-col gap-1 md:gap-1 overflow-x-auto md:overflow-y-auto flex-nowrap scrollbar-hide shrink-0 z-10">
      <div className="hidden md:block text-xs font-semibold text-slate-400 dark:text-zinc-500 uppercase tracking-wider mb-4 px-3">
        Menu
      </div>
      {tabs.map(tab => {
        const Icon = tab.icon;
        const isActive = activeTab === tab.id;
        return (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex-shrink-0 md:w-full flex items-center justify-between px-3 py-2.5 rounded-lg transition-all duration-200 ${
              isActive 
                ? 'bg-brand-500/10 text-brand-600 dark:text-brand-400 font-medium' 
                : 'text-slate-600 dark:text-zinc-400 hover:bg-slate-200/50 dark:hover:bg-zinc-800/50 hover:text-slate-900 dark:hover:text-zinc-100'
            }`}
          >
            <div className="flex items-center gap-2 md:gap-3">
              <Icon size={18} className={isActive ? 'text-brand-500' : ''} />
              <span className="text-sm md:text-base whitespace-nowrap">{tab.label}</span>
            </div>
            {tab.badge !== undefined && (
              <span className={`hidden md:inline-block ml-2 px-2 py-0.5 text-xs rounded-full ${isActive ? 'bg-brand-500 text-white' : 'bg-slate-200 dark:bg-zinc-800 text-slate-500'}`}>
                {tab.badge}
              </span>
            )}
          </button>
        );
      })}
    </nav>
  );
}
