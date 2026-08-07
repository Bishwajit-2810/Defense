/**
 * Defense Analysis Dashboard — app.js
 * Plain vanilla ES5-compatible JavaScript. No frameworks, no build step.
 * window.API_BASE can be set before this script loads.
 *
 * Tabs: Overview (usage + corpus charts), Posts (upload + results),
 * Jobs (run analysis + live SSE progress), Reports (grounded LLM reports),
 * Search (keyword/semantic), Agents (analyst Q&A), Chat (free-form chatbot),
 * Pipeline (live stage flow).
 * The active tab auto-refreshes every 15 s (toggle in the header).
 */

/* ============================================================
   Config & globals
   ============================================================ */
var API_BASE = window.API_BASE || 'http://127.0.0.1:8001';  // dev API (run.md / easy_run.md); override via window.API_BASE
var AUTO_REFRESH_MS = 15000;
var authToken = localStorage.getItem('auth_token');
var apiKey    = localStorage.getItem('api_key') || '';
var authUser  = localStorage.getItem('auth_user') || '';   // display name for the header chip
var authMode  = 'signin';       // 'signin' | 'signup' — which login-card form is showing
var authConfig = null;          // last GET /v1/auth/config (null = not asked / unavailable)
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
var chatHistory = [];           // [{role, content}] conversation for /v1/chat[/stream]
var chatBackend = 'auto';       // 'auto' (follow toggle) | 'local' | 'groq'
var chatBusy = false;           // a chat request is in flight (blocks concurrent sends)
var chatModel = '';             // '' = the backend's default model, else a specific model id
var chatModelsData = null;      // last /v1/chat/models payload (per-backend model lists)

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

  // Every call is recorded for the log drawer, tagged with the tab that made it,
  // so each section shows what it asked for and what came back. The log
  // endpoints themselves are skipped — otherwise reading the logs generates
  // logs, which quickly buries everything else.
  var logStarted = (typeof performance !== 'undefined' ? performance.now() : Date.now());
  var logMethod = (options.method || 'GET').toUpperCase();
  var traceable = path.indexOf('/v1/logs') !== 0;

  try {
    var response = await fetch(url, fetchOptions);

    if (traceable) {
      logClient(logMethod, path, response.status, logStarted,
                response.headers ? response.headers.get('X-Request-ID') : null);
    }

    if (response.status === 401) {
      // Clear the session AND tear down every open stream. Previously only the
      // token was dropped, so the UI said "logged out" while the Trace and Logs
      // tabs kept streaming on the same dead credential — the split state
      // PROJECT_ASSESSMENT §6.6 defect 4 describes, and very likely what
      // "JWT auth is not working properly" looked like from the outside.
      authToken = null;
      authUser = '';
      localStorage.removeItem('auth_token');
      localStorage.removeItem('auth_user');
      updateAuthStatus(false);
      disconnectAllStreams();
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
        // FastAPI validation errors arrive as [{loc, msg, ...}]. Stringifying the
        // array put raw JSON in front of the user — on the signup form, where a
        // 422 is the most likely error, that is the whole message they get. Pull
        // the human sentences out and leave every other shape as it was.
        if (Object.prototype.toString.call(data.detail) === '[object Array]') {
          var parts = data.detail.map(function(d) {
            return d && d.msg ? String(d.msg).replace(/^Value error,\s*/, '') : null;
          }).filter(Boolean);
          msg = parts.length ? parts.join('; ') : JSON.stringify(data.detail);
        } else {
          msg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
        }
      } else if (typeof data === 'string' && data) {
        msg = data;
      }
      throw new Error(msg);
    }

    return data;

  } catch (err) {
    if (err.name === 'TypeError' && err.message.indexOf('fetch') !== -1) {
      updateApiStatus('error');
      if (traceable) logClient(logMethod, path, 'unreachable', logStarted, null, err.message);
      throw new Error('Cannot reach API at ' + API_BASE + '. Is the server running?');
    }
    if (traceable) logClient(logMethod, path, 'error', logStarted, null, err.message);
    throw err;
  }
}

/** Close every SSE stream. Called when the session dies, so the UI cannot show
 *  "logged out" while streams keep delivering data on a dead credential. */
function disconnectAllStreams() {
  try { disconnectPipelineLive(); } catch (e) { /* tab may not be mounted */ }
  try { disconnectLogStream(); } catch (e) { /* idem */ }
  try { if (typeof traceES !== 'undefined' && traceES) { traceES.close(); traceES = null; } } catch (e) {}
  try { stopAllJobStreams(); } catch (e) {}
}

/** Credential usable as a query param for EventSource (cannot set headers).
 *
 * DEPRECATED as a bearer credential — prefer sseQuery() below. Kept only as the
 * fallback for a server that predates POST /v1/auth/sse-ticket.
 */
function sseCredential() {
  return encodeURIComponent(apiKey || authToken || 'demo');
}

/** Query string for an EventSource URL: a single-use ticket where possible.
 *
 * `EventSource` genuinely cannot set headers, so a streaming client needs
 * something in the URL. Putting the SESSION credential there is the wrong
 * something — URLs land in proxy logs, browser history and Referer headers, and
 * that credential is good for an hour (or forever, for an API key).
 *
 * A ticket is single-use and ~60s, so the same leak is harmless. Falls back to
 * the old parameter if the endpoint is unavailable, so the dashboard keeps
 * working against an older server rather than silently showing no data.
 */
async function sseQuery(extra) {
  var suffix = extra ? '&' + extra : '';
  try {
    var res = await apiCall('/v1/auth/sse-ticket', {method: 'POST'});
    if (res && res.ticket) {
      return '?ticket=' + encodeURIComponent(res.ticket) + suffix;
    }
  } catch (err) {
    console.warn('sse-ticket unavailable, falling back to api_key param:', err.message);
  }
  return '?api_key=' + sseCredential() + suffix;
}

/* ============================================================
   Auth
   ============================================================ */
