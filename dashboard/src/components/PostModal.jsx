import React, { useRef, useState, useEffect } from 'react';
import { X, Download, Heart, MessageCircle, Share2, ThumbsUp, ThumbsDown, Minus, ChevronLeft, ChevronRight } from 'lucide-react';
import { Chart as ChartJS, ArcElement, Tooltip, Legend, CategoryScale, LinearScale, BarElement, Title } from 'chart.js';
import { Doughnut, Bar } from 'react-chartjs-2';
import { apiCall } from '../utils/api';

ChartJS.register(ArcElement, Tooltip, Legend, CategoryScale, LinearScale, BarElement, Title);

export default function PostModal({ post, onClose }) {
  const modalRef = useRef(null);
  
  // Comments state
  const [commentsData, setCommentsData] = useState(null);
  const [commentsLoading, setCommentsLoading] = useState(false);
  const [commentsError, setCommentsError] = useState(null);
  const [commentFilter, setCommentFilter] = useState('all');
  const [commentOffset, setCommentOffset] = useState(0);
  const limit = 100;

  useEffect(() => {
    if (post?.post_id) {
      loadComments(0, 'all');
    }
  }, [post?.post_id]);

  const loadComments = async (offset, sentiment) => {
    setCommentsLoading(true);
    setCommentsError(null);
    try {
      const qs = `?limit=${limit}&offset=${offset}&sentiment=${encodeURIComponent(sentiment)}`;
      const data = await apiCall(`/v1/analysis/post/${encodeURIComponent(post.post_id)}/comments${qs}`);
      setCommentsData(data);
      setCommentOffset(offset);
      setCommentFilter(sentiment);
    } catch (err) {
      setCommentsError(err.message);
    } finally {
      setCommentsLoading(false);
    }
  };

  const handleFilterChange = (f) => loadComments(0, f);
  const handlePage = (delta) => loadComments(Math.max(0, commentOffset + delta * limit), commentFilter);

  if (!post) return null;

  const handleDownload = () => {
    if (!modalRef.current) return;
    const clone = modalRef.current.cloneNode(true);
    const buttons = clone.querySelectorAll('button');
    buttons.forEach(b => b.remove());
    
    const liveCanvas = modalRef.current.querySelectorAll('canvas');
    const clonedCanvas = clone.querySelectorAll('canvas');
    for (let i = 0; i < liveCanvas.length && i < clonedCanvas.length; i++) {
      try {
        const src = liveCanvas[i].toDataURL('image/png');
        const img = document.createElement('img');
        img.src = src;
        img.style.width = liveCanvas[i].style.width || liveCanvas[i].offsetWidth + 'px';
        img.style.height = liveCanvas[i].style.height || liveCanvas[i].offsetHeight + 'px';
        img.style.display = 'block';
        clonedCanvas[i].parentNode.replaceChild(img, clonedCanvas[i]);
      } catch (e) {}
    }

    const w = window.open('', '_blank');
    w.document.write('<!DOCTYPE html><html><head><title>Post Analysis - ' + (post.post_id || 'Detail') + '</title>');
    w.document.write(`
      <style>
        body { font-family: system-ui, -apple-system, sans-serif; color: #333; line-height: 1.5; padding: 20px; max-width: 900px; margin: 0 auto; background: white; }
        h1, h2, h3, h4, h5 { color: #111; margin-bottom: 0.5em; margin-top: 0; }
        .chip { display: inline-flex; align-items: center; padding: 2px 8px; margin: 2px 4px 2px 0; border-radius: 999px; font-size: 11px; font-weight: 600; text-transform: uppercase; background: #f1f5f9; border: 1px solid #e2e8f0; color: #475569; }
        .chip-pos { background: #dcfce7; border-color: #bbf7d0; color: #15803d; }
        .chip-neg { background: #ffe4e6; border-color: #fecdd3; color: #be123c; }
        .chip-brand { background: #eff6ff; border-color: #bfdbfe; color: #1d4ed8; }
        .section { margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid #e2e8f0; }
        .section-title { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: #64748b; margin-bottom: 12px; border-bottom: 1px solid #e2e8f0; padding-bottom: 4px; }
        .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
        .grid-2 { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
        .box { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; font-size: 14px; }
        .bar-wrap { margin-bottom: 8px; }
        .bar-label { display: flex; justify-content: space-between; font-size: 11px; font-weight: 600; margin-bottom: 4px; color: #475569; }
        .bar-track { height: 6px; background: #e2e8f0; border-radius: 3px; overflow: hidden; }
        .bar-fill { height: 100%; border-radius: 3px; }
        .comment-item { padding: 12px; border-bottom: 1px solid #e2e8f0; font-size: 13px; display: flex; gap: 12px; }
        .comment-meta { font-size: 11px; color: #64748b; min-width: 120px; }
        .mini-list { font-size: 12px; }
        .mini-row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #f1f5f9; }
        @media print {
          body { padding: 0; }
          .section { page-break-inside: avoid; }
        }
      </style>
    `);
    w.document.write('</head><body>');
    w.document.write('<h2>Defense Analysis — Post Detail</h2>');
    w.document.write('<div style="font-size:12px; color:#64748b; margin-bottom: 16px;">Post ID: ' + (post.post_id || 'Unknown') + '</div>');
    w.document.write(clone.innerHTML);
    w.document.write('</body></html>');
    w.document.close();
    
    setTimeout(() => {
      w.print();
    }, 500);
  };

  const pct = (val) => Math.round((val || 0) * 100) + '%';
  const formatNumber = (num) => {
    if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
    if (num >= 1000) return (num / 1000).toFixed(1) + 'K';
    return num;
  };

  const getSentimentColor = (s) => {
    if (s === 'positive') return 'text-emerald-700 bg-emerald-100 border-emerald-200 dark:bg-emerald-900/30 dark:text-emerald-400 dark:border-emerald-800';
    if (s === 'negative') return 'text-rose-700 bg-rose-100 border-rose-200 dark:bg-rose-900/30 dark:text-rose-400 dark:border-rose-800';
    return 'text-slate-700 bg-slate-100 border-slate-200 dark:bg-zinc-800 dark:text-zinc-300 dark:border-zinc-700';
  };

  const getSevColor = (score) => {
    if (score > 0.5) return 'bg-rose-500';
    if (score > 0.2) return 'bg-amber-500';
    return 'bg-emerald-500';
  };

  const renderBar = (label, score, colorClass) => (
    <div className="mb-3">
      <div className="flex justify-between text-xs font-semibold mb-1 text-slate-600 dark:text-zinc-400">
        <span>{label}</span>
        <span>{pct(score)}</span>
      </div>
      <div className="w-full h-1.5 bg-slate-200 dark:bg-zinc-800 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${colorClass}`} style={{ width: pct(score) }}></div>
      </div>
    </div>
  );

  const renderSentimentItem = (label, val, score) => (
    <div className="flex flex-col gap-1 p-3 bg-slate-50 dark:bg-zinc-900 rounded-lg border border-slate-200 dark:border-zinc-800 text-center">
      <span className="text-xs font-bold text-slate-500 uppercase tracking-wider">{label}</span>
      <span className={`font-semibold ${val === 'positive' ? 'text-emerald-600 dark:text-emerald-400' : val === 'negative' ? 'text-rose-600 dark:text-rose-400' : 'text-slate-600 dark:text-slate-400'}`}>
        {val || 'N/A'}
      </span>
      {score !== undefined && score !== null && (
        <span className="text-xs text-slate-400">{Number(score).toFixed(3)}</span>
      )}
    </div>
  );

  // Charts
  const emoScores = post.emotion?.scores || {};
  const emotionData = {
    labels: Object.keys(emoScores),
    datasets: [{
      data: Object.values(emoScores).map(v => Number(v) * 100),
      backgroundColor: '#8b5cf6',
      borderRadius: 4,
    }]
  };
  const barOptions = {
    indexAxis: 'y',
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: { x: { display: false, max: 100 }, y: { grid: { display: false } } }
  };

  const reactions = post.reaction_breakdown || {};
  const reactionData = {
    labels: Object.keys(reactions),
    datasets: [{
      data: Object.values(reactions),
      backgroundColor: '#3b82f6',
      borderRadius: 4,
    }]
  };

  const renderCommentAnalytics = () => {
    if (!commentsData) return null;
    
    // Hist
    const hist = commentsData.score_histogram || [];
    const histData = {
      labels: hist.map(b => b.bucket),
      datasets: [{
        data: hist.map(b => b.count),
        backgroundColor: '#0ea5e9',
        borderRadius: 2,
      }]
    };
    
    const topAuthors = (commentsData.top_authors || []).filter(a => a.author && a.author !== '—').slice(0, 5);
    const topLiked = (commentsData.top_liked || []).filter(c => (c.likes || 0) > 0).slice(0, 4);

    return (
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mt-6">
        <div>
          <h4 className="text-sm font-semibold mb-3 flex items-center justify-between">
            Sentiment-score dist
            {commentsData.avg_sentiment_score !== undefined && <span className="text-xs font-normal text-slate-500">avg {commentsData.avg_sentiment_score.toFixed(2)}</span>}
          </h4>
          <div className="h-32"><Bar data={histData} options={{ responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { display: false }, y: { display: false } } }} /></div>
        </div>
        
        <div>
          <h4 className="text-sm font-semibold mb-3">Top authors <span className="text-xs font-normal text-slate-500">(by likes)</span></h4>
          {topAuthors.length > 0 ? (
            <div className="space-y-2">
              {topAuthors.map((a, i) => (
                <div key={i} className="flex justify-between items-center text-xs border-b border-slate-100 dark:border-zinc-800 pb-1">
                  <span className="truncate font-medium max-w-[120px]" title={a.author}>{a.author}</span>
                  <span className="text-slate-500">{a.count}× · <span className="text-rose-500">♥ {formatNumber(a.likes)}</span></span>
                </div>
              ))}
            </div>
          ) : <div className="text-sm text-slate-500">—</div>}
        </div>

        <div>
          <h4 className="text-sm font-semibold mb-3">Most-liked comments</h4>
          {topLiked.length > 0 ? (
            <div className="space-y-3">
              {topLiked.map((c, i) => (
                <div key={i} className="text-xs">
                  <div className="flex gap-2 items-center mb-1">
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold uppercase ${getSentimentColor(c.sentiment || 'neutral')}`}>{c.sentiment || 'neutral'}</span>
                    <span className="text-rose-500 font-medium">♥ {formatNumber(c.likes || 0)}</span>
                  </div>
                  <div className="truncate text-slate-700 dark:text-slate-300">{c.text}</div>
                </div>
              ))}
            </div>
          ) : <div className="text-sm text-slate-500">—</div>}
        </div>
      </div>
    );
  };

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50 backdrop-blur-sm p-4 md:p-6 animate-in fade-in duration-200">
      <div className="bg-white dark:bg-black border border-slate-200 dark:border-zinc-800 rounded-xl shadow-xl w-full max-w-5xl max-h-[95vh] flex flex-col overflow-hidden text-slate-900 dark:text-slate-200">
        
        {/* Header */}
        <div className="flex justify-between items-center p-4 border-b border-slate-200 dark:border-zinc-800 bg-slate-50 dark:bg-zinc-950">
          <div>
            <h3 className="font-bold text-lg">Post Detail</h3>
            <div className="text-xs font-mono text-slate-500">{post.post_id}</div>
          </div>
          <div className="flex gap-2">
            <button onClick={handleDownload} className="flex items-center gap-2 px-3 py-1.5 bg-slate-200 dark:bg-zinc-800 hover:bg-slate-300 dark:hover:bg-zinc-700 rounded-lg text-sm font-medium transition-colors border border-slate-300 dark:border-zinc-700">
              <Download size={16} /> Download
            </button>
            <button onClick={onClose} className="p-1.5 bg-slate-200 dark:bg-zinc-800 hover:bg-slate-300 dark:hover:bg-zinc-700 rounded-lg transition-colors border border-slate-300 dark:border-zinc-700">
              <X size={20} />
            </button>
          </div>
        </div>

        {/* Scrollable Body */}
        <div className="p-6 overflow-y-auto" ref={modalRef}>
          
          {/* Post Info */}
          {post.watchlist_alert && (
            <div className="mb-6 p-4 bg-rose-50 dark:bg-rose-900/20 border border-rose-200 dark:border-rose-800 rounded-xl">
              <h3 className="text-rose-700 dark:text-rose-400 font-bold flex items-center gap-2 mb-1">
                ⚠️ Watchlist Under Attack
              </h3>
              <p className="text-sm text-rose-600 dark:text-rose-300">
                This post or its comments are exhibiting negative or hostile behavior toward a watchlist target.
              </p>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-3 mb-6">
            <div className="flex flex-wrap gap-2 flex-1">
              <span className={`chip ${getSentimentColor(post.overall_sentiment)}`}>sentiment {post.overall_sentiment} {post.sentiment_score?.toFixed(2)}</span>
              {post.confidence?.overall !== undefined && <span className="chip bg-blue-50 border-blue-200 text-blue-700 dark:bg-blue-900/30 dark:border-blue-800 dark:text-blue-400">confidence {pct(post.confidence.overall)}</span>}
              {post.platform && <span className="chip text-slate-600 bg-slate-100 dark:bg-zinc-800 dark:text-zinc-300 dark:border-zinc-700">{post.platform} • lang: {post.language || 'und'}</span>}
              {post.post_type && <span className="chip text-indigo-700 bg-indigo-50 border-indigo-200 dark:bg-indigo-900/30 dark:border-indigo-800 dark:text-indigo-400">type {post.post_type}</span>}
              {post.emotion?.primary && <span className="chip text-purple-700 bg-purple-50 border-purple-200 dark:bg-purple-900/30 dark:border-purple-800 dark:text-purple-400">emotion {post.emotion.primary}</span>}
              {post.toxicity_score !== undefined && <span className="chip bg-slate-100 border-slate-200 text-slate-700 dark:bg-zinc-800 dark:border-zinc-700 dark:text-zinc-300">toxicity {pct(post.toxicity_score)}</span>}
              {post.hate_speech_score !== undefined && <span className="chip bg-slate-100 border-slate-200 text-slate-700 dark:bg-zinc-800 dark:border-zinc-700 dark:text-zinc-300">hate {pct(post.hate_speech_score)}</span>}
            </div>
            {post.engagement && (
              <div className="flex gap-4 text-sm font-medium border-l border-slate-200 dark:border-zinc-800 pl-4 bg-slate-50 dark:bg-zinc-900/50 p-2 rounded-lg">
                <div className="flex items-center gap-1.5 text-rose-500"><Heart size={16} /> {formatNumber(post.engagement.reactions || post.engagement.total_reactions || 0)}</div>
                <div className="flex items-center gap-1.5 text-blue-500"><MessageCircle size={16} /> {formatNumber(post.engagement.comment_count || 0)}</div>
                <div className="flex items-center gap-1.5 text-emerald-500"><Share2 size={16} /> {formatNumber(post.engagement.share_count || 0)}</div>
              </div>
            )}
          </div>

          {/* Original Post */}
          {post.post_text && (
            <div className="section mb-8">
              <div className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2 border-b border-slate-200 dark:border-zinc-800 pb-2">Original Post</div>
              <div className="p-4 bg-slate-50 dark:bg-zinc-900 border border-slate-200 dark:border-zinc-800 rounded-lg whitespace-pre-wrap text-[15px] leading-relaxed">
                {post.post_text}
              </div>
            </div>
          )}

          {/* Post Summary & Insight */}
          {(post.post_summary || post.insight) && (
            <div className="section mb-8 grid grid-cols-1 md:grid-cols-2 gap-6">
              {post.post_summary && (
                <div>
                  <div className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2 border-b border-slate-200 dark:border-zinc-800 pb-2 flex items-center justify-between">
                    <span>Post Summary</span>
                    {post.post_summary_source && <span className="px-1.5 py-0.5 bg-brand-100 text-brand-700 dark:bg-brand-900/30 dark:text-brand-400 rounded border border-brand-200 dark:border-brand-800 text-[10px] uppercase">{post.post_summary_source}</span>}
                  </div>
                  <div className="p-4 bg-brand-50/50 dark:bg-brand-900/10 border border-brand-200 dark:border-brand-900/30 rounded-lg text-sm text-brand-950 dark:text-brand-100 leading-relaxed">
                    {post.post_summary}
                    {post.post_summary_grounding && <div className="text-xs text-brand-600/70 dark:text-brand-400/70 mt-3 pt-3 border-t border-brand-200/50 dark:border-brand-800/50">Grounded on: {post.post_summary_grounding}</div>}
                  </div>
                </div>
              )}
              {post.insight && (
                <div>
                  <div className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2 border-b border-slate-200 dark:border-zinc-800 pb-2">Insight (Stage 2)</div>
                  <div className="p-4 bg-amber-50/50 dark:bg-amber-900/10 border border-amber-200 dark:border-amber-900/30 rounded-lg text-sm text-amber-950 dark:text-amber-100 leading-relaxed">
                    {post.insight}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Sentiment Breakdown */}
          <div className="section mb-8">
            <div className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3 border-b border-slate-200 dark:border-zinc-800 pb-2">Sentiment Breakdown</div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              {renderSentimentItem('Text', post.text_sentiment?.label, post.text_sentiment?.score)}
              {renderSentimentItem('Image', post.image_sentiment?.label, post.image_sentiment?.score)}
              {renderSentimentItem('Overall', post.overall_sentiment, post.sentiment_score)}
              {post.baseline_sentiment !== undefined && renderSentimentItem('Baseline (Upstream)', Number(post.baseline_sentiment) > 0.1 ? 'positive' : Number(post.baseline_sentiment) < -0.1 ? 'negative' : 'neutral', post.baseline_sentiment)}
            </div>
          </div>

          {/* Signals */}
          <div className="section mb-8">
            <div className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3 border-b border-slate-200 dark:border-zinc-800 pb-2">Signals</div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-8 p-4 bg-slate-50 dark:bg-zinc-900 border border-slate-200 dark:border-zinc-800 rounded-lg">
              
              <div>
                <h4 className="text-sm font-semibold mb-3 flex items-center justify-between">
                  Emotion
                  {post.emotion?.primary && <span className="text-purple-600 dark:text-purple-400 text-xs bg-purple-100 dark:bg-purple-900/30 px-2 py-0.5 rounded-full uppercase">{post.emotion.primary}</span>}
                </h4>
                {Object.keys(emoScores).length > 0 ? (
                  <div className="h-28"><Bar data={emotionData} options={barOptions} /></div>
                ) : <div className="text-sm text-slate-400">—</div>}
              </div>

              <div>
                <h4 className="text-sm font-semibold mb-3">Confidence</h4>
                {renderBar('Overall', post.confidence?.overall, 'bg-brand-500')}
                {renderBar('Sentiment', post.confidence?.sentiment, 'bg-brand-500')}
                {renderBar('Language', post.confidence?.language, 'bg-brand-500')}
                {renderBar('Topics', post.confidence?.topics, 'bg-brand-500')}
              </div>

              <div>
                <h4 className="text-sm font-semibold mb-3">Safety</h4>
                {renderBar('Toxicity', post.toxicity_score, getSevColor(post.toxicity_score))}
                {renderBar('Hate speech', post.hate_speech_score, getSevColor(post.hate_speech_score))}
                {post.intents && post.intents.length > 0 && (
                  <div className="mt-4">
                    <h4 className="text-xs font-semibold mb-2 text-slate-500">Intents</h4>
                    <div className="flex flex-wrap gap-1">
                      {post.intents.map(t => <span key={t} className="px-2 py-0.5 bg-white dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded text-[10px] font-bold text-slate-600 dark:text-zinc-300 uppercase">{t}</span>)}
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Reaction Breakdown */}
          {Object.keys(reactions).length > 0 && (
            <div className="section mb-8">
              <div className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3 border-b border-slate-200 dark:border-zinc-800 pb-2">Reaction Breakdown</div>
              <div className="h-32"><Bar data={reactionData} options={barOptions} /></div>
            </div>
          )}

          {/* Dynamic Comment Section */}
          <div className="section mb-6">
            <div className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-4 border-b border-slate-200 dark:border-zinc-800 pb-2 flex items-center justify-between">
              <span>Comment Sentiment {post.comment_analysis?.coverage_label && `(${post.comment_analysis.coverage_label})`}</span>
              {commentsLoading && <span className="text-brand-500 flex items-center gap-2"><div className="w-3 h-3 rounded-full border-2 border-brand-500 border-t-transparent animate-spin"></div> Loading...</span>}
            </div>

            {commentsError && (
              <div className="p-4 bg-rose-50 dark:bg-rose-900/20 text-rose-700 dark:text-rose-400 rounded-lg border border-rose-200 dark:border-rose-800/50 mb-4 text-sm flex items-center gap-2">
                <AlertCircle size={16} /> {commentsError}
              </div>
            )}

            {/* AI Summary and Top Themes */}
            {post.comment_analysis?.ai_summary && (
              <div className="mb-6 bg-slate-50 dark:bg-zinc-900 border border-slate-200 dark:border-zinc-800 rounded-lg p-4">
                <h4 className="text-sm font-semibold mb-2 text-slate-800 dark:text-slate-200">AI Summary</h4>
                <div className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed mb-4">{post.comment_analysis.ai_summary}</div>
                {post.comment_analysis.top_themes?.length > 0 && (
                  <div>
                    <h4 className="text-xs font-semibold mb-2 text-slate-500 uppercase tracking-wider">Top Themes</h4>
                    <div className="flex flex-wrap gap-2">
                      {post.comment_analysis.top_themes.map((t, i) => (
                        <span key={i} className="px-2.5 py-1 bg-white dark:bg-zinc-800 text-slate-700 dark:text-zinc-300 rounded-full border border-slate-200 dark:border-zinc-700 text-xs font-medium">
                          {t.theme || t}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Injected Analytics Grid */}
            {renderCommentAnalytics()}

            {/* Comment List */}
            {commentsData && (
              <div className="mt-8 border-t border-slate-200 dark:border-zinc-800 pt-6">
                <div className="flex flex-wrap justify-between items-center mb-4 gap-4">
                  <h4 className="font-semibold text-slate-800 dark:text-slate-200 text-sm">Every comment <span className="text-xs font-normal text-slate-500 ml-1">(stance toward post · full coverage)</span></h4>
                  <div className="flex items-center gap-2">
                    <div className="flex bg-slate-100 dark:bg-zinc-900 p-1 rounded-lg">
                      {['all', 'positive', 'negative', 'neutral'].map(f => (
                        <button key={f} onClick={() => handleFilterChange(f)} className={`px-3 py-1 text-xs font-medium rounded-md capitalize transition-colors ${commentFilter === f ? 'bg-white dark:bg-zinc-700 shadow-sm text-slate-900 dark:text-white' : 'text-slate-500 hover:text-slate-700 dark:hover:text-zinc-300'}`}>
                          {f}
                        </button>
                      ))}
                    </div>
                    <span className="text-xs text-slate-500 ml-2 font-medium bg-slate-50 dark:bg-zinc-900 px-2 py-1 rounded border border-slate-200 dark:border-zinc-800">{commentsData.filtered_total} / {commentsData.total} comments</span>
                  </div>
                </div>

                <div className="border border-slate-200 dark:border-zinc-800 rounded-xl overflow-hidden bg-white dark:bg-black">
                  {commentsData.comments?.length === 0 ? (
                    <div className="p-8 text-center text-sm text-slate-500 bg-slate-50 dark:bg-zinc-900">No comments found for this filter.</div>
                  ) : (
                    <div className="divide-y divide-slate-100 dark:divide-zinc-800/60">
                      {commentsData.comments?.map((c, i) => (
                        <div key={c.id || i} className="p-3 md:p-4 flex flex-col md:flex-row md:items-start gap-3 md:gap-4 hover:bg-slate-50 dark:hover:bg-zinc-900 transition-colors">
                          <div className="w-32 shrink-0 flex flex-col gap-1.5">
                            <div className="flex items-center mb-1">
                              <span className={`px-2 py-1 rounded text-[10px] font-bold uppercase tracking-wider ${getSentimentColor(c.sentiment || 'neutral')}`}>
                                {c.sentiment || 'neutral'}
                              </span>
                            </div>
                            {c.parallel_labels && (
                              <>
                                {c.parallel_labels.llm && (
                                  <div className="flex justify-between items-center bg-slate-50 dark:bg-zinc-900/50 px-2 py-1 rounded border border-slate-100 dark:border-zinc-800">
                                    <span className="text-[9px] font-bold text-slate-500">LLM</span>
                                    <span className={`text-[10px] font-bold uppercase tracking-wider ${getSentimentColor(c.parallel_labels.llm.sentiment)}`}>
                                      {c.parallel_labels.llm.sentiment}
                                    </span>
                                  </div>
                                )}
                                {c.parallel_labels.xlmr && (
                                  <div className="flex justify-between items-center bg-slate-50 dark:bg-zinc-900/50 px-2 py-1 rounded border border-slate-100 dark:border-zinc-800">
                                    <span className="text-[9px] font-bold text-slate-500">XLM-R</span>
                                    <span className={`text-[10px] font-bold uppercase tracking-wider ${getSentimentColor(c.parallel_labels.xlmr.sentiment)}`}>
                                      {c.parallel_labels.xlmr.sentiment}
                                    </span>
                                  </div>
                                )}
                                {c.parallel_labels.distilbert && (
                                  <div className="flex justify-between items-center bg-slate-50 dark:bg-zinc-900/50 px-2 py-1 rounded border border-slate-100 dark:border-zinc-800">
                                    <span className="text-[9px] font-bold text-slate-500">DistilBERT</span>
                                    <span className={`text-[10px] font-bold uppercase tracking-wider ${getSentimentColor(c.parallel_labels.distilbert.sentiment)}`}>
                                      {c.parallel_labels.distilbert.sentiment}
                                    </span>
                                  </div>
                                )}
                              </>
                            )}
                          </div>
                          <div className="flex-1 text-sm text-slate-800 dark:text-slate-200">{c.text || c.comment_text}</div>
                          <div className="w-full md:w-auto shrink-0 flex flex-wrap items-center gap-4 text-xs font-medium text-slate-500 dark:text-zinc-400 bg-slate-50 dark:bg-zinc-900 px-3 py-1.5 rounded-lg border border-slate-100 dark:border-zinc-800/50">
                            {c.emotion && <span className="flex items-center gap-1"><span className="text-slate-400 text-[10px] uppercase">E:</span> {c.emotion}</span>}
                            {c.sentiment_score !== undefined && <span className="flex items-center gap-1"><span className="text-slate-400 text-[10px] uppercase">Score:</span> {c.sentiment_score.toFixed(2)}</span>}
                            {c.likes !== undefined && c.likes > 0 && <span className="flex items-center gap-1 text-rose-500 bg-rose-50 dark:bg-rose-900/20 px-1.5 py-0.5 rounded"><Heart size={10}/> {formatNumber(c.likes)}</span>}
                            {(c.label_method || c.method) && <span className="text-[10px] bg-slate-200 dark:bg-zinc-800 px-1.5 py-0.5 rounded">{c.label_method || c.method}</span>}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                  
                  {/* Pagination */}
                  {(commentOffset > 0 || (commentsData.comments?.length === limit)) && (
                    <div className="p-3 border-t border-slate-200 dark:border-zinc-800 bg-slate-50 dark:bg-zinc-900 flex justify-between items-center">
                      <button 
                        onClick={() => handlePage(-1)}
                        disabled={commentOffset === 0}
                        className="flex items-center gap-1 px-3 py-1.5 text-xs font-medium bg-white dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded-lg hover:bg-slate-100 dark:hover:bg-zinc-700 disabled:opacity-50 transition-colors"
                      >
                        <ChevronLeft size={14} /> Previous
                      </button>
                      <span className="text-xs font-medium text-slate-500">
                        {commentOffset + 1} - {Math.min(commentOffset + limit, commentsData.filtered_total)}
                      </span>
                      <button 
                        onClick={() => handlePage(1)}
                        disabled={commentOffset + limit >= commentsData.filtered_total}
                        className="flex items-center gap-1 px-3 py-1.5 text-xs font-medium bg-white dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded-lg hover:bg-slate-100 dark:hover:bg-zinc-700 disabled:opacity-50 transition-colors"
                      >
                        Next <ChevronRight size={14} />
                      </button>
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
          
        </div>
      </div>
    </div>
  );
}
