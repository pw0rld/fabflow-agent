'use strict';
// Real precomputed public geometry only. No customer API, uploads, SMS or AI.
if (process.platform !== 'linux') throw Error('Run on ZeroClave only.');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
process.env.PLAYWRIGHT_BROWSERS_PATH ||= '/srv/dsh/build-runtime/ms-playwright';
const { chromium } = require('/srv/dsh/build-runtime/node_modules/playwright');
const origin = new URL(process.env.EXAMPLES_ORIGIN || 'https://8.135.55.251');
const output = process.env.EXAMPLES_BROWSER_OUT;
assert(output && path.isAbsolute(output) && !fs.existsSync(output));
fs.mkdirSync(output, { recursive: true, mode: 0o700 });
const ROOT = '/workbench/public-examples/';
const report = { passed: false, checks: [], page_errors: [], blocked_requests: [],
  customer_api_requests: 0, real_uploads: 0, provider_requests: 0, production_quota_used: false };
const cents = value => { assert(/^\d+\.\d{2}$/.test(value)); return BigInt(value.replace('.', '')); };
const money = value => '¥' + Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
let baseline;

async function contextFor(browser, viewport, options = {}) {
  const context = await browser.newContext({ viewport, ignoreHTTPSErrors: true,
    reducedMotion: 'reduce', serviceWorkers: 'block', acceptDownloads: true });
  context.setDefaultTimeout(30000);
  await context.addInitScript(() => {
    localStorage.setItem('fabflow-trial-last-order', 'BATCH-' + 'a'.repeat(32));
    localStorage.setItem('public-example-private-canary', 'PRIVATE-CUSTOMER-CANARY-ybm-fhz-20-00.stp');
  });
  let corrupt = options.corrupt === true;
  const details = new Map();
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (['data:', 'blob:', 'about:'].includes(url.protocol)) return route.continue();
    if (url.pathname.startsWith('/workbench/customer/api')) report.customer_api_requests++;
    if (url.origin !== origin.origin || !['GET', 'HEAD'].includes(request.method()) ||
        !(['/try/', '/favicon.ico'].includes(url.pathname) ||
          ['/workbench/_next/', '/workbench/cad-preview/', ROOT].some(prefix => url.pathname.startsWith(prefix)))) {
      report.blocked_requests.push(request.method() + ' ' + url.pathname); return route.abort();
    }
    if (url.pathname === ROOT + 'motor-bracket/example.json' && corrupt) {
      corrupt = false;
      return route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ ...baseline, id: 'flanged-bushing' }) });
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.on('pageerror', error => report.page_errors.push(error.message));
  page.on('response', response => {
    const url = new URL(response.url());
    if (url.pathname.startsWith(ROOT) && url.pathname.endsWith('/example.json') && response.status() === 200)
      void response.json().then(value => details.set(value.id, value)).catch(() => {});
  });
  const response = await page.goto(new URL('/try/', origin).href, { waitUntil: 'networkidle' });
  assert.equal(response.status(), 200);
  await page.getByTestId('public-examples-page').waitFor();
  assert.equal(await page.getByTestId('trial-page').count(), 0);
  return { context, page, details };
}
async function ready(page) {
  await page.waitForFunction(() => document.querySelector('[data-testid="public-example-viewer"]')?.getAttribute('data-state') === 'ready', null, { timeout: 60000 });
  assert.equal(await page.getByTestId('public-example-viewer').locator('canvas').count(), 1);
}
async function fits(page) { assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)); }

