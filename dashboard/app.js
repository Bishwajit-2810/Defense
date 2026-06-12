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

  refreshTab(tabName);
}

/** Load (or reload) the data behind a tab. */
function refreshTab(tabName) {
  if (tabName === 'overview') {
    loadOverview();
  } else if (tabName === 'posts') {
    loadAnalysisResults();
  } else if (tabName === 'jobs') {
    loadJobs();
  } else if (tabName === 'reports') {
    loadReports();
  } else if (tabName === 'agents') {
    loadAgentRuns();
  }
  // 'search' is on-demand only.
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

  // Corpus-derived charts from the latest results
  var results = [];
  try {
    var data = await apiCall('/v1/analysis/latest?limit=500', { method: 'GET' });
    results = (data && data.results) || [];
  } catch (err) {
    // charts simply stay empty
  }

  renderOverviewCharts(results);
  renderLlmPanel(usage, results);
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

function renderOverviewCharts(results) {
  // ---- Sentiment donut ----
  var sentCounts = { positive: 0, negative: 0, neutral: 0, mixed: 0 };
  var langCounts = {};
  var topicCounts = {};

  results.forEach(function(r) {
    var s = r.overall_sentiment || 'neutral';
    sentCounts[s] = (sentCounts[s] || 0) + 1;
    var lang = r.language || 'und';
    langCounts[lang] = (langCounts[lang] || 0) + 1;
    (r.topics || []).forEach(function(t) {
      topicCounts[t] = (topicCounts[t] || 0) + 1;
    });
  });

  var pieCanvas = document.getElementById('overview-sentiment-pie');
  if (pieCanvas) {
    renderSentimentPie(pieCanvas, {
      positive: sentCounts.positive,
      negative: sentCounts.negative,
      neutral:  sentCounts.neutral + (sentCounts.mixed || 0)
    });
  }
  var legendEl = document.getElementById('overview-sentiment-legend');
  if (legendEl) {
    legendEl.innerHTML =
        makeLegendItem('Positive', sentCounts.positive, 'var(--color-positive)')
      + makeLegendItem('Negative', sentCounts.negative, 'var(--color-negative)')
      + makeLegendItem('Neutral',  sentCounts.neutral,  'var(--color-neutral)')
      + (sentCounts.mixed ? makeLegendItem('Mixed', sentCounts.mixed, 'var(--color-mixed)') : '');
  }

  // ---- Language bar chart ----
  var langCanvas = document.getElementById('overview-lang-chart');
  if (langCanvas) {
    var langData = Object.keys(langCounts).map(function(k) {
      return { label: k, value: langCounts[k], color: '#6366f1' };
    }).sort(function(a, b) { return b.value - a.value; }).slice(0, 6);
    renderBarChart(langCanvas, langData);
  }

  // ---- Top topics bar chart ----
  var topicsCanvas = document.getElementById('overview-topics-chart');
  if (topicsCanvas) {
    var topicData = Object.keys(topicCounts).map(function(k) {
      return { label: k, value: topicCounts[k], color: '#8b5cf6' };
    }).sort(function(a, b) { return b.value - a.value; }).slice(0, 8);
    renderBarChart(topicsCanvas, topicData, 110);
  }
}

function renderLlmPanel(usage, results) {
  var el = document.getElementById('overview-llm-panel');
  if (!el) return;

  var withSummary = 0, vlmGrounded = 0, llmUsed = 0;
  var backends = {};
  results.forEach(function(r) {
    if (r.post_summary) withSummary++;
    if (r.processing && r.processing.llm_used) llmUsed++;
    var be = r.processing && r.processing.llm_backend;
    if (be) backends[be] = (backends[be] || 0) + 1;
  });

  var html = '<div class="meta-grid">'
    + makeMetaField('Active backend', llmConfig ? llmBackendLabel(llmConfig.backend) : '—')
    + makeMetaField('Posts with LLM output', llmUsed + ' / ' + results.length)
    + makeMetaField('Posts with summaries', withSummary)
    + makeMetaField('Backends seen', Object.keys(backends).map(function(k) {
        return k + ' (' + backends[k] + ')';
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
async function loadAnalysisResults() {
  var container = document.getElementById('results-table-body');
  if (!container) return;

  container.innerHTML = '<tr><td colspan="9" class="table-empty"><div class="loading-overlay"><div class="spinner spinner-dark"></div> Loading results...</div></td></tr>';

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
    container.innerHTML = '<tr><td colspan="9" class="table-empty">'
      + '<div class="alert alert-error" style="display:inline-block">Could not load results: ' + escHtml(err.message) + '</div>'
      + '</td></tr>';
  }
}

function renderResultsTable(results) {
  var tbody = document.getElementById('results-table-body');
  if (!tbody) return;

  if (!results || results.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" class="table-empty">No results found. Upload posts to get started.</td></tr>';
    return;
  }

  var html = '';
  results.forEach(function(r, i) {
    var sentiment = r.overall_sentiment || 'neutral';
    var toxScore  = typeof r.toxicity_score === 'number' ? r.toxicity_score : null;
    var coverage  = buildCoverageText(r.comment_analysis, r.engagement);
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

function buildCoverageText(commentAnalysis, engagement) {
  if (!commentAnalysis) return '—';
  var analyzed = commentAnalysis.analyzed || 0;
  var total = (engagement && engagement.comment_count) || 0;
  if (!total) return analyzed + ' analyzed';
  var p = total > 0 ? Math.round(analyzed / total * 100) : 0;
  return p + '% (' + analyzed + '/' + total + ')';
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
    return;
  }

  // Collapse other expanded rows
  document.querySelectorAll('tr.expanded').forEach(function(r) {
    r.classList.remove('expanded');
    var oldDetail = document.getElementById('row-detail-' + r.getAttribute('data-idx'));
    if (oldDetail) oldDetail.parentNode.removeChild(oldDetail);
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
    ['emotion',           r.emotion && typeof r.emotion === 'object' ? topEmotion(r.emotion) : r.emotion],
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

  // Summary preview
  var captionHtml = '';
  if (r.post_summary) {
    captionHtml = '<div class="detail-field" style="grid-column:1/-1">'
      + '<span class="detail-field-label">Post Summary'
      + (r.post_summary_source ? ' (' + escHtml(r.post_summary_source) + ')' : '') + '</span>'
      + '<span class="detail-field-value" style="font-family:var(--font-sans);white-space:normal">' + escHtml(r.post_summary) + '</span>'
    + '</div>';
  }

  var tdContent = '<div class="row-detail-content">' + fieldsHtml + captionHtml + '</div>';

  detailRow.innerHTML = '<td colspan="9">' + tdContent + '</td>';
  row.parentNode.insertBefore(detailRow, row.nextSibling);
}

function topEmotion(emotionObj) {
  var best = null, bestV = -1;
  Object.keys(emotionObj).forEach(function(k) {
    var v = Number(emotionObj[k]);
    if (v > bestV) { bestV = v; best = k; }
  });
  return best ? best + ' (' + bestV.toFixed(2) + ')' : null;
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
    + '</div>'
  + '</div>';

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

  // ---- Comment analysis ----
  if (r.comment_analysis) {
    var ca = r.comment_analysis;
    var coverage = ca.coverage || (ca.analyzed + ' analyzed');
    var sb = ca.sentiment_breakdown || {};

    html += '<div class="modal-section">'
      + '<div class="modal-section-title">Comment Analysis</div>'
      + '<div class="charts-row">'
      + '<div>'
      + '<p class="chart-title">Coverage: ' + escHtml(coverage) + '</p>'
      + '<div style="display:flex;gap:20px;align-items:center">'
      + '<canvas id="sentiment-pie" width="160" height="160"></canvas>'
      + '<div style="flex:1">'
      + makeLegendItem('Positive', sb.positive || 0, 'var(--color-positive)')
      + makeLegendItem('Negative', sb.negative || 0, 'var(--color-negative)')
      + makeLegendItem('Neutral',  sb.neutral  || 0, 'var(--color-neutral)')
      + '</div>'
      + '</div>'
      + '</div>';

    if (ca.themes && ca.themes.length > 0) {
      html += '<div>'
        + '<p class="chart-title">Top Themes</p>'
        + '<ul class="theme-list">'
        + ca.themes.map(function(t) { return '<li>' + escHtml(t) + '</li>'; }).join('')
        + '</ul>'
      + '</div>';
    }

    html += '</div>';

    // Representative comments
    if (ca.representative_comments && ca.representative_comments.length > 0) {
      html += '<p class="chart-title" style="margin-top:12px">Representative Comments</p>';
      ca.representative_comments.forEach(function(c) {
        var cObj = (typeof c === 'string') ? { text: c } : (c || {});
        html += '<div class="rep-comment">'
          + '<div class="rep-comment-meta">'
          + (cObj.sentiment ? '<span class="badge badge-' + escAttr(cObj.sentiment) + '">' + escHtml(cObj.sentiment) + '</span>' : '')
          + (cObj.lang ? '<span class="text-muted">' + escHtml(cObj.lang) + '</span>' : '')
          + (cObj.likes != null ? '<span class="text-muted">♥ ' + cObj.likes + '</span>' : '')
          + '</div>'
          + '<div class="rep-comment-text">' + escHtml(cObj.text || '') + '</div>'
        + '</div>';
      });
    }

    html += '</div>';
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

  if (r.comment_analysis && r.comment_analysis.sentiment_breakdown) {
    setTimeout(function() {
      var canvas = document.getElementById('sentiment-pie');
      if (canvas) renderSentimentPie(canvas, r.comment_analysis.sentiment_breakdown);
    }, 50);
  }
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

async function loadJobs() {
  var container = document.getElementById('jobs-table-body');
  if (!container) return;

  container.innerHTML = '<tr><td colspan="6" class="table-empty"><div class="loading-overlay"><div class="spinner spinner-dark"></div> Loading jobs...</div></td></tr>';

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
async function loadReports() {
  var container = document.getElementById('reports-list');
  if (!container) return;

  container.innerHTML = '<div class="loading-overlay"><div class="spinner spinner-dark"></div> Loading reports...</div>';

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
    } else {
      // API unreachable — show login modal if no creds
      if (!authToken && !apiKey) {
        showLoginModal();
      } else {
        showTab('overview');
      }
    }
  });

  // Auto-refresh the active tab
  setInterval(function() {
    if (autoRefreshEnabled) {
      refreshTab(currentTab);
    }
  }, AUTO_REFRESH_MS);

  // Check API health every 60s
  setInterval(function() {
    checkHealth();
  }, 60000);
}
