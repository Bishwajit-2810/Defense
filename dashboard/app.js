/**
 * Defense Analysis Dashboard — app.js
 * Plain vanilla ES5-compatible JavaScript. No frameworks, no build step.
 * window.API_BASE can be set before this script loads.
 *
 * Tabs: Overview (usage + corpus charts), Posts (upload + results),
 * Jobs (run analysis + live SSE progress), Reports (grounded LLM reports),
 * Search (keyword/semantic), Agents (analyst Q&A).
 * The active tab auto-refreshes every 15 s (toggle in the header).
 */

/* ============================================================
   Config & globals
   ============================================================ */
var API_BASE = window.API_BASE || 'http://127.0.0.1:8001';  // dev API (run.md / easy_run.md); override via window.API_BASE
var AUTO_REFRESH_MS = 15000;
var authToken = localStorage.getItem('auth_token');
var apiKey    = localStorage.getItem('api_key') || '';
var currentTab = 'overview';
var llmConfig = null;           // last loaded /v1/config/llm payload (null = unavailable)
var nlpConfig = null;           // last loaded /v1/config/nlp payload (null = unavailable)
var pollTimers = {};            // jobId -> timer id
var agentPollTimers = {};       // runId -> timer id
var sseStreams = {};            // jobId -> EventSource
var currentPostId = null;       // post being shown in modal
var analysisResultsCache = [];  // last loaded results
var agentRunsCache = [];        // last loaded agent runs
var autoRefreshEnabled = true;

/* ============================================================
   API helpers
   ============================================================ */
async function apiCall(path, options) {
  options = options || {};

  var headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});

  if (authToken) {
    headers['Authorization'] = 'Bearer ' + authToken;
  }
  if (apiKey && !authToken) {
    headers['X-API-Key'] = apiKey;
  }

  var url = API_BASE + path;
  var fetchOptions = Object.assign({}, options, { headers: headers });

  try {
    var response = await fetch(url, fetchOptions);

    if (response.status === 401) {
      authToken = null;
      localStorage.removeItem('auth_token');
      updateAuthStatus(false);
      throw new Error('Unauthorized — please log in again.');
    }

    var data;
    var contentType = response.headers.get('content-type') || '';
    if (contentType.indexOf('application/json') !== -1) {
      data = await response.json();
    } else {
      data = await response.text();
    }

    if (!response.ok) {
      var msg = 'API error ' + response.status;
      if (data && data.error) {
        msg = data.error.message || JSON.stringify(data.error);
      } else if (data && data.detail) {
        msg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      } else if (typeof data === 'string' && data) {
        msg = data;
      }
      throw new Error(msg);
    }

    return data;

  } catch (err) {
    if (err.name === 'TypeError' && err.message.indexOf('fetch') !== -1) {
      updateApiStatus('error');
      throw new Error('Cannot reach API at ' + API_BASE + '. Is the server running?');
    }
    throw err;
  }
}

/** Credential usable as a query param for EventSource (cannot set headers). */
function sseCredential() {
  return encodeURIComponent(apiKey || authToken || 'demo');
}

/* ============================================================
   Auth
   ============================================================ */
async function login(username, password, apiKeyInput) {
  // If API key provided, store it and skip JWT login
  if (apiKeyInput) {
    apiKey = apiKeyInput.trim();
    localStorage.setItem('api_key', apiKey);
    updateAuthStatus(true);
    updateApiStatus('ok');
    return;
  }

  // Otherwise try JWT
  var data = await apiCall('/v1/auth/token', {
    method: 'POST',
    body: JSON.stringify({ username: username, password: password })
  });
  authToken = data.access_token || data.token;
  localStorage.setItem('auth_token', authToken);
  updateAuthStatus(true);
  updateApiStatus('ok');
}

async function checkHealth() {
  try {
    await apiCall('/v1/health', { method: 'GET' });
    updateApiStatus('ok');
    return true;
  } catch (err) {
    updateApiStatus('error');
    return false;
  }
}

/* ============================================================
   Tab management & auto-refresh
   ============================================================ */
function showTab(tabName) {
  currentTab = tabName;

  document.querySelectorAll('.tab-panel').forEach(function(p) {
    p.classList.remove('active');
  });
  document.querySelectorAll('.nav-tab').forEach(function(t) {
    t.classList.remove('active');
  });

  var panel = document.getElementById('tab-' + tabName);
  if (panel) panel.classList.add('active');

  var tabBtn = document.querySelector('[data-tab="' + tabName + '"]');
  if (tabBtn) tabBtn.classList.add('active');

  // Live pipeline flow streams only while its tab is open.
  if (tabName === 'pipeline') {
    connectPipelineLive();
  } else {
    disconnectPipelineLive();
  }

  refreshTab(tabName);
}

/** Load (or reload) the data behind a tab.
 * background=true marks an automatic (timer-driven) refresh: loaders skip the
 * loading spinner and preserve in-place state so the view doesn't flash. */
function refreshTab(tabName, background) {
  if (tabName === 'overview') {
    loadOverview(background);
  } else if (tabName === 'posts') {
    loadAnalysisResults(background);
  } else if (tabName === 'jobs') {
    loadJobs(background);
  } else if (tabName === 'reports') {
    loadReports(background);
  } else if (tabName === 'agents') {
    loadAgentRuns(background);
  } else if (tabName === 'pipeline') {
    loadPipeline(background);
  }
  // 'search' is on-demand only.
}

/** Skip an automatic refresh while the user is actively engaged, so a timer
 * tick never yanks the page out from under them. Covers: an open detail modal,
 * an expanded inline row, a focused input/select, an open dropdown menu, and a
 * backgrounded browser tab (Page Visibility API). */
function shouldSkipAutoRefresh() {
  if (document.hidden) return true;
  var overlay = document.getElementById('modal-overlay');
  if (overlay && !overlay.classList.contains('hidden')) return true;
  if (document.querySelector('tr.expanded')) return true;
  var ae = document.activeElement;
  if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.tagName === 'SELECT')) return true;
  return false;
}

function markRefreshed() {
  var el = document.getElementById('status-last-refresh');
  if (el) {
    var now = new Date();
    el.textContent = 'Updated ' + now.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }
}

/* ============================================================
   OVERVIEW TAB — usage stats + corpus charts
   ============================================================ */
async function loadOverview() {
  var statsEl = document.getElementById('overview-stats');
  if (!statsEl) return;

  var usage = null;
  try {
    usage = await apiCall('/v1/usage', { method: 'GET' });
  } catch (err) {
    statsEl.innerHTML = '<div class="alert alert-error" style="grid-column:1/-1">Could not load usage: ' + escHtml(err.message) + '</div>';
  }

  if (usage) {
    statsEl.innerHTML =
        makeStatCard('Posts analyzed', formatNumber(usage.posts_analyzed), null)
      + makeStatCard('LLM-routed posts', formatNumber(usage.llm_calls),
                     pct(usage.llm_routing_rate) + ' routing rate')
      + makeStatCard('LLM API calls', formatNumber(usage.llm_api_calls || 0),
                     (usage.cache_hits || 0) + ' cache hits (' + pct(usage.cache_hit_rate) + ')')
      + makeStatCard('Tokens used', formatNumber(usage.total_tokens),
                     '≈ $' + Number(usage.estimated_cost_usd || 0).toFixed(4) + ' est. cost');
    updateStatusText('overview-updated', 'Live counters — refreshed ' + new Date().toLocaleTimeString());
  }

  // All corpus aggregates are computed server-side; the frontend only renders.
  var overview = null;
  try {
    overview = await apiCall('/v1/analysis/overview', { method: 'GET' });
  } catch (err) {
    // charts simply stay empty
  }

  renderOverviewCharts(overview);
  renderLlmPanel(overview);
  markRefreshed();
}

function makeStatCard(label, value, hint) {
  return '<div class="stat-card">'
    + '<div class="stat-card-value">' + escHtml(String(value)) + '</div>'
    + '<div class="stat-card-label">' + escHtml(label) + '</div>'
    + (hint ? '<div class="stat-card-hint">' + escHtml(hint) + '</div>' : '')
  + '</div>';
}

function pct(v) {
  return Math.round((Number(v) || 0) * 100) + '%';
}

// All distributions below come pre-aggregated from GET /v1/analysis/overview —
// the frontend does no counting, it only paints the server's numbers.
function renderOverviewCharts(overview) {
  overview = overview || {};
  var sent = overview.sentiment_distribution || {};

  // ---- Sentiment donut ----
  var pieCanvas = document.getElementById('overview-sentiment-pie');
  if (pieCanvas) {
    renderSentimentPie(pieCanvas, {
      positive: sent.positive || 0,
      negative: sent.negative || 0,
      neutral:  (sent.neutral || 0) + (sent.mixed || 0)
    });
  }
  var legendEl = document.getElementById('overview-sentiment-legend');
  if (legendEl) {
    legendEl.innerHTML =
        makeLegendItem('Positive', sent.positive || 0, 'var(--color-positive)')
      + makeLegendItem('Negative', sent.negative || 0, 'var(--color-negative)')
      + makeLegendItem('Neutral',  sent.neutral  || 0, 'var(--color-neutral)')
      + (sent.mixed ? makeLegendItem('Mixed', sent.mixed, 'var(--color-mixed)') : '');
  }

  // ---- Language bar chart ----
  var langCanvas = document.getElementById('overview-lang-chart');
  if (langCanvas) {
    renderBarChart(langCanvas, (overview.language_distribution || []).map(function(d) {
      return { label: d.label, value: d.count, color: '#6366f1' };
    }));
  }

  // ---- Top topics bar chart ----
  var topicsCanvas = document.getElementById('overview-topics-chart');
  if (topicsCanvas) {
    renderBarChart(topicsCanvas, (overview.top_topics || []).map(function(d) {
      return { label: d.label, value: d.count, color: '#8b5cf6' };
    }), 110);
  }

  // ---- Comment emotion distribution (corpus-wide) ----
  var emoCanvas = document.getElementById('overview-emotion-chart');
  if (emoCanvas) {
    renderBarChart(emoCanvas, (overview.comment_emotion_distribution || []).map(function(d) {
      var m = EMOTION_META[d.label] || EMOTION_META.neutral;
      return { label: m.emoji + ' ' + d.label, value: d.count, color: m.color };
    }), 90);
  }
}

function renderLlmPanel(overview) {
  var el = document.getElementById('overview-llm-panel');
  if (!el) return;

  var panel = (overview && overview.llm_panel) || {};
  var backends = panel.backends_seen || [];

  var html = '<div class="meta-grid">'
    + makeMetaField('Active backend', llmConfig ? llmBackendLabel(llmConfig.backend) : '—')
    + makeMetaField('Posts with LLM output', (panel.posts_with_llm || 0) + ' / ' + (panel.total_posts || 0))
    + makeMetaField('Posts with summaries', panel.posts_with_summaries || 0)
    + makeMetaField('Backends seen', backends.map(function(b) {
        return b.label + ' (' + b.count + ')';
      }).join(', ') || '—')
    + '</div>'
    + '<div style="margin-top:12px">'
    + '<button class="btn btn-secondary btn-sm" onclick="openLlmSettings()">LLM Settings</button>'
    + '</div>';

  el.innerHTML = html;
}

/* ============================================================
   Posts tab — upload
   ============================================================ */
async function uploadPosts(file) {
  if (!file) { showToast('Please select a JSON file.', 'warning'); return; }
  if (!file.name.endsWith('.json') && file.type !== 'application/json') {
    showToast('Only .json files are supported.', 'error');
    return;
  }

  var btn = document.getElementById('upload-btn');
  setLoading(btn, true);

  try {
    var text = await readFileAsText(file);
    var parsed;
    try {
      parsed = JSON.parse(text);
    } catch (e) {
      throw new Error('Invalid JSON file: ' + e.message);
    }

    var wantSummary = true;
    var summaryToggle = document.getElementById('upload-want-summary');
    if (summaryToggle) wantSummary = summaryToggle.checked;

    // Build the push-path payload
    var posts = Array.isArray(parsed) ? parsed : [parsed];
    var body = {
      source: 'inline',
      posts: posts,
      options: { tasks: ['all'], want_summary: wantSummary, summary_lang: 'auto', llm_backend: 'auto' }
    };

    var result = await apiCall('/v1/posts/upload', {
      method: 'POST',
      body: JSON.stringify(body),
      headers: { 'Idempotency-Key': 'upload-' + Date.now() }
    });

    showToast(
      'Accepted ' + (result.count || posts.length) + ' post(s). Job: ' + shortenId(result.job_id || 'n/a'),
      'success'
    );

    // Live progress bar in the upload card + SSE stream + polling fallback
    if (result.job_id) {
      var progEl = document.getElementById('upload-progress');
      if (progEl) {
        progEl.classList.remove('hidden');
        progEl.innerHTML = makeProgressBar('upload-job', 0, posts.length, 'queued');
      }
      startJobStream(result.job_id, function(ev) {
        if (progEl) {
          progEl.innerHTML = makeProgressBar('upload-job', ev.completed || 0, ev.total || posts.length,
            ev.event === 'done' ? 'done' : 'running', ev.failed || 0);
        }
        if (ev.event === 'done') {
          showToast('Upload analyzed: ' + (ev.completed || 0) + ' post(s).', 'success');
          loadAnalysisResults();
        }
      });
      setTimeout(function() { pollJobStatus(result.job_id); }, 2000);
    }

  } catch (err) {
    showToast('Upload failed: ' + err.message, 'error');
  } finally {
    setLoading(btn, false);
    document.getElementById('file-input').value = '';
    document.getElementById('upload-filename').textContent = '';
  }
}

/* ============================================================
   Posts tab — results
   ============================================================ */
async function loadAnalysisResults(background) {
  var container = document.getElementById('results-table-body');
  if (!container) return;

  // Only show the loading spinner on a foreground load (initial / manual /
  // tab-switch). A background timer refresh repaints in place, so the table
  // never flashes empty while you're reading it.
  if (!background || analysisResultsCache.length === 0) {
    container.innerHTML = '<tr><td colspan="10" class="table-empty"><div class="loading-overlay"><div class="spinner spinner-dark"></div> Loading results...</div></td></tr>';
  }

  var campaignEl = document.getElementById('posts-campaign-filter');
  var campaign = campaignEl ? campaignEl.value.trim() : '';
  var url = '/v1/analysis/latest?limit=100&include=results';
  if (campaign) url += '&campaign_id=' + encodeURIComponent(campaign);

  try {
    var data = await apiCall(url, { method: 'GET' });
    var results = [];

    if (data && data.results) {
      results = data.results;
    } else if (Array.isArray(data)) {
      results = data;
    }

    analysisResultsCache = results;
    renderResultsTable(results);
    updateStatusText('results-count', results.length + ' results loaded' + (campaign ? ' for ' + campaign : ''));
    markRefreshed();

  } catch (err) {
    // On a background refresh, keep the data already on screen rather than
    // replacing it with an error — a transient blip shouldn't blank the table.
    if (background && analysisResultsCache.length > 0) {
      updateStatusText('results-count', 'refresh failed — showing last data');
      return;
    }
    container.innerHTML = '<tr><td colspan="10" class="table-empty">'
      + '<div class="alert alert-error" style="display:inline-block">Could not load results: ' + escHtml(err.message) + '</div>'
      + '</td></tr>';
  }
}

