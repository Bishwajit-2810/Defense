/**
 * capture_screens.mjs — drive the live dashboard and save the report figures.
 *
 * The paper embeds one screenshot per feature; this script produces every one
 * of them from the running system rather than from a mock, so a figure cannot
 * drift from what the software does.
 *
 * Prerequisites: the stack is up (`uv run run_all.py --with-agents`), i.e. the
 * API answers on :8001 and the dashboard is served on :8080.
 *
 *   node paper/capture_screens.mjs                 # every shot
 *   node paper/capture_screens.mjs overview posts  # only these
 *
 * Output: paper/figures/fig-shot-<name>.png at 2x device scale.
 */
// playwright-core ships with the dashboard's @playwright/test dependency; the
// bare 'playwright' package is not installed, so resolve it by path.
import playwright from '../dashboard/node_modules/playwright-core/index.js';

const { chromium } = playwright;
import { mkdirSync } from 'fs';
import { dirname, resolve } from 'path';
import { fileURLToPath } from 'url';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(HERE, 'figures');
const DASH = process.env.DASH_URL || 'http://localhost:8080';
const API = process.env.API_URL || 'http://127.0.0.1:8001';
const API_KEY = process.env.DASH_API_KEY || 'demo';

mkdirSync(OUT, { recursive: true });

// Internal shot key -> the figure label the report uses.  The report names a
// figure by its label ("fig:ui-overview"), and generate_pdf.py resolves that to
// figures/fig-ui-overview.png, so the file this script writes has to be named
// for the label rather than for the tab it came from.  Anything absent here
// falls back to fig-shot-<key>.png.
const FIG = {
  login: 'ui-login',
  overview: 'ui-overview',
  'overview-cost': 'shot-economics',
  postmodal: 'shot-postdetail',
  'postmodal-comments': 'ui-postmodal',
  'postmodal-ensemble': 'shot-ensemble',
  'jobs-live': 'shot-jobs',
  'pipeline-live': 'shot-pipeline',
  'logs-live': 'shot-logs',
  trace: 'ui-trace',
  'trace-tape': 'shot-canonical',
  'trace-result': 'shot-canonical-2',
  dark: 'shot-dark',
};

const only = process.argv.slice(2);
const want = (name) => only.length === 0 || only.includes(name);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function shot(page, name, opts = {}) {
  const path = `${OUT}/fig-${FIG[name] || 'shot-' + name}.png`;
  await page.screenshot({ path, fullPage: !!opts.fullPage, ...(opts.clip ? { clip: opts.clip } : {}) });
  console.log(`  saved ${path}`);
}

/** Dismiss whichever modal is open — its backdrop swallows every nav click. */
async function closeModal(page) {
  const x = page.locator('div.fixed.inset-0 button:has(svg.lucide-x)').first();
  if (await x.count()) await x.click();
  await sleep(800);
}

/** Click a left-hand nav tab and wait for its content to settle. */
async function tab(page, label, settle = 1500) {
  await page.getByRole('button', { name: label, exact: true }).first().click();
  await page.waitForLoadState('networkidle').catch(() => {});
  await sleep(settle);
}

