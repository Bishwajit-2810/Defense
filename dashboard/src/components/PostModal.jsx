import React, { useRef, useState, useEffect, useCallback } from 'react';
import { X, Download, Heart, MessageCircle, Share2, ChevronLeft, ChevronRight, AlertCircle } from 'lucide-react';
import { Chart as ChartJS, ArcElement, Tooltip, Legend, CategoryScale, LinearScale, BarElement, Title } from 'chart.js';
import { Bar } from 'react-chartjs-2';
import { apiCall } from '../utils/api';
import { formatAlertReason } from '../utils/sentiment';
import { commentScrape, SCRAPE_TAG, scrapeTooltip } from '../utils/coverage';

ChartJS.register(ArcElement, Tooltip, Legend, CategoryScale, LinearScale, BarElement, Title);

// Every labeller the ensemble can hear from, in the order they are worth
// reading: the one that saw the post first, then the seven cheap heads.
// Must stay in sync with Settings.stage2_classifier_names + ensemble.CHEAP_SOURCES
// — a voter missing here runs, costs its forward pass, and is invisible.
//
// Stage 1's emoji + lexicon rule ('heuristic') is deliberately NOT here: it stopped
// voting on 17 Aug 2026, because a keyword rule — largely the deterministic hash
// stub in the shipped configuration — is not a model reading the comment. Its
// label still shows up in the row's `method` chip and in `provenance`; it is no
// longer presented as one of the verdicts.
const LABELLERS = [
  ['LLM', 'llm', 'context-aware stance pass — the only one that read the post'],
  ['XLM-R', 'xlmr', 'tabularisai/multilingual-sentiment-analysis'],
  ['DistilBERT', 'distilbert', 'lxyuan/distilbert-base-multilingual-cased-sentiments-student'],
  ['Twitter XLM-R', 'twitter_xlmr', 'cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual'],
  ['BanglaBERT', 'banglabert', 'ADn-001/banglabert-sentnob-sentiment'],
  ['Bangla 5-cls', 'bengali_sentiment_bert', 'ahs95/banglabert-sentiment-analysis'],
  ['mBERT', 'mbert', 'nlptown/bert-base-multilingual-uncased-sentiment'],
  ['ModernBERT', 'modernbert', 'clapAI/modernBERT-base-multilingual-sentiment'],
];

// The router's TEXTLESS_KINDS (rules.py): comments with nothing for a model to
// read. They come back `sentiment: "uncertain"`, which is true but misleading in
// the stance column — "uncertain" is the ensemble abstaining after reading, and
// these were never readable. A 👍👍 row showing amber `UNCERTAIN` next to eight
// "—" voters invites the reader to count it as a failed label instead of a
// comment that carries a reaction and no text.
const TEXTLESS_BADGE = {
  emoji: { label: 'emoji', title: 'Emoji-only comment — no words for a model to read, so no stance is claimed. Its emotion (E:) comes from Stage 1\u2019s emoji rule, which is not one of the eight voters.' },
  link: { label: 'link only', title: 'The comment is a bare link — no text for a model to read, so no stance is claimed.' },
  filtered: { label: 'filtered', title: 'The comment was filtered upstream (no readable text), so no stance is claimed.' },
};