function renderResultsTable(results) {
  var tbody = document.getElementById('results-table-body');
  if (!tbody) return;

  if (!results || results.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" class="table-empty">No results found. Upload posts to get started.</td></tr>';
    return;
  }

  var html = '';
  results.forEach(function(r, i) {
    var sentiment = r.overall_sentiment || 'neutral';
    var toxScore  = typeof r.toxicity_score === 'number' ? r.toxicity_score : null;
    var coverage  = (r.comment_analysis && r.comment_analysis.coverage_label) || '—';
    var dateStr   = r.created_at ? formatDate(r.created_at) : '—';
    var langStr   = r.language || '—';
    if (r.language_mix && r.language_mix.length > 1) {
      langStr += ' <span class="text-muted">+' + (r.language_mix.length - 1) + '</span>';
    }
    var summaryCell = r.post_summary
      ? '<span class="summary-snippet" title="' + escAttr(r.post_summary) + '">' + escHtml(truncate(r.post_summary, 70)) + '</span>'
        + (r.post_summary_source === 'vlm' ? ' <span class="tag tag-sm" title="image-grounded by the VLM">vlm</span>' : '')
      : '<span class="text-muted">—</span>';

    html += '<tr data-post-id="' + escAttr(r.post_id) + '" data-idx="' + i + '" onclick="toggleRowDetail(this)">'
      + '<td class="post-number">' + (i + 1) + '</td>'
      + '<td class="monospace truncate" style="max-width:140px;" title="' + escAttr(r.post_id) + '">' + escHtml(shortenId(r.post_id)) + '</td>'
      + '<td>' + escHtml(r.platform || '—') + '</td>'
      + '<td>' + langStr + '</td>'
      + '<td><span class="badge badge-' + escAttr(sentiment) + '">' + escHtml(sentiment) + '</span></td>'
      + '<td>' + (toxScore !== null ? renderToxicityBar(toxScore) : '—') + '</td>'
      + '<td style="max-width:240px">' + summaryCell + '</td>'
      + '<td><span class="coverage-text">' + escHtml(coverage) + '</span></td>'
      + '<td>' + escHtml(dateStr) + '</td>'
      + '<td><button class="btn btn-sm btn-secondary" onclick="event.stopPropagation(); openPostModal(\'' + escAttr(r.post_id) + '\')">Details</button></td>'
    + '</tr>';
  });

  tbody.innerHTML = html;
}

function renderToxicityBar(score) {
  var p = Math.round(score * 100);
  var color = score < 0.33 ? 'var(--color-positive)' : score < 0.66 ? 'var(--color-mixed)' : 'var(--color-negative)';
  return '<div class="toxicity-bar">'
    + '<div class="toxicity-track"><div class="toxicity-fill" style="width:' + p + '%;background:' + color + '"></div></div>'
    + '<span class="toxicity-label">' + p + '%</span>'
    + '</div>';
}

function toggleRowDetail(row) {
  var idx = parseInt(row.getAttribute('data-idx'), 10);

  // Remove any existing detail rows
  var existingDetail = document.getElementById('row-detail-' + idx);
  if (existingDetail) {
    existingDetail.parentNode.removeChild(existingDetail);
    row.classList.remove('expanded');
    delete commentCtx['row' + idx];
    return;
  }

  // Collapse other expanded rows
  document.querySelectorAll('tr.expanded').forEach(function(r) {
    r.classList.remove('expanded');
    var oldIdx = r.getAttribute('data-idx');
    var oldDetail = document.getElementById('row-detail-' + oldIdx);
    if (oldDetail) oldDetail.parentNode.removeChild(oldDetail);
    delete commentCtx['row' + oldIdx];
  });

  row.classList.add('expanded');

  var r = analysisResultsCache[idx];
  if (!r) return;

  var detailRow = document.createElement('tr');
  detailRow.id = 'row-detail-' + idx;
  detailRow.className = 'row-detail';
  detailRow.setAttribute('onclick', ''); // prevent bubbling

  var fields = [
    ['campaign_id',       r.campaign_id],
    ['platform_post_id',  r.platform_post_id],
    ['media_type',        r.media_type],
    ['emotion',           r.emotion && typeof r.emotion === 'object' ? r.emotion.primary : r.emotion],
    ['sentiment_score',   typeof r.sentiment_score === 'number' ? r.sentiment_score.toFixed(3) : null],
    ['toxicity_score',    typeof r.toxicity_score  === 'number' ? r.toxicity_score.toFixed(3)  : null],
    ['hate_speech_score', typeof r.hate_speech_score === 'number' ? r.hate_speech_score.toFixed(3) : null],
    ['post_type',         r.post_type],
    ['topics',            (r.topics || []).join(', ')],
    ['intents',           (r.intents || []).join(', ')],
    ['llm_used',          r.processing ? String(r.processing.llm_used) : null],
    ['llm_backend',       r.processing && r.processing.llm_backend],
    ['llm_model',         r.processing && r.processing.llm_model],
    ['stage1_ms',         r.processing && r.processing.stage1_ms != null ? r.processing.stage1_ms + 'ms' : null],
    ['stage2_ms',         r.processing && r.processing.stage2_ms != null ? r.processing.stage2_ms + 'ms' : null],
  ].filter(function(f) { return f[1] != null && f[1] !== '' && f[1] !== 'null'; });

  var fieldsHtml = fields.map(function(f) {
    return '<div class="detail-field">'
      + '<span class="detail-field-label">' + escHtml(f[0]) + '</span>'
      + '<span class="detail-field-value">' + escHtml(String(f[1])) + '</span>'
    + '</div>';
  }).join('');

  // Original post content (full width) — the source text next to the summary.
  var originalHtml = '';
  if (r.post_text) {
    originalHtml = '<div class="detail-field" style="grid-column:1/-1">'
      + '<span class="detail-field-label">Original Post</span>'
      + '<span class="detail-field-value" style="font-family:var(--font-sans);white-space:pre-wrap">' + escHtml(r.post_text) + '</span>'
    + '</div>';
  }

  // Summary preview
  var captionHtml = '';
  if (r.post_summary) {
    captionHtml = '<div class="detail-field" style="grid-column:1/-1">'
      + '<span class="detail-field-label">Post Summary'
      + (r.post_summary_source ? ' (' + escHtml(r.post_summary_source) + ')' : '') + '</span>'
      + '<span class="detail-field-value" style="font-family:var(--font-sans);white-space:normal">' + escHtml(r.post_summary) + '</span>'
    + '</div>';
  }

  var tdContent = '<div class="row-detail-content">' + fieldsHtml + originalHtml + captionHtml + '</div>';

  // Full per-post comment-sentiment section, inline on the page (no modal).
  // Same renderer the detail modal uses, scoped to this row by index.
  var prefix = 'row' + idx;
  var hasComments = r.comment_analysis
    && ((r.comment_analysis.analyzed || 0) > 0
        || ((r.engagement || {}).stored_comments || 0) > 0);
  if (hasComments) {
    tdContent += '<div class="row-comment-insights">' + commentInsightsHtml(prefix, r) + '</div>';
  }

  detailRow.innerHTML = '<td colspan="10">' + tdContent + '</td>';
  row.parentNode.insertBefore(detailRow, row.nextSibling);

  if (hasComments) mountCommentInsights(prefix, r);
}

/* ============================================================
   Post detail modal
   ============================================================ */
async function openPostModal(postId) {
  currentPostId = postId;
  var modal = document.getElementById('post-modal');
  var overlay = document.getElementById('modal-overlay');
  if (!modal || !overlay) return;

  overlay.classList.remove('hidden');
  document.getElementById('modal-post-id').textContent = shortenId(postId);
  document.getElementById('modal-body').innerHTML =
    '<div class="loading-overlay"><div class="spinner spinner-dark"></div> Loading post details...</div>';

  // Prefer the cached row (it carries the full result already)
  var cached = null;
  analysisResultsCache.forEach(function(r) {
    if (r.post_id === postId) cached = r;
  });
  if (cached) {
    renderPostModal(cached);
    return;
  }

  try {
    var data = await apiCall('/v1/analysis/' + encodeURIComponent(postId), { method: 'GET' });
    var result = (data.results && data.results[0]) || data;
    renderPostModal(result);
  } catch (err) {
    document.getElementById('modal-body').innerHTML =
      '<div class="alert alert-error">Failed to load post: ' + escHtml(err.message) + '</div>';
  }
}

// Alias used in spec — both names work
function showPostDetail(postId) { return openPostModal(postId); }

function renderPostModal(r) {
  if (!r) {
    document.getElementById('modal-body').innerHTML = '<div class="alert alert-info">No data returned.</div>';
    return;
  }

  document.getElementById('modal-post-id').textContent = shortenId(r.post_id || currentPostId);
  document.getElementById('modal-platform').textContent = (r.platform || '') + (r.language ? '  •  lang: ' + r.language : '');

  var html = '';

  // ---- Header chip strip (quick-scan key signals) ----
  var emo = (r.emotion && typeof r.emotion === 'object') ? r.emotion : null;
  var conf = r.confidence || {};
  var chips = [];
  chips.push(makeChip('sentiment',
    (r.overall_sentiment || 'neutral') + (typeof r.sentiment_score === 'number' ? ' ' + r.sentiment_score.toFixed(2) : ''),
    sentColorVar(r.overall_sentiment)));
  if (r.post_type) chips.push(makeChip('type', r.post_type, 'var(--color-primary, #6366f1)'));
  var langChip = (r.language || 'und') + (r.script ? '/' + r.script : '') + (r.is_banglish ? ' ·banglish' : '');
  chips.push(makeChip('language', langChip, '#6366f1'));
  if (emo && emo.primary) chips.push(makeChip('emotion', emo.primary, '#8b5cf6'));
  if (typeof r.toxicity_score === 'number') chips.push(makeChip('toxicity', pct(r.toxicity_score), sevColor(r.toxicity_score)));
  if (typeof r.hate_speech_score === 'number') chips.push(makeChip('hate', pct(r.hate_speech_score), sevColor(r.hate_speech_score)));
  if (typeof conf.overall === 'number') chips.push(makeChip('confidence', pct(conf.overall), '#0ea5e9'));
  var eg = r.engagement || {};
  chips.push(makeChip('♥ reactions', formatNumber(eg.total_reactions || eg.reactions || 0), '#64748b'));
  chips.push(makeChip('💬 comments', formatNumber(eg.comment_count || 0), '#64748b'));
  chips.push(makeChip('↗ shares', formatNumber(eg.share_count || 0), '#64748b'));
  html += '<div class="chip-strip">' + chips.join('') + '</div>';

  // ---- Original post content ----
  if (r.post_text) {
    html += '<div class="modal-section">'
      + '<div class="modal-section-title">Original Post</div>'
      + '<div class="original-post-box">' + escHtml(r.post_text) + '</div>'
    + '</div>';
  }

  // ---- Summary ----
  if (r.post_summary) {
    var srcBadge = '';
    if (r.post_summary_source === 'vlm') {
      srcBadge = ' <span class="tag tag-sm" title="image-grounded by the vision model">VLM</span>';
    } else if (r.post_summary_source) {
      srcBadge = ' <span class="tag tag-sm">' + escHtml(r.post_summary_source) + '</span>';
    }
    html += '<div class="modal-section">'
      + '<div class="modal-section-title">Post Summary' + srcBadge + '</div>'
      + '<div class="summary-box">' + escHtml(r.post_summary) + '</div>'
      + (r.post_summary_grounding
          ? '<div style="font-size:0.75rem;color:var(--text-muted);margin-top:6px">Grounded on: ' + escHtml(String(r.post_summary_grounding)) + '</div>'
          : '')
    + '</div>';
  }

  // ---- Sentiment breakdown ----
  var textSent  = r.text_sentiment  || {};
  var imageSent = r.image_sentiment || null;
  var overall   = r.overall_sentiment || 'neutral';

  html += '<div class="modal-section">'
    + '<div class="modal-section-title">Sentiment Breakdown</div>'
    + '<div class="sentiment-breakdown-grid">'
    + makeSentimentItem('Text', textSent.label || '—', textSent.score)
    + makeSentimentItem('Image', imageSent ? (imageSent.label || '—') : 'N/A', imageSent ? imageSent.score : null)
    + makeSentimentItem('Overall', overall, r.sentiment_score)
    + (r.baseline_sentiment != null
        ? makeSentimentItem('Baseline (upstream)',
            (Number(r.baseline_sentiment) > 0.1 ? 'positive' : Number(r.baseline_sentiment) < -0.1 ? 'negative' : 'neutral'),
            Number(r.baseline_sentiment))
        : '')
    + '</div>'
  + '</div>';

  // ---- Emotion + Confidence + Safety (three-up detail row) ----
  var emoScores = emo && emo.scores ? emo.scores : null;
  html += '<div class="modal-section"><div class="modal-section-title">Signals</div>'
    + '<div class="detail-3col">';

  // Emotion column (bar chart)
  html += '<div><p class="chart-title">Emotion'
    + (emo && emo.primary ? ' · <span style="color:var(--color-mixed)">' + escHtml(emo.primary) + '</span>' : '')
    + '</p>'
    + (emoScores ? '<canvas id="emotion-chart" height="150"></canvas>' : '<div class="text-muted">—</div>')
    + '</div>';

  // Confidence column (DOM bars)
  html += '<div><p class="chart-title">Confidence</p>'
    + makeBar('Overall',   conf.overall,   '#0ea5e9')
    + makeBar('Sentiment', conf.sentiment, '#0ea5e9')
    + makeBar('Language',  conf.language,  '#0ea5e9')
    + makeBar('Topics',    conf.topics,    '#0ea5e9')
    + '</div>';

  // Safety column (DOM bars)
  html += '<div><p class="chart-title">Safety</p>'
    + makeBar('Toxicity',    r.toxicity_score,    sevColor(r.toxicity_score || 0))
    + makeBar('Hate speech', r.hate_speech_score, sevColor(r.hate_speech_score || 0))
    + (r.intents && r.intents.length
        ? '<p class="chart-title" style="margin-top:10px">Intents</p><div class="tag-list">'
          + r.intents.map(function(t){ return '<span class="tag tag-sm">' + escHtml(t) + '</span>'; }).join('')
          + '</div>'
        : '')
    + '</div>';

  html += '</div></div>';

  // ---- Image analysis ----
  if (r.image_analysis && (r.image_analysis.ocr_text || r.image_analysis.description || (r.image_analysis.images || []).length)) {
    var ia = r.image_analysis;
    html += '<div class="modal-section">'
      + '<div class="modal-section-title">Image Analysis</div>'
      + '<div class="meta-grid">'
      + makeMetaField('OCR text', ia.ocr_text || '—')
      + makeMetaField('Description', ia.description || '—')
      + (ia.sentiment ? makeMetaField('Image sentiment', ia.sentiment.label || JSON.stringify(ia.sentiment)) : '')
      + '</div>'
    + '</div>';
  }

  // ---- Reaction breakdown (horizontal bar chart) ----
  if (r.reaction_breakdown && Object.keys(r.reaction_breakdown).length > 0) {
    html += '<div class="modal-section">'
      + '<div class="modal-section-title">Reaction Breakdown</div>'
      + '<div class="chart-container">'
      + '<canvas id="reaction-chart" height="200"></canvas>'
      + '</div>'
    + '</div>';
  }

  // ---- Comment analysis (unified renderer — same section used inline in the
  //      Posts results row). Charts + AI summary + per-comment list. ----
  if (r.comment_analysis) {
    html += '<div class="modal-section">' + commentInsightsHtml('modal', r) + '</div>';
  }

  // ---- Engagement metrics ----
  if (r.engagement) {
    var eng = r.engagement;
    html += '<div class="modal-section">'
      + '<div class="modal-section-title">Engagement</div>'
      + '<div class="meta-grid">'
      + makeMetaField('Reactions',     eng.reactions || eng.total_reactions || 0)
      + makeMetaField('Comments',      eng.comment_count || 0)
      + makeMetaField('Shares',        eng.share_count || 0)
      + makeMetaField('Stored Comments', eng.stored_comments || 0)
      + '</div>'
    + '</div>';
  }

  // ---- All metadata ----
  var skip = new Set(['post_summary','text_sentiment','image_sentiment','overall_sentiment',
    'reaction_breakdown','comment_analysis','engagement','processing','entities','keywords',
    'topics','intents','brand_mentions','language_mix','post_summary_grounding','post_summary_source',
    'image_analysis','emotion','confidence']);

  var metaFields = [];
  Object.keys(r).forEach(function(k) {
    if (skip.has(k)) return;
    var v = r[k];
    if (v === null || v === undefined) return;
    if (typeof v === 'object') {
      metaFields.push([k, JSON.stringify(v)]);
    } else {
      metaFields.push([k, String(v)]);
    }
  });

  if (metaFields.length > 0) {
    html += '<div class="modal-section">'
      + '<div class="modal-section-title">All Fields</div>'
      + '<div class="meta-grid">'
      + metaFields.map(function(f) { return makeMetaField(f[0], f[1]); }).join('')
      + '</div>'
    + '</div>';
  }

  // ---- Tags ----
  var tagsHtml = '';
  if (r.topics && r.topics.length) {
    tagsHtml += '<div class="modal-section"><div class="modal-section-title">Topics</div><div class="tag-list">'
      + r.topics.map(function(t) { return '<span class="tag">' + escHtml(t) + '</span>'; }).join('')
      + '</div></div>';
  }
  if (r.keywords && r.keywords.length) {
    tagsHtml += '<div class="modal-section"><div class="modal-section-title">Keywords</div><div class="tag-list">'
      + r.keywords.map(function(t) { return '<span class="tag">' + escHtml(t) + '</span>'; }).join('')
      + '</div></div>';
  }
  if (r.entities && r.entities.length) {
    tagsHtml += '<div class="modal-section"><div class="modal-section-title">Entities</div><div class="tag-list">'
      + r.entities.map(function(e) {
          return '<span class="tag">' + escHtml((e.type || 'entity') + ': ' + (e.value || e.text || '')) + '</span>';
        }).join('')
      + '</div></div>';
  }
  html += tagsHtml;

  // ---- Processing info ----
  if (r.processing) {
    var p = r.processing;
    html += '<div class="modal-section">'
      + '<div class="modal-section-title">Processing Info</div>'
      + '<div class="meta-grid">'
      + makeMetaField('Stage1',      p.stage1_ms != null ? p.stage1_ms + ' ms' : null)
      + makeMetaField('Stage2',      p.stage2_ms != null ? p.stage2_ms + ' ms' : null)
      + makeMetaField('LLM Used',    p.llm_used != null ? String(p.llm_used) : null)
      + makeMetaField('LLM Backend', p.llm_backend)
      + makeMetaField('LLM Model',   p.llm_model)
      + makeMetaField('Schema',      p.schema_version)
      + '</div>'
    + '</div>';
  }

  document.getElementById('modal-body').innerHTML = html;

  // Render charts after DOM update
  if (r.reaction_breakdown) {
    setTimeout(function() {
      var canvas = document.getElementById('reaction-chart');
      if (canvas) renderReactionChart(canvas, r.reaction_breakdown);
    }, 50);
  }

  // Emotion bar chart
  if (emoScores) {
    setTimeout(function() {
      var canvas = document.getElementById('emotion-chart');
      if (!canvas) return;
      var emoColors = { joy:'#22c55e', sadness:'#3b82f6', anger:'#ef4444', fear:'#a855f7',
                        surprise:'#f59e0b', disgust:'#84cc16', neutral:'#94a3b8' };
      var data = Object.keys(emoScores).map(function(k){
        return { label: k, value: Number(emoScores[k]) || 0, color: emoColors[k] || '#6366f1' };
      }).sort(function(a,b){ return b.value - a.value; });
      renderBarChart(canvas, data, 76);
    }, 50);
  }

  // Mount the comment-insights charts + lazy-load the per-comment list.
  if (r.comment_analysis && (r.post_id || currentPostId)) {
    mountCommentInsights('modal', r);
  }
}