async function login(username, password, apiKeyInput) {
  // If API key provided, store it and skip JWT login
  if (apiKeyInput) {
    apiKey = apiKeyInput.trim();
    localStorage.setItem('api_key', apiKey);
    setAuthUser('');                 // an API key is a service identity, not a person
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
  setAuthUser((username || '').trim().toLowerCase());
  updateAuthStatus(true);
  updateApiStatus('ok');
}

/** Register an account, then use the token the server hands back.
 *
 * No second round-trip to /v1/auth/token: signup returns a token precisely so
 * the password does not have to cross the wire twice to get a session.
 *
 * Note what is NOT sent — tenant or role. The server assigns both, so a form
 * field for either would be a lie about who decides.
 */
async function signup(username, password) {
  var data = await apiCall('/v1/auth/signup', {
    method: 'POST',
    body: JSON.stringify({ username: username, password: password })
  });
  authToken = data.access_token || data.token;
  localStorage.setItem('auth_token', authToken);
  // A stale API key would otherwise keep winning in apiCall()'s header order on
  // some paths — clear it so the new session is the one credential in play.
  apiKey = '';
  localStorage.removeItem('api_key');
  setAuthUser(data.username || username);
  updateAuthStatus(true);
  updateApiStatus('ok');
  return data;
}

/** Drop every stored credential and tear down the streams running on them.
 *
 * The teardown is the part that matters: without it the header reads "Not
 * authenticated" while the Trace, Logs and Pipeline EventSources keep delivering
 * data on the credential the user just revoked — the same split state §6.6
 * defect 4 describes, arrived at from the other direction.
 */
function logout() {
  authToken = null;
  apiKey = '';
  localStorage.removeItem('auth_token');
  localStorage.removeItem('api_key');
  setAuthUser('');
  disconnectAllStreams();
  updateAuthStatus(false);
  showToast('Signed out', 'info');
  showLoginModal();
}

function setAuthUser(name) {
  authUser = name || '';
  if (authUser) localStorage.setItem('auth_user', authUser);
  else localStorage.removeItem('auth_user');
}

/** Ask the server what the login screen may offer, before rendering it.
 *
 * Without this the signup tab is a guess: shown on a deployment that returns 403
 * for every registration, or hidden on one whose users table is empty and needs
 * a first admin. Tolerates an older server (or an unreachable one) by leaving
 * signup enabled — a 403 from the form is a clearer failure than a tab that
 * silently vanished.
 */
async function loadAuthConfig() {
  try {
    authConfig = await apiCall('/v1/auth/config');
  } catch (err) {
    console.warn('auth config unavailable:', err.message);
    authConfig = null;
  }
  applyAuthConfig();
  return authConfig;
}

function applyAuthConfig() {
  var signupTab = document.getElementById('auth-mode-signup');
  var note = document.getElementById('signup-bootstrap-note');
  var hint = document.getElementById('signup-password-hint');
  var cfg = authConfig;

  if (signupTab) {
    var enabled = !cfg || cfg.signup_enabled !== false;
    signupTab.disabled = !enabled;
    if (enabled) {
      signupTab.title = 'Register a new account';
    } else if (cfg && cfg.users_table_ready === false) {
      // Different cause, different fix — "ask an administrator" is useless advice
      // when the real problem is an unmigrated database.
      signupTab.title = 'The API has no users table — run deploy/init-db.sql to enable accounts';
    } else {
      signupTab.title = 'Signup is disabled on this deployment — ask an administrator '
                      + 'for an account or an API key';
    }
    // If signup just became unavailable while its form was open, fall back
    // rather than leaving a form up that cannot succeed.
    if (!enabled && authMode === 'signup') setAuthMode('signin');
  }
  if (note) {
    if (cfg && cfg.bootstrap) note.classList.remove('hidden');
    else note.classList.add('hidden');
  }
  if (hint && cfg && cfg.min_password_length) {
    hint.textContent = 'At least ' + cfg.min_password_length + ' characters';
  }
  // A missing users table means signup will 503 with the fix in the message;
  // say so up front instead of after a failed submit.
  if (cfg && cfg.users_table_ready === false) {
    var sub = document.getElementById('login-subtitle');
    if (sub) {
      sub.textContent = 'The API has no users table yet — run deploy/init-db.sql to '
                      + 'enable accounts. An API key still works.';
    }
  }
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
  // Remember where we came from so the Logs tab's "this tab" scope, and the
  // Ctrl+` toggle, both have somewhere sensible to point.
  if (tabName === 'logs' && currentTab !== 'logs') logPrevTab = currentTab;
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

  // Same for the log tail: hold the SSE connection only while Logs is visible.
  if (tabName !== 'logs') suspendLogs();

  // Chat is stateful (keeps its conversation); just refresh the backend hint
  // and focus the composer instead of reloading data.
  if (tabName === 'chat') {
    initChatTab();
    return;
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
  } else if (tabName === 'trace') {
    loadTrace(background);
  } else if (tabName === 'logs') {
    loadLogs(background);
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
    // Pipeline lane split: the number that actually bounds per-post cost. The
    // routing rate alone is not the cost story once every comment reaches an LLM.
    //
    // The share is computed over the PIPELINE lanes only. `lane_split` gained
    // `interactive` and `agent` when usage tracking moved into LLMClient, and
    // reading `comment.call_share` straight off the response would silently
    // divide by chat and agent traffic too — a per-post figure diluted by
    // per-question spend, still labelled post-vs-comment (§13.4).
    var lanes = usage.lane_split || {};
    var postLane = lanes.post || {};
    var commentLane = lanes.comment || {};
    var stage1Lane = lanes.stage1 || {};
    var pipelineCalls = (postLane.calls || 0) + (commentLane.calls || 0) + (stage1Lane.calls || 0);
    var commentShare = pipelineCalls ? (commentLane.calls || 0) / pipelineCalls : 0;
    var laneNote = pipelineCalls
      ? pct(commentShare) + ' of pipeline calls are comment-level'
      : 'no pipeline LLM calls recorded yet';
    var laneHint = formatNumber(postLane.calls || 0) + ' post · '
      + formatNumber(commentLane.calls || 0) + ' comment'
      + (stage1Lane.calls ? ' · ' + formatNumber(stage1Lane.calls) + ' stage-1' : '');

    statsEl.innerHTML =
        makeStatCard('Posts analyzed', formatNumber(usage.posts_analyzed), null)
      + makeStatCard('LLM-routed posts', formatNumber(usage.llm_calls),
                     pct(usage.llm_routing_rate) + ' routing rate — a measure of Stage-1 quality')
      + makeStatCard('LLM API calls', formatNumber(usage.llm_api_calls || 0),
                     (usage.cache_hits || 0) + ' cache hits (' + pct(usage.cache_hit_rate) + ')')
      + makeStatCard('Cost split', laneNote, laneHint)
      // `pipeline_tokens` is the per-post figure; `total_tokens` includes chat
      // and agent spend, which is per-question and unrelated to corpus size.
      // Showing only the total would attribute a chatbot session to the posts.
      + makeStatCard('Pipeline tokens', formatNumber(usage.pipeline_tokens || 0),
                     tokenScopeSubtitle(usage))
      + makeStatCard('Tokens used (all)', formatNumber(usage.total_tokens),
                     costSubtitle(usage));
    statsEl.innerHTML += usageProvenanceHtml(usage);
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

/** Cost subtitle for the token card, priced per backend.
 *
 * `local` is 0.0/token by definition (the cost is GPU time, not tokens), so a
 * bare "$0.0000" would read as "no data" rather than "genuinely free". Say which.
 */
function costSubtitle(usage) {
  var byModel = usage.cost_by_backend_model || {};
  var models = Object.keys(byModel);
  var cost = Number(usage.estimated_cost_usd || 0);
  if (!models.length) return 'no per-model token counters yet';
  var allLocal = models.every(function(k) { return k.indexOf('local:') === 0; });
  if (allLocal && cost === 0) {
    return 'local backend — $0 per token by definition';
  }
  return '≈ $' + cost.toFixed(4) + ' across ' + models.length + ' model'
       + (models.length === 1 ? '' : 's');
}

/** Subtitle for the pipeline-token card: what the total covers that this doesn't.
 *
 * The counters used to be Stage-2 only while `scope_note` called them
 * system-wide (§13.4). They now cover every caller, which makes the opposite
 * distinction the one worth drawing: `total_tokens` includes chat, report and
 * agent spend, and only the pipeline lanes belong in a per-post cost model.
 */
function tokenScopeSubtitle(usage) {
  var total = Number(usage.total_tokens || 0);
  var pipeline = Number(usage.pipeline_tokens || 0);
  if (!total) return 'post + comment + stage-1 lanes';
  var other = total - pipeline;
  if (other <= 0) return 'all spend so far is pipeline work';
  return formatNumber(other) + ' more spent on chat / reports / agents';
}

/** Per-model token/cost table + the campaign-scope caveat.
 *
 * One blended rate used to be applied to every token, which was wrong for both
 * backends in opposite directions. This shows the dimension that replaced it.
 */
function usageProvenanceHtml(usage) {
  var tokens = usage.tokens_by_backend_model || {};
  var costs = usage.cost_by_backend_model || {};
  var keys = Object.keys(tokens);
  if (!keys.length && !usage.scope_note) return '';

  var html = '<div class="stat-note" style="grid-column:1/-1">';
  if (keys.length) {
    html += '<div class="stat-note-title">Tokens by backend and model</div>'
          + '<div class="mini-list">'
          + keys.sort(function(a, b) { return tokens[b] - tokens[a]; }).map(function(k) {
              var c = Number(costs[k] || 0);
              var priced = k.indexOf('local:') === 0 ? 'free (local)' : '$' + c.toFixed(4);
              return '<div class="mini-row"><span class="mini-name">' + escHtml(k) + '</span>'
                   + '<span class="text-muted">' + formatNumber(tokens[k]) + ' tok · ' + priced + '</span></div>';
            }).join('')
          + '</div>';
  }
  // Every lane, not just the two on the card. `interactive` and `agent` exist
  // because usage tracking moved into LLMClient and started counting chat,
  // report and agent calls that previously reached no counter at all (§13.4) —
  // leaving them off this panel would put them straight back out of sight.
  var lanes = usage.lane_split || {};
  var laneKeys = Object.keys(lanes).filter(function(k) {
    return (lanes[k] && (lanes[k].calls || lanes[k].tokens));
  });
  if (laneKeys.length) {
    html += '<div class="stat-note-title" style="margin-top:10px">Spend by lane</div>'
          + '<div class="mini-list">'
          + laneKeys.sort(function(a, b) {
              return (lanes[b].tokens || 0) - (lanes[a].tokens || 0);
            }).map(function(k) {
              var l = lanes[k];
              return '<div class="mini-row"><span class="mini-name">' + escHtml(k)
                   + (LANE_HINTS[k] ? ' <span class="text-muted">' + escHtml(LANE_HINTS[k]) + '</span>' : '')
                   + '</span><span class="text-muted">' + formatNumber(l.calls || 0)
                   + ' calls · ' + formatNumber(l.tokens || 0) + ' tok</span></div>';
            }).join('')
          + '</div>';
  }

  if (usage.scope_note) {
    html += '<div class="text-muted" style="font-size:.72rem;margin-top:6px">'
          + escHtml(usage.scope_note) + '</div>';
  }
  return html + '</div>';
}

/** What each usage lane means — mirrors libs/llm/usage.py. Per-post cost is the
 *  first three; the last two are per-question and scale with usage, not corpus. */
var LANE_HINTS = {
  post:        '(per post)',
  comment:     '(per comment)',
  stage1:      '(per post, STAGE1_LLM)',
  interactive: '(chat + reports)',
  agent:       '(per agent turn)'
};

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

  // ---- Corpus-level comment coverage ----
  // The per-post figure is what every other view shows; the aggregate is much
  // smaller and it is the one that bounds what a thread-level sentiment claim
  // can support. It used to appear nowhere.
  var covEl = document.getElementById('overview-corpus-coverage');
  if (covEl) {
    var cc = overview.corpus_coverage || {};
    if (cc.reported) {
      covEl.innerHTML =
          '<span class="tag tag-sm">' + pct(cc.coverage) + ' corpus coverage</span> '
        + '<span class="text-muted">' + formatNumber(cc.analyzed) + ' comments analysed of '
        + formatNumber(cc.reported) + ' the platform reports</span>'
        + (cc.posts_with_anomaly
            ? ' <span class="tag tag-sm tag-warning" title="stored comments exceed the platform\'s reported count — coverage is capped at 100%">'
              + cc.posts_with_anomaly + ' upstream mismatch'
              + (cc.posts_with_anomaly === 1 ? '' : 'es') + '</span>'
            : '');
    } else {
      covEl.innerHTML = '<span class="text-muted">no coverage data yet</span>';
    }
  }

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
      return { label: d.label, value: d.count, color: chartPalette().accent };
    }));
  }

  // ---- Top topics bar chart ----
  var topicsCanvas = document.getElementById('overview-topics-chart');
  if (topicsCanvas) {
    renderBarChart(topicsCanvas, (overview.top_topics || []).map(function(d) {
      return { label: d.label, value: d.count, color: chartPalette().accentDeep };
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
    // A reused post inherited its analysis from a near-duplicate caption — the
    // summary below was written for a DIFFERENT post. Flag it in the list, not
    // only in the detail modal, or the row reads as this post's own finding.
    var reusedTag = (r.processing && r.processing.reused_from)
      ? ' <span class="tag tag-sm" title="reused from near-duplicate post '
        + escAttr(String(r.processing.reused_from.source_post_id || '')) + '">near-dup</span>'
      : '';
    var summaryCell = r.post_summary
      ? '<span class="summary-snippet" title="' + escAttr(r.post_summary) + '">' + escHtml(truncate(r.post_summary, 70)) + '</span>'
        + (r.post_summary_source === 'vlm' ? ' <span class="tag tag-sm" title="image-grounded by the VLM">vlm</span>' : '')
        + reusedTag
      : '<span class="text-muted">—</span>' + reusedTag;

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
  chips.push(makeChip('language', langChip, chartPalette().accent));
  if (emo && emo.primary) chips.push(makeChip('emotion', emo.primary, (EMOTION_META[emo.primary] || {}).color || chartPalette().accentDeep));
  if (typeof r.toxicity_score === 'number') chips.push(makeChip('toxicity', pct(r.toxicity_score), sevColor(r.toxicity_score)));
  if (typeof r.hate_speech_score === 'number') chips.push(makeChip('hate', pct(r.hate_speech_score), sevColor(r.hate_speech_score)));
  if (typeof conf.overall === 'number') chips.push(makeChip('confidence', pct(conf.overall), chartPalette().accentAlt));
  var eg = r.engagement || {};
  chips.push(makeChip('♥ reactions', formatNumber(eg.total_reactions || eg.reactions || 0), chartPalette().secondary));
  chips.push(makeChip('💬 comments', formatNumber(eg.comment_count || 0), chartPalette().secondary));
  chips.push(makeChip('↗ shares', formatNumber(eg.share_count || 0), chartPalette().secondary));
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
    // A summary that hit the model's token ceiling even after auto-continuation
    // is shown as incomplete rather than passed off as the whole answer (§6.1).
    if (r.post_summary_truncated) {
      srcBadge += ' <span class="tag tag-sm tag-warning" title="the model hit its token limit; this summary was trimmed to its last complete sentence">truncated</span>';
    }
    html += '<div class="modal-section">'
      + '<div class="modal-section-title">Post Summary' + srcBadge + '</div>'
      + '<div class="summary-box">' + escHtml(r.post_summary) + '</div>'
      + (r.post_summary_grounding
          ? '<div style="font-size:0.75rem;color:var(--text-muted);margin-top:6px">Grounded on: ' + escHtml(String(r.post_summary_grounding)) + '</div>'
          : '')
    + '</div>';
  }

  // ---- Insight ----
  // Stage 2's one-line analytical takeaway (null when Stage 2 was skipped).
  // §11.1 restored this to the canonical result and the API, but it arrived here
  // only as an untitled row in the "All Fields" dump at the bottom — next to
  // post_id and created_at, which is not where a reader looks for the finding.
  if (r.insight) {
    html += '<div class="modal-section">'
      + '<div class="modal-section-title">Insight'
      + ' <span class="tag tag-sm" title="produced by the Stage-2 LLM insight task">stage 2</span>'
      + '</div>'
      + '<div class="summary-box">' + escHtml(r.insight) + '</div>'
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

  // Confidence column (DOM bars). One accent for all four, resolved from the
  // blue ramp so the theme stays single-sourced in styles.css.
  var confBarColor = chartPalette().accent;
  html += '<div><p class="chart-title">Confidence</p>'
    + makeBar('Overall',   conf.overall,   confBarColor)
    + makeBar('Sentiment', conf.sentiment, confBarColor)
    + makeBar('Language',  conf.language,  confBarColor)
    + makeBar('Topics',    conf.topics,    confBarColor)
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
    'post_summary_truncated','image_analysis','emotion','confidence',
    // Rendered in its own section above, not as a raw metadata row.
    'insight']);

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
    // Near-duplicate reuse: this post skipped Stage 1 and Stage 2 entirely and
    // inherited another post's analysis. Without saying so, the block below
    // reads as a genuine 0 ms analysis that simply never used an LLM — which is
    // how a reused row would pass for a fresh one.
    var reuse = p.reused_from;
    if (reuse) {
      html += '<div class="modal-section">'
        + '<div class="modal-section-title">Reused Analysis'
        + ' <span class="tag tag-sm" title="near-duplicate caption; Stage 1 and Stage 2 were skipped">near-dup</span>'
        + '</div>'
        + '<div class="alert alert-warning" style="margin-bottom:8px">'
        + 'The post-level analysis below was <strong>copied from another post</strong> whose '
        + 'caption is a near-duplicate of this one — no model ran for this post. '
        + 'Its comment thread is its own and was <strong>not</strong> analysed.'
        + '</div>'
        + '<div class="meta-grid">'
        + makeMetaField('Source post', reuse.source_post_id)
        + makeMetaField('Similarity', reuse.similarity != null ? reuse.similarity : null)
        + makeMetaField('Reused', reuse.reused)
        + '</div>'
      + '</div>';
    }
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
      // Reuse the one emotion palette rather than keeping a second copy that
      // can drift from it (EMOTION_META, defined below).
      var emoColors = {};
      Object.keys(EMOTION_META).forEach(function(k) { emoColors[k] = EMOTION_META[k].color; });
      var data = Object.keys(emoScores).map(function(k){
        return { label: k, value: Number(emoScores[k]) || 0, color: emoColors[k] || chartPalette().accent };
      }).sort(function(a,b){ return b.value - a.value; });
      renderBarChart(canvas, data, 76);
    }, 50);
  }

  // Mount the comment-insights charts + lazy-load the per-comment list.
  if (r.comment_analysis && (r.post_id || currentPostId)) {
    mountCommentInsights('modal', r);
  }
}

/* ============================================================
   Post detail → PDF
   ============================================================
   No PDF library and no build step: clone the rendered modal body into an
   offscreen iframe that links the dashboard stylesheet, then let the browser
   print it ("Save as PDF" in the print dialog). Two things need fixing up in
   the clone — cloneNode() does not copy canvas pixels (so each chart is
   rasterized to a PNG), and interactive controls are meaningless on paper
   (so they are dropped). What is on screen is what lands in the file,
   including any lazily-loaded comment pages. */
