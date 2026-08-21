import React, { useState, useEffect } from 'react';
import Header from './components/Header';
import NavTabs from './components/NavTabs';
import SystemMonitorDrawer from './components/SystemMonitorDrawer';
import Overview from './pages/Overview';
import Posts from './pages/Posts';
import AnalysisJobs from './pages/AnalysisJobs';
import Reports from './pages/Reports';
import Search from './pages/Search';
import Logs from './pages/Logs';
import Pipeline from './pages/Pipeline';
import Chat from './pages/Chat';
import Agents from './pages/Agents';
import Trace from './pages/Trace';
import Welcome from './pages/Welcome';
import Warnings from './pages/Warnings';
import { getAuthToken, logout } from './utils/api';

function App() {
  const [activeTab, setActiveTab] = useState('overview');
  const [authStatus, setAuthStatus] = useState(!!getAuthToken());
  const [isSystemMonitorOpen, setIsSystemMonitorOpen] = useState(false);

  useEffect(() => {
    const handleAuthExpired = () => setAuthStatus(false);
    window.addEventListener('auth-expired', handleAuthExpired);
    return () => window.removeEventListener('auth-expired', handleAuthExpired);
  }, []);

  useEffect(() => {
    const handleKeyDown = (e) => {
      // Toggle System Monitor with Alt+M or Ctrl+Shift+M
      if ((e.altKey && e.key.toLowerCase() === 'm') || (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'm')) {
        e.preventDefault();
        setIsSystemMonitorOpen((prev) => !prev);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  useEffect(() => {
    const intervalId = setInterval(() => {
      window.dispatchEvent(new Event('auto-refresh'));
    }, 15000);
    return () => clearInterval(intervalId);
  }, []);

  const handleLogout = () => {
    logout();
    setAuthStatus(false);
  };

  if (!authStatus) {
    return <Welcome onAuthenticated={() => setAuthStatus(true)} />;
  }

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-black text-slate-900 dark:text-slate-50 flex flex-col font-sans transition-colors duration-200">
      <Header 
        authStatus={authStatus} 
        onLogout={handleLogout}
        onOpenSystemMonitor={() => setIsSystemMonitorOpen(true)}
      />
      
      <div className="flex-1 flex flex-col md:flex-row overflow-hidden">
        <NavTabs activeTab={activeTab} setActiveTab={setActiveTab} />
        
        <main className="flex-1 overflow-y-auto p-4 md:p-8 bg-white dark:bg-[#09090b] rounded-tl-xl md:border-l md:border-t border-slate-200 dark:border-zinc-800 shadow-inner">
          <div className="max-w-7xl mx-auto">
            {activeTab === 'overview' && <Overview isActive={activeTab === 'overview'} />}
            {activeTab === 'posts' && <Posts />}
            {activeTab === 'jobs' && <AnalysisJobs />}
            {activeTab === 'reports' && <Reports />}
            {activeTab === 'search' && <Search />}
            {activeTab === 'agents' && <Agents />}
            {activeTab === 'chat' && <Chat />}
            {activeTab === 'pipeline' && <Pipeline />}
            {activeTab === 'trace' && <Trace />}
            {activeTab === 'warnings' && <Warnings />}
            {activeTab === 'logs' && <Logs />}
          </div>
        </main>
      </div>

      <div className="h-8 border-t border-slate-200 dark:border-zinc-800 bg-slate-100 dark:bg-[#121214] text-xs flex items-center px-4 gap-4 text-slate-500 dark:text-zinc-400">
        <button
          onClick={() => setIsSystemMonitorOpen(true)}
          className="flex items-center gap-2 hover:text-brand-600 dark:hover:text-brand-400 transition-colors"
          title="Open System Monitor"
        >
          <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>
          <span>System Status: Online</span>
        </button>
        <span className="status-item">
          API: <span id="status-api-base">http://127.0.0.1:8001</span>
        </span>
        <button
          onClick={() => setIsSystemMonitorOpen(true)}
          className="ml-auto text-brand-600 dark:text-brand-400 hover:underline font-medium flex items-center gap-1"
        >
          <span>System Monitor (Alt+M)</span>
        </button>
      </div>

      {/* Slide-over Right Popup Section: System Monitor */}
      <SystemMonitorDrawer
        isOpen={isSystemMonitorOpen}
        onClose={() => setIsSystemMonitorOpen(false)}
      />
    </div>
  );
}

export default App;