// ---- Per-comment sentiment + emotion (full coverage) ---------------------
// One context per rendered comment-insights instance, keyed by a DOM id prefix
// ('modal' for the detail modal, 'rowN' for an expanded results row). This lets
// the same renderer drive several instances at once without id collisions.
var commentCtx = {};   // prefix -> { prefix, postId, sentiment, offset, limit }

// Shared emotion taxonomy → colour + emoji (matches the post emotion chart and
// the backend libs/schemas/output_schema.json taxonomy).
var EMOTION_META = {
  anger:    { color: '#ef4444', emoji: '😠' },
  sadness:  { color: '#3b82f6', emoji: '😢' },
  joy:      { color: '#22c55e', emoji: '😊' },
  fear:     { color: '#a855f7', emoji: '😨' },
  disgust:  { color: '#84cc16', emoji: '🤢' },
  surprise: { color: '#f59e0b', emoji: '😮' },
  neutral:  { color: '#94a3b8', emoji: '😐' }
};

function emotionTag(emo) {
  if (!emo) return '';
  var m = EMOTION_META[emo] || EMOTION_META.neutral;
  return '<span class="tag tag-sm" style="border-color:' + m.color + ';color:' + m.color + '" title="emotion">'
    + m.emoji + ' ' + escHtml(emo) + '</span>';
}

// AI-written summary of the comment mood (empty string when none yet).
function commentSummaryBox(summary) {
  if (!summary) return '';
  return '<div class="comment-summary-box">'
    + '<span class="comment-summary-tag">AI summary</span>'
    + '<span class="comment-summary-text">' + escHtml(summary) + '</span>'
  + '</div>';
}

// Emotion distribution as a horizontal bar chart (every comment), coloured by
// the shared EMOTION_META taxonomy. Replaces the old tag-list with a real chart.
function renderEmotionChart(canvas, emotionBreakdown) {
  var order = ['anger', 'sadness', 'joy', 'fear', 'disgust', 'surprise', 'neutral'];
  var eb = emotionBreakdown || {};
  var data = order.filter(function(k){ return eb[k]; }).map(function(k){
    var m = EMOTION_META[k] || EMOTION_META.neutral;
    return { label: m.emoji + ' ' + k, value: eb[k], color: m.color };
  });
  renderBarChart(canvas, data, 92);
}

/* Build the full comment-insights section (AI summary + stance donut + emotion
 * chart + themes + representative + analytics + paginated per-comment list).
 * Every id is scoped by `prefix` so the modal and any number of expanded rows
 * can coexist. Static parts render from the cached result `r`; the analytics /
 * per-comment list are filled by mountCommentInsights's fetch. */
function commentInsightsHtml(prefix, r) {
  var ca = (r && r.comment_analysis) || {};
  var coverage = ca.coverage_label || '—';
  var sb = ca.sentiment_breakdown || {};

  var html = '<div class="comment-insights">'
    + '<div class="modal-section-title">Comment Sentiment <span class="text-muted">(' + escHtml(coverage) + ')</span></div>'
    + '<div id="cs-summary-' + prefix + '">' + commentSummaryBox(ca.summary) + '</div>'
    + '<div class="charts-row" style="margin-top:12px">'
    + '<div>'
    + '<p class="chart-title">Stance toward post</p>'
    + '<div style="display:flex;gap:20px;align-items:center">'
    + '<canvas id="cs-pie-' + prefix + '" width="160" height="160"></canvas>'
    + '<div style="flex:1">'
    + makeLegendItem('Positive', sb.positive || 0, 'var(--color-positive)')
    + makeLegendItem('Negative', sb.negative || 0, 'var(--color-negative)')
    + makeLegendItem('Neutral',  sb.neutral  || 0, 'var(--color-neutral)')
    + '</div></div>'
    + '</div>'
    + '<div>'
    + '<p class="chart-title">Emotion mix <span class="text-muted">(every comment)</span></p>'
    + '<canvas id="cs-emotion-' + prefix + '" height="120"></canvas>'
    + '</div>'
    + '</div>';

  if (ca.themes && ca.themes.length > 0) {
    html += '<p class="chart-title" style="margin-top:12px">Top Themes</p>'
      + '<ul class="theme-list">'
      + ca.themes.map(function(t){ return '<li>' + escHtml(t) + '</li>'; }).join('')
      + '</ul>';
  }

  if (ca.representative_comments && ca.representative_comments.length > 0) {
    html += '<p class="chart-title" style="margin-top:12px">Representative Comments</p>';
    ca.representative_comments.forEach(function(c) {
      var cObj = (typeof c === 'string') ? { text: c } : (c || {});
      html += '<div class="rep-comment">'
        + '<div class="rep-comment-meta">'
        + (cObj.sentiment ? '<span class="badge badge-' + escAttr(cObj.sentiment) + '">' + escHtml(cObj.sentiment) + '</span>' : '')
        + (cObj.likes != null ? '<span class="text-muted">♥ ' + cObj.likes + '</span>' : '')
        + '</div>'
        + '<div class="rep-comment-text">' + escHtml(cObj.text || '') + '</div>'
      + '</div>';
    });
  }

  html += '<div id="cs-analytics-' + prefix + '"></div>'
    + '<p class="chart-title" style="margin-top:16px">Every comment <span class="text-muted">(stance toward post · full coverage)</span></p>'
    + '<div id="cs-controls-' + prefix + '" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px"></div>'
    + '<div id="cs-list-' + prefix + '"><div class="text-muted" style="padding:8px 0">Loading comments…</div></div>'
  + '</div>';

  return html;
}

// Paint the charts that need no fetch, then lazy-load the analytics + comments.
function mountCommentInsights(prefix, r) {
  var ca = (r && r.comment_analysis) || {};
  setTimeout(function() {
    var pie = document.getElementById('cs-pie-' + prefix);
    if (pie) renderSentimentPie(pie, ca.sentiment_breakdown || {});
    var emo = document.getElementById('cs-emotion-' + prefix);
    if (emo) renderEmotionChart(emo, ca.emotion_breakdown || {});
  }, 50);

  commentCtx[prefix] = {
    prefix: prefix,
    postId: (r && r.post_id) || currentPostId,
    sentiment: 'all',
    offset: 0,
    limit: 100
  };
  loadComments(prefix);
}

async function loadComments(prefix) {
  var st = commentCtx[prefix];
  if (!st) return;
  var listEl = document.getElementById('cs-list-' + prefix);
  if (listEl) listEl.innerHTML = '<div class="text-muted" style="padding:8px 0">Loading comments…</div>';
  try {
    var qs = '?limit=' + st.limit + '&offset=' + st.offset + '&sentiment=' + encodeURIComponent(st.sentiment);
    var data = await apiCall('/v1/analysis/post/' + encodeURIComponent(st.postId) + '/comments' + qs, { method: 'GET' });
    renderComments(prefix, data);
  } catch (err) {
    if (listEl) listEl.innerHTML = '<div class="alert alert-error">Could not load comments: ' + escHtml(err.message) + '</div>';
  }
}

function setCommentFilter(prefix, sentiment) {
  var st = commentCtx[prefix];
  if (!st) return;
  st.sentiment = sentiment;
  st.offset = 0;
  loadComments(prefix);
}

function pageComments(prefix, delta) {
  var st = commentCtx[prefix];
  if (!st) return;
  st.offset = Math.max(0, st.offset + delta * st.limit);
  loadComments(prefix);
}

