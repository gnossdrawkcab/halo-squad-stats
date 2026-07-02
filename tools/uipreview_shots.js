#!/usr/bin/env node
/**
 * Screenshot companion for tools/uipreview.py.
 *
 * Captures the dashboard, trends and setup pages at desktop (1400px) and
 * mobile (390px) widths, full-page, and reports per-page console errors and
 * horizontal-overflow checks. Exit code 1 if any console error or overflow.
 *
 * Usage:
 *   NODE_PATH=/usr/local/lib/node_modules node tools/uipreview_shots.js \
 *       --base http://127.0.0.1:5601 --out /tmp/shots --tag n5
 *
 * Run ONE chromium instance at a time on this box.
 */
const { chromium } = require('playwright');
const fs = require('fs');

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i > -1 ? process.argv[i + 1] : dflt;
}

const BASE = arg('base', 'http://127.0.0.1:5601');
const OUT = arg('out', '/tmp/uipreview-shots');
const TAG = arg('tag', 'n');
const PAGES = [
  ['dashboard', '/'],
  ['trends', '/trends'],
  ['setup', '/setup'],
];
const VIEWPORTS = [
  ['desktop', { width: 1400, height: 1000 }],
  ['mobile', { width: 390, height: 844 }],
];

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  let failures = 0;
  for (const [vpName, viewport] of VIEWPORTS) {
    const ctx = await browser.newContext({ viewport, deviceScaleFactor: 1 });
    const page = await ctx.newPage();
    for (const [name, path] of PAGES) {
      const errors = [];
      const onConsole = msg => { if (msg.type() === 'error') errors.push(msg.text()); };
      const onPageError = err => errors.push(String(err));
      page.on('console', onConsole);
      page.on('pageerror', onPageError);
      try {
        await page.goto(BASE + path, { waitUntil: 'networkidle', timeout: 30000 });
        // Freeze animations/transitions so full-page shots aren't caught mid-fade.
        await page.addStyleTag({ content: '*,*::before,*::after{animation:none !important;transition:none !important;}' });
        await page.waitForTimeout(900); // charts settle
        const overflow = await page.evaluate(() => {
          const el = document.scrollingElement || document.documentElement;
          return { scrollW: el.scrollWidth, clientW: el.clientWidth };
        });
        const file = `${OUT}/${TAG}-${name}-${vpName}.png`;
        await page.screenshot({ path: file, fullPage: true });
        const over = overflow.scrollW > overflow.clientW + 1;
        if (over) failures++;
        if (errors.length) failures++;
        console.log(`${TAG} ${name} ${vpName}: shot=${file}` +
          ` overflow=${over ? `YES (${overflow.scrollW}>${overflow.clientW})` : 'no'}` +
          ` consoleErrors=${errors.length}`);
        errors.forEach(e => console.log(`    ERR: ${e}`));
      } catch (e) {
        failures++;
        console.log(`${TAG} ${name} ${vpName}: FAILED ${e.message}`);
      }
      page.off('console', onConsole);
      page.off('pageerror', onPageError);
    }
    await ctx.close();
  }
  await browser.close();
  process.exit(failures ? 1 : 0);
})();
