'use strict';
// A real anonymous page/session smoke check. Upload and paid-work POSTs are blocked.
if (process.platform !== 'linux') throw Error('Run on the remote build server only.');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
process.env.PLAYWRIGHT_BROWSERS_PATH ||= '/srv/dsh/build-runtime/ms-playwright';
const { chromium } = require(process.env.FABFLOW_PLAYWRIGHT_MODULE || '/srv/dsh/build-runtime/node_modules/playwright');
const target = new URL(process.env.TRIAL_URL || 'https://8.135.55.251/try/?mode=upload');
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
    let quotaResult = null;
    let quotaParseFailed = false;
    page.setDefaultTimeout(25000);
    page.on('pageerror', error => report.page_errors.push(error.message));
    page.on('response', response => {
      const url = new URL(response.url());
      if (['/workbench/customer/api/session', '/workbench/customer/api/quota', '/workbench/customer/api/orders'].includes(url.pathname))
        report.response_statuses[url.pathname] = response.status();
      if (url.pathname === '/workbench/customer/api/quota' && response.status() === 200) {
        void response.json().then(value => {
          if (value?.scope !== 'global' || value.limit !== 10 || !Number.isSafeInteger(value.remaining)
              || value.remaining < 0 || value.remaining > 10 || typeof value.blocked !== 'boolean') throw Error('Invalid server availability response.');
          quotaResult = { remaining: value.remaining, blocked: value.blocked };
        }).catch(() => { quotaParseFailed = true; });
      }
    });
    const response = await page.goto(target.href, { waitUntil: 'networkidle' });
    assert.equal(response.status(), 200);
    await page.getByTestId('trial-page').waitFor();
    await page.waitForFunction(() => document.querySelector('[data-testid="trial-page"]')?.getAttribute('data-session-ready') === 'true', null, { timeout: 25000 });
    const deadline = Date.now() + 25000;
    while ((!quotaResult || report.response_statuses['/workbench/customer/api/orders'] !== 200) && !quotaParseFailed && Date.now() < deadline)
      await new Promise(resolve => setTimeout(resolve, 50));
    assert(!quotaParseFailed && quotaResult, 'A valid server availability response is required.');
    for (const endpoint of ['session', 'quota', 'orders']) assert.equal(report.response_statuses['/workbench/customer/api/' + endpoint], 200);
    const remaining = quotaResult.remaining;
    assert(remaining >= 0 && remaining <= 10);
    if (quotaResult.blocked || remaining === 0) await page.waitForFunction(() => document.querySelector('[data-testid="trial-page"]')?.getAttribute('data-upload-state') === 'unavailable');
    assert.equal(await page.getByTestId('trial-file-input').isDisabled(), quotaResult.blocked || remaining === 0);
    const cookie = (await context.cookies(target.origin + '/workbench/customer/api/orders')).find(value => value.name === 'HYL_CUSTOMER_SESSION');
    assert(cookie && cookie.httpOnly && cookie.secure && cookie.path === '/workbench/customer/');
    assert.equal(await page.getByTestId('trial-history-order').count(), 0);
    assert.equal(await page.locator('#trial-history, .fft-history, .fft-footer, .fft-quota').count(), 0);
    await page.waitForFunction(() => document.querySelector('[data-testid="trial-page"]')?.getAttribute('data-lead-flow') !== 'loading');
    if (await page.getByTestId('trial-page').getAttribute('data-lead-flow') === 'enabled') {
      await page.getByTestId('trial-profile-form').waitFor();
      assert.equal(await page.getByTestId('trial-contact-consent').isChecked(), false);
      assert((await page.getByTestId('trial-contact-profile').innerText()).includes('未验证'));
      report.lead_capture = { enabled: true, verification_status: 'unverified', contact_opt_in_default: false };
    }
    const text = await page.locator('body').innerText();
    for (const phrase of ['我的询价', '本浏览器的提交记录', '刷新体验额度', '共享体验额度',
      'FabFlow · 从 CAD 到制造报价', '原生几何量测 · 工程候选待审核'])
      assert(!text.includes(phrase), 'Removed customer chrome is still visible: ' + phrase);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(report.page_errors, []);
    assert.deepEqual(report.blocked_requests, []);
    await page.screenshot({ path: path.join(output, 'live-entry.png'), fullPage: true });
    report.anonymous_session_verified = true;
    report.cookie_scope_preserved = true;
    report.quota_controls_and_history_hidden = true;
    report.remaining_uploads = remaining;
    report.passed = true;
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
  }
  console.log(JSON.stringify(report, null, 2));
}
main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