function renderComments(prefix, data) {
  var st = commentCtx[prefix];
  if (!st) return;
  var controls = document.getElementById('cs-controls-' + prefix);
  var listEl = document.getElementById('cs-list-' + prefix);
  if (!listEl) return;

  // The detail fetch is the authoritative source for the AI summary and the
  // full-coverage emotion mix — refresh both if they arrived with it.
  var sumEl = document.getElementById('cs-summary-' + prefix);
  if (sumEl && data.summary) sumEl.innerHTML = commentSummaryBox(data.summary);
  var emoCanvas = document.getElementById('cs-emotion-' + prefix);
  if (emoCanvas && data.emotion_breakdown) renderEmotionChart(emoCanvas, data.emotion_breakdown);

  if (controls) {
    var filters = ['all', 'positive', 'negative', 'neutral'];
    controls.innerHTML = filters.map(function(f) {
      var active = (st.sentiment === f) ? ' btn-secondary' : '';
      return '<button class="btn btn-sm' + active + '" onclick="setCommentFilter(\'' + prefix + '\',\'' + f + '\')">' + f + '</button>';
    }).join('')
      + '<span class="text-muted" style="margin-left:auto">' + data.filtered_total + ' / ' + data.total + ' comments</span>';
  }

  // ---- Full-set comment analytics (constant across pages/filters) ----
  var an = document.getElementById('cs-analytics-' + prefix);
  if (an) {
    var mb = data.method_breakdown || {};
    var mbTotal = Object.keys(mb).reduce(function(s, k){ return s + (mb[k] || 0); }, 0);
    var analyticsHtml = '<div class="detail-3col" style="margin-top:8px">';

    // Score distribution histogram
    analyticsHtml += '<div><p class="chart-title">Sentiment-score distribution'
      + (typeof data.avg_sentiment_score === 'number' ? ' <span class="text-muted">(avg ' + data.avg_sentiment_score.toFixed(2) + ')</span>' : '')
      + '</p><canvas id="cs-hist-' + prefix + '" height="130"></canvas></div>';

    // Top authors
    var ta = (data.top_authors || []).filter(function(a){ return a.author && a.author !== '—'; });
    analyticsHtml += '<div><p class="chart-title">Top authors <span class="text-muted">(by likes)</span></p>';
    if (ta.length) {
      analyticsHtml += '<div class="mini-list">' + ta.slice(0, 6).map(function(a){
        return '<div class="mini-row"><span class="mini-name" title="' + escAttr(a.author) + '">' + escHtml(a.author) + '</span>'
          + '<span class="text-muted">' + a.count + '× · ♥ ' + formatNumber(a.likes) + '</span></div>';
      }).join('') + '</div>';
    } else { analyticsHtml += '<div class="text-muted">—</div>'; }
    // Engine mix (llm = context-aware stance; fast/model = standalone fallback)
    if (mbTotal > 0) {
      var llmPct = Math.round((mb.llm || 0) / mbTotal * 100);
      var mixParts = Object.keys(mb).filter(function(k){ return mb[k]; })
        .map(function(k){ return k + ' ' + mb[k]; });
      analyticsHtml += '<p class="chart-title" style="margin-top:10px">Engine mix '
        + '<span class="text-muted">(' + llmPct + '% LLM-stance)</span></p>'
        + '<div class="split-bar"><span class="split-fast" style="width:' + llmPct + '%"></span></div>'
        + '<div class="text-muted" style="font-size:.72rem;margin-top:2px">' + escHtml(mixParts.join(' · ')) + '</div>';
    }
    analyticsHtml += '</div>';

    // Most-liked comments
    var tl = (data.top_liked || []).filter(function(c){ return (c.likes || 0) > 0; });
    analyticsHtml += '<div><p class="chart-title">Most-liked comments</p>';
    if (tl.length) {
      analyticsHtml += tl.slice(0, 4).map(function(c){
        return '<div class="rep-comment" style="margin-bottom:6px">'
          + '<div class="rep-comment-meta">'
          + '<span class="badge badge-' + escAttr(c.sentiment || 'neutral') + '">' + escHtml(c.sentiment || 'neutral') + '</span>'
          + '<span class="text-muted">♥ ' + formatNumber(c.likes || 0) + '</span></div>'
          + '<div class="rep-comment-text">' + escHtml(truncate(c.text || '', 90)) + '</div></div>';
      }).join('');
    } else { analyticsHtml += '<div class="text-muted">—</div>'; }
    analyticsHtml += '</div></div>';

    an.innerHTML = analyticsHtml;
    setTimeout(function(){
      var hc = document.getElementById('cs-hist-' + prefix);
      if (hc) renderHistogram(hc, data.score_histogram || []);
    }, 30);
  }

  var comments = data.comments || [];
  if (comments.length === 0) {
    listEl.innerHTML = '<div class="text-muted" style="padding:8px 0">No comments for this filter.</div>';
    return;
  }

  var rows = comments.map(function(c) {
    var sent = c.sentiment || 'neutral';
    var scoreStr = (typeof c.sentiment_score === 'number') ? c.sentiment_score.toFixed(2) : '';
    return '<div class="rep-comment"' + (c.id ? ' title="comment ' + escAttr(String(c.id)) + '"' : '') + '>'
      + '<div class="rep-comment-meta">'
      + '<span class="badge badge-' + escAttr(sent) + '">' + escHtml(sent) + '</span>'
      + emotionTag(c.emotion)
      + (scoreStr ? '<span class="text-muted">' + escHtml(scoreStr) + '</span>' : '')
      + (c.method ? '<span class="tag tag-sm">' + escHtml(c.method) + '</span>' : '')
      + (c.parent_id ? '<span class="tag tag-sm" title="reply to ' + escAttr(String(c.parent_id)) + '">↳ reply</span>' : '')
      + (c.author ? '<span class="text-muted">' + escHtml(c.author) + '</span>' : '')
      + (c.likes != null ? '<span class="text-muted">♥ ' + c.likes + '</span>' : '')
      + '</div>'
      + '<div class="rep-comment-text">' + escHtml(c.text || '') + '</div>'
    + '</div>';
  }).join('');

  var from = data.offset + 1;
  var to = data.offset + data.returned;
  var hasPrev = data.offset > 0;
  var hasNext = (data.offset + data.returned) < data.filtered_total;
  var pager = '<div style="display:flex;gap:8px;align-items:center;margin-top:10px">'
    + '<button class="btn btn-sm"' + (hasPrev ? '' : ' disabled') + ' onclick="pageComments(\'' + prefix + '\',-1)">‹ Prev</button>'
    + '<span class="text-muted">' + from + '–' + to + '</span>'
    + '<button class="btn btn-sm"' + (hasNext ? '' : ' disabled') + ' onclick="pageComments(\'' + prefix + '\',1)">Next ›</button>'
    + '</div>';

  listEl.innerHTML = rows + pager;
}

function makeSentimentItem(label, value, score) {
  var cls = 'neutral';
  if (value === 'positive') cls = 'positive';
  else if (value === 'negative') cls = 'negative';
  else if (value === 'mixed') cls = 'mixed';

  var scoreStr = (typeof score === 'number') ? score.toFixed(3) : '';

  return '<div class="sentiment-item">'
    + '<div class="sentiment-item-label">' + escHtml(label) + '</div>'
    + '<div class="sentiment-item-value" style="color:var(--color-' + cls + ')">' + escHtml(value) + '</div>'
    + (scoreStr ? '<div class="sentiment-item-score">' + escHtml(scoreStr) + '</div>' : '')
  + '</div>';
}

function makeLegendItem(label, count, color) {
  return '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">'
    + '<span style="width:10px;height:10px;background:' + color + ';border-radius:50%;flex-shrink:0"></span>'
    + '<span style="font-size:0.8125rem;color:var(--text-secondary)">' + escHtml(label) + ': <strong>' + count + '</strong></span>'
  + '</div>';
}

function makeMetaField(key, value) {
  if (value == null || value === '') return '';
  return '<div class="meta-field">'
    + '<div class="meta-field-key">' + escHtml(key) + '</div>'
    + '<div class="meta-field-val">' + escHtml(String(value)) + '</div>'
  + '</div>';
}

/* ---- Detail UI helpers (chips, bars, colors, histogram) ---- */
function sentColorVar(label) {
  if (label === 'positive') return 'var(--color-positive)';
  if (label === 'negative') return 'var(--color-negative)';
  if (label === 'mixed')    return 'var(--color-mixed)';
  return 'var(--color-neutral)';
}

function sevColor(v) {
  v = Number(v) || 0;
  return v < 0.33 ? 'var(--color-positive)' : v < 0.66 ? 'var(--color-mixed)' : 'var(--color-negative)';
}

function makeChip(label, value, color) {
  return '<span class="chip"><span class="chip-dot" style="background:' + (color || '#64748b') + '"></span>'
    + '<span class="chip-label">' + escHtml(label) + '</span>'
    + '<span class="chip-val">' + escHtml(String(value)) + '</span></span>';
}

function makeBar(label, frac, color) {
  if (typeof frac !== 'number' || isNaN(frac)) frac = 0;
  var p = Math.max(0, Math.min(100, Math.round(frac * 100)));
  return '<div class="dbar">'
    + '<span class="dbar-label">' + escHtml(label) + '</span>'
    + '<span class="dbar-track"><span class="dbar-fill" style="width:' + p + '%;background:' + (color || '#6366f1') + '"></span></span>'
    + '<span class="dbar-val">' + p + '%</span>'
  + '</div>';
}

function renderHistogram(canvas, buckets) {
  var ctx = canvas.getContext('2d');
  var dpr = window.devicePixelRatio || 1;
  var width = canvas.parentNode.offsetWidth || 300;
  var height = 130;
  canvas.width = width * dpr; canvas.height = height * dpr;
  canvas.style.width = width + 'px'; canvas.style.height = height + 'px';
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);
  if (!buckets || !buckets.length) return;
  var maxV = Math.max.apply(null, buckets) || 1;
  var n = buckets.length;
  var padB = 16, padT = 6;
  var gap = 3;
  var bw = (width - gap * (n - 1)) / n;
  var css = getComputedStyle(document.documentElement);
  var neg = css.getPropertyValue('--color-negative').trim() || '#ef4444';
  var neu = css.getPropertyValue('--color-neutral').trim() || '#94a3b8';
  var pos = css.getPropertyValue('--color-positive').trim() || '#22c55e';
  for (var i = 0; i < n; i++) {
    var h = (buckets[i] / maxV) * (height - padB - padT);
    var x = i * (bw + gap);
    var y = height - padB - h;
    // bucket i maps to score range; left=negative, mid=neutral, right=positive
    var frac = i / (n - 1);
    ctx.fillStyle = frac < 0.4 ? neg : frac > 0.6 ? pos : neu;
    roundRect(ctx, x, y, bw, h, 2);
    ctx.fill();
    if (buckets[i] > 0) {
      ctx.fillStyle = neu;
      ctx.font = '9px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(String(buckets[i]), x + bw / 2, y - 2);
    }
  }
  // axis labels
  ctx.fillStyle = neu; ctx.font = '9px system-ui, sans-serif';
  ctx.textAlign = 'left';  ctx.fillText('−1', 0, height - 4);
  ctx.textAlign = 'center'; ctx.fillText('0', width / 2, height - 4);
  ctx.textAlign = 'right'; ctx.fillText('+1', width, height - 4);
}

function closeModal() {
  var overlay = document.getElementById('modal-overlay');
  if (overlay) overlay.classList.add('hidden');
  currentPostId = null;
}

/* ============================================================
   Canvas charts — generic horizontal bar chart
   ============================================================ */
function renderBarChart(canvas, data, labelWidth) {
  if (!data || data.length === 0) {
    var ctx0 = canvas.getContext('2d');
    ctx0.clearRect(0, 0, canvas.width, canvas.height);
    return;
  }

  var maxVal = Math.max.apply(null, data.map(function(d) { return d.value; }));

  var dpr    = window.devicePixelRatio || 1;
  var width  = canvas.parentNode.offsetWidth || 500;
  var labelW = labelWidth || 64;
  var valueW = 52;
  var barH   = 20;
  var gap    = 9;
  var padTop = 8;
  var padBot = 8;
  var height = padTop + data.length * (barH + gap) - gap + padBot;

  canvas.width  = width * dpr;
  canvas.height = height * dpr;
  canvas.style.width  = width + 'px';
  canvas.style.height = height + 'px';

  var ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  var isDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  var textColor = isDark ? '#94a3b8' : '#64748b';
  var trackColor = isDark ? '#1e293b' : '#e2e8f0';

  ctx.clearRect(0, 0, width, height);

  data.forEach(function(d, i) {
    var y = padTop + i * (barH + gap);
    var trackX = labelW;
    var trackW = width - labelW - valueW;
    var fillW  = maxVal > 0 ? Math.max(4, (d.value / maxVal) * trackW) : 0;

    ctx.fillStyle = textColor;
    ctx.font = '600 11px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    var label = d.label.length > 14 ? d.label.slice(0, 13) + '…' : d.label;
    ctx.fillText(label, labelW - 8, y + barH / 2);

    ctx.fillStyle = trackColor;
    roundRect(ctx, trackX, y, trackW, barH, 4);
    ctx.fill();

    ctx.fillStyle = d.color || '#6366f1';
    roundRect(ctx, trackX, y, fillW, barH, 4);
    ctx.fill();

    ctx.fillStyle = textColor;
    ctx.font = '12px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText(formatNumber(d.value), trackX + trackW + 8, y + barH / 2);
  });
}

/* ============================================================
   Canvas charts — reaction bar chart
   ============================================================ */
function renderReactionChart(canvas, reactionBreakdown) {
  var REACTION_ORDER = ['LIKE','LOVE','HAHA','WOW','SAD','ANGRY','CARE'];
  var REACTION_COLORS = {
    LIKE:  '#3b82f6',
    LOVE:  '#ec4899',
    HAHA:  '#f59e0b',
    WOW:   '#8b5cf6',
    SAD:   '#60a5fa',
    ANGRY: '#ef4444',
    CARE:  '#f97316'
  };

  var data = [];
  REACTION_ORDER.forEach(function(key) {
    if (reactionBreakdown[key] != null) {
      data.push({ label: key, value: reactionBreakdown[key], color: REACTION_COLORS[key] });
    }
  });

  // Also add any unknown keys
  Object.keys(reactionBreakdown).forEach(function(key) {
    if (REACTION_ORDER.indexOf(key) === -1) {
      data.push({ label: key, value: reactionBreakdown[key], color: '#94a3b8' });
    }
  });

  renderBarChart(canvas, data, 52);
}

/* ============================================================
   Canvas charts — sentiment pie
   ============================================================ */
function renderSentimentPie(canvas, sentimentBreakdown) {
  var positive = sentimentBreakdown.positive || 0;
  var negative = sentimentBreakdown.negative || 0;
  var neutral  = sentimentBreakdown.neutral  || 0;
  var total = positive + negative + neutral;

  var dpr  = window.devicePixelRatio || 1;
  var size = Math.min(canvas.width || 160, 170);

  canvas.width  = size * dpr;
  canvas.height = size * dpr;
  canvas.style.width  = size + 'px';
  canvas.style.height = size + 'px';

  var ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, size, size);

  if (total === 0) return;

  var COLORS = [
    { label: 'Positive', value: positive, color: '#22c55e' },
    { label: 'Negative', value: negative, color: '#ef4444' },
    { label: 'Neutral',  value: neutral,  color: '#94a3b8' }
  ].filter(function(d) { return d.value > 0; });

  var cx = size / 2;
  var cy = size / 2;
  var r  = size / 2 - 8;
  var innerR = r * 0.55;
  var start  = -Math.PI / 2;

  COLORS.forEach(function(d) {
    var slice = (d.value / total) * 2 * Math.PI;

    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, r, start, start + slice);
    ctx.closePath();
    ctx.fillStyle = d.color;
    ctx.fill();

    start += slice;
  });

  // Donut hole
  ctx.beginPath();
  ctx.arc(cx, cy, innerR, 0, 2 * Math.PI);
  var isDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  ctx.fillStyle = isDark ? '#1e293b' : '#ffffff';
  ctx.fill();

  // Center text
  ctx.fillStyle = isDark ? '#f1f5f9' : '#0f172a';
  ctx.font = 'bold 14px -apple-system, BlinkMacSystemFont, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(total, cx, cy);
}

/* ============================================================
   Helper: roundRect for older browsers
   ============================================================ */