async function main() {
  const browser = await chromium.launch({ headless: true, chromiumSandbox: false,
    args: ['--disable-dev-shm-usage', '--renderer-process-limit=1', '--no-first-run', '--no-proxy-server',
      '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  try {
    const desktop = await contextFor(browser, { width: 1440, height: 1080 });
    const { page, details } = desktop;
    await ready(page);
    assert.equal(await page.getByTestId('public-examples-page').getAttribute('data-example-id'), 'motor-bracket');
    assert.equal(await page.getByTestId('public-example-upload-own').getAttribute('href'), '/try/?mode=upload');
    assert.equal(await page.locator('[data-testid^="public-example-card-"]').count(), 3);
    baseline = details.get('motor-bracket'); assert(baseline);
    await fits(page);
    await page.screenshot({ path: path.join(output, 'public-entry.png'), fullPage: true, animations: 'disabled' });
    for (const id of ['motor-bracket', 'sheet-metal-enclosure', 'flanged-bushing']) {
      if (id !== 'motor-bracket') { await page.getByTestId('public-example-card-' + id).click(); await ready(page); }
      const detail = details.get(id); assert(detail && detail.id === id);
      assert.equal(detail.stats.part_count, id === 'sheet-metal-enclosure' ? 31 : 1);
      assert.equal(detail.stats.solid_count, id === 'sheet-metal-enclosure' ? 38 : 1);
      assert.equal(detail.parts.reduce((sum, part) => sum + part.quantity, 0), detail.stats.occurrence_count);
      await page.getByTestId('public-example-part-' + detail.parts[0].id).click();
      await page.getByTestId('public-example-part-facts').waitFor();
      await page.getByTestId('public-example-isolate').click();
      assert((await page.locator('.pce-model-toolbar').innerText()).includes('单件视图'));
      await page.getByTestId('public-example-isolate').click();
      if (id === 'sheet-metal-enclosure') await page.screenshot({ path: path.join(output, 'enclosure-native.png'), fullPage: true, animations: 'disabled' });
      await page.getByTestId('public-example-tab-bom').click();
      assert.equal(await page.getByTestId('public-example-bom').locator('tbody tr').count(), detail.parts.length);
      await page.getByTestId('public-example-tab-quote').click();
      assert.equal(await page.getByTestId('public-example-quote-total').innerText(), money(detail.quotation.total));
      let total = 0n;
      for (const item of detail.quotation.items) {
        assert.equal(cents(item.unit_price) * BigInt(item.quantity_total), cents(item.amount));
        total += cents(item.amount);
      }
      assert.equal(total, cents(detail.quotation.total));
      assert((await page.getByTestId('public-example-quote').innerText()).includes('示例'));
      await page.getByTestId('public-example-source').locator('summary').click();
      assert((await page.getByTestId('public-example-source').innerText()).includes(detail.source.license));
      await fits(page);
      report.checks.push({ example: id, parts: detail.stats.part_count, occurrences: detail.stats.occurrence_count,
        native_canvas: true, bom: true, quote_cents_match: true, attribution: true });
    }
    const download = await Promise.all([page.waitForEvent('download'), page.getByTestId('public-example-source-download').click()]);
    const downloaded = path.join(output, 'public-bushing.step'); await download[0].saveAs(downloaded);
    assert.equal(crypto.createHash('sha256').update(fs.readFileSync(downloaded)).digest('hex'), details.get('flanged-bushing').source.sha256);
    const body = await page.locator('body').innerText();
    assert(!body.includes('ybm-fhz') && !body.includes('PRIVATE-CUSTOMER-CANARY'));
    await desktop.context.close();

    const mobile = await contextFor(browser, { width: 390, height: 844 });
    await ready(mobile.page); await fits(mobile.page);
    await mobile.page.screenshot({ path: path.join(output, 'public-mobile.png'), fullPage: true, animations: 'disabled' });
    await mobile.context.close();
    report.checks.push('Mobile public entry fits without customer-session calls.');

    const rejected = await contextFor(browser, { width: 1280, height: 900 }, { corrupt: true });
    await rejected.page.locator('.pce-loading.pce-error[role="alert"]').waitFor();
    assert.equal(await rejected.page.getByTestId('public-example-viewer').count(), 0);
    await rejected.page.getByRole('button', { name: '重新载入示例', exact: true }).click();
    await ready(rejected.page); await rejected.context.close();
    report.checks.push('Mismatched source identity is rejected; an explicit retry restores only the public model.');
    assert.equal(report.customer_api_requests, 0);
    assert.deepEqual(report.page_errors, []); assert.deepEqual(report.blocked_requests, []);
    report.passed = true;
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
  }
  console.log(JSON.stringify(report, null, 2));
}
main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
