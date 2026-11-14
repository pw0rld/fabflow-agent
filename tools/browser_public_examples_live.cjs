'use strict';
// Production smoke: public rendering, then an explicit private-entry session.
// No file upload, lead submission, model call or quotation mutation is allowed.
if (process.platform !== 'linux') throw Error('Run on ZeroClave only.');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
process.env.PLAYWRIGHT_BROWSERS_PATH ||= '/srv/dsh/build-runtime/ms-playwright';
const { chromium } = require('/srv/dsh/build-runtime/node_modules/playwright');
const target = new URL(process.env.EXAMPLES_ORIGIN || 'https://8.135.55.251');
const output = process.env.EXAMPLES_BROWSER_OUT;
assert(output && path.isAbsolute(output) && !fs.existsSync(output));
fs.mkdirSync(output, { recursive: true, mode: 0o700 });

async function main() {
  const report = { passed: false, page_errors: [], blocked_requests: [],
    public_customer_requests: 0, uploads: 0, provider_requests: 0, production_quota_used: false };
  let phase = 'public', publicReads = 0;
  const browser = await chromium.launch({ headless: true, chromiumSandbox: false,
    args: ['--disable-dev-shm-usage', '--renderer-process-limit=1', '--no-first-run', '--no-proxy-server',
      '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1080 }, ignoreHTTPSErrors: true, serviceWorkers: 'block' });
    await context.addInitScript(() => localStorage.setItem('fabflow-trial-last-order', 'BATCH-' + 'a'.repeat(32)));
    await context.route('**/*', route => {
      const r = route.request(), url = new URL(r.url());
      if (['data:', 'blob:'].includes(url.protocol)) return route.continue();
      if (phase === 'public' && url.pathname.startsWith('/workbench/customer/api/')) report.public_customer_requests++;
      if (url.pathname.startsWith('/workbench/public-examples/')) publicReads++;
      const read = ['GET', 'HEAD'].includes(r.method());
      const session = phase === 'private' && r.method() === 'POST' && url.pathname === '/workbench/customer/api/session';
      if (url.origin !== target.origin || !read && !session) {
        report.blocked_requests.push(r.method() + ' ' + url.pathname); return route.abort();
      }
      return route.continue();
    });
    const page = await context.newPage(); page.setDefaultTimeout(30000);
    page.on('pageerror', error => report.page_errors.push(error.message));
    assert.equal((await page.goto(new URL('/try/', target).href, { waitUntil: 'networkidle' })).status(), 200);
    await page.waitForFunction(() => document.querySelector('[data-testid="public-example-viewer"]')?.getAttribute('data-state') === 'ready', null, { timeout: 60000 });
    assert.equal(await page.getByTestId('public-examples-page').getAttribute('data-example-id'), 'motor-bracket');
    assert.equal(await page.getByTestId('trial-page').count(), 0);
    assert.equal(report.public_customer_requests, 0);
    assert(!(await page.locator('body').innerText()).includes('ybm-fhz'));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: path.join(output, 'live-public.png'), fullPage: true, animations: 'disabled' });
    const before = publicReads;
    phase = 'private';
    await page.getByTestId('public-example-upload-own').click();
    await page.getByTestId('trial-page').waitFor();
    await page.waitForFunction(() => document.querySelector('[data-testid="trial-page"]')?.getAttribute('data-session-ready') === 'true');
    await page.getByTestId('trial-profile-form').waitFor();
    assert.equal(new URL(page.url()).searchParams.get('mode'), 'upload');
    assert.equal(await page.getByTestId('public-examples-page').count(), 0);
    assert.equal(await page.getByTestId('trial-contact-consent').isChecked(), false);
    assert.equal(publicReads, before);
    assert((await context.cookies(target.origin + '/workbench/customer/api/orders')).some(c => c.name === 'HYL_CUSTOMER_SESSION' && c.httpOnly && c.secure));
    assert.deepEqual(report.page_errors, []); assert.deepEqual(report.blocked_requests, []);
    report.explicit_private_entry = true; report.passed = true;
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
  }
  console.log(JSON.stringify(report, null, 2));
}
main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