function roundRect(ctx, x, y, w, h, r) {
  if (w < 2 * r) r = w / 2;
  if (h < 2 * r) r = h / 2;
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/* ============================================================
   Jobs tab — list, run, poll, SSE live progress
   ============================================================ */
var jobsCache = [];

async function loadJobs(background) {
  var container = document.getElementById('jobs-table-body');
  if (!container) return;

  if (!background || jobsCache.length === 0) {
    container.innerHTML = '<tr><td colspan="6" class="table-empty"><div class="loading-overlay"><div class="spinner spinner-dark"></div> Loading jobs...</div></td></tr>';
  }

  try {
    var data = await apiCall('/v1/analysis?limit=25', { method: 'GET' });
    var jobs = [];

    if (Array.isArray(data)) {
      jobs = data;
    } else if (data && Array.isArray(data.jobs)) {
      jobs = data.jobs;
    } else if (data && Array.isArray(data.results)) {
      jobs = data.results;
    }

    jobsCache = jobs;
    renderJobsTable(jobs);
    markRefreshed();

    // Refresh progress for jobs that are still in flight
    jobs.forEach(function(j) {
      var st = j.status || '';
      if (st === 'pending' || st === 'queued' || st === 'running' || st === 'processing') {
        pollJobStatus(j.id || j.job_id || j.analysis_id);
      }
    });

  } catch (err) {
    container.innerHTML = '<tr><td colspan="6" class="table-empty">'
      + '<div class="alert alert-error" style="display:inline-block">Could not load jobs: ' + escHtml(err.message) + '</div>'
      + '</td></tr>';
  }
}

function makeProgressBar(id, completed, total, status, failed) {
  completed = completed || 0;
  failed = failed || 0;
  var p = total ? Math.min(100, Math.round((completed + failed) / total * 100)) : 0;
  var failP = total ? Math.min(100, Math.round(failed / total * 100)) : 0;
  var label = total
    ? (completed + failed) + ' / ' + total + (failed ? ' (' + failed + ' failed)' : '')
    : (status || '');
  var cls = status === 'done' ? ' progress-done' : (failed ? ' progress-warn' : '');
  return '<div class="progress-wrap' + cls + '" id="progress-' + escAttr(id) + '">'
    + '<div class="progress-track">'
    + '<div class="progress-fill" style="width:' + p + '%"></div>'
    + (failP ? '<div class="progress-fill-failed" style="width:' + failP + '%"></div>' : '')
    + '</div>'
    + '<span class="progress-label">' + escHtml(label) + '</span>'
    + '</div>';
}

function renderJobsTable(jobs) {
  var tbody = document.getElementById('jobs-table-body');
  if (!tbody) return;

  if (!jobs || jobs.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="table-empty">No jobs found. Run an analysis to get started.</td></tr>';
    return;
  }

  var html = '';
  jobs.forEach(function(j) {
    var id      = j.id || j.job_id || j.analysis_id || '—';
    var status  = j.status || 'unknown';
    var created = j.created_at ? formatDate(j.created_at) : '—';
    var prog    = j.progress
      ? makeProgressBar(id, j.progress.completed, j.progress.total, status, j.progress.failed)
      : (j.post_count ? makeProgressBar(id, status === 'done' ? j.post_count : 0, j.post_count, status) : '<span class="text-muted">—</span>');

    html += '<tr>'
      + '<td class="monospace" title="' + escAttr(id) + '">' + escHtml(shortenId(id)) + '</td>'
      + '<td>' + escHtml(j.type || 'analysis') + '</td>'
      + '<td><span class="badge badge-status-' + escAttr(status) + '">' + escHtml(status) + '</span></td>'
      + '<td style="min-width:160px">' + prog + '</td>'
      + '<td>' + escHtml(created) + '</td>'
      + '<td class="job-actions">'
      + '<button class="btn btn-sm btn-secondary" onclick="refreshJobStatus(\'' + escAttr(id) + '\')">Refresh</button>'
      + (status === 'done' || status === 'completed'
          ? '<button class="btn btn-sm btn-primary" onclick="showTab(\'posts\')">See Results</button>'
          : '')
      + '</td>'
    + '</tr>';
  });

  tbody.innerHTML = html;
}

/** Open an SSE stream for a job and invoke onEvent for each progress message. */
function startJobStream(jobId, onEvent) {
  if (!jobId || typeof EventSource === 'undefined') return null;
  stopJobStream(jobId);

  var url = API_BASE + '/v1/analysis/' + encodeURIComponent(jobId)
    + '/stream?api_key=' + sseCredential();

  var es;
  try {
    es = new EventSource(url);
  } catch (e) {
    return null; // polling fallback still runs
  }
  sseStreams[jobId] = es;

  function handle(evt) {
    var payload = {};
    try { payload = JSON.parse(evt.data); } catch (e) { /* keepalive */ }
    payload.event = payload.event || evt.type;
    if (onEvent) onEvent(payload);
    updateJobInCache(jobId, payload);
    if (payload.event === 'done' || payload.event === 'error' || evt.type === 'timeout') {
      stopJobStream(jobId);
    }
  }

  es.addEventListener('progress', handle);
  es.addEventListener('done', handle);
  es.addEventListener('error', function(evt) {
    // Network error or server closed: stop and rely on the polling fallback.
    if (es.readyState === EventSource.CLOSED) stopJobStream(jobId);
  });
  es.addEventListener('timeout', handle);

  return es;
}

function stopJobStream(jobId) {
  if (sseStreams[jobId]) {
    try { sseStreams[jobId].close(); } catch (e) { /* noop */ }
    delete sseStreams[jobId];
  }
}

function updateJobInCache(jobId, payload) {
  var found = false;
  jobsCache.forEach(function(j) {
    if ((j.id || j.job_id || j.analysis_id) === jobId) {
      found = true;
      if (payload.completed != null) {
        j.progress = { completed: payload.completed, failed: payload.failed, total: payload.total };
      }
      if (payload.event === 'done') j.status = 'done';
      else if (payload.completed != null) j.status = 'running';
    }
  });
  if (found && currentTab === 'jobs') renderJobsTable(jobsCache);
}

async function runAnalysis(campaignId) {
  if (!campaignId || !campaignId.trim()) {
    showToast('Please enter a campaign ID.', 'warning');
    return;
  }

  var btn = document.getElementById('run-analysis-btn');
  setLoading(btn, true);

  var wantSummary = true;
  var summaryToggle = document.getElementById('run-want-summary');
  if (summaryToggle) wantSummary = summaryToggle.checked;

  try {
    var body = {
      campaign_id: campaignId.trim(),
      options: {
        want_summary: wantSummary,
        want_insight: true,
        llm_backend: 'auto'
      }
    };

    var result = await apiCall('/v1/analysis/run', {
      method: 'POST',
      body: JSON.stringify(body)
    });

    var analysisId = result.analysis_id || result.job_id;
    var sharePct = result.estimated_llm_share != null
      ? Math.round(result.estimated_llm_share * 100) + '% est. LLM share' : '';
    showToast('Analysis queued: ' + shortenId(analysisId || 'n/a') + (sharePct ? ' • ' + sharePct : ''), 'success');

    if (analysisId) {
      // Live progress bar + SSE + polling fallback
      var live = document.getElementById('live-progress');
      if (live) {
        live.classList.remove('hidden');
        live.innerHTML = '<p class="chart-title">Job ' + escHtml(shortenId(analysisId)) + '</p>'
          + makeProgressBar('live', 0, null, 'queued');
      }
      startJobStream(analysisId, function(ev) {
        if (live) {
          live.innerHTML = '<p class="chart-title">Job ' + escHtml(shortenId(analysisId))
            + (ev.post_id ? ' — last post: ' + escHtml(shortenId(ev.post_id)) : '') + '</p>'
            + makeProgressBar('live', ev.completed || 0, ev.total, ev.event === 'done' ? 'done' : 'running', ev.failed || 0);
        }
        if (ev.event === 'done') {
          showToast('Analysis ' + shortenId(analysisId) + ' finished.', 'success');
        }
      });

      jobsCache.unshift({
        id:         analysisId,
        status:     result.status || 'queued',
        type:       'analysis_run',
        created_at: new Date().toISOString()
      });
      renderJobsTable(jobsCache);
      setTimeout(function() { pollJobStatus(analysisId); }, 2000);
    }

  } catch (err) {
    showToast('Failed to run analysis: ' + err.message, 'error');
  } finally {
    setLoading(btn, false);
  }
}

async function pollJobStatus(jobId) {
  if (!jobId) return;

  // Clear any existing timer for this job
  if (pollTimers[jobId]) {
    clearTimeout(pollTimers[jobId]);
    delete pollTimers[jobId];
  }

  try {
    var data = await apiCall('/v1/analysis/' + encodeURIComponent(jobId), { method: 'GET' });
    var status = data.status || 'unknown';

    // Update in cache
    var found = false;
    jobsCache.forEach(function(j) {
      if ((j.id || j.job_id || j.analysis_id) === jobId) {
        j.status   = status;
        j.progress = data.progress;
        found = true;
      }
    });

    if (!found) {
      jobsCache.unshift({ id: jobId, status: status, progress: data.progress, created_at: data.created_at });
    }

    if (currentTab === 'jobs') {
      renderJobsTable(jobsCache);
    }

    if (status !== 'done' && status !== 'completed' && status !== 'failed' && status !== 'error') {
      pollTimers[jobId] = setTimeout(function() { pollJobStatus(jobId); }, 3000);
    } else if (status === 'done' || status === 'completed') {
      stopJobStream(jobId);
    } else if (status === 'failed' || status === 'error') {
      stopJobStream(jobId);
      showToast('Job ' + shortenId(jobId) + ' failed.', 'error');
    }

  } catch (err) {
    // Stop polling on auth errors, continue on transient errors
    if (err.message.indexOf('Unauthorized') === -1) {
      pollTimers[jobId] = setTimeout(function() { pollJobStatus(jobId); }, 5000);
    }
  }
}

async function refreshJobStatus(jobId) {
  try {
    var data = await apiCall('/v1/analysis/' + encodeURIComponent(jobId), { method: 'GET' });
    var found = false;
    jobsCache.forEach(function(j) {
      if ((j.id || j.job_id || j.analysis_id) === jobId) {
        j.status   = data.status;
        j.progress = data.progress;
        found = true;
      }
    });
    if (!found) {
      jobsCache.unshift({ id: jobId, status: data.status, progress: data.progress, created_at: data.created_at });
    }
    renderJobsTable(jobsCache);
    showToast('Status: ' + (data.status || 'unknown'), 'info');
  } catch (err) {
    showToast('Refresh failed: ' + err.message, 'error');
  }
}

/* ============================================================
   Reports tab
   ============================================================ */
async function loadReports(background) {
  var container = document.getElementById('reports-list');
  if (!container) return;

  if (!background) {
    container.innerHTML = '<div class="loading-overlay"><div class="spinner spinner-dark"></div> Loading reports...</div>';
  }

  try {
    var data = await apiCall('/v1/reports?limit=20', { method: 'GET' });
    var reports = [];

    if (Array.isArray(data)) {
      reports = data;
    } else if (data && Array.isArray(data.reports)) {
      reports = data.reports;
    }

    markRefreshed();

    if (reports.length === 0) {
      container.innerHTML = '<div class="alert alert-info">No reports yet. Generate one to get started.</div>';
      return;
    }

    var html = '';
    reports.forEach(function(rep) {
      var typeIcon = { trend: '📈', brand_mentions: '🏷️', political: '🏛️', sentiment_summary: '💬' }[rep.type] || '📄';
      var srcBadge = rep.summary_source === 'llm'
        ? '<span class="tag tag-sm" title="executive summary written by LLM-B">LLM</span>'
        : (rep.summary_source ? '<span class="tag tag-sm">aggregate</span>' : '');
      html += '<div class="report-item">'
        + '<div class="report-icon">' + typeIcon + '</div>'
        + '<div class="report-info">'
        + '<div class="report-title">' + escHtml(rep.title || rep.type || 'Report') + ' ' + srcBadge + '</div>'
        + '<div class="report-meta">'
        + 'ID: ' + escHtml(shortenId(rep.report_id || rep.id || '—'))
        + (rep.campaign_id ? '  •  ' + escHtml(rep.campaign_id) : '')
        + (rep.period ? '  •  ' + escHtml(rep.period) : '')
        + '  •  ' + (rep.created_at ? escHtml(formatDate(rep.created_at)) : '—')
        + '</div>'
        + '</div>'
        + '<div class="report-actions">'
        + '<button class="btn btn-sm btn-secondary" onclick="viewReport(\'' + escAttr(rep.report_id || rep.id) + '\')">View</button>'
        + '</div>'
      + '</div>';
    });

    container.innerHTML = html;

  } catch (err) {
    container.innerHTML = '<div class="alert alert-error">Could not load reports: ' + escHtml(err.message) + '</div>';
  }
}

async function generateReport() {
  var typeEl = document.getElementById('report-type');
  var type = typeEl ? typeEl.value : 'trend';
  var campEl = document.getElementById('report-campaign');
  var campaign = campEl ? campEl.value.trim() : '';
  var groundedEl = document.getElementById('report-grounded');
  var grounded = groundedEl ? groundedEl.checked : true;
  var btn = document.getElementById('generate-report-btn');

  setLoading(btn, true);

  try {
    var body = {
      type: type,
      options: { grounded: grounded }
    };
    if (campaign) body.campaign_id = campaign;

    var result = await apiCall('/v1/reports', {
      method: 'POST',
      body: JSON.stringify(body)
    });

    showToast(
      'Report generated' + (result.summary_source === 'llm' ? ' (LLM summary)' : '') + ': '
        + shortenId(result.report_id || 'n/a'),
      'success'
    );
    loadReports();
    if (result.report_id) viewReport(result.report_id);

  } catch (err) {
    showToast('Failed to generate report: ' + err.message, 'error');
  } finally {
    setLoading(btn, false);
  }
}

async function viewReport(reportId) {
  var container = document.getElementById('report-detail');
  if (!container) return;

  container.innerHTML = '<div class="loading-overlay"><div class="spinner spinner-dark"></div> Loading report...</div>';
  container.classList.remove('hidden');
  container.scrollIntoView({ behavior: 'smooth' });

  try {
    var rep = await apiCall('/v1/reports/' + encodeURIComponent(reportId), { method: 'GET' });

    var srcBadge = rep.summary_source === 'llm'
      ? ' <span class="tag tag-sm" title="executive summary written by LLM-B">LLM</span>'
      : (rep.summary_source ? ' <span class="tag tag-sm">aggregate</span>' : '');
    var html = '<div class="modal-section-title">' + escHtml(rep.title || rep.type || 'Report') + srcBadge + '</div>';

    if (rep.period) {
      html += '<p style="font-size:0.8125rem;color:var(--text-muted);margin-bottom:12px">Period: ' + escHtml(rep.period) + '</p>';
    }

    if (rep.summary) {
      html += '<p class="report-summary-text">' + escHtml(rep.summary) + '</p>';
    }

    if (rep.clusters && rep.clusters.length > 0) {
      html += '<div class="modal-section-title" style="margin-top:16px">Topic Clusters</div>';
      rep.clusters.forEach(function(c) {
        var sent = c.top_sentiment || 'neutral';
        html += '<div class="cluster-item">'
          + '<div class="cluster-header">'
          + '<span class="cluster-label">' + escHtml(c.label || c.cluster_id) + '</span>'
          + '<span class="badge badge-' + escAttr(sent) + '">' + escHtml(sent) + '</span>'
          + '</div>'
          + '<span class="cluster-count">' + (c.size || c.post_count || 0) + ' posts</span>'
          + (c.summary ? '<p class="cluster-summary" style="margin-top:6px">' + escHtml(c.summary) + '</p>' : '')
        + '</div>';
      });
    }

    if (rep.metrics) {
      html += '<div class="modal-section-title" style="margin-top:16px">Metrics</div>'
        + '<div class="meta-grid">'
        + makeMetaField('Total Posts', rep.metrics.total_posts)
        + (rep.metrics.sentiment_breakdown ? makeMetaField('Sentiment', JSON.stringify(rep.metrics.sentiment_breakdown)) : '')
        + (rep.metrics.languages ? makeMetaField('Languages', JSON.stringify(rep.metrics.languages)) : '')
        + '</div>';
    }

    container.innerHTML = html;

  } catch (err) {
    container.innerHTML = '<div class="alert alert-error">Could not load report: ' + escHtml(err.message) + '</div>';
  }
}

/* ============================================================
   Search tab
   ============================================================ */
async function search(query, semantic) {
  if (!query || !query.trim()) {
    showToast('Please enter a search query.', 'warning');
    return;
  }

  var container = document.getElementById('search-results');
  if (!container) return;

  container.innerHTML = '<div class="loading-overlay"><div class="spinner spinner-dark"></div> Searching...</div>';

  var campEl = document.getElementById('search-campaign');
  var campaign = campEl ? campEl.value.trim() : '';

  try {
    var params = '?q=' + encodeURIComponent(query.trim())
      + '&semantic=' + (semantic ? 'true' : 'false') + '&limit=20';
    if (campaign) params += '&campaign_id=' + encodeURIComponent(campaign);
    var data = await apiCall('/v1/search' + params, { method: 'GET' });

    var results = [];
    if (Array.isArray(data)) {
      results = data;
    } else if (data && Array.isArray(data.results)) {
      results = data.results;
    } else if (data && Array.isArray(data.posts)) {
      results = data.posts;
    }

    if (results.length === 0) {
      container.innerHTML = '<div class="alert alert-info">No results found for "' + escHtml(query) + '".</div>';
      return;
    }

    var html = '<p style="font-size:0.8125rem;color:var(--text-muted);margin-bottom:12px">'
      + results.length + ' result(s) for “' + escHtml(query) + '”'
      + (semantic ? ' • semantic search' : ' • keyword search')
      + (campaign ? ' • campaign ' + escHtml(campaign) : '')
      + '</p>';

    results.forEach(function(r) {
      // SearchResult wraps the full analysis under .result
      var res = r.result || r;
      var sentiment = res.overall_sentiment || res.sentiment || 'neutral';
      var caption   = r.snippet || res.post_summary || res.caption || res.text || '';

      html += '<div class="search-result-item" onclick="openPostModal(\'' + escAttr(r.post_id || res.post_id || r.id) + '\')">'
        + '<div class="search-result-header">'
        + '<span class="search-result-id">' + escHtml(shortenId(r.post_id || res.post_id || r.id || '')) + '</span>'
        + '<span class="badge badge-' + escAttr(sentiment) + '">' + escHtml(sentiment) + '</span>'
        + '</div>'
        + '<div class="search-result-text">' + escHtml(caption) + '</div>'
        + '<div class="search-result-meta">'
        + '<span class="search-result-meta-item">' + escHtml(res.platform || '—') + '</span>'
        + '<span class="search-result-meta-item">' + escHtml(res.language || '') + '</span>'
        + (r.score != null ? '<span class="search-result-meta-item">score: ' + Number(r.score).toFixed(3) + '</span>' : '')
        + '</div>'
      + '</div>';
    });

    container.innerHTML = html;

  } catch (err) {
    container.innerHTML = '<div class="alert alert-error">Search failed: ' + escHtml(err.message) + '</div>';
  }
}

/* ============================================================
   Agents tab
   ============================================================ */
async function askAgent() {
  var qEl = document.getElementById('agent-question');
  var question = qEl ? qEl.value.trim() : '';
  if (!question) {
    showToast('Please enter a question.', 'warning');
    return;
  }
  var agentEl = document.getElementById('agent-type');
  var campEl  = document.getElementById('agent-campaign');
  var btn     = document.getElementById('agent-ask-btn');
  var answerEl = document.getElementById('agent-answer');

  setLoading(btn, true);
  if (answerEl) {
    answerEl.classList.remove('hidden');
    answerEl.innerHTML = '<div class="loading-overlay"><div class="spinner spinner-dark"></div> The '
      + escHtml(agentEl ? agentEl.value : 'analyst') + ' agent is thinking (uses LLM tool-calling — may take up to ~30 s)...</div>';
  }

  try {
    var body = {
      query: question,
      agent_type: agentEl ? agentEl.value : 'analyst'
    };
    var campaign = campEl ? campEl.value.trim() : '';
    if (campaign) body.campaign_id = campaign;

    var result = await apiCall('/v1/agents/query', {
      method: 'POST',
      body: JSON.stringify(body)
    });

    if (result.status === 'running' && result.run_id) {
      // 202 — keep polling the run
      if (answerEl) {
        answerEl.innerHTML = '<div class="alert alert-info">Run ' + escHtml(shortenId(result.run_id))
          + ' is still working — polling for the answer...</div>';
      }
      pollAgentRun(result.run_id, answerEl);
    } else {
      renderAgentAnswer(answerEl, result);
      loadAgentRuns();
    }

  } catch (err) {
    if (answerEl) {
      answerEl.innerHTML = '<div class="alert alert-error">Agent query failed: ' + escHtml(err.message)
        + '<br><span style="font-size:0.75rem">Is the agent layer running? Start it with <code>uv run run_all.py --with-agents</code>.</span></div>';
    }
  } finally {
    setLoading(btn, false);
  }
}

function pollAgentRun(runId, answerEl) {
  if (agentPollTimers[runId]) {
    clearTimeout(agentPollTimers[runId]);
    delete agentPollTimers[runId];
  }

  apiCall('/v1/agents/' + encodeURIComponent(runId), { method: 'GET' })
    .then(function(run) {
      if (run.status === 'running') {
        agentPollTimers[runId] = setTimeout(function() { pollAgentRun(runId, answerEl); }, 3000);
      } else {
        renderAgentAnswer(answerEl, run);
        loadAgentRuns();
      }
    })
    .catch(function(err) {
      if (answerEl) {
        answerEl.innerHTML = '<div class="alert alert-error">Could not poll run: ' + escHtml(err.message) + '</div>';
      }
    });
}

function renderAgentAnswer(el, run) {
  if (!el) return;
  var html = '<div class="agent-answer-card">'
    + '<div class="agent-answer-header">'
    + '<span class="badge badge-status-' + escAttr(run.status || 'done') + '">' + escHtml(run.status || 'done') + '</span>'
    + (run.llm_model ? '<span class="text-muted" style="font-size:0.75rem">' + escHtml(run.llm_model)
        + (run.llm_backend ? ' @ ' + escHtml(run.llm_backend) : '') + '</span>' : '')
    + '</div>'
    + '<div class="agent-answer-text">' + escHtml(run.answer || '(no answer)') + '</div>';

  if (run.citations && run.citations.length > 0) {
    html += '<div class="agent-citations"><strong>Citations:</strong> '
      + run.citations.map(function(c) {
          return '<a href="#" onclick="openPostModal(\'' + escAttr(c) + '\');return false" class="tag tag-sm">' + escHtml(shortenId(c)) + '</a>';
        }).join(' ')
      + '</div>';
  }

  if (run.tools_used && run.tools_used.length > 0) {
    html += '<div class="agent-tools"><strong>Tools used:</strong> '
      + run.tools_used.map(function(t) {
          var name = typeof t === 'string' ? t : (t.tool || t.name || JSON.stringify(t));
          return '<span class="tag tag-sm">' + escHtml(name) + '</span>';
        }).join(' ')
      + '</div>';
  }

  html += '</div>';
  el.classList.remove('hidden');
  el.innerHTML = html;
}

async function loadAgentRuns() {
  var container = document.getElementById('agent-runs-list');
  if (!container) return;

  try {
    var data = await apiCall('/v1/agents?limit=15', { method: 'GET' });
    var runs = Array.isArray(data) ? data : (data.runs || []);
    agentRunsCache = runs;
    markRefreshed();

    if (runs.length === 0) {
      container.innerHTML = '<div class="alert alert-info">No agent runs yet. Ask a question above.</div>';
      return;
    }

    var html = '';
    runs.forEach(function(run, i) {
      html += '<div class="report-item" onclick="showAgentRun(' + i + ')" style="cursor:pointer">'
        + '<div class="report-icon">🤖</div>'
        + '<div class="report-info">'
        + '<div class="report-title">' + escHtml(truncate(run.answer || '(running…)', 90)) + '</div>'
        + '<div class="report-meta">'
        + escHtml(shortenId(run.run_id || ''))
        + '  •  <span class="badge badge-status-' + escAttr(run.status || '') + '">' + escHtml(run.status || '') + '</span>'
        + (run.llm_model ? '  •  ' + escHtml(run.llm_model) : '')
        + '</div>'
        + '</div>'
      + '</div>';
    });

    container.innerHTML = html;

  } catch (err) {
    container.innerHTML = '<div class="alert alert-error">Could not load agent runs: ' + escHtml(err.message)
      + '<br><span style="font-size:0.75rem">The agent layer is optional — start it with <code>uv run run_all.py --with-agents</code>.</span></div>';
  }
}

function showAgentRun(idx) {
  var run = agentRunsCache[idx];
  if (!run) return;
  var answerEl = document.getElementById('agent-answer');
  renderAgentAnswer(answerEl, run);
  if (answerEl) answerEl.scrollIntoView({ behavior: 'smooth' });
}

/* ============================================================
   LLM backend config (Settings panel + header chip)
   ============================================================ */
function llmBackendLabel(backend) {
  if (backend === 'local') return 'Local (Ollama)';
  if (backend === 'groq')  return 'Groq Cloud';
  return backend || 'unknown';
}

async function loadLlmConfig() {
  try {
    llmConfig = await apiCall('/v1/config/llm', { method: 'GET' });
  } catch (err) {
    // Endpoint absent (404) or unreachable — hide the chip gracefully, no console spam.
    llmConfig = null;
  }
  updateLlmChip();
}

function updateLlmChip() {
  var chip = document.getElementById('llm-chip');
  if (!chip) return;
  if (!llmConfig || !llmConfig.backend) {
    chip.classList.add('hidden');
    return;
  }
  chip.textContent = 'LLM: ' + llmBackendLabel(llmConfig.backend);
  chip.classList.remove('hidden');
}

function openLlmSettings() {
  var overlay = document.getElementById('llm-settings-overlay');
  if (!overlay) return;
  overlay.classList.remove('hidden');
  renderLlmSettings();
  // Refresh from the server in case it changed elsewhere
  loadLlmConfig().then(renderLlmSettings);
}

function closeLlmSettings() {
  var overlay = document.getElementById('llm-settings-overlay');
  if (overlay) overlay.classList.add('hidden');
}

function renderLlmModelList(title, models) {
  var html = '<div>'
    + '<p class="chart-title">' + escHtml(title) + '</p>';

  if (!models || Object.keys(models).length === 0) {
    html += '<p style="font-size:0.8125rem;color:var(--text-muted)">No models configured.</p></div>';
    return html;
  }

  html += '<div class="meta-grid" style="grid-template-columns:1fr">';
  Object.keys(models).forEach(function(role) {
    html += makeMetaField(role, models[role]);
  });
  html += '</div></div>';
  return html;
}

function renderLlmSettings() {
  var body = document.getElementById('llm-settings-body');
  if (!body) return;

  if (!llmConfig) {
    body.innerHTML = '<div class="alert alert-info">LLM configuration is not available on this server.</div>';
    return;
  }

  var c = llmConfig;
  var html = '';

  // ---- Status ----
  html += '<div class="modal-section">'
    + '<div class="modal-section-title">Status</div>'
    + '<div class="meta-grid">'
    + makeMetaField('Active Backend',  llmBackendLabel(c.backend))
    + makeMetaField('Default Backend', llmBackendLabel(c.default_backend))
    + makeMetaField('Override',        c.override ? llmBackendLabel(c.override) : 'none (using default)')
    + '</div>'
  + '</div>';

  // ---- Toggle ----
  html += '<div class="modal-section">'
    + '<div class="modal-section-title">Switch Backend</div>'
    + '<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">'
    + '<div class="llm-segmented">'
    + '<button type="button" class="llm-segment' + (c.backend === 'local' ? ' active' : '') + '" onclick="setLlmBackend(\'local\')">Local (Ollama)</button>'
    + '<button type="button" class="llm-segment' + (c.backend === 'groq' ? ' active' : '') + '" onclick="setLlmBackend(\'groq\')">Groq Cloud</button>'
    + '</div>'
    + '<button type="button" class="btn btn-secondary btn-sm" onclick="setLlmBackend(null)"'
    + (c.override == null ? ' disabled' : '')
    + '>Use default (' + escHtml(llmBackendLabel(c.default_backend)) + ')</button>'
    + '</div>'
  + '</div>';

  // ---- Models (read-only) ----
  html += '<div class="modal-section">'
    + '<div class="modal-section-title">Models per Role</div>'
    + '<div class="charts-row">'
    + renderLlmModelList('Local (Ollama)', c.models && c.models.local)
    + renderLlmModelList('Groq Cloud',     c.models && c.models.groq)
    + '</div>'
  + '</div>';

  // ---- Note ----
  html += '<div class="alert alert-warning" style="margin-bottom:0">'
    + '<strong>Note:</strong>&nbsp;Applies to new analysis runs; Groq requires GROQ_API_KEY on the server.'
  + '</div>';

  body.innerHTML = html;
}

async function setLlmBackend(backend) {
  try {
    var data = await apiCall('/v1/config/llm', {
      method: 'PUT',
      body: JSON.stringify({ backend: backend })
    });
    llmConfig = data;
    updateLlmChip();
    renderLlmSettings();
    if (backend === null) {
      showToast('Override cleared — using default backend: ' + llmBackendLabel(data.backend) + '.', 'success');
    } else {
      showToast('LLM backend set to ' + llmBackendLabel(data.backend) + '.', 'success');
    }
  } catch (err) {
    showToast('Failed to update LLM backend: ' + err.message, 'error');
  }
}

/* ============================================================
   Stage-1 sentiment model config (Settings panel + header chip)
   ============================================================ */
function nlpModelLabel(key) {
  if (!key) return 'Auto';
  if (nlpConfig && nlpConfig.options) {
    for (var i = 0; i < nlpConfig.options.length; i++) {
      if (nlpConfig.options[i].key === key) return nlpConfig.options[i].label;
    }
  }
  return key;
}

async function loadNlpConfig() {
  try {
    nlpConfig = await apiCall('/v1/config/nlp', { method: 'GET' });
  } catch (err) {
    nlpConfig = null;  // endpoint absent/unreachable — hide the chip
  }
  updateNlpChip();
}

function updateNlpChip() {
  var chip = document.getElementById('nlp-chip');
  if (!chip) return;
  if (!nlpConfig) {
    chip.classList.add('hidden');
    return;
  }
  chip.textContent = 'NLP: ' + (nlpConfig.mode === 'forced'
    ? nlpModelLabel(nlpConfig.override)
    : 'Auto-route');
  chip.classList.remove('hidden');
}

function openNlpSettings() {
  var overlay = document.getElementById('nlp-settings-overlay');
  if (!overlay) return;
  overlay.classList.remove('hidden');
  renderNlpSettings();
  loadNlpConfig().then(renderNlpSettings);
}

function closeNlpSettings() {
  var overlay = document.getElementById('nlp-settings-overlay');
  if (overlay) overlay.classList.add('hidden');
}

function renderNlpSettings() {
  var body = document.getElementById('nlp-settings-body');
  if (!body) return;

  if (!nlpConfig) {
    body.innerHTML = '<div class="alert alert-info">Sentiment-model configuration is not available on this server.</div>';
    return;
  }

  var c = nlpConfig;
  var html = '';

  // ---- Status ----
  html += '<div class="modal-section">'
    + '<div class="modal-section-title">Status</div>'
    + '<div class="meta-grid">'
    + makeMetaField('Mode',     c.mode === 'forced' ? 'Forced' : 'Auto-route by language')
    + makeMetaField('Active',   c.mode === 'forced' ? nlpModelLabel(c.override) : 'per detected language')
    + makeMetaField('Fallback', nlpModelLabel(c.default_key))
    + '</div>'
  + '</div>';

  // ---- Switch ----
  var btns = '<button type="button" class="llm-segment' + (c.mode !== 'forced' ? ' active' : '')
    + '" onclick="setNlpModel(null)">Auto-route</button>';
  (c.options || []).forEach(function(o) {
    var active = (c.mode === 'forced' && c.override === o.key) ? ' active' : '';
    var dis = o.available ? '' : ' disabled';
    var title = o.available ? '' : ' title="No checkpoint configured — set ' + escHtml(o.key.toUpperCase()) + '_SENTIMENT_MODEL"';
    btns += '<button type="button" class="llm-segment' + active + '"' + dis + title
      + ' onclick="setNlpModel(\'' + o.key + '\')">' + escHtml(o.label)
      + (o.available ? '' : ' (n/a)') + '</button>';
  });
  html += '<div class="modal-section">'
    + '<div class="modal-section-title">Switch Model</div>'
    + '<div class="llm-segmented" style="flex-wrap:wrap">' + btns + '</div>'
  + '</div>';

  // ---- Route table ----
  var rt = c.route_table || {};
  html += '<div class="modal-section">'
    + '<div class="modal-section-title">Auto-route Table</div>'
    + '<div class="meta-grid">'
    + makeMetaField('Bangla (script)',  nlpModelLabel(rt.bn))
    + makeMetaField('Banglish / mixed', nlpModelLabel(rt.banglish))
    + makeMetaField('English / other',  nlpModelLabel(rt.en))
    + '</div>'
  + '</div>';

  // ---- Note ----
  html += '<div class="alert alert-warning" style="margin-bottom:0">'
    + '<strong>Note:</strong>&nbsp;Applies to new Stage-1 work. Greyed-out models need a '
    + 'sentiment-fine-tuned checkpoint (their <code>*_SENTIMENT_MODEL</code> env var) before they can be forced.'
  + '</div>';

  body.innerHTML = html;
}

async function setNlpModel(model) {
  try {
    var data = await apiCall('/v1/config/nlp', {
      method: 'PUT',
      body: JSON.stringify({ model: model })
    });
    nlpConfig = data;
    updateNlpChip();
    renderNlpSettings();
    if (model === null) {
      showToast('Sentiment model set to auto-route by language.', 'success');
    } else {
      showToast('Sentiment model forced to ' + nlpModelLabel(data.override) + '.', 'success');
    }
  } catch (err) {
    showToast('Failed to update sentiment model: ' + err.message, 'error');
  }
}

/* ============================================================
   Status bar & auth UI helpers
   ============================================================ */
function updateApiStatus(state) {
  var dot    = document.getElementById('api-status-dot');
  var label  = document.getElementById('api-status-label');
  if (!dot || !label) return;

  if (state === 'ok') {
    dot.className   = 'status-dot green';
    label.textContent = 'API connected';
  } else if (state === 'error') {
    dot.className   = 'status-dot red';
    label.textContent = 'API unreachable';
  } else {
    dot.className   = 'status-dot yellow';
    label.textContent = 'Connecting...';
  }
}

function updateAuthStatus(connected) {
  var el = document.getElementById('header-auth-status');
  if (!el) return;
  el.textContent = connected ? 'Authenticated' : 'Not authenticated';
  el.className = 'auth-status ' + (connected ? 'connected' : 'disconnected');
}

function updateStatusText(id, text) {
  var el = document.getElementById(id);
  if (el) el.textContent = text;
}

/* ============================================================
   Login modal
   ============================================================ */
function showLoginModal() {
  var overlay = document.getElementById('login-overlay');
  if (overlay) overlay.classList.remove('hidden');
  // Autofocus the API-key field (autofocus attr only fires on page load)
  var apiKeyEl = document.getElementById('login-apikey');
  if (apiKeyEl) {
    setTimeout(function() { apiKeyEl.focus(); }, 50);
  }
}

function hideLoginModal() {
  var overlay = document.getElementById('login-overlay');
  if (overlay) overlay.classList.add('hidden');
}

async function handleLoginSubmit(evt) {
  evt.preventDefault();
  var username = document.getElementById('login-username').value;
  var password = document.getElementById('login-password').value;
  var apiKeyEl = document.getElementById('login-apikey');
  var apiKeyVal = apiKeyEl ? apiKeyEl.value : '';

  // Dev convenience: all fields empty -> use "demo" as the API key
  if (!apiKeyVal.trim() && !username.trim() && !password.trim()) {
    apiKeyVal = 'demo';
  }

  var btn = document.getElementById('login-submit-btn');
  setLoading(btn, true);

  var errEl = document.getElementById('login-error');
  if (errEl) errEl.textContent = '';

  try {
    await login(username, password, apiKeyVal);
    hideLoginModal();
    refreshTab(currentTab);
    loadLlmConfig();
    loadNlpConfig();
  } catch (err) {
    if (errEl) errEl.textContent = err.message;
  } finally {
    setLoading(btn, false);
  }
}

/* ============================================================
   Toast notifications
   ============================================================ */
function showToast(message, type) {
  type = type || 'info';
  var container = document.getElementById('toast-container');
  if (!container) return;

  var toast = document.createElement('div');
  toast.className = 'toast toast-' + type;
  toast.textContent = message;
  container.appendChild(toast);

  setTimeout(function() {
    toast.style.opacity = '0';
    toast.style.transition = 'opacity 0.3s';
    setTimeout(function() {
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }, 300);
  }, 4000);
}

/* ============================================================
   Button loading state
   ============================================================ */
function setLoading(btn, isLoading) {
  if (!btn) return;
  if (isLoading) {
    btn.disabled = true;
    btn._origText = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span> ' + (btn._origText || '').replace(/<[^>]+>/g, '').trim();
  } else {
    btn.disabled = false;
    if (btn._origText != null) {
      btn.innerHTML = btn._origText;
      btn._origText = null;
    }
  }
}

/* ============================================================
   PIPELINE TAB (live flow) — real-time per-stage state via SSE
   ------------------------------------------------------------
   Streams GET /v1/pipeline/stream (a `stats` frame ~every 1.5s) and renders
   each stage with its backlog (waiting), in-flight (processing) and DLQ
   (failed) counts. Falls back to polling /v1/pipeline/stats if SSE is
   unavailable. Active only while the Pipeline tab is open.
   ============================================================ */

var pipelineES = null;          // EventSource for the live stream
var pipelinePollTimer = null;   // setInterval fallback id

function setPipelineLiveStatus(state) {
  var el = document.getElementById('pipeline-live-status');
  if (!el) return;
  var map = {
    live:    ['Live', 'tag-success'],
    polling: ['Polling', 'tag-warning'],
    offline: ['Offline', 'tag-danger'],
    connecting: ['connecting…', '']
  };
  var m = map[state] || map.connecting;
  el.textContent = m[0];
  el.className = 'tag tag-sm ' + m[1];
}

function connectPipelineLive() {
  if (pipelineES || pipelinePollTimer) return;  // already streaming
  setPipelineLiveStatus('connecting');

  if (typeof EventSource !== 'undefined') {
    var url = API_BASE + '/v1/pipeline/stream?api_key=' + sseCredential();
    try {
      pipelineES = new EventSource(url);
    } catch (e) {
      pipelineES = null;
    }
    if (pipelineES) {
      pipelineES.addEventListener('connected', function () { setPipelineLiveStatus('live'); });
      pipelineES.addEventListener('stats', function (evt) {
        try { renderPipelineLive(JSON.parse(evt.data)); setPipelineLiveStatus('live'); }
        catch (e) { /* ignore a malformed frame */ }
      });
      pipelineES.addEventListener('timeout', function () {
        // Server closes the stream after its max duration — reconnect.
        stopPipelineES();
        if (currentTab === 'pipeline') connectPipelineLive();
      });
      pipelineES.addEventListener('error', function () {
        if (pipelineES && pipelineES.readyState === EventSource.CLOSED) {
          stopPipelineES();
          startPipelinePoll();  // degrade to polling
        }
      });
      return;
    }
  }
  startPipelinePoll();
}

function startPipelinePoll() {
  if (pipelinePollTimer) return;
  var tick = function () {
    apiCall('/v1/pipeline/stats', { method: 'GET' })
      .then(function (s) { renderPipelineLive(s); setPipelineLiveStatus('polling'); })
      .catch(function () { setPipelineLiveStatus('offline'); });
  };
  tick();
  pipelinePollTimer = setInterval(tick, 2000);
}

function stopPipelineES() {
  if (pipelineES) {
    try { pipelineES.close(); } catch (e) { /* noop */ }
    pipelineES = null;
  }
}

function disconnectPipelineLive() {
  stopPipelineES();
  if (pipelinePollTimer) { clearInterval(pipelinePollTimer); pipelinePollTimer = null; }
}

/** Render the live stage-flow row from a /v1/pipeline stats payload. */
function renderPipelineLive(stats) {
  var host = document.getElementById('pipeline-flow');
  if (!host || !stats || !stats.stages) return;

  var html = '';
  stats.stages.forEach(function (s, i) {
    var active = (s.in_flight || 0) > 0;
    var waiting = (s.backlog || 0) > 0;
    html += '<div class="flow-node' + (active ? ' active' : '') + (waiting ? ' waiting' : '') + '">'
      + '<div class="flow-node-label">' + escHtml(s.label) + '</div>'
      + '<div class="flow-node-metrics">'
      + '<span class="flow-metric proc" title="in-flight (processing)">&#9654; ' + (s.in_flight || 0) + '</span>'
      + '<span class="flow-metric wait" title="backlog (waiting)">&#9612; ' + (s.backlog || 0) + '</span>'
      + ((s.dlq || 0) > 0
          ? '<span class="flow-metric dlq" title="dead-lettered (failed)">&#10007; ' + s.dlq + '</span>'
          : '')
      + '</div></div>';

    // Arrow between stages — "flowing" when work is moving across it.
    var next = stats.stages[i + 1];
    var flowing = active || (next && (next.in_flight || 0) > 0) || waiting;
    html += '<div class="flow-arrow' + (flowing ? ' flowing' : '') + '">&#8594;</div>';
  });

  // Terminal "Completed" node (rows persisted to analysis_results).
  html += '<div class="flow-node done"><div class="flow-node-label">Completed</div>'
    + '<div class="flow-node-metrics"><span class="flow-metric ok" title="analysis_results rows">&#10003; '
    + (stats.completed || 0) + '</span></div></div>';

  host.innerHTML = html;

  var sum = document.getElementById('pipeline-flow-summary');
  if (sum) {
    sum.innerHTML =
      '<span class="flow-sum proc">' + (stats.in_flight_total || 0) + ' processing</span>'
      + '<span class="flow-sum wait">' + (stats.backlog_total || 0) + ' waiting</span>'
      + '<span class="flow-sum dlq">' + (stats.dlq_total || 0) + ' failed</span>'
      + '<span class="flow-sum ok">' + (stats.completed || 0) + ' completed</span>'
      + '<span class="flow-sum muted">' + (stats.total_processed || 0) + ' processed total · '
      + (stats.llm_routed || 0) + ' routed to LLM</span>';
  }
}

/* ============================================================
   PIPELINE TAB — visualize the payload each stage hands the next
   ------------------------------------------------------------
   The ingestion → analysis flow is a chain of Redis Stream queues:

     Ingestion ─▶ Stage 1 (NLP) ─▶ Router ─▶ Stage 2 (LLM) ─▶ Assembler

   Each arrow is a queue carrying a JSON envelope. This tab renders a
   stepper of the five stages and a slider that scrubs across the four
   transitions, showing the exact payload (queue + JSON) on that edge.

   "What I send to Stage 2 from Stage 1" is transition index 2
   (Router ─▶ Stage 2), so the slider defaults there.

   Data source: if a recently-analyzed post is available we reconstruct
   real-looking payloads from it; otherwise we fall back to sample data.
   ============================================================ */

// The five pipeline stages, in order.
var PIPELINE_STAGES = [
  { key: 'ingest',  label: 'Ingestion',     sub: 'normalize raw posts' },
  { key: 'stage1',  label: 'Stage 1 · NLP', sub: 'language, sentiment, vision, comments' },
  { key: 'router',  label: 'Router',        sub: 'decide if LLM is needed' },
  { key: 'stage2',  label: 'Stage 2 · LLM', sub: 'summary, post-type, insight' },
  { key: 'assembler', label: 'Assembler',   sub: 'merge + persist' }
];

// The four transitions (edges) between stages, with the queue each rides.
var PIPELINE_EDGES = [
  { from: 0, to: 1, queue: 'nlp:stage1:queue',  title: 'Ingestion ▶ Stage 1' },
  { from: 1, to: 2, queue: 'router:queue',      title: 'Stage 1 ▶ Router' },
  { from: 2, to: 3, queue: 'llm:stage2:queue',  title: 'Router ▶ Stage 2' },
  { from: 3, to: 4, queue: 'assembler:queue',   title: 'Stage 2 ▶ Assembler' }
];

var pipelinePayloads = null;   // [edgeIndex] -> payload object
var pipelineLive = false;      // true when built from a real post
var pipelineEdgeIndex = 2;     // default: Stage 1 ▶ Stage 2

/** Build the four edge payloads. If `post` is given, fold its real values
 * into the envelopes; otherwise everything is sample data. */
function buildPipelinePayloads(post) {
  var p = post || {};
  var postId = p.post_id || p.id || 'cmsamplepost000000000000';
  var campaignId = p.campaign_id || 'cmsamplecampaign00000000';
  var platform = p.platform || 'facebook';
  var lang = p.language || 'bn';
  var text = p.post_text || p.text || 'জ্বালানি তেলের দাম আবার বাড়ানো হয়েছে — মানুষ ক্ষুব্ধ।';
  var sentiment = p.overall_sentiment || 'negative';
  var sentScore = (typeof p.sentiment_score === 'number') ? p.sentiment_score : -0.62;
  var toxicity = (typeof p.toxicity_score === 'number') ? p.toxicity_score : 0.18;
  var topics = p.topics || ['fuel prices', 'economy', 'public anger'];
  var summary = p.post_summary || null;
  var postType = p.post_type || null;

  // Stage-1 result block (produced by the NLP worker). Real fields when
  // present on the post, representative samples otherwise.
  var stage1Result = {
    post_id: postId,
    campaign_id: campaignId,
    platform: platform,
    media_type: p.media_type || 'PHOTO_TEXT',
    language: lang,
    language_confidence: 0.97,
    is_banglish: !!p.is_banglish,
    overall_sentiment: sentiment,
    sentiment_score: sentScore,
    text_sentiment: { label: sentiment, score: Math.abs(sentScore) },
    image_sentiment: p.image_sentiment || { label: 'negative', score: 0.55 },
    emotion: p.emotion || { primary: 'anger', scores: { anger: 0.61, sadness: 0.22, fear: 0.1 } },
    topics: topics,
    intents: p.intents || ['complain', 'inform'],
    toxicity_score: toxicity,
    hate_speech_score: (typeof p.hate_speech_score === 'number') ? p.hate_speech_score : 0.04,
    entities: p.entities || [{ text: 'BPC', type: 'ORG' }],
    keywords: p.keywords || ['জ্বালানি', 'তেল', 'দাম'],
    embedding: ['…768-dim vector…'],
    image_analysis: { image_count: 1, ocr_text: 'নতুন মূল্য তালিকা', vision_model: 'SigLIP' },
    comment_analysis: {
      analyzed: 42,
      coverage: 0.84,
      sentiment_breakdown: { positive: 5, negative: 31, neutral: 6 },
      themes: ['price hike', 'government'],
      representative_comments: ['এটা অন্যায়', 'দাম কমান']
    },
    engagement: { reactions: 1240, comment_count: 50, share_count: 88, stored_comments: 42 },
    // Stage-2 fields are always null coming out of Stage 1:
    post_type: null,
    post_summary: null,
    post_summary_lang: null,
    confidence: 0.71,
    processing: { unit: 'post+thread', stage1_ms: painlessNum(p.stage1_ms, 312), llm_used: false, vision_used: true, vision_model: 'SigLIP' }
  };

  // Normalized upstream post (carried through for grounding in Stage 2).
  var normalizedPost = {
    post_id: postId,
    campaign_id: campaignId,
    platform: platform,
    platform_post_id: p.platform_post_id || '100xxxxxxxxxxxx_900xxxxxxxxx',
    text: text,
    media_type: stage1Result.media_type,
    url: p.url || 'https://facebook.com/…',
    images: [{ ref: 's3://media/sample.jpg' }],
    comments: [{ id: 'c1', text: 'এটা অন্যায়' }, { id: 'c2', text: 'দাম কমান' }],
    scraped_at: p.scraped_at || '2026-06-13T08:00:00Z'
  };

  // Router's decision flags for Stage 2.
  var taskFlags = {
    want_summary: true,
    want_post_type: true,
    want_insight: true,
    target_lang: null
  };

  // Stage-2 result (only exists after the LLM worker runs).
  var stage2Result = {
    post_summary: summary || 'A photo-and-text post protesting a new fuel-price hike; commenters are overwhelmingly angry and demand a rollback.',
    post_summary_lang: 'en',
    post_summary_source: 'vlm',
    post_summary_grounding: 'caption+ocr+image',
    post_type: postType || 'grievance',
    post_type_confidence: 0.88,
    topics: topics,
    intents: ['complain', 'mobilize'],
    insight: 'Fuel-price grievance with high negative engagement — candidate for alerting.',
    processing: { stage2_ms: painlessNum(p.stage2_ms, 1840), llm_backend: 'groq', llm_model: 'llama-3.3-70b' }
  };

  return [
    // Edge 0: Ingestion ▶ Stage 1
    { post_id: postId, raw_post: normalizedPost },
    // Edge 1: Stage 1 ▶ Router
    { post_id: postId, stage1_result: stage1Result },
    // Edge 2: Router ▶ Stage 2  ← "what Stage 1 sends to Stage 2"
    { post_id: postId, stage1_result: stage1Result, normalized_post: normalizedPost, task_flags: taskFlags },
    // Edge 3: Stage 2 ▶ Assembler
    { post_id: postId, stage1_result: stage1Result, stage2_result: stage2Result, normalized_post: normalizedPost }
  ];
}

function painlessNum(v, fallback) {
  return (typeof v === 'number' && !isNaN(v)) ? v : fallback;
}

/** Load (or rebuild) the pipeline view. Tries to seed from the most recent
 * analyzed post; falls back to sample data when the corpus is empty. */
async function loadPipeline(background) {
  var stepper = document.getElementById('pipeline-stepper');
  if (!stepper) return;

  var post = null;
  try {
    var data = await apiCall('/v1/analysis/latest?limit=1&include=results', { method: 'GET' });
    var results = (data && data.results) || (Array.isArray(data) ? data : []);
    if (results && results.length) post = results[0];
  } catch (err) {
    // API unreachable or no corpus — sample data is fine.
  }

  pipelineLive = !!post;
  pipelinePayloads = buildPipelinePayloads(post);

  var note = document.getElementById('pipeline-source-note');
  if (note) {
    note.innerHTML = pipelineLive
      ? 'Live — payloads reconstructed from the most recent analyzed post '
        + '<code>' + escHtml(shortenId(post.post_id || post.id || '')) + '</code>. '
        + 'Scrub the slider to step through each hand-off.'
      : 'Idle — no analyzed posts yet, so this shows <strong>sample data</strong>. '
        + 'Scrub the slider to step through each stage hand-off.';
  }

  renderPipelineTicks();
  renderPipeline(pipelineEdgeIndex);
  markRefreshed();
}

/** Render the slider tick labels (one per transition). */
function renderPipelineTicks() {
  var ticks = document.getElementById('pipeline-slider-ticks');
  if (!ticks) return;
  var html = '';
  for (var i = 0; i < PIPELINE_EDGES.length; i++) {
    html += '<span class="pipeline-tick' + (i === pipelineEdgeIndex ? ' active' : '') + '">'
      + escHtml(PIPELINE_EDGES[i].title) + '</span>';
  }
  ticks.innerHTML = html;
}

/** Render everything that depends on the selected edge: stepper highlight,
 * edge header, and the payload JSON. */
function renderPipeline(edgeIndex) {
  pipelineEdgeIndex = edgeIndex;
  var edge = PIPELINE_EDGES[edgeIndex];

  // --- Stepper ---
  var stepper = document.getElementById('pipeline-stepper');
  if (stepper) {
    var html = '';
    for (var i = 0; i < PIPELINE_STAGES.length; i++) {
      var s = PIPELINE_STAGES[i];
      var cls = 'pipeline-node';
      if (i === edge.from) cls += ' source';
      else if (i === edge.to) cls += ' target';
      else if (i < edge.from) cls += ' done';
      html += '<div class="' + cls + '">'
        + '<div class="pipeline-node-dot">' + (i + 1) + '</div>'
        + '<div class="pipeline-node-label">' + escHtml(s.label) + '</div>'
        + '<div class="pipeline-node-sub">' + escHtml(s.sub) + '</div>'
        + '</div>';
      if (i < PIPELINE_STAGES.length - 1) {
        var arrowCls = 'pipeline-arrow' + (i === edge.from ? ' active' : '');
        html += '<div class="' + arrowCls + '">&#10142;</div>';
      }
    }
    stepper.innerHTML = html;
  }

  // --- Edge header ---
  var head = document.getElementById('pipeline-edge-head');
  if (head) {
    var fromStage = PIPELINE_STAGES[edge.from];
    var toStage = PIPELINE_STAGES[edge.to];
    head.className = 'card-header';
    head.innerHTML =
      '<div>'
        + '<div class="card-title">' + escHtml(fromStage.label) + ' &#10142; ' + escHtml(toStage.label) + '</div>'
        + '<div class="card-description">Payload pushed onto Redis stream '
          + '<code>' + escHtml(edge.queue) + '</code></div>'
      + '</div>'
      + '<span class="badge ' + (pipelineLive ? 'badge-positive' : 'badge-neutral') + '">'
        + (pipelineLive ? 'live data' : 'sample data') + '</span>';
  }

  // --- Payload ---
  var payloadEl = document.getElementById('pipeline-payload');
  if (payloadEl && pipelinePayloads) {
    var obj = pipelinePayloads[edgeIndex];
    var keys = Object.keys(obj);
    var chips = '';
    for (var k = 0; k < keys.length; k++) {
      chips += '<span class="tag tag-sm">' + escHtml(keys[k]) + '</span>';
    }
    var json = '';
    try { json = JSON.stringify(obj, null, 2); } catch (e) { json = String(obj); }
    payloadEl.innerHTML =
      '<div class="pipeline-fields">'
        + '<span class="pipeline-fields-label">Top-level fields (' + keys.length + '):</span>'
        + chips
      + '</div>'
      + '<pre class="pipeline-json">' + escHtml(json) + '</pre>';
  }
}

/* ============================================================
   Utilities
   ============================================================ */
function escHtml(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function escAttr(str) {
  return escHtml(str);
}

function shortenId(id) {
  if (!id) return '';
  if (id.length <= 16) return id;
  return id.slice(0, 8) + '…' + id.slice(-4);
}

function truncate(str, n) {
  if (!str) return '';
  return str.length > n ? str.slice(0, n - 1) + '…' : str;
}

function formatDate(dateStr) {
  try {
    var d = new Date(dateStr);
    if (isNaN(d)) return dateStr;
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
      + ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  } catch (e) {
    return dateStr;
  }
}

function formatNumber(n) {
  if (n == null) return '0';
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000)    return (n / 1000).toFixed(1) + 'K';
  return String(n);
}

function readFileAsText(file) {
  return new Promise(function(resolve, reject) {
    var reader = new FileReader();
    reader.onload = function(e) { resolve(e.target.result); };
    reader.onerror = function(e) { reject(new Error('File read error')); };
    reader.readAsText(file);
  });
}

/* ============================================================
   Initialization
   ============================================================ */
document.addEventListener('DOMContentLoaded', init);

function init() {
  // Status bar API base display
  var apiBaseEl = document.getElementById('status-api-base');
  if (apiBaseEl) apiBaseEl.textContent = API_BASE;

  updateApiStatus('connecting');
  updateAuthStatus(!!(authToken || apiKey));

  // Set up nav tab click handlers
  document.querySelectorAll('.nav-tab').forEach(function(btn) {
    btn.addEventListener('click', function() {
      showTab(this.getAttribute('data-tab'));
    });
  });

  // Pipeline transition slider
  var pipelineSlider = document.getElementById('pipeline-slider');
  if (pipelineSlider) {
    pipelineSlider.addEventListener('input', function() {
      var idx = parseInt(this.value, 10) || 0;
      renderPipeline(idx);
      renderPipelineTicks();
    });
  }

  // Auto-refresh toggle
  var refreshToggle = document.getElementById('auto-refresh-toggle');
  if (refreshToggle) {
    autoRefreshEnabled = refreshToggle.checked;
    refreshToggle.addEventListener('change', function() {
      autoRefreshEnabled = this.checked;
      showToast('Auto-refresh ' + (autoRefreshEnabled ? 'enabled (every ' + (AUTO_REFRESH_MS / 1000) + 's)' : 'disabled') + '.', 'info');
    });
  }

  // Upload area drag & drop
  var uploadArea = document.getElementById('upload-area');
  var fileInput  = document.getElementById('file-input');
  var filename   = document.getElementById('upload-filename');

  if (uploadArea && fileInput) {
    uploadArea.addEventListener('click', function() { fileInput.click(); });

    uploadArea.addEventListener('dragover', function(e) {
      e.preventDefault();
      uploadArea.classList.add('drag-over');
    });

    uploadArea.addEventListener('dragleave', function() {
      uploadArea.classList.remove('drag-over');
    });

    uploadArea.addEventListener('drop', function(e) {
      e.preventDefault();
      uploadArea.classList.remove('drag-over');
      var file = e.dataTransfer.files[0];
      if (file && filename) filename.textContent = file.name;
      if (file) fileInput._droppedFile = file;
    });

    fileInput.addEventListener('change', function() {
      if (this.files[0] && filename) {
        filename.textContent = this.files[0].name;
      }
    });
  }

  // Upload button
  var uploadBtn = document.getElementById('upload-btn');
  if (uploadBtn) {
    uploadBtn.addEventListener('click', function() {
      var file = (fileInput && fileInput.files[0]) || (fileInput && fileInput._droppedFile);
      uploadPosts(file);
    });
  }

  // Run analysis form
  var runAnalysisBtn = document.getElementById('run-analysis-btn');
  if (runAnalysisBtn) {
    runAnalysisBtn.addEventListener('click', function() {
      var campaignInput = document.getElementById('campaign-id-input');
      var val = campaignInput ? campaignInput.value : '';
      runAnalysis(val);
    });
  }

  // Generate report button
  var generateReportBtn = document.getElementById('generate-report-btn');
  if (generateReportBtn) {
    generateReportBtn.addEventListener('click', generateReport);
  }

  // Search
  var searchBtn = document.getElementById('search-btn');
  if (searchBtn) {
    searchBtn.addEventListener('click', function() {
      var q       = document.getElementById('search-input');
      var toggle  = document.getElementById('semantic-toggle');
      search(q ? q.value : '', toggle ? toggle.checked : false);
    });
  }

  var searchInput = document.getElementById('search-input');
  if (searchInput) {
    searchInput.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') {
        var toggle  = document.getElementById('semantic-toggle');
        search(this.value, toggle ? toggle.checked : false);
      }
    });
  }

  // Posts campaign filter — Enter to apply
  var postsFilter = document.getElementById('posts-campaign-filter');
  if (postsFilter) {
    postsFilter.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') loadAnalysisResults();
    });
  }

  // Agents
  var agentAskBtn = document.getElementById('agent-ask-btn');
  if (agentAskBtn) {
    agentAskBtn.addEventListener('click', askAgent);
  }
  var agentQuestion = document.getElementById('agent-question');
  if (agentQuestion) {
    agentQuestion.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') askAgent();
    });
  }

  // Modal close
  var modalOverlay = document.getElementById('modal-overlay');
  if (modalOverlay) {
    modalOverlay.addEventListener('click', function(e) {
      if (e.target === modalOverlay) closeModal();
    });
  }

  var closeBtn = document.getElementById('modal-close-btn');
  if (closeBtn) {
    closeBtn.addEventListener('click', closeModal);
  }

  // Keyboard close modals
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      closeModal();
      closeLlmSettings();
    }
  });

  // LLM chip + settings modal
  var llmChip = document.getElementById('llm-chip');
  if (llmChip) {
    llmChip.addEventListener('click', openLlmSettings);
  }

  var llmOverlay = document.getElementById('llm-settings-overlay');
  if (llmOverlay) {
    llmOverlay.addEventListener('click', function(e) {
      if (e.target === llmOverlay) closeLlmSettings();
    });
  }

  var llmCloseBtn = document.getElementById('llm-settings-close-btn');
  if (llmCloseBtn) {
    llmCloseBtn.addEventListener('click', closeLlmSettings);
  }

  // NLP (sentiment model) chip + settings modal
  var nlpChip = document.getElementById('nlp-chip');
  if (nlpChip) {
    nlpChip.addEventListener('click', openNlpSettings);
  }

  var nlpOverlay = document.getElementById('nlp-settings-overlay');
  if (nlpOverlay) {
    nlpOverlay.addEventListener('click', function(e) {
      if (e.target === nlpOverlay) closeNlpSettings();
    });
  }

  var nlpCloseBtn = document.getElementById('nlp-settings-close-btn');
  if (nlpCloseBtn) {
    nlpCloseBtn.addEventListener('click', closeNlpSettings);
  }

  // Login form
  var loginForm = document.getElementById('login-form');
  if (loginForm) {
    loginForm.addEventListener('submit', handleLoginSubmit);
  }

  var loginBtn = document.getElementById('header-login-btn');
  if (loginBtn) {
    loginBtn.addEventListener('click', showLoginModal);
  }

  // Health check and initial load (auto-loads the overview)
  checkHealth().then(function(ok) {
    if (ok) {
      showTab('overview');
      loadLlmConfig();
      loadNlpConfig();
    } else {
      // API unreachable — show login modal if no creds
      if (!authToken && !apiKey) {
        showLoginModal();
      } else {
        showTab('overview');
      }
    }
  });

  // Auto-refresh the active tab — quietly, in the background, and never while
  // the user is interacting (modal open, row expanded, typing, tab hidden).
  setInterval(function() {
    if (autoRefreshEnabled && !shouldSkipAutoRefresh()) {
      refreshTab(currentTab, true);
    }
  }, AUTO_REFRESH_MS);

  // Check API health every 60s
  setInterval(function() {
    checkHealth();
  }, 60000);
}
