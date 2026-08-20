import React, { useState, useEffect } from 'react';
import { Download, X } from 'lucide-react';
import { apiCall, API_BASE, getAuthHeaders } from '../utils/api.js';

export default function Reports() {
  const [reports, setReports] = useState([]);
  const [reportType, setReportType] = useState('trend');
  const [campaignId, setCampaignId] = useState('');
  const [grounded, setGrounded] = useState(true);
  const [loading, setLoading] = useState(false);
  const [viewReport, setViewReport] = useState(null);

  useEffect(() => {
    fetchReports();
    
    const handleAutoRefresh = () => fetchReports();
    window.addEventListener('auto-refresh', handleAutoRefresh);
    return () => window.removeEventListener('auto-refresh', handleAutoRefresh);
  }, []);

  const fetchReports = async () => {
    try {
      const data = await apiCall('/v1/reports?limit=20');
      let reportsArr = [];
      if (Array.isArray(data)) reportsArr = data;
      else if (data && Array.isArray(data.reports)) reportsArr = data.reports;
      else if (data && Array.isArray(data.results)) reportsArr = data.results;
      
      setReports(reportsArr);
    } catch (err) {
      console.error(err);
      try {
        const data = await apiCall('/v1/analysis/reports?limit=20');
        let reportsArr = [];
        if (Array.isArray(data)) reportsArr = data;
        else if (data && Array.isArray(data.reports)) reportsArr = data.reports;
        else if (data && Array.isArray(data.results)) reportsArr = data.results;
        setReports(reportsArr);
      } catch {}
    }
  };

  const downloadReport = async (reportId, format = 'pdf') => {
    try {
      const isLatest = !reportId || reportId === 'export_latest' || String(reportId).startsWith('Report ');
      const endpoint = isLatest
        ? `${API_BASE}/v1/reports/export_latest?campaign_id=all&format=${format}`
        : `${API_BASE}/v1/reports/${reportId}/export?format=${format}`;
      const res = await fetch(endpoint, {
        headers: getAuthHeaders()
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || 'Report download failed');
      }
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      const ext = format === 'html' ? 'html' : 'pdf';
      a.download = `analysis_report_${String(reportId).slice(0, 8)}.${ext}`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (err) {
      console.error(err);
      alert('Failed to download report: ' + err.message);
    }
  };

  const handleInstantDownload = async () => {
    setLoading(true);
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
      fetchReports();
    } catch (err) {
      alert('Error generating report: ' + err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleGenerate = async () => {
    setLoading(true);
    try {
      const body = {
        type: reportType,
        grounded: grounded
      };
      if (campaignId.trim()) body.campaign_id = campaignId.trim();

      await apiCall('/v1/reports', {
        method: 'POST',
        body: JSON.stringify(body)
      });
      fetchReports();
    } catch (err) {
      alert("Error generating report: " + err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleView = async (report) => {
    setViewReport(report);
  };

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500 relative">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">Intelligence Reports</h2>
        <p className="text-slate-500 dark:text-zinc-400">Generate and download executive summaries.</p>
      </div>

      <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm p-6">
        <h3 className="font-semibold text-lg mb-2">Generate Report</h3>
        <p className="text-slate-500 text-sm mb-4">Aggregates the analyzed corpus; "Grounded" adds an LLM-written executive summary</p>
        
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex flex-col">
            <label className="text-xs font-semibold text-slate-500 mb-1">Report Type</label>
            <select 
              value={reportType}
              onChange={e => setReportType(e.target.value)}
              className="px-3 py-2 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg w-48"
            >
              <option value="trend">Trend Digest</option>
              <option value="brand_mentions">Brand Mentions</option>
              <option value="political">Political Analysis</option>
              <option value="sentiment_summary">Sentiment Summary</option>
            </select>
          </div>
          <div className="flex flex-col">
            <label className="text-xs font-semibold text-slate-500 mb-1">Campaign (optional)</label>
            <input 
              type="text" 
              placeholder="all campaigns" 
              value={campaignId}
              onChange={e => setCampaignId(e.target.value)}
              className="px-3 py-2 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg w-48"
            />
          </div>
          <div className="flex items-center gap-2 mt-5">
            <label className="flex items-center gap-2 text-sm cursor-pointer" title="Use LLM-B to write the executive summary">
              <input type="checkbox" checked={grounded} onChange={e => setGrounded(e.target.checked)} className="rounded text-brand-500" />
              Grounded (LLM)
            </label>
            <button 
              onClick={handleGenerate}
              disabled={loading}
              className="px-4 py-2 bg-brand-500 hover:bg-brand-600 text-white rounded-lg text-sm font-medium transition-colors"
            >
              {loading ? 'Generating...' : 'Generate Report'}
            </button>
            <button 
              onClick={handleInstantDownload}
              disabled={loading}
              className="px-4 py-2 bg-slate-100 dark:bg-zinc-800 hover:bg-slate-200 dark:hover:bg-zinc-700 text-slate-700 dark:text-zinc-200 border border-slate-200 dark:border-zinc-700 rounded-lg text-sm font-medium flex items-center gap-1.5 transition-colors"
            >
              <Download size={16} /> Download PDF
            </button>
          </div>
        </div>
      </div>

      <div className="bg-white dark:bg-[#09090b] border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm">
        <div className="p-4 border-b border-slate-200 dark:border-zinc-800 bg-slate-50 dark:bg-[#121214] flex justify-between items-center">
          <h3 className="font-semibold">Existing Reports</h3>
          <button onClick={fetchReports} className="px-3 py-1.5 text-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg hover:bg-slate-100 dark:hover:bg-zinc-800">Refresh</button>
        </div>
        
        <div className="p-6">
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {reports.length === 0 ? (
              <div className="border border-slate-200 dark:border-zinc-800 border-dashed rounded-xl p-8 flex flex-col items-center justify-center text-slate-500 dark:text-zinc-500 bg-slate-50 dark:bg-[#121214] col-span-full">
                <p className="text-sm">No reports generated yet.</p>
              </div>
            ) : (
              reports.map((report, i) => {
                const rid = report.report_id || report.id || `Report ${i + 1}`;
                const title = report.title || report.type || `Report ${i + 1}`;
                return (
                  <div key={rid} className="border border-slate-200 dark:border-zinc-800 rounded-xl p-6 bg-white dark:bg-[#121214] flex flex-col shadow-sm">
                    <div className="flex justify-between items-start mb-2">
                      <h3 className="font-semibold text-lg">{title}</h3>
                      {report.summary_source === 'llm' && (
                        <span className="text-xs font-semibold px-2 py-1 bg-brand-500/10 text-brand-600 dark:text-brand-400 rounded-full">LLM</span>
                      )}
                    </div>
                    <p className="text-sm text-slate-500 dark:text-zinc-400 flex-grow mb-4">
                      {report.summary || 'Executive summary generated.'}
                    </p>
                    <div className="flex justify-between items-center text-sm border-t border-slate-200 dark:border-zinc-800 pt-4 mt-auto">
                      <span className="text-slate-400">{report.created_at ? new Date(report.created_at).toLocaleDateString() : ''}</span>
                      <div className="flex items-center gap-3">
                        <button 
                          onClick={() => downloadReport(rid, 'pdf')} 
                          className="text-xs font-semibold px-2 py-1 bg-slate-100 dark:bg-zinc-800 hover:bg-slate-200 dark:hover:bg-zinc-700 text-slate-700 dark:text-zinc-300 rounded flex items-center gap-1 transition-colors"
                          title="Download PDF"
                        >
                          <Download size={13} /> PDF
                        </button>
                        <button onClick={() => handleView(report)} className="text-brand-500 hover:text-brand-600 font-medium">View Report</button>
                      </div>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>

      {viewReport && (() => {
        // Cluster summaries may sit on the report body or on its embedded data.
        const rep = viewReport.data || viewReport;
        return (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
          <div className="bg-white dark:bg-[#121214] border border-slate-200 dark:border-zinc-800 rounded-xl shadow-xl w-full max-w-3xl max-h-[80vh] flex flex-col overflow-hidden">
            <div className="p-4 border-b border-slate-200 dark:border-zinc-800 flex justify-between items-center bg-slate-50 dark:bg-zinc-900">
              <h3 className="font-bold text-lg">{viewReport.title || viewReport.type}</h3>
              <div className="flex items-center gap-2">
                <button 
                  onClick={() => downloadReport(viewReport.report_id || viewReport.id, 'pdf')}
                  className="px-3 py-1.5 bg-brand-500 hover:bg-brand-600 text-white rounded-lg text-xs font-medium flex items-center gap-1.5 transition-colors"
                >
                  <Download size={14} /> Download PDF Report
                </button>
                <button onClick={() => setViewReport(null)} className="p-1 hover:bg-slate-200 dark:hover:bg-zinc-800 rounded">
                  <X size={20} />
                </button>
              </div>
            </div>
            <div className="p-6 overflow-y-auto">
              {viewReport.summary && (
                <div className="mb-6">
                  <h4 className="font-semibold mb-2">Executive Summary</h4>
                  <div className="text-slate-700 dark:text-slate-300 whitespace-pre-wrap leading-relaxed">{viewReport.summary}</div>
                </div>
              )}
              {/* Cluster summaries cost a real LLM call each. They were being
                  computed, paid for and then dropped before reaching any
                  surface (OPEN_ISSUES #4); the dashboard rewrite lost the fix,
                  so they are rendered here again — with their honesty flag. */}
              {Array.isArray(rep.embedding_clusters) && rep.embedding_clusters.length > 0 && (
                <div className="mb-6">
                  <h4 className="font-semibold mb-2 flex items-center gap-2">
                    Embedding clusters
                    <span className="text-xs font-normal text-slate-500">({rep.embedding_clusters.length})</span>
                    {rep.embedding_clusters_are_stub && (
                      <span
                        title="These clusters were computed over STUB embedding vectors — the grouping is reproducible, but it is not semantic."
                        className="text-[10px] font-semibold px-2 py-0.5 bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400 rounded-full uppercase"
                      >
                        stub vectors
                      </span>
                    )}
                  </h4>
                  <div className="space-y-3">
                    {rep.embedding_clusters.map((cluster, ci) => (
                      <div key={ci} className="border border-slate-200 dark:border-zinc-800 rounded-lg p-3 bg-slate-50 dark:bg-[#09090b]">
                        <div className="flex justify-between items-center mb-1">
                          <span className="text-xs font-semibold text-slate-500 uppercase tracking-wider">
                            Cluster {cluster.cluster_id ?? ci}
                          </span>
                          <span className="text-xs text-slate-500">{cluster.size ?? 0} posts</span>
                        </div>
                        <div className="text-sm text-slate-700 dark:text-slate-300">
                          {cluster.summary || cluster.label || '—'}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {viewReport.data && (
                <div>
                  <h4 className="font-semibold mb-2">Raw Data</h4>
                  <pre className="bg-slate-100 dark:bg-[#09090b] p-4 rounded-lg overflow-x-auto text-xs text-slate-600 dark:text-slate-400 font-mono">
                    {JSON.stringify(viewReport.data, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          </div>
        </div>
        );
      })()}
    </div>
  );
}
