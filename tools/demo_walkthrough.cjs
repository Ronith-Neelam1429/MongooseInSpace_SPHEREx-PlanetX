#!/usr/bin/env node
// Pre-pitch smoke test: walks the demo in a real Chrome, saves a screenshot per step, and
// fails (exit 1) when the API is down, a page errors, or "Find what moves" does not finish.
//
//   NODE_PATH=<a node_modules that has playwright-core> \
//   node tools/demo_walkthrough.cjs --site http://localhost:5173 --api http://127.0.0.1:8000 [--out tools/out/walkthrough]
//
// Needs Google Chrome installed (uses playwright-core's `chrome` channel: no browser download) and
// playwright-core (not a project dependency; `npm i --no-save playwright-core` in a scratch dir, or the
// copy in ~/.npm/_npx/*/node_modules). --site is the page being demoed (dev server, preview or
// the GitHub Pages URL); --api is only used for the health check, the page uses its own VITE_API_BASE.
// Options: --targets barnard,wise0855,... (default all)  --motion-timeout-s 240  --headed
const fs = require('node:fs');
const path = require('node:path');

let chromium;
try {
  ({ chromium } = require('playwright-core'));
} catch {
  console.error('playwright-core not found. Set NODE_PATH to a node_modules that has it (see the header of this file).');
  process.exit(2);
}

const arg = (name, dflt) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 && process.argv[i + 1] && !process.argv[i + 1].startsWith('--') ? process.argv[i + 1] : dflt;
};
const SITE = arg('site', 'http://localhost:5173').replace(/\/$/, '');
const API = arg('api', 'http://127.0.0.1:8000').replace(/\/$/, '');
const OUT = path.resolve(arg('out', 'tools/out/walkthrough'));
const MOTION_TIMEOUT_MS = Number(arg('motion-timeout-s', '240')) * 1000;
const HEADED = process.argv.includes('--headed');

// Shareable views (docs/DEMO.md). Same positions as the website's own targets at today's epoch.
const TARGETS = [
  { id: 'barnard', label: "Barnard's Star", query: 'ra=269.4473&dec=4.7685&fov=0.25', expect: /mover|moving|10\.\d/i },
  { id: 'eps-indi-b', label: 'Epsilon Indi B', query: 'ra=331.0984&dec=-56.8013&fov=0.25', expect: /mover|moving|4\.\d/i },
  { id: 'wise0855', label: 'WISE 0855', query: 'ra=133.7578&dec=-7.2420&fov=0.25', expect: /mover|moving|candidate|7\.\d|8\.\d/i },
  { id: 'rw-aur', label: 'RW Aurigae', query: 'ra=76.9566&dec=30.4013&fov=0.25', expect: /variable|brightness/i },
  { id: 'r-dra', label: 'R Draconis', query: 'ra=248.1672&dec=66.7550&fov=0.25', expect: /variable|brightness/i },
];
const wanted = arg('targets', 'all');
const chosen = wanted === 'all' ? TARGETS : TARGETS.filter((t) => wanted.split(',').includes(t.id));

const steps = [];
const problems = [];
const warnings = []; // reported, but they do not fail the run (e.g. a sky-map tile service hiccup)
async function step(name, fn) {
  const t0 = Date.now();
  try {
    const detail = (await fn()) || '';
    steps.push({ name, ok: true, s: (Date.now() - t0) / 1000, detail });
    console.log(`[ok]   ${name} (${((Date.now() - t0) / 1000).toFixed(1)}s) ${detail}`);
  } catch (e) {
    steps.push({ name, ok: false, s: (Date.now() - t0) / 1000, detail: String(e.message || e).split('\n')[0] });
    console.log(`[FAIL] ${name}: ${String(e.message || e).split('\n')[0]}`);
  }
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });

  await step('API health', async () => {
    const r = await fetch(`${API}/api/health`, { signal: AbortSignal.timeout(10000) });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return JSON.stringify(await r.json());
  });

  const browser = await chromium.launch({ channel: 'chrome', headless: !HEADED });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.on('pageerror', (e) => warnings.push(`page error: ${e.message}`));
  page.on('response', (r) => {
    // Archive checks may legitimately 404 (no coverage); anything else from our API is a problem.
    if (r.url().includes('/api/') && r.status() >= 500 && r.status() !== 503) problems.push(`${r.status()} ${r.url().slice(0, 120)}`);
  });
  const shot = (name) => page.screenshot({ path: path.join(OUT, `${name}.png`) });

  await step('home page loads', async () => {
    await page.goto(SITE, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.locator('canvas').first().waitFor({ timeout: 30000 });
    await page.waitForTimeout(3000); // let the sky tiles draw
    await shot('00-home');
  });

  await step('search suggestions list the test targets', async () => {
    const box = page.getByRole('combobox', { name: /search/i });
    await box.click();
    await page.locator('[role=option]').first().waitFor({ timeout: 5000 });
    const n = await page.locator('[role=option]').count();
    await shot('01-search');
    await box.press('Escape');
    if (n < 5) throw new Error(`only ${n} suggestions`);
    return `${n} suggestions`;
  });

  for (const t of chosen) {
    await step(`${t.id}: shared URL opens`, async () => {
      await page.goto(`${SITE}/?${t.query}`, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.locator('canvas').first().waitFor({ timeout: 30000 });
      await page.waitForTimeout(4000);
      await shot(`${t.id}-1-view`);
    });

    await step(`${t.id}: Find what moves`, async () => {
      // Selecting the patch: click the centre of the map, as a presenter would.
      const vp = page.viewportSize();
      await page.mouse.click(vp.width / 2, vp.height / 2);
      const run = page.getByRole('button', { name: /find what moves/i });
      await run.waitFor({ timeout: 20000 });
      await shot(`${t.id}-2-selected`);
      await run.click();
      const t0 = Date.now();
      // Done when the verdict panel says how many images were compared; a failed run shows an alert.
      const done = page.getByText(/SPHEREx: compared \d+ images?/).waitFor({ timeout: MOTION_TIMEOUT_MS });
      const failed = page.getByText(/The run failed/).waitFor({ timeout: MOTION_TIMEOUT_MS }).then(async () => {
        throw new Error(`the run failed: ${await page.locator('[role=alert]').first().innerText()}`);
      });
      done.catch(() => {}); // the loser of the race may reject later (timeout, page closed): not a crash
      failed.catch(() => {});
      await Promise.race([done, failed]);
      await shot(`${t.id}-3-motion`);
      const text = await page.locator('body').innerText();
      if (!t.expect.test(text)) throw new Error(`result text does not mention what is expected for ${t.label}: ${t.expect}`);
      return `${((Date.now() - t0) / 1000).toFixed(0)}s`;
    });

    await step(`${t.id}: no error alert on screen`, async () => {
      const alerts = await page.locator('[role=alert]').allInnerTexts();
      if (alerts.length) throw new Error(`alert: ${alerts[0].slice(0, 120)}`);
    });
  }

  await step('no API 5xx during the walk', async () => {
    if (problems.length) throw new Error(`${problems.length}: ${problems.slice(0, 3).join(' | ')}`);
  });

  await browser.close();
  for (const w of warnings) console.log(`[warn] ${w.slice(0, 160)}`);
  const failed = steps.filter((s) => !s.ok);
  console.log(`\n${steps.length - failed.length}/${steps.length} steps ok. Screenshots in ${OUT}`);
  fs.writeFileSync(path.join(OUT, 'summary.json'), JSON.stringify({ site: SITE, api: API, steps }, null, 2));
  process.exit(failed.length ? 1 : 0);
})().catch((e) => {
  console.error(e);
  process.exit(2);
});