function downloadPostPdf() {
  var body = document.getElementById('modal-body');
  if (!body || !body.children.length || body.querySelector('.loading-overlay')) {
    showToast('Post details are still loading.', 'error');
    return;
  }

  var clone = body.cloneNode(true);

  // Canvas pixels don't survive cloneNode — swap each chart for a PNG at the
  // size it renders on screen. Both lists come from querySelectorAll on the
  // same markup, so they stay index-aligned while we replace nodes.
  var liveCanvas  = body.querySelectorAll('canvas');
  var clonedCanvas = clone.querySelectorAll('canvas');
  for (var i = 0; i < liveCanvas.length && i < clonedCanvas.length; i++) {
    var src;
    try {
      src = liveCanvas[i].toDataURL('image/png');
    } catch (e) {
      continue;   // tainted or zero-sized canvas — leave the blank one in place
    }
    var img = document.createElement('img');
    img.src = src;
    img.className = 'pdf-chart';
    img.style.width  = (liveCanvas[i].offsetWidth  || liveCanvas[i].width)  + 'px';
    img.style.height = (liveCanvas[i].offsetHeight || liveCanvas[i].height) + 'px';
    clonedCanvas[i].parentNode.replaceChild(img, clonedCanvas[i]);
  }

  // Drop controls (filters, "load more", pagination) and any spinner.
  clone.querySelectorAll('button, select, input, .spinner, .loading-overlay').forEach(function(el) {
    if (el.parentNode) el.parentNode.removeChild(el);
  });

  // Same stylesheets the dashboard uses — .href is already absolute, so this
  // resolves whether the dashboard is served over http:// or opened as file://.
  var sheets = Array.prototype.map.call(
    document.querySelectorAll('link[rel="stylesheet"]'),
    function(l) { return '<link rel="stylesheet" href="' + escAttr(l.href) + '" />'; }
  ).join('');

  var postId   = currentPostId || '';
  var platform = (document.getElementById('modal-platform') || {}).textContent || '';
  // Chrome/Firefox seed the "Save as PDF" filename from the document title.
  var fileName = 'post-' + (String(postId).replace(/[^A-Za-z0-9_-]/g, '') || 'detail') + '-analysis';

  var doc = '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8" />'
    + '<title>' + escHtml(fileName) + '</title>'
    + sheets
    + '</head><body class="pdf-doc">'
    + '<div class="pdf-head">'
    +   '<div class="pdf-brand">Defense Analysis — Post Detail</div>'
    +   '<div class="pdf-meta">'
    +     '<span>Post ID: ' + escHtml(postId || '—') + '</span>'
    +     (platform ? '<span>' + escHtml(platform) + '</span>' : '')
    +     '<span>Generated ' + escHtml(new Date().toLocaleString()) + '</span>'
    +   '</div>'
    + '</div>'
    + '<div class="pdf-body">' + clone.innerHTML + '</div>'
    + '</body></html>';

  var stale = document.getElementById('pdf-print-frame');
  if (stale && stale.parentNode) stale.parentNode.removeChild(stale);

  var frame = document.createElement('iframe');
  frame.id = 'pdf-print-frame';
  frame.className = 'pdf-print-frame';
  frame.setAttribute('aria-hidden', 'true');
  document.body.appendChild(frame);

  var fdoc = frame.contentWindow.document;
  fdoc.open();
  fdoc.write(doc);
  fdoc.close();

  function cleanup() {
    if (frame && frame.parentNode) frame.parentNode.removeChild(frame);
    frame = null;
  }

  // Let the linked stylesheet and the inline PNGs settle before printing.
  setTimeout(function() {
    if (!frame) return;
    showToast('Choose “Save as PDF” in the print dialog.', 'info');
    try {
      frame.contentWindow.onafterprint = cleanup;
      frame.contentWindow.focus();
      frame.contentWindow.print();
    } catch (err) {
      showToast('Could not open the print dialog: ' + err.message, 'error');
      cleanup();
      return;
    }
    // onafterprint is not fired by every browser — reclaim the frame anyway.
    setTimeout(cleanup, 60000);
  }, 400);
}

// ---- Per-comment sentiment + emotion (full coverage) ---------------------
// One context per rendered comment-insights instance, keyed by a DOM id prefix
// ('modal' for the detail modal, 'rowN' for an expanded results row). This lets
// the same renderer drive several instances at once without id collisions.
var commentCtx = {};   // prefix -> { prefix, postId, sentiment, offset, limit }

// Shared emotion taxonomy → colour + emoji (matches the post emotion chart and
// the backend libs/schemas/output_schema.json taxonomy).
/** Read a CSS custom property. Canvas cannot consume `var()`, so charts resolve
 *  the design tokens once at draw time — keeping styles.css the single source of
 *  truth for colour rather than duplicating hexes here. */
function cssVar(name, fallback) {
  try {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  } catch (e) {
    return fallback;
  }
}

/** Chrome colours for charts, resolved from the blue ramp in styles.css. */
function chartPalette() {
  return {
    accent:    cssVar('--accent', '#2563eb'),
    accentAlt: cssVar('--blue-400', '#60a5fa'),
    accentDeep:cssVar('--blue-800', '#1e40af'),
    muted:     cssVar('--text-muted', '#8aa2c0'),
    border:    cssVar('--border-color', '#d9e6f7'),
    card:      cssVar('--bg-card', '#ffffff'),
    text:      cssVar('--text-primary', '#0f2547'),
    secondary: cssVar('--text-secondary', '#47617f')
  };
}

