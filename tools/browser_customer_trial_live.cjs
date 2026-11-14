'use strict';
// A real anonymous page/session smoke check. Upload and paid-work POSTs are blocked.
if (process.platform !== 'linux') throw Error('Run on the remote build server only.');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
process.env.PLAYWRIGHT_BROWSERS_PATH ||= '/srv/dsh/build-runtime/ms-playwright';
const { chromium } = require(process.env.FABFLOW_PLAYWRIGHT_MODULE || '/srv/dsh/build-runtime/node_modules/playwright');
const target = new URL(process.env.TRIAL_URL || 'https://8.135.55.251/try/');
const output = process.env.TRIAL_BROWSER_OUT;
assert(output && path.isAbsolute(output) && !fs.existsSync(output));
fs.mkdirSync(output, { recursive: true, mode: 0o700 });

async function main() {
  const report = { passed: false, real_uploads: 0, paid_work_requests: 0, page_errors: [], response_statuses: {}, blocked_requests: [] };
  const browser = await chromium.launch({ headless: true, chromiumSandbox: false,
    args: ['--disable-dev-shm-usage', '--renderer-process-limit=1', '--no-first-run', '--no-proxy-server'] });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1024 }, ignoreHTTPSErrors: true, serviceWorkers: 'block' });
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      const allowed = url.origin === target.origin && (['GET', 'HEAD'].includes(request.method()) ||
        request.method() === 'POST' && url.pathname === '/workbench/customer/api/session');
      if (!allowed) { report.blocked_requests.push(url.origin + url.pathname); return route.abort(); }
      return route.continue();
    });
    const page = await context.newPage();
    page.setDefaultTimeout(25000);
    page.on('pageerror', error => report.page_errors.push(error.message));
    page.on('response', response => {
      const url = new URL(response.url());
      if (['/workbench/customer/api/session', '/workbench/customer/api/quota', '/workbench/customer/api/orders'].includes(url.pathname))
        report.response_statuses[url.pathname] = response.status();
    });
    const response = await page.goto(target.href, { waitUntil: 'networkidle' });
    assert.equal(response.status(), 200);
    await page.getByTestId('trial-page').waitFor();
    await page.waitForFunction(() => /^\d+$/.test(document.querySelector('[data-testid="trial-quota"] strong')?.textContent?.trim() || ''), null, { timeout: 25000 });
    for (const endpoint of ['session', 'quota', 'orders']) assert.equal(report.response_statuses['/workbench/customer/api/' + endpoint], 200);
    const remaining = Number(await page.locator('[data-testid="trial-quota"] strong').innerText());
    assert(remaining >= 0 && remaining <= 10);
    assert.equal(await page.getByTestId('trial-file-input').isDisabled(), remaining === 0);
    const cookie = (await context.cookies(target.origin + '/workbench/customer/api/orders')).find(value => value.name === 'HYL_CUSTOMER_SESSION');
    assert(cookie && cookie.httpOnly && cookie.secure && cookie.path === '/workbench/customer/');
    assert.equal(await page.getByTestId('trial-history-order').count(), 0);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(report.page_errors, []);
    assert.deepEqual(report.blocked_requests, []);
    await page.screenshot({ path: path.join(output, 'live-entry.png'), fullPage: true });
    report.anonymous_session_verified = true;
    report.cookie_scope_preserved = true;
    report.remaining_uploads = remaining;
    report.passed = true;
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
  }
  console.log(JSON.stringify(report, null, 2));
}
main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