async function main() {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    deviceScaleFactor: 2,
    colorScheme: 'light',
  });
  // Authenticate before the first paint, and pin the light theme: the report is
  // printed on white, and the app otherwise follows the OS preference.
  await ctx.addInitScript(
    ([key]) => {
      localStorage.setItem('api_key', key);
      localStorage.setItem('theme', 'light');
    },
    [API_KEY],
  );

  const page = await ctx.newPage();
  // Playwright dismisses dialogs silently, and two pages report failure through
  // one — so surface them instead of screenshotting a mystery.
  page.on('dialog', (d) => {
    console.log(`  [dialog] ${d.message()}`);
    d.dismiss().catch(() => {});
  });
  page.on('pageerror', (e) => console.log(`  [pageerror] ${e.message}`));
  page.on('response', (r) => {
    if (r.status() >= 400) console.log(`  [http ${r.status()}] ${r.url()}`);
  });

  // ---------------------------------------------------------------- login
  if (want('login')) {
    const anon = await browser.newContext({
      viewport: { width: 1500, height: 950 },
      deviceScaleFactor: 2,
      colorScheme: 'light',
    });
    await anon.addInitScript(() => localStorage.setItem('theme', 'light'));
    const p = await anon.newPage();
    await p.goto(DASH, { waitUntil: 'networkidle' });
    await sleep(1200);
    await shot(p, 'login');
    await anon.close();
  }

  await page.goto(DASH, { waitUntil: 'networkidle' });
  await sleep(2500);

  // ------------------------------------------------------------- overview
  if (want('overview')) {
    await shot(page, 'overview');
    // Far enough down for the token-economics panel, which is the part of this
    // tab the cost argument in Chapter 4 actually rests on.
    await page.mouse.wheel(0, 2050);
    await sleep(1500);
    await shot(page, 'overview-cost');
    await page.mouse.wheel(0, -4000);
    await sleep(400);
  }

  // ---------------------------------------------------------------- posts
  if (want('posts')) {
    await tab(page, 'Posts', 2500);
    await shot(page, 'posts');
  }

  if (want('postmodal')) {
    await tab(page, 'Posts', 2500);
    await page.locator('tbody tr').first().click();
    await sleep(2500);
    await shot(page, 'postmodal');
    // The comment table sits below the fold inside the modal's own scroller.
    await page.mouse.wheel(0, 1400);
    await sleep(900);
    await shot(page, 'postmodal-comments');
    await page.mouse.wheel(0, 1400);
    await sleep(900);
    await shot(page, 'postmodal-ensemble');
    await closeModal(page);
  }

  // ----------------------------------------------------------------- jobs
  if (want('jobs')) {
    // A job list with nothing running says nothing about job control, so start
    // a real re-analysis and wait until it is genuinely part-way through.
    const ids = await page.evaluate(
      async ([api, k]) => {
        const r = await fetch(`${api}/v1/analysis/latest?limit=6`, { headers: { 'X-API-Key': k } });
        const d = await r.json();
        return (d.results || []).map((x) => x.post_id).slice(0, 6);
      },
      [API, API_KEY],
    );
    await page.evaluate(
      async ([api, k, postIds]) => {
        await fetch(`${api}/v1/analysis/run`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-API-Key': k },
          body: JSON.stringify({ post_ids: postIds }),
        });
      },
      [API, API_KEY, ids],
    );
    await tab(page, 'Analysis Jobs', 2000);
    for (let i = 0; i < 90; i++) {
      await sleep(5000);
      const done = await page.evaluate(
        async ([api, k]) => {
          const r = await fetch(`${api}/v1/analysis?limit=1`, { headers: { 'X-API-Key': k } });
          const d = await r.json();
          const j = (d.jobs || [])[0] || {};
          return j.completed || 0;
        },
        [API, API_KEY],
      );
      if (done >= 2) break;
    }
    await sleep(3000);
    await shot(page, 'jobs');
  }

  // -------------------------------------------------------------- reports
  if (want('reports')) {
    await tab(page, 'Reports', 2500);
    await shot(page, 'reports');
    const view = page.getByRole('button', { name: /View Report/i }).first();
    if (await view.count()) {
      await view.click();
      await sleep(2000);
      await shot(page, 'report-view');
      await page.mouse.wheel(0, 1200);
      await sleep(800);
      await shot(page, 'report-body');
      await closeModal(page);
    }
  }

  // --------------------------------------------------------------- search
  if (want('search')) {
    await tab(page, 'Search', 1500);
    await page.getByPlaceholder(/Post id, platform id/i).fill('politics');
    await page.getByRole('button', { name: /^Search$/ }).last().click();
    await sleep(3500);
    await shot(page, 'search');
    // Same query in vector mode: the mode badge and the stub-vector notice are
    // both part of what the figure is meant to show.
    await page.getByText(/Semantic Search \(pgvector/i).click();
    await sleep(500);
    await page.getByRole('button', { name: /^Search$/ }).last().click();
    await sleep(4000);
    await shot(page, 'search-semantic');
  }

  // -------------------------------------------------------------- pipeline
  if (want('pipeline')) {
    await tab(page, 'Pipeline', 2500);
    await shot(page, 'pipeline');
  }

  // -------------------------------------------------------------- warnings
  if (want('warnings')) {
    await tab(page, 'Warnings', 2500);
    await shot(page, 'warnings');
  }

  // ------------------------------------------------------------------ logs
  if (want('logs')) {
    await tab(page, 'Logs', 3000);
    await shot(page, 'logs');
  }

  // --------------------------------------------------------------- monitor
  if (want('monitor')) {
    await tab(page, 'Overview', 1500);
    await page.keyboard.press('Alt+m');
    await sleep(3000);
    await shot(page, 'monitor');
    await page.keyboard.press('Alt+m');
    await sleep(600);
  }

  // ------------------ the same three surfaces, while work is already running
  if (want('busy')) {
    await tab(page, 'Pipeline', 3000);
    await shot(page, 'pipeline-live');
    await tab(page, 'Logs', 4000);
    await shot(page, 'logs-live');
    await tab(page, 'Analysis Jobs', 3000);
    await shot(page, 'jobs-live');
  }

  // ---------------------------------------------------------------- agents
  if (want('agents')) {
    await tab(page, 'Agents', 2000);
    await shot(page, 'agents');
    await page
      .getByPlaceholder(/Ask the .* agent/i)
      .fill('Which posts drew the most negative reaction, and what were people angry about?');
    await sleep(400);
    await shot(page, 'agents-form');
    await page.getByRole('button', { name: /Ask Agent/i }).click();
    // A local 8B model plans, calls tools and writes; give it room.
    await page.getByText(/Run Output|Tool|completed|failed/i).first().waitFor({ timeout: 600000 }).catch(() => {});
    for (let i = 0; i < 120; i++) {
      await sleep(5000);
      const running = await page.getByText(/Tracing|running/i).count();
      const done = await page.locator('text=/tokens|Tools:|duration/i').count();
      if (done && !running) break;
    }
    await sleep(3000);
    await shot(page, 'agent-run');
    await page.mouse.wheel(0, 1000);
    await sleep(1000);
    await shot(page, 'agent-trace');
  }

  // ------- the agent surfaces, from a run that already exists in the history
  if (want('agentshist')) {
    await tab(page, 'Agents', 3000);
    await shot(page, 'agents');
    // The history list replays a stored run into the output panel, which is the
    // same rendering a live run produces and costs no further model time.
    await page.locator('h4').last().click().catch(() => {});
    await sleep(2500);
    await page.getByText(/Run Output/i).first().scrollIntoViewIfNeeded().catch(() => {});
    await sleep(1200);
    await shot(page, 'agent-run');
    // The tool trace is behind a disclosure; the figure is the trace itself,
    // not the fact that one exists.
    const trace = page.getByText(/MCP Tool Invocation Trace/i).first();
    await trace.click().catch(() => {});
    await sleep(1500);
    await trace.evaluate((el) => el.scrollIntoView({ block: 'start' })).catch(() => {});
    await sleep(1500);
    await shot(page, 'agent-trace');
  }

  // ------------------------------------------------------------------ chat
  if (want('chat')) {
    await tab(page, 'Chat', 2000);
    await shot(page, 'chat-empty');

    const latestRun = () =>
      page.evaluate(
        async ([api, k]) => {
          const r = await fetch(`${api}/v1/agents/runs?limit=1`, { headers: { 'X-API-Key': k } });
          const d = await r.json();
          return d[0] ? `${d[0].run_id}:${d[0].status}` : 'none';
        },
        [API, API_KEY],
      );
    // A corpus question is routed to an agent, and the page offers no reliable
    // "finished" signal, so wait for a run record that is both NEW and closed.
    // Comparing against the previous run's id matters: without it the poll sees
    // the last completed run and returns on its first tick.
    const before = await latestRun();

    await page.getByPlaceholder(/Message the assistant/i).fill(
      'Summarise the overall public reaction across the analysed posts, and cite the posts you used.',
    );
    await sleep(300);
    await page.keyboard.press('Enter');

    for (let i = 0; i < 96; i++) {
      await sleep(5000);
      const now = await latestRun();
      if (now !== before && !now.endsWith(':running')) break;
    }
    // The answer streams in after the run record closes.
    await sleep(20000);
    await shot(page, 'chat');
  }

  // ----------------------------------------------------------------- trace
  if (want('trace')) {
    await tab(page, 'Trace', 2500);
    await shot(page, 'trace-idle');
    await page.getByRole('button', { name: /Run Trace/i }).click();
    // Stage 1 is an LLM call on this deployment, so the rail only fills after
    // the better part of a minute; the tape and the canonical result come later
    // still. Wait for the run rather than for a fixed frame.
    // Cap the wait below the API's 300-second stream ceiling: past that the
    // server closes the connection, the page marks the trace failed, and the
    // figure would show a failure that belongs to the stream rather than to the
    // run. See the limitation recorded in Section 6.2.
    for (let i = 0; i < 44; i++) {
      await sleep(5000);
      if (await page.getByText(/Canonical Result/i).count()) break;
    }
    await sleep(1000);
    await shot(page, 'trace');
    await page.mouse.wheel(0, 900);
    await sleep(1500);
    await shot(page, 'trace-tape');
    await page.mouse.wheel(0, 1200);
    await sleep(1500);
    await shot(page, 'trace-result');
  }

  // ----------------------------------- the pipeline and the logs under load
  if (want('live')) {
    const key = API_KEY;
    const ids = await page.evaluate(
      async ([api, k]) => {
        const r = await fetch(`${api}/v1/analysis/latest?limit=8`, { headers: { 'X-API-Key': k } });
        const d = await r.json();
        return (d.results || []).map((x) => x.post_id).slice(0, 8);
      },
      [API, key],
    );
    const jobs = await ctx.newPage();
    await jobs.goto(DASH, { waitUntil: 'networkidle' });
    await sleep(2000);
    await tab(jobs, 'Analysis Jobs', 1500);
    // Re-run a handful of stored posts so every stage has something in flight.
    await page.evaluate(
      async ([api, k, postIds]) => {
        await fetch(`${api}/v1/analysis/run`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-API-Key': k },
          body: JSON.stringify({ post_ids: postIds }),
        });
      },
      [API, key, ids],
    );
    await tab(page, 'Pipeline', 1200);
    for (const [n, wait] of [['pipeline-live', 4000], ['pipeline-live2', 8000]]) {
      await sleep(wait);
      await shot(page, n);
    }
    await sleep(2000);
    await shot(jobs, 'jobs-live');
    await tab(page, 'Logs', 3000);
    await shot(page, 'logs-live');
    await jobs.close();
  }

  // ------------------------------------------------------------ dark theme
  if (want('dark')) {
    const dark = await browser.newContext({
      viewport: { width: 1500, height: 950 },
      deviceScaleFactor: 2,
      colorScheme: 'dark',
    });
    await dark.addInitScript(
      ([key]) => {
        localStorage.setItem('api_key', key);
        localStorage.setItem('theme', 'dark');
      },
      [API_KEY],
    );
    const p = await dark.newPage();
    await p.goto(DASH, { waitUntil: 'networkidle' });
    await sleep(3000);
    await shot(p, 'dark');
    await dark.close();
  }

  // ------------------------------------------------------------- API docs
  if (want('apidocs')) {
    const p = await ctx.newPage();
    await p.goto(`${API}/docs`, { waitUntil: 'networkidle' });
    await sleep(3000);
    await shot(p, 'apidocs');
    await p.close();
  }

  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