// Emotion is categorical DATA, so these stay mutually distinguishable rather
// than being harmonised into the blue ramp — see the note atop styles.css.
// `sadness` uses the cyan end of the ramp so it cannot be read as UI chrome.
var EMOTION_META = {
  anger:    { color: '#dc2626', emoji: '😠' },
  sadness:  { color: '#0891b2', emoji: '😢' },
  joy:      { color: '#059669', emoji: '😊' },
  fear:     { color: '#7c3aed', emoji: '😨' },
  disgust:  { color: '#65a30d', emoji: '🤢' },
  surprise: { color: '#d97706', emoji: '😮' },
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
/** Per-entity stance rollup from the watchlist (stance_targets.md).
 *
 * Rendered as its own block, deliberately apart from the sentiment donut: the
 * two answer different questions ("is this comment angry?" vs "who is it angry
 * at?") and conflating them destroys the only distinction the feature exists to
 * make. The `polarity` badge shows the operator's declared stance, because that
 * is an editorial choice and should be visible rather than implicit.
 */
/** How the per-comment emotion labels were produced.
 *
 * Emotion is the free emoji+lexicon heuristic at Stage 1 for EVERY comment, even
 * in real mode where a transformer emotion head is loaded and used for the post.
 * Stage 2 upgrades only the comments it re-labels. That was true and documented
 * in a docstring, and invisible in the UI.
 */
function emotionMixNote(ca) {
  var comments = (ca && ca.comments) || [];
  if (!comments.length) return 'every comment';
  var llm = comments.filter(function(c) { return c.emotion_method === 'llm'; }).length;
  if (!llm) return 'every comment · keyword heuristic';
  if (llm === comments.length) return 'every comment · LLM';
  return 'every comment · ' + llm + ' of ' + comments.length + ' via LLM, rest heuristic';
}

function targetStanceHtml(targets) {
  if (!targets) return '';
  var ids = Object.keys(targets);
  if (!ids.length) return '';

  // Most-mentioned first — that is the ordering an analyst wants.
  ids.sort(function(a, b) { return (targets[b].mentions || 0) - (targets[a].mentions || 0); });

  var rows = ids.map(function(id) {
    var t = targets[id] || {};
    var mentions = t.mentions || 0;
    var opp = t.opposing || 0, sup = t.supportive || 0, neu = t.neutral || 0;
    var w = function(n) { return mentions ? Math.round(n / mentions * 100) : 0; };

    var polarityTag = '';
    if (t.polarity === 'favored') {
      polarityTag = '<span class="tag tag-sm tag-positive" title="declared FAVORED in the watchlist — an editorial choice, not a measurement">favored</span>';
    } else if (t.polarity === 'opposed') {
      polarityTag = '<span class="tag tag-sm tag-negative" title="declared OPPOSED in the watchlist — an editorial choice, not a measurement">opposed</span>';
    } else {
      polarityTag = '<span class="tag tag-sm" title="tracked without a declared polarity — stance is reported, no framing imposed">tracked</span>';
    }

    var methodTag = t.method === 'llm'
      ? '<span class="tag tag-sm" title="context-aware LLM verdict">llm</span>'
      : '<span class="tag tag-sm tag-warning" title="deterministic clause-and-cue fallback, not a model">heuristic</span>';

    var aliases = t.aliases_matched || {};
    var aliasNote = Object.keys(aliases).length
      ? '<div class="text-muted" style="font-size:.68rem;margin-top:2px">matched as: '
        + escHtml(Object.keys(aliases).map(function(a) { return a + ' ×' + aliases[a]; }).join(', '))
        + '</div>'
      : '';

    return '<div class="target-stance-row">'
      + '<div class="target-stance-head">'
      + '<span class="target-stance-name">' + escHtml(t.display || id) + '</span>'
      + polarityTag + ' ' + methodTag
      + '<span class="text-muted" style="margin-left:auto">' + mentions + ' mention'
      + (mentions === 1 ? '' : 's') + '</span>'
      + '</div>'
      + '<div class="split-bar target-stance-bar">'
      + '<span class="split-opposing" style="width:' + w(opp) + '%" title="opposing: ' + opp + '"></span>'
      + '<span class="split-supportive" style="width:' + w(sup) + '%" title="supportive: ' + sup + '"></span>'
      + '<span class="split-neutral" style="width:' + w(neu) + '%" title="neutral: ' + neu + '"></span>'
      + '</div>'
      + '<div class="text-muted" style="font-size:.7rem">'
      + opp + ' opposing · ' + sup + ' supportive · ' + neu + ' neutral</div>'
      + aliasNote
      + '</div>';
  }).join('');

  return '<div class="modal-section target-stance-block">'
    + '<div class="modal-section-title">Stance toward watched entities '
    + '<span class="text-muted" style="font-weight:400;font-size:.72rem">'
    + '— separate from comment sentiment above; the watchlist is a stated bias model'
    + '</span></div>'
    + rows
    + '</div>';
}

function commentInsightsHtml(prefix, r) {
  var ca = (r && r.comment_analysis) || {};
  var coverage = ca.coverage_label || '—';
  var sb = ca.sentiment_breakdown || {};
  // "Text opinion" vs "emoji reactions" as two series (§6.2): an emoji-only
  // reaction is real crowd signal but it is not a written opinion, and blending
  // the two into one bar makes the chart unable to say which it is showing.
  var sub = ca.sentiment_breakdown_substantive || {};
  var reactionOnly = ca.reaction_only || 0;
  var hasSplit = reactionOnly > 0 && (sub.positive || sub.negative || sub.neutral);

  // A thread that was never analysed must say WHY. Absent and zero are different
  // findings and must not look the same — the rule §13.3 applied to reused
  // posts, whose analysis was inherited from a near-duplicate caption while
  // their own comment thread was deliberately left unread.
  var caProv = ca.provenance || {};
  var notAnalysedNote = (!(ca.analyzed || 0) && caProv.note)
    ? '<div class="alert alert-warning" style="margin-top:6px">'
      + '<strong>This thread was not analysed.</strong> ' + escHtml(caProv.note)
      + (caProv.stored_comments
          ? ' (' + escHtml(String(caProv.stored_comments)) + ' comment(s) stored, none labelled.)'
          : '')
      + '</div>'
    : '';

  var html = '<div class="comment-insights">'
    + '<div class="modal-section-title">Comment Sentiment <span class="text-muted">(' + escHtml(coverage) + ')</span></div>'
    + notAnalysedNote
    + '<div id="cs-summary-' + prefix + '">' + commentSummaryBox(ca.summary) + '</div>'
    + (ca.coverage_anomaly
        ? '<div class="text-muted" style="font-size:.75rem;margin-top:4px">⚠ '
          + escHtml(String(ca.coverage_anomaly.analyzed)) + ' stored comments against '
          + escHtml(String(ca.coverage_anomaly.reported_comment_count))
          + ' reported by the platform — upstream mismatch, coverage capped at 100%.</div>'
        : '')
    + '<div class="charts-row" style="margin-top:12px">'
    + '<div>'
    + '<p class="chart-title">Stance toward post</p>'
    + '<div style="display:flex;gap:20px;align-items:center">'
    + '<canvas id="cs-pie-' + prefix + '" width="160" height="160"></canvas>'
    + '<div style="flex:1">'
    + makeLegendItem('Positive', sb.positive || 0, 'var(--color-positive)')
    + makeLegendItem('Negative', sb.negative || 0, 'var(--color-negative)')
    + makeLegendItem('Neutral',  sb.neutral  || 0, 'var(--color-neutral)')
    + (hasSplit
        ? '<div class="text-muted" style="font-size:.72rem;margin-top:6px">'
          + 'Written comments only: '
          + escHtml(String(sub.positive || 0)) + ' / '
          + escHtml(String(sub.negative || 0)) + ' / '
          + escHtml(String(sub.neutral || 0))
          + ' · ' + escHtml(String(reactionOnly)) + ' emoji-only reaction'
          + (reactionOnly === 1 ? '' : 's') + ' included above.</div>'
        : '')
    + '</div></div>'
    + '</div>'
    + '<div>'
    + '<p class="chart-title">Emotion mix <span class="text-muted">('
    + emotionMixNote(ca) + ')</span></p>'
    + '<canvas id="cs-emotion-' + prefix + '" height="120"></canvas>'
    + '</div>'
    + '</div>';

  // ---- Target stance (watchlist) ----
  // The novelty item. A SEPARATE measurement from the sentiment donut above: a
  // comment can be positive in tone while opposing a listed entity, so these are
  // never merged. Absent entirely when no watchlist is configured or nothing
  // was mentioned. See stance_targets.md.
  html += targetStanceHtml(ca.target_stances);

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
    // Label provenance — what actually produced these labels.
    //   llm/model = inferred;  fast = emoji+lexicon heuristic;
    //   stub = the deterministic hash fallback (reproducible, NOT sentiment).
    // Shown as a share so the sentiment chart above can never be read as "the
    // models said this" when most of it came from a keyword list.
    if (mbTotal > 0) {
      var prov = data.provenance || {};
      var inferred = typeof prov.inferred === 'number'
        ? prov.inferred : ((mb.llm || 0) + (mb.model || 0));
      var infPct = Math.round(inferred / mbTotal * 100);
      var mixParts = Object.keys(mb).filter(function(k){ return mb[k]; })
        .map(function(k){ return k + ' ' + mb[k]; });
      analyticsHtml += '<p class="chart-title" style="margin-top:10px">Label provenance '
        + '<span class="text-muted" title="share of comment labels produced by a model or the LLM; the rest come from an emoji+lexicon heuristic or the deterministic stub">('
        + infPct + '% model/LLM)</span></p>'
        + '<div class="split-bar"><span class="split-fast" style="width:' + infPct + '%"></span></div>'
        + '<div class="text-muted" style="font-size:.72rem;margin-top:2px">' + escHtml(mixParts.join(' · ')) + '</div>';
      if (mb.stub) {
        analyticsHtml += '<div class="text-muted" style="font-size:.72rem;margin-top:2px">'
          + escHtml(String(mb.stub)) + ' label' + (mb.stub === 1 ? '' : 's')
          + ' from the deterministic stub — reproducible, but not sentiment.</div>';
      }
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
  return '<span class="chip"><span class="chip-dot" style="background:' + (color || chartPalette().secondary) + '"></span>'
    + '<span class="chip-label">' + escHtml(label) + '</span>'
    + '<span class="chip-val">' + escHtml(String(value)) + '</span></span>';
}

function makeBar(label, frac, color) {
  if (typeof frac !== 'number' || isNaN(frac)) frac = 0;
  var p = Math.max(0, Math.min(100, Math.round(frac * 100)));
  return '<div class="dbar">'
    + '<span class="dbar-label">' + escHtml(label) + '</span>'
    + '<span class="dbar-track"><span class="dbar-fill" style="width:' + p + '%;background:' + (color || chartPalette().accent) + '"></span></span>'
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
  var neg = css.getPropertyValue('--color-negative').trim() || '#dc2626';
  var neu = css.getPropertyValue('--color-neutral').trim() || '#94a3b8';
  var pos = css.getPropertyValue('--color-positive').trim() || '#059669';
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
  // Resolved from the tokens, so the charts follow the theme (including dark
  // mode) without a second copy of the palette living in JS.
  var pal = chartPalette();
  var textColor = pal.muted;
  var trackColor = pal.border;

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

    ctx.fillStyle = d.color || chartPalette().accent;
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
    LIKE:  cssVar('--color-like',  '#1d4ed8'),
    LOVE:  cssVar('--color-love',  '#db2777'),
    HAHA:  cssVar('--color-haha',  '#d97706'),
    WOW:   cssVar('--color-wow',   '#7c3aed'),
    SAD:   cssVar('--color-sad',   '#0891b2'),
    ANGRY: cssVar('--color-angry', '#dc2626'),
    CARE:  cssVar('--color-care',  '#ea580c')
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
      // Unknown reaction type — neutral grey rather than inventing a colour.
      data.push({ label: key, value: reactionBreakdown[key], color: cssVar('--color-neutral', '#94a3b8') });
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
    { label: 'Positive', value: positive, color: cssVar('--color-positive', '#059669') },
    { label: 'Negative', value: negative, color: cssVar('--color-negative', '#dc2626') },
    { label: 'Neutral',  value: neutral,  color: cssVar('--color-neutral',  '#94a3b8') }
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
  ctx.fillStyle = chartPalette().card;
  ctx.fill();

  // Center text
  ctx.fillStyle = chartPalette().text;
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
async function startJobStream(jobId, onEvent) {
  if (!jobId || typeof EventSource === 'undefined') return null;
  stopJobStream(jobId);

  // Single-use ticket rather than the session credential — see sseQuery().
  var url = API_BASE + '/v1/analysis/' + encodeURIComponent(jobId)
    + '/stream' + (await sseQuery());

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

/** Close every per-job stream — used when the session dies. */
function stopAllJobStreams() {
  Object.keys(sseStreams).forEach(stopJobStream);
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

    // ---- Embedding clusters ----
    // The LLM cost lever (architecture.md §5): one LLM-B call per cluster rather
    // than one per post. These were computed and paid for on every grounded
    // report but never reached any response — ReportResponse did not declare the
    // field, so the response model stripped it (§13.1). The "Topic Clusters"
    // block above is the SQL aggregate and costs nothing; this one is the spend.
    if (rep.embedding_clusters && rep.embedding_clusters.length > 0) {
      html += '<div class="modal-section-title" style="margin-top:16px">Embedding Clusters'
        + ' <span class="tag tag-sm" title="one LLM-B call per cluster, not per post">llm</span>'
        + '</div>';
      // A stub vector is a hash, so the clusters group posts arbitrarily and each
      // summary describes an arbitrary set. Say so above the summaries rather
      // than letting them read as findings (§13.2).
      if (rep.embedding_clusters_are_stub) {
        html += '<p class="warn-note" style="font-size:0.8125rem;color:var(--warn,#b45309);margin-bottom:8px">'
          + 'Clustered over stub (hash) embeddings — these groupings are arbitrary and the '
          + 'summaries are not meaningful. Load a real embedding model to make this a finding.'
        + '</p>';
      }
      rep.embedding_clusters.forEach(function(c) {
        var sent = c.top_sentiment || 'neutral';
        html += '<div class="cluster-item">'
          + '<div class="cluster-header">'
          + '<span class="cluster-label">' + escHtml(c.cluster_id || 'cluster') + '</span>'
          + '<span class="badge badge-' + escAttr(sent) + '">' + escHtml(sent) + '</span>'
          + '</div>'
          + '<span class="cluster-count">' + (c.size || 0) + ' posts</span>'
          + (c.representative_post_id
              ? '<span class="cluster-count" style="margin-left:8px">rep: ' + escHtml(c.representative_post_id) + '</span>'
              : '')
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

    // A stub vector is a hash of the text, so kNN over stub rows returns
    // ARBITRARY neighbours — with scores that look exactly as plausible as real
    // ones. Say so rather than letting the ranking imply meaning it lacks.
    var stubHits = results.filter(function(r) { return r.embedding_is_stub; }).length;
    if (semantic && stubHits) {
      html += '<div class="alert alert-warning" style="margin-bottom:12px">'
        + '<strong>' + stubHits + ' of ' + results.length + ' result(s) ranked on stub embeddings.</strong> '
        + 'These vectors are deterministic hashes of the text, not semantic — the ordering '
        + 'is arbitrary even though the scores look plausible. Load a real embedding model, '
        + 'or set <code>EMBEDDING_ALLOW_STUB=false</code> to refuse writing them.'
        + '</div>';
    }

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

/** Reflect the session in the header.
 *
 * `label` overrides the derived text (verifySession passes the server's own
 * `sub`, which is the authoritative answer to "who am I"). Omit it and the
 * caller's existing boolean-only calls keep working.
 */
function updateAuthStatus(connected, label) {
  var el = document.getElementById('header-auth-status');
  if (el) {
    var text;
    if (!connected) {
      text = 'Not authenticated';
    } else if (label) {
      text = label;
    } else if (authUser) {
      text = authUser;
    } else if (apiKey && !authToken) {
      text = 'API key';
    } else {
      text = 'Authenticated';
    }
    el.textContent = text;
    el.className = 'auth-status ' + (connected ? 'connected' : 'disconnected');
    el.title = connected ? 'Signed in — click Sign out to clear credentials' : 'No credential stored';
  }

  // Sign out is only meaningful when there is something to clear.
  var logoutBtn = document.getElementById('header-logout-btn');
  if (logoutBtn) logoutBtn.classList.toggle('hidden', !connected);
  var loginBtn = document.getElementById('header-login-btn');
  if (loginBtn) loginBtn.textContent = connected ? 'Switch account' : 'Sign in / Sign up';
}

function updateStatusText(id, text) {
  var el = document.getElementById(id);
  if (el) el.textContent = text;
}

/* ============================================================
   Login modal
   ============================================================ */
function showLoginModal(mode) {
  var overlay = document.getElementById('login-overlay');
  if (overlay) overlay.classList.remove('hidden');
  setAuthMode(mode || 'signin');
  // Asked every time the modal opens, not once at boot: signup availability
  // flips the moment the first account is created, and a cached "bootstrap: true"
  // would keep promising admin rights that the next signup will not get.
  loadAuthConfig();
}

function hideLoginModal() {
  var overlay = document.getElementById('login-overlay');
  if (overlay) overlay.classList.add('hidden');
}

/** Switch the login card between signing in and registering.
 *
 * One card with two forms rather than two modals: the user who lands on the
 * wrong one is one click from the other, and both share the card's framing.
 */
function setAuthMode(mode) {
  if (mode === 'signup') {
    var tab = document.getElementById('auth-mode-signup');
    if (tab && tab.disabled) mode = 'signin';   // server says no; do not offer it
  }
  authMode = mode === 'signup' ? 'signup' : 'signin';
  var isSignup = authMode === 'signup';

  var loginForm  = document.getElementById('login-form');
  var signupForm = document.getElementById('signup-form');
  if (loginForm)  loginForm.classList.toggle('hidden', isSignup);
  if (signupForm) signupForm.classList.toggle('hidden', !isSignup);

  ['signin', 'signup'].forEach(function(m) {
    var t = document.getElementById('auth-mode-' + m);
    if (!t) return;
    var active = (m === authMode);
    t.classList.toggle('active', active);
    t.setAttribute('aria-selected', active ? 'true' : 'false');
  });

  var title = document.getElementById('login-title');
  if (title) title.textContent = isSignup ? 'Create Account' : 'Sign In';
  var sub = document.getElementById('login-subtitle');
  if (sub) {
    sub.textContent = isSignup
      ? 'Pick a username and password. The server assigns your tenant and role.'
      : 'Enter your credentials or API key to connect to the API.';
  }

  // Clear the other form's error so a stale message cannot appear to belong to
  // the form now on screen.
  var stale = document.getElementById(isSignup ? 'login-error' : 'signup-error');
  if (stale) stale.textContent = '';

  var focusEl = document.getElementById(isSignup ? 'signup-username' : 'login-apikey');
  if (focusEl) setTimeout(function() { focusEl.focus(); }, 50);
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

async function handleSignupSubmit(evt) {
  evt.preventDefault();
  var username  = (document.getElementById('signup-username').value || '').trim().toLowerCase();
  var password  = document.getElementById('signup-password').value || '';
  var password2 = document.getElementById('signup-password2').value || '';
  var errEl = document.getElementById('signup-error');
  var btn   = document.getElementById('signup-submit-btn');
  var minLen = (authConfig && authConfig.min_password_length) || 8;

  function fail(msg) {
    if (errEl) errEl.textContent = msg;
  }
  if (errEl) errEl.textContent = '';

  // Client-side checks mirror the server's rules — they do not replace them.
  // Confirm-password is the one check that is client-only by nature: the server
  // never sees the second field, so a typo caught here is a locked-out account
  // avoided.
  if (!username) return fail('Choose a username.');
  if (!/^[a-z0-9][a-z0-9._-]{2,63}$/.test(username)) {
    return fail('Username must be 3–64 characters: lowercase letters, digits, . _ or -, starting with a letter or digit.');
  }
  if (password.length < minLen) return fail('Password must be at least ' + minLen + ' characters.');
  if (password !== password2) return fail('The two passwords do not match.');

  setLoading(btn, true);
  try {
    var data = await signup(username, password);
    hideLoginModal();
    showToast(
      'Account created — signed in as ' + (data.username || username)
        + (data.role === 'admin' ? ' (administrator)' : ''),
      'success'
    );
    refreshTab(currentTab);
    loadLlmConfig();
    loadNlpConfig();
  } catch (err) {
    fail(err.message);
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

async function connectPipelineLive() {
  if (pipelineES || pipelinePollTimer) return;  // already streaming
  setPipelineLiveStatus('connecting');

  if (typeof EventSource !== 'undefined') {
    var url = API_BASE + '/v1/pipeline/stream' + (await sseQuery());
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
   CHAT TAB — free-form chatbot over the pipeline LLM
   Streams /v1/chat/stream (POST SSE via fetch; EventSource can't POST),
   with a one-shot /v1/chat fallback when streaming is unavailable.
   ============================================================ */

var CHAT_AVATAR = {
  assistant: '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M10 1.4l1.8 4.3 4.3 1.8-4.3 1.8L10 13.6 8.2 9.3 3.9 7.5l4.3-1.8L10 1.4z"/><circle cx="15.5" cy="14.5" r="1.6"/><circle cx="4.6" cy="14.2" r="1.1"/></svg>',
  user: '<svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M10 9a3 3 0 100-6 3 3 0 000 6zm-7 9a7 7 0 1114 0H3z" clip-rule="evenodd"/></svg>'
};

/** The welcome/empty state shown when there are no messages. */
function chatEmptyHTML() {
  return '<div class="chat-empty" id="chat-empty">'
    + '<div class="chat-empty-icon" aria-hidden="true">' + CHAT_AVATAR.assistant + '</div>'
    + '<div class="chat-empty-title">Ask me anything</div>'
    + '<div class="chat-empty-sub">A general-purpose assistant powered by the same LLM as the pipeline. '
    + 'It doesn’t see your analyzed posts — use the <strong>Agents</strong> tab for questions about your data.</div>'
    + '<div class="chat-suggestions">'
    + '<button class="chat-suggestion" type="button">What can you help me with?</button>'
    + '<button class="chat-suggestion" type="button">Explain the trade-offs of local vs cloud LLMs</button>'
    + '<button class="chat-suggestion" type="button">Write a haiku about data pipelines</button>'
    + '<button class="chat-suggestion" type="button">Give me 3 social-media monitoring tips</button>'
    + '</div></div>';
}

/** Minimal, safe Markdown → HTML for assistant replies (bold/italic/code/
 * links/lists). Everything is HTML-escaped before formatting tags are added,
 * so model output can never inject markup. */
function renderMarkdown(src) {
  if (src == null) return '';
  src = String(src);
  var blocks = [], inlines = [];
  // Pull out fenced code blocks, then inline code, replacing each with an
  // ASCII sentinel (contents escaped now, restored verbatim at the end).
  src = src.replace(/```[ \t]*[\w+-]*\n?([\s\S]*?)```/g, function(_, code) {
    blocks.push('<pre class="chat-code"><code>' + escHtml(code.replace(/\n$/, '')) + '</code></pre>');
    return '@@BLK' + (blocks.length - 1) + '@@';
  });
  src = src.replace(/`([^`\n]+)`/g, function(_, code) {
    inlines.push('<code class="chat-inline-code">' + escHtml(code) + '</code>');
    return '@@INL' + (inlines.length - 1) + '@@';
  });
  src = escHtml(src);
  src = src.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  src = src.replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>');
  src = src.replace(/__([^_]+?)__/g, '<strong>$1</strong>');
  src = src.replace(/(^|[^*])\*([^*\n]+?)\*/g, '$1<em>$2</em>');

  var lines = src.split('\n'), out = '', listType = null, para = [];
  function flushPara() { if (para.length) { out += '<p>' + para.join('<br>') + '</p>'; para = []; } }
  function closeList() { if (listType) { out += '</' + listType + '>'; listType = null; } }
  for (var k = 0; k < lines.length; k++) {
    var t = lines[k].trim();
    if (t === '') { flushPara(); closeList(); continue; }
    if (/^@@BLK\d+@@$/.test(t)) { flushPara(); closeList(); out += t; continue; }
    var ul = t.match(/^[-*]\s+(.*)$/), ol = t.match(/^\d+\.\s+(.*)$/), h = t.match(/^#{1,6}\s+(.*)$/);
    if (ul) { flushPara(); if (listType !== 'ul') { closeList(); out += '<ul class="chat-list">'; listType = 'ul'; } out += '<li>' + ul[1] + '</li>'; continue; }
    if (ol) { flushPara(); if (listType !== 'ol') { closeList(); out += '<ol class="chat-list">'; listType = 'ol'; } out += '<li>' + ol[1] + '</li>'; continue; }
    if (h)  { flushPara(); closeList(); out += '<p class="chat-h"><strong>' + h[1] + '</strong></p>'; continue; }
    closeList(); para.push(t);
  }
  flushPara(); closeList();
  out = out.replace(/@@BLK(\d+)@@/g, function(_, i) { return blocks[+i]; });
  out = out.replace(/@@INL(\d+)@@/g, function(_, i) { return inlines[+i]; });
  return out;
}

/** Render the final assistant text as Markdown and attach a Copy button. */
function finalizeAssistant(refs, text) {
  if (!refs) return;
  refs.body.innerHTML = renderMarkdown(text);
  addCopyButton(refs, text);
  scrollChatToBottom();
}

function addCopyButton(refs, text) {
  if (!refs || !refs.meta) return;
  var btn = document.createElement('button');
  btn.className = 'chat-copy';
  btn.type = 'button';
  btn.textContent = 'Copy';
  btn.addEventListener('click', function() {
    try {
      navigator.clipboard.writeText(text).then(function() {
        btn.textContent = 'Copied';
        setTimeout(function() { btn.textContent = 'Copy'; }, 1200);
      });
    } catch (e) { /* clipboard unavailable */ }
  });
  refs.meta.appendChild(btn);
}

/** Opening the tab: sync the backend hint, load the model list, focus composer. */
function initChatTab() {
  updateChatBackendHint();
  loadLlmConfig().then(function() { updateChatBackendHint(); populateChatModelSelect(); });
  refreshChatModels();
  var input = document.getElementById('chat-input');
  if (input) setTimeout(function() { input.focus(); }, 30);
}

/** Header subtitle: which backend a message will actually use. */
function updateChatBackendHint() {
  var hint = document.getElementById('chat-backend-hint');
  if (!hint) return;
  if (chatBackend === 'auto') {
    var active = (llmConfig && llmConfig.backend) ? llmBackendLabel(llmConfig.backend) : 'server default';
    hint.textContent = 'General assistant · Auto (' + active + ')';
  } else {
    hint.textContent = 'General assistant · ' + llmBackendLabel(chatBackend) + ' (forced)';
  }
}

function setChatBackend(backend) {
  chatBackend = backend;
  document.querySelectorAll('#chat-backend-seg .llm-segment').forEach(function(b) {
    b.classList.toggle('active', b.getAttribute('data-backend') === backend);
  });
  updateChatBackendHint();
  // Model lists differ per backend — reset the pick and repopulate for this one.
  chatModel = '';
  populateChatModelSelect();
}

/** Which backend's model list to show: the forced one, or (Auto) the active one. */
function chatActiveBackend() {
  if (chatBackend !== 'auto') return chatBackend;
  if (chatModelsData && chatModelsData.active_backend) return chatModelsData.active_backend;
  if (llmConfig && llmConfig.backend) return llmConfig.backend;
  return 'local';
}

/** Fetch the per-backend model catalogue, then fill the picker. */
async function refreshChatModels() {
  try {
    chatModelsData = await apiCall('/v1/chat/models', { method: 'GET' });
  } catch (e) {
    chatModelsData = null;  // endpoint absent / unreachable — picker stays at Default
  }
  populateChatModelSelect();
}

/** Populate the model <select> for the currently-active backend. */
function populateChatModelSelect() {
  var sel = document.getElementById('chat-model');
  if (!sel) return;
  var be = chatActiveBackend();
  var info = (chatModelsData && chatModelsData.backends && chatModelsData.backends[be]) || null;
  var models = (info && info.models) || [];
  var def = (info && info.default) || '';

  var html = '<option value="">Default' + (def ? ' (' + escHtml(def) + ')' : ' model') + '</option>';
  for (var i = 0; i < models.length; i++) {
    html += '<option value="' + escAttr(models[i]) + '">' + escHtml(models[i]) + '</option>';
  }
  sel.innerHTML = html;

  // Keep the current pick if it's still offered by this backend, else default.
  if (chatModel && models.indexOf(chatModel) !== -1) {
    sel.value = chatModel;
  } else {
    chatModel = '';
    sel.value = '';
  }
}

function clearChat() {
  chatHistory = [];
  var box = document.getElementById('chat-messages');
  if (box) box.innerHTML = chatEmptyHTML();
}

/** Append a message row (avatar + bubble + meta); returns refs so a streaming
 * reply can update the bubble in place. */
function appendChatBubble(role, text) {
  var box = document.getElementById('chat-messages');
  if (!box) return null;
  var empty = box.querySelector('.chat-empty');
  if (empty) empty.parentNode.removeChild(empty);

  var isUser = role === 'user';

  var wrap = document.createElement('div');
  wrap.className = 'chat-msg ' + (isUser ? 'user' : 'assistant');

  var avatar = document.createElement('div');
  avatar.className = 'chat-avatar ' + (isUser ? 'user' : 'assistant');
  avatar.setAttribute('aria-hidden', 'true');
  avatar.innerHTML = isUser ? CHAT_AVATAR.user : CHAT_AVATAR.assistant;

  var col = document.createElement('div');
  col.className = 'chat-col';

  var bubble = document.createElement('div');
  bubble.className = 'chat-bubble';
  var body = document.createElement('div');
  body.className = 'chat-bubble-text';
  if (text != null) body.textContent = text;
  bubble.appendChild(body);
  col.appendChild(bubble);

  var meta = document.createElement('div');
  meta.className = 'chat-meta';
  col.appendChild(meta);

  wrap.appendChild(avatar);
  wrap.appendChild(col);
  box.appendChild(wrap);
  box.scrollTop = box.scrollHeight;
  return { wrap: wrap, bubble: bubble, body: body, meta: meta };
}

function setChatTyping(refs) {
  if (refs) refs.body.innerHTML = '<span class="chat-typing"><span></span><span></span><span></span></span>';
}

function scrollChatToBottom() {
  var box = document.getElementById('chat-messages');
  if (box) box.scrollTop = box.scrollHeight;
}

function setChatBusy(busy) {
  chatBusy = busy;
  var btn = document.getElementById('chat-send-btn');
  if (btn) btn.disabled = busy;  // the send button is an icon; just disable it
}

function autoGrowChatInput() {
  var input = document.getElementById('chat-input');
  if (!input) return;
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 160) + 'px';
}

function showChatError(refs, msg, keepText) {
  if (!refs) { showToast(msg, 'error'); return; }
  refs.bubble.classList.add('chat-error');
  if (keepText) {
    refs.meta.textContent = '⚠ ' + msg;
  } else {
    refs.body.textContent = '⚠ ' + msg;
  }
}

/** Parse one SSE frame ("event: x\ndata: y") into {event, data}. */
function parseSseFrame(frame) {
  var event = 'message';
  var data = '';
  frame.split('\n').forEach(function(line) {
    if (line.indexOf('event:') === 0) event = line.slice(6).trim();
    else if (line.indexOf('data:') === 0) data += line.slice(5).replace(/^ /, '');
  });
  return { event: event, data: data };
}

/** POST /v1/chat/stream and dispatch each SSE frame to onEvent(evt, data).
 * Returns quietly (no events) if the browser can't read the response stream —
 * the caller then uses the non-streaming fallback. */
async function streamChat(body, onEvent) {
  var headers = { 'Content-Type': 'application/json' };
  if (authToken) headers['Authorization'] = 'Bearer ' + authToken;
  else if (apiKey) headers['X-API-Key'] = apiKey;

  var resp = await fetch(API_BASE + '/v1/chat/stream', {
    method: 'POST', headers: headers, body: JSON.stringify(body)
  });

  if (resp.status === 401) {
    authToken = null; localStorage.removeItem('auth_token'); updateAuthStatus(false);
    throw new Error('Unauthorized — please log in again.');
  }
  if (!resp.ok) {
    var errText = '';
    try { errText = await resp.text(); } catch (e) { /* ignore */ }
    var msg = 'API error ' + resp.status;
    try {
      var j = JSON.parse(errText);
      if (j && j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch (e) { if (errText) msg = errText; }
    throw new Error(msg);
  }
  if (!resp.body || !resp.body.getReader) return;  // no streaming → caller falls back

  var reader = resp.body.getReader();
  var decoder = new TextDecoder();
  var buffer = '';
  while (true) {
    var chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    var frames = buffer.split('\n\n');
    buffer = frames.pop();  // trailing partial frame stays buffered
    for (var i = 0; i < frames.length; i++) {
      var p = parseSseFrame(frames[i]);
      if (p.data !== '') onEvent(p.event, p.data);
    }
  }
  if (buffer.trim()) {
    var last = parseSseFrame(buffer);
    if (last.data !== '') onEvent(last.event, last.data);
  }
}

/** Non-streaming fallback: one POST /v1/chat, render the whole reply. */
async function chatFallback(body, refs) {
  var result = await apiCall('/v1/chat', { method: 'POST', body: JSON.stringify(body) });
  var reply = (result && result.reply) || '';
  if (refs && reply) {
    var meta = llmBackendLabel(result.backend) + ' · ' + (result.model || '');
    if (result.usage && result.usage.total_tokens) meta += ' · ' + result.usage.total_tokens + ' tok';
    refs.meta.textContent = meta;
    finalizeAssistant(refs, reply);
  }
  return reply;
}

async function sendChatMessage() {
  if (chatBusy) return;
  var input = document.getElementById('chat-input');
  var text = input ? input.value.trim() : '';
  if (!text) return;

  input.value = '';
  autoGrowChatInput();
  appendChatBubble('user', text);
  chatHistory.push({ role: 'user', content: text });

  var refs = appendChatBubble('assistant', null);
  setChatTyping(refs);
  setChatBusy(true);

  // Send only the recent tail — the server caps history at 50 messages.
  var body = { messages: chatHistory.slice(-40), backend: chatBackend };
  if (chatModel) body.model = chatModel;  // omit → backend's default model
  var acc = '';
  var gotDelta = false;
  var gotAnyEvent = false;
  var metaText = '';

  try {
    await streamChat(body, function(evt, data) {
      gotAnyEvent = true;
      var payload = {};
      try { payload = JSON.parse(data); } catch (e) { /* keep {} */ }
      if (evt === 'meta') {
        metaText = llmBackendLabel(payload.backend) + ' · ' + (payload.model || '');
        if (refs) refs.meta.textContent = metaText;
      } else if (evt === 'delta') {
        if (!gotDelta) { gotDelta = true; if (refs) refs.body.textContent = ''; }
        acc += (payload.content || '');
        if (refs) refs.body.textContent = acc;
        scrollChatToBottom();
      } else if (evt === 'done') {
        if (payload.usage && payload.usage.total_tokens && refs) {
          refs.meta.textContent = metaText + ' · ' + payload.usage.total_tokens + ' tok';
        }
      } else if (evt === 'error') {
        throw new Error(payload.error || 'stream error');
      }
    });

    if (!gotAnyEvent) {
      acc = await chatFallback(body, refs);      // streaming unsupported → one-shot
    } else if (acc) {
      finalizeAssistant(refs, acc);              // Markdown-render the streamed text
    }

    if (acc) {
      chatHistory.push({ role: 'assistant', content: acc });
    } else if (refs && !refs.bubble.classList.contains('chat-error')) {
      refs.body.textContent = '(no response)';
    }

  } catch (err) {
    // Failed before any token → try the one-shot endpoint once more.
    if (!gotDelta) {
      try {
        acc = await chatFallback(body, refs);
        if (acc) chatHistory.push({ role: 'assistant', content: acc });
      } catch (err2) {
        showChatError(refs, err2.message || String(err2), false);
      }
    } else {
      if (acc) finalizeAssistant(refs, acc);     // keep the partial answer, rendered
      showChatError(refs, err.message || String(err), true);
    }
  } finally {
    setChatBusy(false);
  }
}

/* ============================================================
   TRACE TAB — follow ONE post through the real pipeline
   ------------------------------------------------------------
   The Pipeline tab shows aggregate stage pressure (how many posts are
   waiting / in-flight per queue). This tab answers a different
   question: what happened to *this* post, layer by layer, as it
   happened.

   Mechanism:
     1. POST /v1/analysis/run for a single post_id -> { analysis_id }
     2. Subscribe to /v1/analysis/{analysis_id}/stream (SSE)
     3. Workers publish one `stage` frame per layer boundary onto
        analysis:progress:{job_id} (see libs/progress.py); render each
        as it lands.
     4. On the terminal `done` frame, fetch the canonical result.

   The first frame (ingestion) is published while step 1 is still
   running, so it can never be seen live — the API replays the buffered
   frames on connect and each carries a `seq` we dedupe on.
   ============================================================ */

// Layers in pipeline order, with the stream each one reads from. Mirrors
// STAGES in libs/progress.py and the chain in architecture.md §3.
var TRACE_LAYERS = [
  { key: 'ingest',    label: 'Ingestion',      queue: 'ingestion:queue',  note: 'normalize · OCR · dedup · enqueue' },
  { key: 'stage1',    label: 'Stage 1 · NLP',  queue: 'nlp:stage1:queue', note: 'text · vision · fuse · comments' },
  { key: 'router',    label: 'Router',         queue: 'router:queue',     note: 'six rules decide if the LLM runs' },
  { key: 'stage2',    label: 'Stage 2 · LLM',  queue: 'llm:stage2:queue', note: 'summary · post type · insight' },
  { key: 'assembler', label: 'Assembler',      queue: 'assembler:queue',  note: 'merge · validate · fan-out' }
];

// Field render order per layer. Anything the worker sends that isn't listed
// still renders, just after these — so adding a detail key server-side never
// requires a dashboard change.
var TRACE_FIELD_ORDER = {
  ingest: ['platform', 'media_type', 'caption_chars', 'photo_count', 'comment_rows',
           'comment_count', 'coverage', 'content_hash', 'baseline_sentiment'],
  stage1: ['language', 'script', 'is_banglish', 'text_sentiment', 'image_sentiment',
           'overall_sentiment', 'sentiment_score', 'emotion', 'topics', 'keywords',
           'toxicity_score', 'entity_count', 'embedding_dims', 'comments_analyzed',
           'comment_coverage', 'comment_breakdown', 'confidence',
           'post_type', 'post_type_confidence'],
  router: ['use_llm', 'reasons', 'task_flags'],
  stage2: ['post_type', 'post_type_confidence', 'post_summary_preview', 'post_summary_chars',
           'post_summary_lang', 'post_summary_source', 'grounding', 'role',
           'llm_backend', 'llm_model', 'comment_summary_added', 'tasks', 'target_lang', 'backend'],
  assembler: ['schema_version', 'schema_valid', 'top_level_keys', 'confidence',
              'overall_sentiment', 'post_type', 'llm_used', 'stage1_ms', 'stage2_ms',
              'embedding_stored', 'writes']
};

var traceES = null;          // EventSource for the traced job
var traceJobId = null;
var tracePostId = null;
var traceSeen = {};          // seq -> true, so a replayed frame isn't drawn twice
var traceState = {};         // layer key -> { status, ms, detail }
var traceTape = [];          // every frame, in arrival order
var traceStartedAt = 0;
var traceTimer = null;       // elapsed-clock interval

function setTraceStatus(state, text) {
  var el = document.getElementById('trace-status');
  if (!el) return;
  var map = {
    idle:     ['idle', ''],
    starting: ['starting…', 'tag-warning'],
    live:     ['live', 'tag-success'],
    done:     ['complete', 'tag-success'],
    failed:   ['failed', 'tag-danger'],
    timeout:  ['stream timed out', 'tag-warning']
  };
  var m = map[state] || map.idle;
  el.textContent = text || m[0];
  el.className = 'tag tag-sm ' + m[1];
}

/** Populate the post picker from the most recent analysis results. */
async function loadTrace(background) {
  var sel = document.getElementById('trace-post');
  if (!sel) return;

  // Don't clobber a running trace's selection on a background refresh.
  if (background && traceES) return;

  try {
    var data = await apiCall('/v1/analysis/latest?limit=50&include=results', { method: 'GET' });
    var results = (data && data.results) || (Array.isArray(data) ? data : []);

    if (!results.length) {
      sel.innerHTML = '<option value="">No stored posts — upload some on the Posts tab first</option>';
      return;
    }

    var keep = sel.value;
    sel.innerHTML = results.map(function (r) {
      var txt = (r.post_text || '').replace(/\s+/g, ' ').trim();
      var label = (r.media_type || 'POST') + ' · '
        + (txt ? txt.slice(0, 60) + (txt.length > 60 ? '…' : '') : '(no caption)');
      return '<option value="' + escHtml(r.post_id) + '">' + escHtml(label) + '</option>';
    }).join('');
    if (keep) sel.value = keep;

  } catch (err) {
    sel.innerHTML = '<option value="">Could not load posts: ' + escHtml(err.message) + '</option>';
  }
}

/** Reset the tab to its empty state and drop any open stream. */
function clearTrace() {
  stopTrace();
  traceJobId = null;
  tracePostId = null;
  traceSeen = {};
  traceState = {};
  traceTape = [];
  traceStartedAt = 0;

  var meta = document.getElementById('trace-meta');
  if (meta) meta.innerHTML = '';
  var rail = document.getElementById('trace-rail');
  if (rail) rail.innerHTML = '';
  var note = document.getElementById('trace-rail-note');
  if (note) note.textContent = 'Nothing traced yet — pick a post and press Run Trace.';
  var elapsed = document.getElementById('trace-elapsed');
  if (elapsed) elapsed.textContent = '';
  var tape = document.getElementById('trace-tape');
  if (tape) tape.innerHTML = '<div class="table-empty">No frames yet.</div>';
  var card = document.getElementById('trace-result-card');
  if (card) card.hidden = true;
  setTraceStatus('idle');
}

function stopTrace() {
  if (traceES) {
    try { traceES.close(); } catch (e) { /* noop */ }
    traceES = null;
  }
  if (traceTimer) { clearInterval(traceTimer); traceTimer = null; }
}

/** Kick off a real pipeline run for the selected post and follow it. */
async function runTrace() {
  var sel = document.getElementById('trace-post');
  var btn = document.getElementById('trace-run');
  var postId = sel ? sel.value : '';

  if (!postId) {
    showToast('Pick a post to trace first.', 'warning');
    return;
  }

  clearTrace();
  tracePostId = postId;
  if (btn) btn.disabled = true;
  setTraceStatus('starting');

  // Seed the rail so every layer is visible as "waiting" before anything runs.
  TRACE_LAYERS.forEach(function (l) { traceState[l.key] = { status: 'idle' }; });
  renderTraceRail();

  var wantSummary = !!(document.getElementById('trace-want-summary') || {}).checked;

  try {
    var resp = await apiCall('/v1/analysis/run', {
      method: 'POST',
      body: JSON.stringify({
        post_ids: [postId],
        options: { want_summary: wantSummary }
      })
    });

    traceJobId = resp.analysis_id || resp.id || resp.job_id;
    if (!traceJobId) throw new Error('No analysis_id in the run response');

    traceStartedAt = Date.now();
    traceTimer = setInterval(renderTraceElapsed, 200);
    renderTraceMeta(resp);
    openTraceStream(traceJobId);
    setTraceStatus('live');

  } catch (err) {
    setTraceStatus('failed');
    var note = document.getElementById('trace-rail-note');
    if (note) note.textContent = 'Could not start: ' + err.message;
    showToast('Trace failed to start: ' + err.message, 'error');
    if (btn) btn.disabled = false;
  }
}

async function openTraceStream(jobId) {
  if (typeof EventSource === 'undefined') {
    setTraceStatus('failed', 'no EventSource');
    return;
  }
  var url = API_BASE + '/v1/analysis/' + encodeURIComponent(jobId)
    + '/stream' + (await sseQuery());

  try {
    traceES = new EventSource(url);
  } catch (e) {
    setTraceStatus('failed', 'stream error');
    return;
  }

  traceES.addEventListener('stage', function (evt) { onTraceFrame(evt, 'stage'); });
  traceES.addEventListener('progress', function (evt) { onTraceFrame(evt, 'progress'); });
  traceES.addEventListener('done', function (evt) { onTraceFrame(evt, 'done'); });
  traceES.addEventListener('timeout', function () {
    setTraceStatus('timeout');
    finishTrace();
  });
  traceES.addEventListener('error', function () {
    // EventSource retries on its own unless the socket is closed for good.
    if (traceES && traceES.readyState === EventSource.CLOSED) {
      setTraceStatus('failed', 'stream closed');
      finishTrace();
    }
  });
}

function onTraceFrame(evt, kind) {
  var f = {};
  try { f = JSON.parse(evt.data); } catch (e) { return; }

  // A frame can arrive twice — once replayed from the buffer, once live.
  if (f.seq != null) {
    if (traceSeen[f.seq]) return;
    traceSeen[f.seq] = true;
  }

  // A multi-post job would interleave; this tab traces one post, so ignore
  // frames about any other.
  if (f.post_id && tracePostId && f.post_id !== tracePostId) return;

  traceTape.push({ kind: kind, frame: f, at: Date.now() });

  if (kind === 'stage' && f.stage) {
    var prev = traceState[f.stage] || {};
    traceState[f.stage] = {
      status: f.status || 'done',
      ms: (f.ms != null) ? f.ms : prev.ms,
      detail: Object.assign({}, prev.detail || {}, f.detail || {}),
      replay: !!f.replay
    };
    // A layer reporting in means every earlier layer must have finished, even
    // if its frame was lost (buffer trimmed, worker restarted mid-flight).
    var idx = TRACE_LAYERS.map(function (l) { return l.key; }).indexOf(f.stage);
    TRACE_LAYERS.slice(0, Math.max(idx, 0)).forEach(function (l) {
      var s = traceState[l.key];
      if (s && (s.status === 'idle' || s.status === 'running')) s.status = 'done';
    });
    renderTraceRail();
  }

  renderTraceTape();

  if (kind === 'done' || f.event === 'done') {
    setTraceStatus('done');
    finishTrace();
    loadTraceResult();
  } else if (f.event === 'error' || f.error) {
    setTraceStatus('failed');
    finishTrace();
  }
}

function finishTrace() {
  stopTrace();
  renderTraceElapsed();
  var btn = document.getElementById('trace-run');
  if (btn) btn.disabled = false;
  // Any layer still marked running never reported a terminal frame.
  TRACE_LAYERS.forEach(function (l) {
    var s = traceState[l.key];
    if (s && s.status === 'running') s.status = 'stalled';
  });
  renderTraceRail();
}

function renderTraceElapsed() {
  var el = document.getElementById('trace-elapsed');
  if (!el || !traceStartedAt) return;
  var secs = (Date.now() - traceStartedAt) / 1000;
  el.textContent = secs.toFixed(1) + 's wall clock';
}

function renderTraceMeta(resp) {
  var el = document.getElementById('trace-meta');
  if (!el) return;
  el.innerHTML =
    '<span class="trace-meta-item"><span class="trace-meta-k">job</span> <code>' + escHtml(traceJobId) + '</code></span>'
    + '<span class="trace-meta-item"><span class="trace-meta-k">post</span> <code>' + escHtml(tracePostId) + '</code></span>'
    + '<span class="trace-meta-item"><span class="trace-meta-k">posts queued</span> ' + escHtml(String(resp.post_count != null ? resp.post_count : 1)) + '</span>'
    + '<span class="trace-meta-item trace-meta-hint">Frames arrive from the workers themselves — nothing here is simulated.</span>';

  var note = document.getElementById('trace-rail-note');
  if (note) note.textContent = 'Following job ' + traceJobId + ' — each layer fills in as its worker reports.';
}

/** Pretty-print one detail value for the layer cards. */
/** "N of M labels came from a model or LLM" — the one-line provenance summary.
 *
 * A sentiment chart that cannot say what produced its numbers is the §4 failure
 * in miniature. Most comment labels are heuristic in the default configuration.
 */
function provenanceSummary(ca) {
  var prov = (ca && ca.provenance) || {};
  if (!prov.total) return '<span class="trace-null">—</span>';
  var cls = prov.inferred_share >= 0.5 ? 'trace-str' : 'trace-warn';
  return '<span class="' + cls + '">' + Math.round((prov.inferred_share || 0) * 100)
       + '% model/LLM</span> <span class="text-muted">(' + prov.inferred + ' of '
       + prov.total + '; ' + prov.heuristic + ' heuristic)</span>';
}

/** Near-duplicate reuse — "no model ran for this post" in one line.
 *
 * A reused post's stage timings are 0 and `llm_used` is false, which reads as a
 * cheap successful analysis rather than as an inherited one. Only
 * `processing.reused_from` distinguishes them (§13.3).
 */
function reuseSummary(proc) {
  var reuse = (proc || {}).reused_from;
  if (!reuse) return '<span class="trace-null">no — analysed directly</span>';
  return '<span class="trace-warn">reused from ' + escHtml(String(reuse.source_post_id || '?'))
       + '</span> <span class="text-muted">(similarity '
       + escHtml(String(reuse.similarity != null ? reuse.similarity : '?'))
       + '; ' + escHtml(String(reuse.reused || 'post-level analysis'))
       + '; comment thread not analysed)</span>';
}

/** Vision status — only `ok` licenses a claim about image sentiment.
 *
 * A failed fetch used to be indistinguishable from a genuine neutral verdict.
 */
function visionSummary(imageAnalysis) {
  if (!imageAnalysis) return '<span class="trace-null">no image</span>';
  var st = imageAnalysis.vision_status;
  if (st === 'ok') {
    return '<span class="trace-str">ok</span> <span class="text-muted">'
         + escHtml(String(imageAnalysis.vision_model || '')) + ' · '
         + (imageAnalysis.analyzed_images || 0) + ' of '
         + (imageAnalysis.image_count || 0) + ' image(s) analysed</span>';
  }
  return '<span class="trace-warn">' + escHtml(String(st || 'unknown'))
       + '</span> <span class="text-muted">no image verdict — not a neutral one</span>';
}

/** Which real-mode components fell back to a heuristic.
 *
 * `nlp_engine: "models"` says which path was INTENDED. This says what ran. A
 * non-empty list means no latency or accuracy figure from the run is quotable.
 */
function degradedSummary(proc) {
  proc = proc || {};
  // ABSENT is not the same as EMPTY. While the assembler was dropping this key
  // the row rendered a confident "none" on every run — a false reassurance,
  // which is worse than rendering nothing. Distinguish the three states.
  if (!('degraded_components' in proc)) {
    return '<span class="trace-null">not reported — this result predates the field</span>';
  }
  var d = proc.degraded_components || [];
  if (!d.length) {
    return proc.stub_mode
      ? '<span class="text-muted">n/a — stub mode (a chosen configuration, not a degradation)</span>'
      : '<span class="trace-str">none</span>';
  }
  return '<span class="trace-warn">' + escHtml(d.join(', ')) + '</span>'
       + '<div class="text-muted" style="font-size:.7rem">fell back to heuristics — '
       + 'no accuracy or latency figure from this run is quotable</div>';
}

function traceValue(key, v) {
  if (v === null || v === undefined) return '<span class="trace-null">null</span>';
  if (typeof v === 'boolean') {
    return '<span class="' + (v ? 'trace-yes' : 'trace-no') + '">' + v + '</span>';
  }
  if (Array.isArray(v)) {
    if (!v.length) return '<span class="trace-null">[]</span>';
    return v.map(function (x) {
      return '<span class="trace-chip">' + escHtml(typeof x === 'object' ? JSON.stringify(x) : String(x)) + '</span>';
    }).join('');
  }
  if (typeof v === 'object') {
    return Object.keys(v).map(function (k) {
      return '<span class="trace-chip">' + escHtml(k) + ' ' + escHtml(String(v[k])) + '</span>';
    }).join('');
  }
  if (typeof v === 'number') {
    var n = Number.isInteger(v) ? v : Math.round(v * 10000) / 10000;
    return '<span class="trace-num">' + n + '</span>';
  }
  var s = String(v);
  // Long free text (a summary preview) gets its own block so it can wrap.
  if (s.length > 90) return '<span class="trace-text">' + escHtml(s) + '</span>';
  return escHtml(s);
}

function traceDetailRows(layerKey, detail) {
  if (!detail) return '';
  var order = TRACE_FIELD_ORDER[layerKey] || [];
  var keys = order.filter(function (k) { return detail[k] !== undefined; });
  Object.keys(detail).forEach(function (k) {
    if (k !== 'next_stream' && keys.indexOf(k) === -1) keys.push(k);
  });

  if (!keys.length) return '';
  return '<dl class="trace-kv">' + keys.map(function (k) {
    return '<dt>' + escHtml(k) + '</dt><dd>' + traceValue(k, detail[k]) + '</dd>';
  }).join('') + '</dl>';
}

function renderTraceRail() {
  var host = document.getElementById('trace-rail');
  if (!host) return;

  var html = '';
  TRACE_LAYERS.forEach(function (l, i) {
    var st = traceState[l.key] || { status: 'idle' };
    var status = st.status || 'idle';

    // Queue chip above each layer — lit once that layer has reported.
    html += '<div class="trace-wire' + (status !== 'idle' ? ' lit' : '') + '">'
      + '<span class="trace-queue">' + escHtml(l.queue) + '</span></div>';

    var ms = (st.ms != null)
      ? (st.ms >= 1000 ? (st.ms / 1000).toFixed(2) + ' s' : st.ms + ' ms')
      : '';

    html += '<div class="trace-layer" data-status="' + status + '">'
      + '<div class="trace-layer-head">'
      + '<span class="trace-pip"></span>'
      + '<span class="trace-layer-n">' + String(i + 1).padStart(2, '0') + '</span>'
      + '<span class="trace-layer-name">' + escHtml(l.label) + '</span>'
      + '<span class="trace-layer-state">' + escHtml(status) + '</span>'
      + (ms ? '<span class="trace-layer-ms">' + ms + '</span>' : '')
      + '</div>'
      + '<div class="trace-layer-note">' + escHtml(l.note) + '</div>'
      + traceDetailRows(l.key, st.detail)
      + '</div>';
  });

  host.innerHTML = html;
}

function renderTraceTape() {
  var host = document.getElementById('trace-tape');
  if (!host) return;
  if (!traceTape.length) {
    host.innerHTML = '<div class="table-empty">No frames yet.</div>';
    return;
  }
  host.innerHTML = traceTape.map(function (t) {
    var f = t.frame;
    var name = f.stage ? (f.stage + ' · ' + (f.status || '')) : (f.event || t.kind);
    return '<div class="trace-frame">'
      + '<span class="trace-frame-seq">' + escHtml(String(f.seq != null ? f.seq : '–')) + '</span>'
      + '<span class="trace-frame-flag">' + (f.replay ? 'R' : 'L') + '</span>'
      + '<span class="trace-frame-name">' + escHtml(name) + '</span>'
      + '<span class="trace-frame-ms">' + escHtml(f.ms != null ? f.ms + ' ms' : '') + '</span>'
      + '</div>';
  }).join('');
  host.scrollTop = host.scrollHeight;
}

/** Once the post lands, show what was actually persisted. */
async function loadTraceResult() {
  var card = document.getElementById('trace-result-card');
  var host = document.getElementById('trace-result');
  if (!host || !tracePostId) return;

  host.innerHTML = '<div class="loading-overlay"><div class="spinner spinner-dark"></div> Fetching canonical result…</div>';
  if (card) card.hidden = false;

  try {
    var r = await apiCall('/v1/analysis/' + encodeURIComponent(tracePostId), { method: 'GET' });
    var proc = r.processing || {};
    var conf = r.confidence;
    var ca = r.comment_analysis || {};

    host.innerHTML =
      '<dl class="trace-kv trace-kv-wide">'
      + '<dt>overall_sentiment</dt><dd>' + traceValue('s', r.overall_sentiment) + ' ' + traceValue('n', r.sentiment_score) + '</dd>'
      + '<dt>post_type</dt><dd>' + traceValue('s', r.post_type) + '</dd>'
      + '<dt>post_summary</dt><dd>' + traceValue('long', r.post_summary || null) + '</dd>'
      + '<dt>summary_lang</dt><dd>' + traceValue('s', r.post_summary_lang) + '</dd>'
      + '<dt>insight</dt><dd>' + traceValue('long', r.insight || null) + '</dd>'
      + '<dt>grounding</dt><dd>' + traceValue('a', r.post_summary_grounding) + '</dd>'
      + '<dt>confidence</dt><dd>' + traceValue('o', (conf && typeof conf === 'object') ? conf : { overall: conf }) + '</dd>'
      + '<dt>comments</dt><dd>' + traceValue('n', ca.analyzed) + ' analyzed, coverage ' + traceValue('n', ca.coverage) + '</dd>'
      + '<dt>label mix</dt><dd>' + provenanceSummary(ca) + '</dd>'
      // A reused post has an empty trace rail — no stage ever ran for it — so
      // this row is the only place the Trace tab can explain why.
      + '<dt>reused</dt><dd>' + reuseSummary(proc) + '</dd>'
      + '<dt>vision</dt><dd>' + visionSummary(r.image_analysis) + '</dd>'
      + '<dt>degraded</dt><dd>' + degradedSummary(proc) + '</dd>'
      + '<dt>processing</dt><dd>' + traceValue('o', proc) + '</dd>'
      + '</dl>';

  } catch (err) {
    host.innerHTML = '<div class="alert alert-error">Could not fetch the result: ' + escHtml(err.message) + '</div>';
  }
}

/* ============================================================
   LOGS TAB — server-side logs + this session's API traffic
   ------------------------------------------------------------
   Two sources on one timeline:

     client — one entry per apiCall(), tagged with the tab that made
              it, so every section shows what it requested and what
              came back (status + duration + X-Request-ID).
     server — SSE tail of /v1/logs/stream. Each backend service
              mirrors its log lines into Redis via the sink in
              libs/common/logging.py, so this is genuinely every
              service's output, not just the API's.

   The X-Request-ID on a client entry matches the request_id field on
   the API's own http_request line, so a slow call can be followed
   from the browser into the server.
   ============================================================ */

var LOG_MAX_ENTRIES = 1500;      // ring buffer; oldest dropped first
var logEntries = [];
var logSeqCounter = 0;
var logES = null;                // EventSource for /v1/logs/stream
var logOpen = false;
var logUnseen = 0;
// Scope defaults to 'all': the Logs tab is its own section now, so "this tab"
// can only sensibly mean the tab you were last on — see logScopeTab().
var logFilters = { scope: 'all', source: 'all', level: 'INFO', service: '', text: '' };
// Last non-Logs tab. Client entries are tagged with the tab that made the
// request, and once you are *on* the Logs tab `currentTab` is 'logs' — which
// would scope the view to the Logs tab's own requests. This is the tab a user
// means by "this tab".
var logPrevTab = 'overview';

function logScopeTab() {
  return (currentTab === 'logs') ? logPrevTab : currentTab;
}

var LOG_LEVEL_RANK = {
  TRACE: 0, DEBUG: 1, INFO: 2, SUCCESS: 3, WARNING: 4, ERROR: 5, CRITICAL: 6
};

function logRank(level) {
  var r = LOG_LEVEL_RANK[(level || '').toUpperCase()];
  return (r === undefined) ? 99 : r;   // unknown levels sort high, never hidden
}

/** Push an entry and repaint if the drawer is open. */
function logPush(entry) {
  entry.seq = ++logSeqCounter;
  logEntries.push(entry);
  if (logEntries.length > LOG_MAX_ENTRIES) {
    logEntries.splice(0, logEntries.length - LOG_MAX_ENTRIES);
  }
  if (logOpen) {
    renderLogs();
  } else {
    logUnseen++;
    updateLogBadge();
  }
}

/** Record one dashboard→API call. */
function logClient(method, path, status, startedAt, requestId, errorMsg) {
  var now = (typeof performance !== 'undefined' ? performance.now() : Date.now());
  var ms = Math.round((now - startedAt) * 10) / 10;
  var numeric = (typeof status === 'number');
  var level = (!numeric || status >= 500) ? 'ERROR'
            : (status >= 400) ? 'WARNING'
            : 'INFO';
  logPush({
    source: 'client',
    tab: currentTab,
    ts: Date.now() / 1000,
    level: level,
    service: 'dashboard',
    message: method + ' ' + path,
    status: status,
    ms: ms,
    requestId: requestId || null,
    error: errorMsg || null
  });
}

/** Record a server log line arriving over SSE. */
function logServer(entry) {
  logPush({
    source: 'server',
    tab: null,
    ts: entry.ts || (Date.now() / 1000),
    level: entry.level || 'INFO',
    service: entry.service || '-',
    message: entry.message || '',
    fields: entry.fields || {},
    module: entry.module || '',
    backfill: !!entry.backfill
  });
}

/** Badge on the Logs nav tab: unseen count, red if any of it is an error. */
function updateLogBadge() {
  var badge = document.getElementById('logs-badge');
  if (!badge) return;
  badge.textContent = logUnseen > 999 ? '999+' : String(logUnseen);
  badge.classList.toggle('has-unseen', logUnseen > 0);
  var errs = logEntries.slice(-200).some(function (e) { return logRank(e.level) >= 5; });
  badge.classList.toggle('has-error', errs);
}

function setLogStreamStatus(state) {
  var el = document.getElementById('log-stream-status');
  if (!el) return;
  var map = { live: 'live', connecting: 'connecting…', offline: 'offline', timeout: 'reconnecting…' };
  el.textContent = map[state] || state;
  el.className = 'log-stream-status ' + state;
}

/** Open (or reopen) the server log tail. */
async function connectLogStream() {
  if (logES || typeof EventSource === 'undefined') return;
  setLogStreamStatus('connecting');

  var qs = await sseQuery('backfill=120');
  if (logFilters.level) qs += '&min_level=' + encodeURIComponent(logFilters.level);

  try {
    logES = new EventSource(API_BASE + '/v1/logs/stream' + qs);
  } catch (e) {
    logES = null;
    setLogStreamStatus('offline');
    return;
  }

  logES.addEventListener('connected', function () { setLogStreamStatus('live'); });
  logES.addEventListener('log', function (evt) {
    try { logServer(JSON.parse(evt.data)); setLogStreamStatus('live'); }
    catch (e) { /* skip a malformed frame */ }
  });
  logES.addEventListener('timeout', function () {
    // The server closes the tail after its max duration — reopen while visible.
    setLogStreamStatus('timeout');
    disconnectLogStream();
    if (logOpen) connectLogStream();
  });
  logES.addEventListener('error', function () {
    if (logES && logES.readyState === EventSource.CLOSED) {
      disconnectLogStream();
      setLogStreamStatus('offline');
    }
  });
}

function disconnectLogStream() {
  if (logES) {
    try { logES.close(); } catch (e) { /* noop */ }
    logES = null;
  }
}

/** Entering the Logs tab: start the tail and clear the unseen badge. */
function loadLogs(background) {
  logOpen = true;
  logUnseen = 0;
  updateLogBadge();
  connectLogStream();
  if (!background) refreshLogServiceFilter();
  renderLogs();
}

/** Leaving the Logs tab: drop the SSE connection.
 * Entries keep accumulating from `logClient` regardless, so the badge still
 * counts dashboard traffic while the tab is closed — it just stops holding a
 * server connection open for a view nobody is looking at. */
function suspendLogs() {
  logOpen = false;
  disconnectLogStream();
  setLogStreamStatus('offline');
}

/** Populate the service dropdown from the server's buffer. */
async function refreshLogServiceFilter() {
  var sel = document.getElementById('log-service');
  if (!sel) return;
  try {
    var data = await apiCall('/v1/logs/services', { method: 'GET' });
    var keep = sel.value;
    var opts = ['<option value="">all services</option>',
                '<option value="dashboard">dashboard (client)</option>'];
    (data.services || []).forEach(function (s) {
      opts.push('<option value="' + escHtml(s.service) + '">'
        + escHtml(s.service) + ' (' + s.count + ')</option>');
    });
    sel.innerHTML = opts.join('');
    if (keep) sel.value = keep;
  } catch (e) {
    /* filter stays as-is — not worth surfacing */
  }
}

function logMatches(e) {
  if (logFilters.source !== 'all' && e.source !== logFilters.source) return false;
  // Scope applies to client entries only: server lines have no tab.
  if (logFilters.scope === 'tab' && e.source === 'client' && e.tab !== logScopeTab()) return false;
  if (logFilters.level && logRank(e.level) < logRank(logFilters.level)) return false;
  if (logFilters.service && (e.service || '-') !== logFilters.service) return false;
  if (logFilters.text) {
    var needle = logFilters.text.toLowerCase();
    var hay = [
      e.message, e.service, e.module, e.tab, e.status, e.requestId, e.error,
      e.fields ? Object.keys(e.fields).map(function (k) { return k + '=' + e.fields[k]; }).join(' ') : ''
    ].join(' ').toLowerCase();
    if (hay.indexOf(needle) === -1) return false;
  }
  return true;
}

function logTime(ts) {
  var d = new Date(ts * 1000);
  return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2)
    + ':' + ('0' + d.getSeconds()).slice(-2)
    + '.' + ('00' + d.getMilliseconds()).slice(-3);
}

function renderLogs() {
  var host = document.getElementById('log-body');
  if (!host) return;

  var rows = logEntries.filter(logMatches);

  var counter = document.getElementById('log-count');
  if (counter) {
    counter.textContent = rows.length + ' of ' + logEntries.length + ' shown';
  }

  if (!rows.length) {
    host.innerHTML = '<div class="table-empty">Nothing matches these filters.'
      + (logFilters.scope === 'tab'
          ? ' Client entries are scoped to the <b>' + escHtml(logScopeTab()) + '</b> tab — switch scope to “all”.'
          : '')
      + '</div>';
    return;
  }

  var follow = (document.getElementById('log-follow') || {}).checked;
  var nearBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 60;

  host.innerHTML = rows.map(function (e) {
    var lvl = (e.level || 'INFO').toUpperCase();
    var detail;

    if (e.source === 'client') {
      detail = '<span class="log-status s' + (typeof e.status === 'number' ? Math.floor(e.status / 100) : 'x') + '">'
        + escHtml(String(e.status)) + '</span>'
        + '<span class="log-ms">' + e.ms + ' ms</span>'
        + (e.tab ? '<span class="log-tab">' + escHtml(e.tab) + '</span>' : '')
        + (e.requestId ? '<span class="log-rid" title="X-Request-ID — matches the server line">'
            + escHtml(e.requestId) + '</span>' : '')
        + (e.error ? '<span class="log-err">' + escHtml(e.error) + '</span>' : '');
    } else {
      var f = e.fields || {};
      detail = Object.keys(f).map(function (k) {
        return '<span class="log-kv"><i>' + escHtml(k) + '</i>' + escHtml(String(f[k])) + '</span>';
      }).join('');
    }

    return '<div class="log-row lvl-' + lvl + (e.source === 'client' ? ' is-client' : '') + '">'
      + '<span class="log-t">' + logTime(e.ts) + '</span>'
      + '<span class="log-lvl">' + escHtml(lvl.slice(0, 4)) + '</span>'
      + '<span class="log-svc">' + escHtml(e.service || '-') + '</span>'
      + '<span class="log-msg">' + escHtml(e.message) + '</span>'
      + '<span class="log-detail">' + detail + '</span>'
      + '</div>';
  }).join('');

  if (follow && nearBottom !== false) host.scrollTop = host.scrollHeight;
}

function clearLogView() {
  logEntries = [];
  logUnseen = 0;
  updateLogBadge();
  renderLogs();
}

async function purgeServerLogs() {
  try {
    var r = await apiCall('/v1/logs', { method: 'DELETE' });
    showToast('Cleared ' + (r.cleared || 0) + ' server log lines.', 'success');
    logEntries = logEntries.filter(function (e) { return e.source === 'client'; });
    renderLogs();
    refreshLogServiceFilter();
  } catch (err) {
    showToast('Could not clear server logs: ' + err.message, 'error');
  }
}

/** Wire the Logs tab's controls. Called once from init(). */
function initLogsTab() {
  var clear = document.getElementById('log-clear');
  if (clear) clear.addEventListener('click', clearLogView);

  var purge = document.getElementById('log-purge');
  if (purge) purge.addEventListener('click', purgeServerLogs);

  document.querySelectorAll('#tab-logs .log-seg button').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var group = this.parentNode;
      group.querySelectorAll('button').forEach(function (b) { b.classList.remove('active'); });
      this.classList.add('active');
      if (this.dataset.scope) logFilters.scope = this.dataset.scope;
      if (this.dataset.source) logFilters.source = this.dataset.source;
      renderLogs();
    });
  });

  var level = document.getElementById('log-level');
  if (level) level.addEventListener('change', function () {
    logFilters.level = this.value;
    // The server filters by level too, so reopen the tail to match.
    disconnectLogStream();
    if (logOpen) connectLogStream();
    renderLogs();
  });

  var svc = document.getElementById('log-service');
  if (svc) svc.addEventListener('change', function () {
    logFilters.service = this.value;
    renderLogs();
  });

  var search = document.getElementById('log-search');
  if (search) search.addEventListener('input', function () {
    logFilters.text = this.value.trim();
    renderLogs();
  });

  var follow = document.getElementById('log-follow');
  if (follow) follow.addEventListener('change', renderLogs);

  // Ctrl+` / Cmd+` jumps to the Logs tab, or back to where you were.
  document.addEventListener('keydown', function (evt) {
    if ((evt.ctrlKey || evt.metaKey) && evt.key === '`') {
      evt.preventDefault();
      showTab(currentTab === 'logs' ? logPrevTab : 'logs');
    }
  });

  updateLogBadge();
}

/* ============================================================
   Initialization
   ============================================================ */
document.addEventListener('DOMContentLoaded', init);

/** Ask the server who we are, so "no token" and "expired token" look different.
 *
 * Without this the dashboard could only discover an expired session by making a
 * request that happened to 401 — and until then it showed a logged-in header
 * while every call failed. §6.6 defect 4.
 */
async function verifySession() {
  if (!authToken && !apiKey) {
    updateAuthStatus(false);
    return;
  }
  try {
    var me = await apiCall('/v1/auth/me');
    // The server's `sub` is the authoritative identity — prefer it over whatever
    // the login form happened to type, and remember it so a reload shows the
    // same name without another round-trip.
    if (me && me.sub && me.auth_method === 'jwt') setAuthUser(me.sub);
    updateAuthStatus(true, me && me.sub ? me.sub : null);
    if (me && me.sub) console.info('session:', me.sub, '· tenant:', me.tenant_id || 'default');
  } catch (err) {
    // apiCall already cleared the token and tore down streams on a 401.
    console.warn('session verification failed:', err.message);
  }
}

function init() {
  // Status bar API base display
  var apiBaseEl = document.getElementById('status-api-base');
  if (apiBaseEl) apiBaseEl.textContent = API_BASE;

  updateApiStatus('connecting');
  // Optimistic, then corrected by verifySession() below.
  updateAuthStatus(!!(authToken || apiKey));
  verifySession();

  // Set up nav tab click handlers
  document.querySelectorAll('.nav-tab').forEach(function(btn) {
    btn.addEventListener('click', function() {
      showTab(this.getAttribute('data-tab'));
    });
  });

  initLogsTab();

  // Trace tab controls
  var traceRunBtn = document.getElementById('trace-run');
  if (traceRunBtn) traceRunBtn.addEventListener('click', runTrace);
  var traceClearBtn = document.getElementById('trace-clear');
  if (traceClearBtn) traceClearBtn.addEventListener('click', clearTrace);

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

  // Chat
  var chatSendBtn = document.getElementById('chat-send-btn');
  if (chatSendBtn) {
    chatSendBtn.addEventListener('click', sendChatMessage);
  }
  var chatClearBtn = document.getElementById('chat-clear-btn');
  if (chatClearBtn) {
    chatClearBtn.addEventListener('click', clearChat);
  }
  var chatInput = document.getElementById('chat-input');
  if (chatInput) {
    chatInput.addEventListener('input', autoGrowChatInput);
    chatInput.addEventListener('keydown', function(e) {
      // Enter sends; Shift+Enter inserts a newline.
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChatMessage();
      }
    });
  }
  var chatSeg = document.getElementById('chat-backend-seg');
  if (chatSeg) {
    chatSeg.addEventListener('click', function(e) {
      var btn = e.target.closest ? e.target.closest('.llm-segment') : null;
      if (btn && btn.getAttribute('data-backend')) {
        setChatBackend(btn.getAttribute('data-backend'));
      }
    });
  }
  var chatModelSel = document.getElementById('chat-model');
  if (chatModelSel) {
    chatModelSel.addEventListener('change', function() { chatModel = this.value; });
  }
  // Suggestion chips in the empty state — click to send that prompt.
  var chatMessages = document.getElementById('chat-messages');
  if (chatMessages) {
    chatMessages.addEventListener('click', function(e) {
      var chip = e.target.closest ? e.target.closest('.chat-suggestion') : null;
      if (!chip) return;
      var input = document.getElementById('chat-input');
      if (input) { input.value = chip.textContent; autoGrowChatInput(); }
      sendChatMessage();
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

  // Post detail → PDF
  var pdfBtn = document.getElementById('modal-pdf-btn');
  if (pdfBtn) {
    pdfBtn.addEventListener('click', downloadPostPdf);
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

  // Login / signup forms
  var loginForm = document.getElementById('login-form');
  if (loginForm) {
    loginForm.addEventListener('submit', handleLoginSubmit);
  }

  var signupForm = document.getElementById('signup-form');
  if (signupForm) {
    signupForm.addEventListener('submit', handleSignupSubmit);
  }

  // Mode switch (Sign in / Create account)
  ['signin', 'signup'].forEach(function(mode) {
    var tab = document.getElementById('auth-mode-' + mode);
    if (tab) tab.addEventListener('click', function() { setAuthMode(mode); });
  });

  var loginBtn = document.getElementById('header-login-btn');
  if (loginBtn) {
    // Wrapped, not passed directly: showLoginModal takes a mode, and handing it
    // the click Event would make that argument meaningless.
    loginBtn.addEventListener('click', function() { showLoginModal('signin'); });
  }

  var logoutBtn = document.getElementById('header-logout-btn');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', logout);
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
