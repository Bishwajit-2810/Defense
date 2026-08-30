/**
 * verify_shots.mjs — re-open each captured surface and assert it holds data.
 *
 * The figures in this report are photographs of a live system, and the failure
 * that matters is not a crash but a *quiet* one: a panel that painted its
 * placeholder ("Waiting for logs...", "CONNECTING TO REAL-TIME TELEMETRY
 * STREAM...", "No jobs loaded yet.") and got photographed before its data
 * arrived.  Such a capture is a valid PNG of the wrong thing, so nothing in the
 * build catches it.  This walks the same tabs and reports, per surface, whether
 * a placeholder is still on screen — so a bad capture is a failed check rather
 * than something a reader notices in the printed report.
 *
 *   node ../paper/verify_shots.mjs      # from dashboard/
 */
import playwright from '../dashboard/node_modules/playwright-core/index.js';
const { chromium } = playwright;

const DASH = process.env.DASH_URL || 'http://localhost:8080';
const API_KEY = process.env.DASH_API_KEY || 'demo';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Surface -> the strings that mean "this panel has no data yet".
const PLACEHOLDERS = {
  Overview:        [/Loading/i],
  'Analysis Jobs': [/No jobs loaded yet/i],
  Pipeline:        [/CONNECTING/i, /Awaiting data stream/i],
  Logs:            [/Waiting for logs/i, /Disconnected/i],
  Warnings:        [/Refreshing/i, /scanned the 0 most recent/i],
  Agents:          [/Loading/i],
};

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1500, height: 950 } });
const page = await ctx.newPage();
await page.addInitScript((k) => localStorage.setItem('api_key', k), API_KEY);
await page.goto(DASH, { waitUntil: 'networkidle' });
await sleep(2500);

let bad = 0;
for (const [label, pats] of Object.entries(PLACEHOLDERS)) {
  await page.getByRole('button', { name: label, exact: true }).first().click();
  await page.waitForLoadState('networkidle').catch(() => {});
  await sleep(6000);
  const body = await page.locator('body').innerText();
  const hit = pats.filter((p) => p.test(body));
  if (hit.length) { bad++; console.log(`FAIL  ${label.padEnd(14)} placeholder present: ${hit.join(', ')}`); }
  else console.log(`ok    ${label.padEnd(14)} has data`);
}
// The telemetry drawer is not a tab; open it the way an operator does.
await page.keyboard.press('Alt+m');
await sleep(8000);
const drawer = await page.locator('body').innerText();
if (/CONNECTING TO REAL-TIME TELEMETRY/i.test(drawer)) { bad++; console.log('FAIL  System Monitor placeholder present'); }
else console.log('ok    System Monitor has data');

await browser.close();
console.log(bad ? `\n${bad} surface(s) would photograph empty` : '\nevery surface holds data');
process.exit(bad ? 1 : 0);