export default function PostModal({ post, onClose }) {
  const modalRef = useRef(null);
  
  // Comments state
  const [commentsData, setCommentsData] = useState(null);
  const [commentsLoading, setCommentsLoading] = useState(false);
  const [commentsError, setCommentsError] = useState(null);
  const [commentFilter, setCommentFilter] = useState('all');
  const [commentOffset, setCommentOffset] = useState(0);
  const limit = 100;
  // One source for the id both the loader and its effect key on.
  const postId = post?.post_id;

  // Declared above the effect that depends on it, and keyed on the post id
  // rather than on `post`: a parent refetch hands this component a new object
  // with the same id on every poll, and re-requesting up to 2,000 comment rows
  // each time is the cost of getting that wrong. Identity therefore changes
  // exactly when the post does — which is also what makes it safe to name in the
  // dependency array below instead of suppressing the warning.
  const loadComments = useCallback(async (offset, sentiment) => {
    if (!postId) return;
    setCommentsLoading(true);
    setCommentsError(null);
    try {
      const qs = `?limit=${limit}&offset=${offset}&sentiment=${encodeURIComponent(sentiment)}`;
      const data = await apiCall(`/v1/analysis/post/${encodeURIComponent(postId)}/comments${qs}`);
      setCommentsData(data);
      setCommentOffset(offset);
      setCommentFilter(sentiment);
    } catch (err) {
      setCommentsError(err.message);
    } finally {
      setCommentsLoading(false);
    }
  }, [postId]);

  useEffect(() => {
    if (postId) {
      loadComments(0, 'all');
    }
  }, [postId, loadComments]);

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
      } catch {}
    }

    const headStyles = Array.from(document.head.querySelectorAll('style, link[rel="stylesheet"]'))
      .map(node => node.outerHTML)
      .join('\n');

    const w = window.open('', '_blank');
    w.document.write('<!DOCTYPE html><html><head><title>Post Analysis - ' + (post.post_id || 'Detail') + '</title>');
    w.document.write(headStyles);
    w.document.write(`
      <style>
        body { 
          font-family: system-ui, -apple-system, sans-serif; 
          background: white !important;
          color: black;
          padding: 20px; 
          max-width: 1000px; 
          margin: 0 auto; 
          -webkit-print-color-adjust: exact; 
          print-color-adjust: exact; 
        }
        @media print {
          body { padding: 0; }
          .section { page-break-inside: avoid; }
        }
      </style>
    `);
    w.document.write('</head><body class="bg-white dark:bg-white text-slate-900">');
    w.document.write('<div style="margin-bottom: 20px;">');
    w.document.write('<h2 class="text-2xl font-bold mb-1">Selective Intelligence — Post Detail</h2>');
    w.document.write('<div class="text-sm text-slate-500 mb-4">Post ID: ' + (post.post_id || 'Unknown') + '</div>');
    w.document.write(clone.innerHTML);
    w.document.write('</div>');
    w.document.write('</body></html>');
    w.document.close();
    
    setTimeout(() => {
      w.print();
    }, 1000);
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
    // `uncertain` is an ABSTENTION, not a neutral verdict. Rendering it in the
    // same grey as neutral hides the one distinction the ensemble exists to
    // make — "we could not label this" reading as "we judged this neutral".
    if (s === 'uncertain') return 'text-amber-700 bg-amber-100 border-amber-200 dark:bg-amber-900/30 dark:text-amber-400 dark:border-amber-800';
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

  // Which comments the router put in front of the models — top-N by reaction
  // count (ROUTER_COMMENT_TOP_N). Read by the ensemble bar AND the per-comment
  // rows, so both explain an empty labeller cell the same way.
  const commentSelection =
    commentsData?.ensemble?.selection || commentsData?.stage2_selection || {};

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
  const scrape = commentScrape(post);

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

    const ens = commentsData.ensemble || {};
    const sel = commentSelection;
    return (
      <>
      {/* How these labels were produced, and what the agreement bought. Shown
          next to the counts so a chart never appears without its provenance. */}
      {ens.comments > 0 && (
        <div className="mt-6 flex flex-wrap items-center gap-x-6 gap-y-2 text-xs bg-slate-50 dark:bg-zinc-900 border border-slate-200 dark:border-zinc-800 rounded-lg px-4 py-3">
          <span className="font-semibold text-slate-700 dark:text-slate-300">Label ensemble</span>
          <span className="text-slate-500">voters: <span className="font-medium text-slate-700 dark:text-slate-300">{(ens.voters || []).join(' · ') || '—'}</span></span>
          <span className="text-slate-500">unanimous: <span className="font-medium text-emerald-600 dark:text-emerald-400">{Math.round((ens.unanimous_share || 0) * 100)}%</span></span>
          {/* LLM coverage is counted from the labels that came back, not from
              what was requested — a half-failed stance pass must not report
              full coverage. */}
          <span className="text-slate-500"
                title={ens.mode === 'all'
                  ? 'COMMENT_LLM_MODE=all — every comment with text is sent to the LLM'
                  : 'COMMENT_LLM_MODE=escalate — only comments the cheap labellers disagreed on'}>
            LLM labelled: <span className="font-medium text-indigo-600 dark:text-indigo-400">
              {ens.llm_labelled ?? 0}/{ens.comments} ({Math.round((ens.llm_share || 0) * 100)}%)
            </span>
          </span>
          {/* The router analyses the top-N comments by reaction count, and every
              model reads that same set. Shown next to LLM coverage because the
              two are read together: 100/2857 is not a failed stance pass, it is
              a cap — and the share OF THE SELECTION is the number that says
              whether the pass succeeded. */}
          {ens.not_analysed > 0 && (
            <span className="text-slate-500"
                  title={`ROUTER_COMMENT_TOP_N=${sel.limit ?? '?'} — the ${sel.selected ?? 0} most-reacted comments with text were analysed by every model (cutoff: ${sel.cutoff_likes ?? 0} reactions). The other ${ens.not_analysed} keep their Stage-1 label. Set ROUTER_COMMENT_TOP_N=0 to analyse the whole thread.`}>
              analysed: <span className="font-medium text-slate-700 dark:text-slate-300">
                top {ens.analysed}/{ens.comments} by reactions
              </span>
              <span className="ml-1 text-indigo-600 dark:text-indigo-400">
                ({Math.round((ens.llm_share_analysed || 0) * 100)}% LLM of those)
              </span>
            </span>
          )}
          <span className="text-slate-500">abstained: <span className="font-medium text-amber-600 dark:text-amber-500">{ens.abstained || 0}</span></span>
          {ens.deduplicated > 0 && (
            <span className="text-slate-500" title="Identical text after normalisation — the twin's verdict was reused rather than re-asked.">
              near-dup reuse: <span className="font-medium text-sky-600 dark:text-sky-400">{ens.deduplicated}</span>
            </span>
          )}
          {ens.capped_out > 0 && (
            <span className="text-rose-600 dark:text-rose-400 font-medium"
                  title="COMMENT_STANCE_MAX_PER_POST dropped these before the LLM saw them. Set it to 0 to label every comment.">
              ⚠ {ens.capped_out} capped out
            </span>
          )}
        </div>
      )}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mt-6">
        <div>
          <h4 className="text-sm font-semibold mb-3 flex items-center justify-between">
            Sentiment-score dist
            {Number.isFinite(commentsData.avg_sentiment_score) && <span className="text-xs font-normal text-slate-500">avg {commentsData.avg_sentiment_score.toFixed(2)}</span>}
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
      </>
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
                ⚠️ Watchlist alert
              </h3>
              {/* The reason, not a blanket "under attack": an `always` target
                  alerts on a plain mention, which is not hostility. */}
              <p className="text-sm text-rose-600 dark:text-rose-300">
                {formatAlertReason(post.watchlist_alert_reason) || 'A watchlist target was matched in this post or its comments.'}
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
              {/* Scrape depth, not analysis depth. Without this the header's
                  comment number reads as a coverage claim it cannot support. */}
              {scrape && (
                <span className={`chip ${SCRAPE_TAG[scrape.state].cls}`} title={SCRAPE_TAG[scrape.state].title(scrape)}>
                  {SCRAPE_TAG[scrape.state].text(scrape)}
                </span>
              )}
            </div>
            {post.engagement && (
              <div className="flex gap-4 text-sm font-medium border-l border-slate-200 dark:border-zinc-800 pl-4 bg-slate-50 dark:bg-zinc-900/50 p-2 rounded-lg">
                <div className="flex items-center gap-1.5 text-rose-500"><Heart size={16} /> {formatNumber(post.engagement.reactions || post.engagement.total_reactions || 0)}</div>
                <div className="flex items-center gap-1.5 text-blue-500" title={scrapeTooltip(scrape, post.platform)}>
                  <MessageCircle size={16} />
                  {scrape
                    ? <span>
                        {scrape.analysed !== null && <>{formatNumber(scrape.analysed)}<span className="text-slate-400 dark:text-zinc-500 font-normal"> / </span></>}
                        {formatNumber(scrape.stored)}
                        <span className="text-slate-400 dark:text-zinc-500 font-normal"> / {scrape.total ? formatNumber(scrape.total) : '?'}</span>
                      </span>
                    : formatNumber(post.engagement.comment_count || 0)}
                </div>
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
                      {['all', 'positive', 'negative', 'neutral', 'uncertain', 'disagreed'].map(f => (
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
                              {/* Only when no model read it: if a voter did label a
                                  textless row, that verdict is the thing to show. */}
                              {TEXTLESS_BADGE[c.kind] && c.label_voters === 0 ? (
                                <span className="px-2 py-1 rounded text-[10px] font-bold uppercase tracking-wider text-sky-700 bg-sky-50 border border-sky-200 dark:bg-sky-950/40 dark:text-sky-300 dark:border-sky-900"
                                      title={TEXTLESS_BADGE[c.kind].title}>
                                  {TEXTLESS_BADGE[c.kind].label}
                                </span>
                              ) : (
                                <span className={`px-2 py-1 rounded text-[10px] font-bold uppercase tracking-wider ${getSentimentColor(c.sentiment || 'neutral')}`}>
                                  {c.sentiment || 'neutral'}
                                </span>
                              )}
                            </div>
                            {/* All eight labellers, ALWAYS rendered. A source
                                that did not vote shows "—" rather than
                                vanishing: an absent row and a neutral verdict
                                must not look the same. Rendering only the
                                three that happened to be loaded is how five
                                dead heads sat in the roster unnoticed. */}
                            {LABELLERS.map(([label, key, what]) => {
                              const v = (c.parallel_labels || {})[key];
                              return (
                                <div key={key}
                                     title={v ? `${label}: ${v.sentiment}\n${what}` : `${label} did not label this comment (${c.escalation_reason || 'not run'})\n${what}`}
                                     className="flex justify-between items-center gap-1 bg-slate-50 dark:bg-zinc-900/50 px-2 py-0.5 rounded border border-slate-100 dark:border-zinc-800">
                                  <span className="text-[9px] font-bold text-slate-500 truncate">{label}</span>
                                  <span className={`text-[10px] font-bold uppercase tracking-wider shrink-0 ${v ? getSentimentColor(v.sentiment) : 'text-slate-400'}`}>
                                    {v ? (v.sentiment || '').slice(0, 3) : '—'}
                                  </span>
                                </div>
                              );
                            })}
                          </div>
                          <div className="flex-1 text-sm text-slate-800 dark:text-slate-200">{c.text || c.comment_text}</div>
                          <div className="w-full md:w-auto shrink-0 flex flex-wrap items-center gap-4 text-xs font-medium text-slate-500 dark:text-zinc-400 bg-slate-50 dark:bg-zinc-900 px-3 py-1.5 rounded-lg border border-slate-100 dark:border-zinc-800/50">
                            {c.emotion && <span className="flex items-center gap-1"><span className="text-slate-400 text-[10px] uppercase">E:</span> {c.emotion}</span>}
                            {/* Not in the router's top-N by reactions, or filtered duplicate/short — so no model
                                read it — the row reports `uncertain`, not a label.
                                Said on the row, because "no model was asked" and
                                "the models could not agree" both end up looking
                                like an unlabelled comment otherwise. */}
                            {c.stage2_selected === false && (
                              <span className={`text-[9px] uppercase tracking-wider rounded px-1.5 py-0.5 border ${
                                c.stage2_skip_reason === 'duplicate' 
                                  ? 'text-purple-600 bg-purple-50 border-purple-200 dark:bg-purple-950/30 dark:text-purple-400 dark:border-purple-800' 
                                  : c.stage2_skip_reason === 'below_min_words' 
                                  ? 'text-amber-600 bg-amber-50 border-amber-200 dark:bg-amber-950/30 dark:text-amber-400 dark:border-amber-800' 
                                  : 'text-slate-400 border-slate-200 dark:border-zinc-700'
                              }`}
                              title={
                                c.stage2_skip_reason === 'duplicate' 
                                  ? 'Filtered as duplicate/repeat comment in router. Representative comment is analysed instead.'
                                  : c.stage2_skip_reason === 'below_min_words'
                                  ? 'Filtered: comment contains fewer than 3 words.'
                                  : `Outside the top ${commentSelection.limit ?? '?'} comments by reaction count (cutoff ${commentSelection.cutoff_likes ?? 0}).`
                              }>
                                {c.stage2_skip_reason === 'duplicate' ? 'duplicate' : (c.stage2_skip_reason === 'below_min_words' ? '<3 words' : 'not selected')}
                              </span>
                            )}
                            {/* No score for a comment nobody read. `label_voters:
                                0` carries `sentiment_score: 0.0`, and "Score:
                                0.00" beside it reads as a measured neutral —
                                the exact claim this project retracts. */}
                            {c.label_voters !== 0 && Number.isFinite(c.sentiment_score) && <span className="flex items-center gap-1"><span className="text-slate-400 text-[10px] uppercase">Score:</span> {c.sentiment_score.toFixed(2)}</span>}
                            {/* "n/m", never a bare percentage: 100% over a
                                single voter is one model's opinion, and
                                rendering it as "100% agree" reads as consensus.
                                Amber whenever it is not unanimous, or when only
                                one labeller spoke.

                                Zero voters is not 0% agreement — it is the
                                absence of a reading. Rendering it as "0%" put a
                                comment nobody labelled in the same visual class
                                as one the labellers fought over. */}
                            {c.label_voters === 0 ? (
                              <span className="flex items-center gap-1 text-amber-600 dark:text-amber-500"
                                    title="No model read this comment — so nothing is claimed about it. Usual causes: the router did not select it, it has no text, every classifier was unavailable, or no LLM verdict returned. Stage 1&#39;s emoji/keyword label is not a voter.">
                                <span className="text-slate-400 text-[10px] uppercase">Agree:</span>
                                <span className="text-[9px] uppercase">not read</span>
                              </span>
                            ) : Number.isFinite(c.label_agreement) && (() => {
                              const m = c.label_voters ?? null;
                              const n = m ? Math.round(c.label_agreement * m) : null;
                              const weak = c.label_agreement < 1 || m === 1;
                              return (
                                <span className={`flex items-center gap-1 ${weak ? 'text-amber-600 dark:text-amber-500' : ''}`}
                                      title={m
                                        ? `${n} of ${m} labellers agreed (${(c.label_sources || []).join(', ')})`
                                        : 'agreement among the labellers that voted'}>
                                  <span className="text-slate-400 text-[10px] uppercase">Agree:</span>
                                  {m ? `${n}/${m}` : `${Math.round(c.label_agreement * 100)}%`}
                                  {m === 1 && <span className="text-[9px] uppercase">(1 model only)</span>}
                                </span>
                              );
                            })()}
                            {c.label_source === 'propagated' && (
                              <span title={`Label copied from near-duplicate comment ${c.propagated_from || ''}`} className="text-[10px] bg-sky-100 dark:bg-sky-900/40 text-sky-700 dark:text-sky-300 px-1.5 py-0.5 rounded">propagated</span>
                            )}
                            {/* Half the voters backed this, and the LLM was one
                                of them. Not consensus — say so. */}
                            {c.tie_broken_by && (
                              <span title={`The labellers split evenly; ${c.tie_broken_by} decided it because it is the only one that reads the post.`}
                                    className="text-[10px] bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-400 px-1.5 py-0.5 rounded">
                                {c.tie_broken_by} tie-break
                              </span>
                            )}
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
