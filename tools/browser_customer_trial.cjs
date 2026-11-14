'use strict';
// Server-only browser acceptance. Every customer API request is mocked; this
// script cannot upload to production, spend tokens, or consume upload quota.
if (process.platform !== 'linux') throw Error('Run only on the remote build server.');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
process.env.PLAYWRIGHT_BROWSERS_PATH ||= '/srv/dsh/build-runtime/ms-playwright';
const { chromium } = require(process.env.FABFLOW_PLAYWRIGHT_MODULE || '/srv/dsh/build-runtime/node_modules/playwright');
const origin = new URL(process.env.TRIAL_ORIGIN || process.env.FABFLOW_TRIAL_ORIGIN || 'https://8.135.55.251');
assert(['https:', 'http:'].includes(origin.protocol));
const targetUrl = new URL(process.env.TRIAL_PATH || '/try/?mode=upload', origin);
assert.equal(targetUrl.origin, origin.origin);
const target = targetUrl.href;
const output = process.env.TRIAL_BROWSER_OUT || process.env.FABFLOW_TRIAL_BROWSER_OUT;
assert(output && path.isAbsolute(output), 'Set a new absolute FABFLOW_TRIAL_BROWSER_OUT directory.');
assert(!fs.existsSync(output), 'Use a new output directory.');
fs.mkdirSync(output, { recursive: true, mode: 0o700 });
const report = { passed: false, target, checks: [], page_errors: [], blocked_requests: [],
  customer_api_mocked: true, real_uploads: 0, provider_requests: 0, production_quota_used: false };
const contextStates = new WeakMap();
const BATCH = 'BATCH-' + 'a'.repeat(32);
const RUN = 'RUN-' + 'b'.repeat(32);
const SOURCE_SHA = 'd'.repeat(64);
const API = '/workbench/customer/api';
const runPrefix = `${API}/orders/${BATCH}/runs/${RUN}`;
const quota = { limit: 10, used: 2, remaining: 8, scope: 'global', accepted_extensions: ['.step', '.stp'],
  max_files: 1, max_bytes: 20 * 1024 * 1024, retry_limit: 1, active: true,
  includes_existing_orders: false, unit: 'new_upload', blocked: false };
const order = (status = 'uploaded') => ({ id: BATCH, name: 'browser-fixture.step', status, revision: status === 'uploaded' ? 1 : 2,
  created_at: '2026-11-14T00:00:00Z', updated_at: '2026-11-14T00:00:00Z',
  files: [{ id: 'F0001', name: 'browser-fixture.step', kind: 'step', status: status === 'uploaded' ? 'uploaded' : 'running' }],
  progress: { total: 1, done: 0, message: status === 'uploaded' ? '合成资料已保存' : '合成后台处理状态，仅用于浏览器检查' },
  order_sets: 10, error: null, can_start: status === 'uploaded', can_retry: false,
  retry_limit: 1, retry_remaining: 1, quote: null });

const weightSummary = { status: 'complete', complete: true, unit: 'kg', role: 'cross_check', source: 'saved_line_weights',
  includes_packaging: false, order_sets: 10, line_count: 2, known_weight_lines: 2, missing_estimated_lines: 0,
  estimated_set_net_weight_kg: '1.50', estimated_total_net_weight_kg: '15.00',
  known_set_net_weight_kg: '1.50', known_total_net_weight_kg: '15.00',
  low_confidence_lines: 0, low_confidence_total_net_weight_kg: '0.00' };
const completedOrder = () => ({ ...order('completed'), revision: 3,
  files: [{ id: 'F0001', name: 'browser-fixture.step', kind: 'step', status: 'ready' }],
  progress: { total: 1, done: 1, message: '合成拆件与报价草稿已完成' }, can_start: false,
  quote: { run_id: RUN, total_price: '1234.50', estimated_subtotal: '1234.50', currency: 'CNY',
    status: 'pending_review', simulated: true, unpriced_lines: 0, quoted_lines: 2,
    quote_url: runPrefix + '/customer-quote.html', print_url: runPrefix + '/customer-print.html',
    manufacturing_url: runPrefix + '/manufacturing', pricing_status: 'estimated',
    costing_mode: 'synthetic-browser-fixture', weight_summary: weightSummary } });
const parts = [
  { id: 'L0001', part_name: '合成底板', source: 'browser-fixture.step', quantity_total: 10, quantity_per_set: 1,
    material: { text: 'SUS304', source: 'cad', adopted: false }, thickness_mm: '2.00', dimensions_mm: [200, 160, 2],
    estimated_net_weight_kg: '1.20', estimated_total_net_weight_kg: '12.00',
    geometry_url: runPrefix + '/customer-parts/L0001', thumbnail_url: runPrefix + '/customer-parts/L0001/thumbnail',
    blockers: [], processes: [{ id: 'laser', name: '激光切割', status: 'candidate' }] },
  { id: 'L0002', part_name: '合成侧板', source: 'browser-fixture.step', quantity_total: 10, quantity_per_set: 1,
    material: { text: 'SUS304', source: 'cad', adopted: false }, thickness_mm: '2.00', dimensions_mm: [200, 2, 40],
    estimated_net_weight_kg: '0.30', estimated_total_net_weight_kg: '3.00',
    geometry_url: runPrefix + '/customer-parts/L0002', thumbnail_url: runPrefix + '/customer-parts/L0002/thumbnail',
    blockers: [], processes: [{ id: 'laser', name: '激光切割', status: 'candidate' }] },
];
function boxMesh(x, y, z, dx, dy, dz) {
  return { positions: [x,y,z, x+dx,y,z, x+dx,y+dy,z, x,y+dy,z,
    x,y,z+dz, x+dx,y,z+dz, x+dx,y+dy,z+dz, x,y+dy,z+dz],
    indices: [0,2,1,0,3,2,4,5,6,4,6,7,0,1,5,0,5,4,1,2,6,1,6,5,2,3,7,2,7,6,3,0,4,3,4,7] };
}
const geometry = { source_sha256: SOURCE_SHA, output_unit: 'mm',
  solids: [{ id: 'S0001', name: '合成底板', mesh: boxMesh(0,0,0,200,160,2) },
           { id: 'S0002', name: '合成侧板', mesh: boxMesh(0,158,2,200,2,40) }] };
function partPayload(lineId) {
  const part = parts.find(item => item.id === lineId);
  assert(part);
  const solid = lineId === 'L0001' ? 'S0001' : 'S0002';
  return { batch_id: BATCH, run_id: RUN, line_id: lineId, part_name: part.part_name, source_document: part.source,
    source_sha256: SOURCE_SHA, geometry, up_axis: 'z', order_sets: 10, selected_id: lineId,
    groups: [{ id: 'L0001', name: parts[0].part_name, solid_ids: ['S0001'] },
             { id: 'L0002', name: parts[1].part_name, solid_ids: ['S0002'] }],
    representative_solid_id: solid, representative_solid_ids: [solid],
    occurrences: [{ id: lineId + '-instance-1', name: part.part_name, solid_ids: [solid] }],
    line_snapshot: { id: lineId, part_name: part.part_name, document: part.source, quantity_per_set: 1,
      quantity_total: 10, estimated_net_weight_kg: part.estimated_net_weight_kg,
      estimated_total_net_weight_kg: part.estimated_total_net_weight_kg } };
}
const thumbnail = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 140"><rect width="200" height="140" fill="#f1f5fb"/><path d="M30 100 120 122 175 82 85 61Z" fill="#99b4d4" stroke="#41658e"/><path d="M85 61V24L175 45V82" fill="#698db6" stroke="#41658e"/><path d="M30 100 85 61V24L30 64Z" fill="#bed0e5" stroke="#41658e"/></svg>';

async function mockedContext(browser, viewport, options = {}) {
  const context = await browser.newContext({ viewport, ignoreHTTPSErrors: true,
    reducedMotion: 'reduce', serviceWorkers: 'block' });
  context.setDefaultTimeout(25000);
  let releaseQuota;
  const quotaGate = new Promise(resolve => { releaseQuota = resolve; });
  const state = { created: !!options.completed, started: false, uploadEvents: [], startEvents: [], detailReads: 0,
    geometryReads: 0, artifactReads: 0, quotaReads: 0, quotaSuccesses: 0,
    geometryFailures: [...(options.geometryFailures || [])],
    quotaAvailable: !options.unavailableQuota, quotaReleased: !options.holdQuota,
    releaseQuota() { state.quotaReleased = true; releaseQuota(); }, unknown: [] };
  contextStates.set(context, state);
  const currentOrder = () => {
    const value = options.completed ? completedOrder() : order(state.started ? 'running' : 'uploaded');
    if (options.partial) value.quote = { ...value.quote, total_price: '1234.50', estimated_subtotal: null,
      unpriced_lines: 1, quoted_lines: 1, pricing_status: 'partial' };
    return value;
  };
  await context.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    if (url.origin !== origin.origin && !['data:', 'blob:', 'about:'].includes(url.protocol)) {
      report.blocked_requests.push(url.origin + url.pathname); return route.abort();
    }
    if (url.pathname === API || url.pathname.startsWith(API + '/')) {
      const suffix = url.pathname.slice(API.length);
      let body, status = 200;
      if (suffix === '/session' && method === 'POST') body = { ready: true };
      else if (suffix === '/trial/profile' && method === 'GET')
        body = { enabled: false, verification_available: false, profile: null };
      else if (suffix === '/quota' && method === 'GET') {
        state.quotaReads += 1;
        if (!state.quotaReleased) await quotaGate;
        if (!state.quotaAvailable) {
          status = 503; body = { error: { message: 'Synthetic availability check has not completed.' } };
        } else {
          const remaining = options.exhausted ? 0 : state.created ? 7 : 8;
          body = { ...quota, used: 10 - remaining, remaining, blocked: remaining === 0 };
        }
      }
      else if (suffix === '/orders' && method === 'GET') body = { orders: state.created ? [currentOrder()] : [] };
      else if (suffix === '/orders' && method === 'POST') {
        assert(state.quotaSuccesses > 0, 'A file cannot be uploaded before a successful server-side availability check.');
        assert.equal(url.searchParams.get('name'), 'browser-fixture.step');
        assert(request.headers()['x-event-id'], 'Upload must keep an idempotency event.');
        state.uploadEvents.push(request.headers()['x-event-id']); state.created = true;
        if (options.loseFirstUpload && state.uploadEvents.length === 1) return route.abort('failed');
        body = order(); status = 201;
      } else if (suffix === '/orders/' + BATCH + '/start' && method === 'POST') {
        const payload = request.postDataJSON();
        assert(payload.event_id); assert.equal(payload.order_sets, 10);
        state.startEvents.push(payload.event_id); state.started = true;
        body = order('queued'); status = 202;
      } else if (suffix === '/orders/' + BATCH && method === 'GET') {
        state.detailReads += 1;
        body = currentOrder();
      } else if (options.completed && method === 'GET' && suffix === `/orders/${BATCH}/runs/${RUN}/manufacturing`) {
        body = { batch_id: BATCH, run_id: RUN, status: 'pending_review', parts,
          summary: { parts_count: 2, quantity_total: 20, estimated_parts: 2, blocked_parts: 0 } };
      } else if (options.completed && method === 'GET' && new RegExp(`^/orders/${BATCH}/runs/${RUN}/customer-parts/L000[12]$`).test(suffix)) {
        state.geometryReads += 1;
        const failure = state.geometryFailures.shift();
        if (failure === 'network') return route.abort('failed');
        if (failure === 'json') return route.fulfill({ status: 200, contentType: 'application/json',
          body: '{"private":"synthetic-preview-body"' });
        if (typeof failure === 'number') return route.fulfill({ status: failure, contentType: 'application/json',
          body: '{"error":"synthetic-preview-body"}' });
        body = partPayload(suffix.split('/').pop());
        if (failure === 'source') body = { ...body, source_sha256: 'e'.repeat(64) };
      } else if (options.completed && method === 'GET' && new RegExp(`^/orders/${BATCH}/runs/${RUN}/customer-parts/L000[12]/thumbnail$`).test(suffix)) {
        return route.fulfill({ status: 200, contentType: 'image/svg+xml', body: thumbnail });
      } else if (options.completed && method === 'GET' && (suffix === `/orders/${BATCH}/runs/${RUN}/customer-quote.html` || suffix === `/orders/${BATCH}/runs/${RUN}/customer-print.html`)) {
        state.artifactReads += 1;
        return route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><html lang="zh-CN"><body><h1>合成客户报价单</h1><p>10套 · SUS304 · 2项物料</p><strong>¥1,234.50</strong><p>报价草稿 · 待审核</p></body></html>' });
      }
      else { state.unknown.push(method + ' ' + suffix); body = { error: { message: 'Unexpected mock request.' } }; status = 404; }
      await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body),
        headers: { 'Cache-Control': 'no-store' } });
      if (suffix === '/quota' && status === 200) state.quotaSuccesses += 1;
      return;
    }
    // Public shells and fixed bundled assets are GET-only. Never let an
    // accidental endpoint change turn this acceptance into a real submission.
    if (!['GET', 'HEAD'].includes(method)) {
      report.blocked_requests.push(method + ' ' + url.pathname); return route.abort();
    }
    return route.continue();
  });
  return { context, state };
}

async function fits(page, label) {
  const bounds = await page.evaluate(() => ({ width: document.documentElement.scrollWidth, viewport: innerWidth }));
  assert(bounds.width <= bounds.viewport + 2, label + ': ' + JSON.stringify(bounds));
}

async function until(predicate, message, timeout = 20000) {
  const deadline = Date.now() + timeout;
  while (!predicate() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 50));
  assert(predicate(), message);
}

async function simplifiedChrome(page) {
  assert.equal(await page.locator('#trial-history, .fft-history, .fft-footer, .fft-quota').count(), 0);
  assert.equal(await page.getByTestId('trial-history-order').count(), 0);
  const text = await page.locator('body').innerText();
  for (const phrase of ['请先刷新体验额度', '刷新体验额度', '共享体验额度', '本浏览器的提交记录', '我的询价',
    'FabFlow · 从 CAD 到制造报价', '原生几何量测 · 工程候选待审核'])
    assert(!text.includes(phrase), 'Removed customer chrome is still visible: ' + phrase);
}

async function open(context, { exhausted = false, quotaPending = false, waitUntil = 'networkidle' } = {}) {
  const page = await context.newPage();
  page.on('pageerror', error => report.page_errors.push(error.message));
  await page.goto(target, { waitUntil });
  await page.getByTestId('trial-page').waitFor();
  await page.getByTestId('trial-file-input').waitFor({ state: 'attached' });
  assert.equal(await page.getByTestId('trial-file-input').getAttribute('multiple'), null);
  const accept = await page.getByTestId('trial-file-input').getAttribute('accept');
  assert(accept.includes('.step') && accept.includes('.stp'));
  assert(!/zip|rar|pdf/i.test(accept));
  await page.waitForFunction(() => document.querySelector('[data-testid="trial-page"]')?.getAttribute('data-session-ready') === 'true');
  if (!quotaPending) await until(() => contextStates.get(context).quotaSuccesses > 0, 'Initial availability check did not complete.');
  if (exhausted) await page.waitForFunction(() => document.querySelector('[data-testid="trial-page"]')?.getAttribute('data-upload-state') === 'unavailable');
  else await page.waitForFunction(() => !document.querySelector('[data-testid="trial-file-input"]')?.disabled);
  await simplifiedChrome(page);
  return page;
}

async function main() {
  const browser = await chromium.launch({ headless: true, chromiumSandbox: false,
    args: ['--disable-dev-shm-usage', '--renderer-process-limit=1', '--no-first-run', '--no-proxy-server',
      '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  try {
    const desktop = await mockedContext(browser, { width: 1440, height: 1024 });
    const page = await open(desktop.context);
    await fits(page, 'Desktop');
    await page.screenshot({ path: path.join(output, 'desktop-entry.png'), fullPage: true });
    report.checks.push('Dedicated desktop entry hydrates without customer API access or overflow.');
    report.checks.push('Customer quota controls, inquiry-history navigation/list and footer copy are absent.');
    const input = page.getByTestId('trial-file-input');
    await input.setInputFiles({ name: 'unsupported.zip', mimeType: 'application/zip', buffer: Buffer.from('not-an-archive') });
    await page.getByTestId('trial-error').waitFor();
    assert(/STEP.*STP/.test(await page.getByTestId('trial-error').innerText()));
    assert.equal(await page.getByTestId('trial-submit').isDisabled(), true);
    const transfer = await page.evaluateHandle(() => {
      const result = new DataTransfer();
      result.items.add(new File(['synthetic-one'], 'one.step', { type: 'application/octet-stream' }));
      result.items.add(new File(['synthetic-two'], 'two.stp', { type: 'application/octet-stream' }));
      return result;
    });
    await page.getByTestId('trial-dropzone').dispatchEvent('drop', { dataTransfer: transfer });
    await transfer.dispose();
    await page.waitForFunction(() => document.querySelector('[data-testid="trial-error"]')?.textContent?.includes('一份'));
    assert.equal(await page.getByTestId('trial-submit').isDisabled(), true);
    assert.equal(desktop.state.uploadEvents.length, 0);
    report.checks.push('ZIP selection and multiple dropped STEP files are rejected before any upload.');
    await input.setInputFiles({ name: 'browser-fixture.step', mimeType: 'application/octet-stream',
      buffer: Buffer.from("ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n") });
    await page.getByTestId('trial-quantity').fill('10');
    await page.getByTestId('trial-submit').click();
    await page.getByTestId('trial-current-order').waitFor();
    // The page should explicitly start the saved order as part of the same
    // customer action, then observe only by GET while background work runs.
    await page.waitForFunction(() => document.querySelector('[data-testid="trial-current-order"]')?.textContent?.includes('后台')
      || document.querySelector('[data-testid="trial-current-order"]')?.textContent?.includes('处理'), null, { timeout: 15000 });
    const pollDeadline = Date.now() + 12000;
    while (!desktop.state.detailReads && Date.now() < pollDeadline) await new Promise(resolve => setTimeout(resolve, 100));
    assert(desktop.state.detailReads >= 1, 'Background work must be observed by GET polling.');
    assert.equal(desktop.state.uploadEvents.length, 1);
    assert.equal(desktop.state.startEvents.length, 1);
    assert.equal(desktop.state.unknown.length, 0, desktop.state.unknown.join(', '));
    await fits(page, 'Desktop submitted state');
    await page.screenshot({ path: path.join(output, 'desktop-background.png'), fullPage: true });
    report.checks.push('Synthetic STEP submission makes one upload and one start; pending work is observed without automatic resubmission.');
    await desktop.context.close();

    const mobile = await mockedContext(browser, { width: 390, height: 844 });
    const mobilePage = await open(mobile.context);
    await fits(mobilePage, 'Mobile');
    await mobilePage.screenshot({ path: path.join(output, 'mobile-entry.png'), fullPage: true });
    assert.equal(mobile.state.uploadEvents.length, 0);
    assert.equal(mobile.state.startEvents.length, 0);
    assert.equal(mobile.state.unknown.length, 0, mobile.state.unknown.join(', '));
    await mobile.context.close();
    report.checks.push('Mobile entry fits the viewport and does not submit on load.');

    const exhausted = await mockedContext(browser, { width: 390, height: 844 }, { exhausted: true });
    const exhaustedPage = await open(exhausted.context, { exhausted: true });
    await exhaustedPage.getByTestId('trial-upload-unavailable').waitFor();
    assert((await exhaustedPage.getByTestId('trial-upload-unavailable').innerText()).includes('当前暂不接受新文件'));
    assert.equal(await exhaustedPage.getByTestId('trial-file-input').isDisabled(), true);
    assert.equal(await exhaustedPage.getByTestId('trial-submit').isDisabled(), true);
    assert.equal(exhausted.state.uploadEvents.length, 0);
    await exhausted.context.close();
    report.checks.push('Exhausted shared quota disables new upload controls without submitting a request.');

    const delayed = await mockedContext(browser, { width: 1440, height: 1024 }, { holdQuota: true });
    const delayedPage = await open(delayed.context, { quotaPending: true, waitUntil: 'domcontentloaded' });
    await until(() => delayed.state.quotaReads > 0, 'The background availability check was not requested.');
    await delayedPage.getByTestId('trial-file-input').setInputFiles({ name: 'browser-fixture.step', mimeType: 'application/octet-stream',
      buffer: Buffer.from('ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n') });
    await delayedPage.getByTestId('trial-quantity').fill('10');
    assert((await delayedPage.getByTestId('trial-dropzone').innerText()).includes('browser-fixture.step'));
    assert.equal(delayed.state.uploadEvents.length, 0);
    assert.equal(delayed.state.quotaSuccesses, 0);
    await simplifiedChrome(delayedPage);
    const delayedSubmit = delayedPage.getByTestId('trial-submit');
    const submittedWhileChecking = !await delayedSubmit.isDisabled();
    if (submittedWhileChecking) {
      await delayedSubmit.click();
      await until(() => delayed.state.quotaReads >= 2, 'Submission must perform a fresh server check.');
      assert.equal(delayed.state.uploadEvents.length, 0);
    }
    delayed.state.releaseQuota();
    if (!submittedWhileChecking) {
      await until(() => delayed.state.quotaSuccesses > 0, 'Delayed server check did not complete.');
      await delayedSubmit.click();
    }
    await delayedPage.getByTestId('trial-progress').waitFor();
    assert.equal(delayed.state.uploadEvents.length, 1);
    assert.equal(delayed.state.startEvents.length, 1);
    assert(delayed.state.quotaSuccesses > 0);
    await delayed.context.close();
    report.checks.push('A STEP can be selected while availability is pending; upload waits for a successful server check.');

    const unavailable = await mockedContext(browser, { width: 1440, height: 1024 }, { unavailableQuota: true });
    const unavailablePage = await open(unavailable.context, { quotaPending: true });
    await until(() => unavailable.state.quotaReads > 0, 'Initial unavailable check was not requested.');
    await unavailablePage.getByTestId('trial-file-input').setInputFiles({ name: 'browser-fixture.step', mimeType: 'application/octet-stream',
      buffer: Buffer.from('ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n') });
    await unavailablePage.getByTestId('trial-quantity').fill('10');
    await simplifiedChrome(unavailablePage);
    const unavailableReads = unavailable.state.quotaReads;
    await unavailablePage.getByTestId('trial-submit').click();
    await until(() => unavailable.state.quotaReads > unavailableReads, 'Submission did not recheck server availability.');
    await unavailablePage.getByTestId('trial-error').waitFor();
    assert.equal(unavailable.state.uploadEvents.length, 0);
    assert.equal(unavailable.state.startEvents.length, 0);
    assert.equal(unavailable.state.quotaSuccesses, 0);
    await simplifiedChrome(unavailablePage);
    unavailable.state.quotaAvailable = true;
    await unavailablePage.getByTestId('trial-submit').click();
    await unavailablePage.getByTestId('trial-progress').waitFor();
    assert.equal(unavailable.state.uploadEvents.length, 1);
    assert.equal(unavailable.state.startEvents.length, 1);
    await unavailable.context.close();
    report.checks.push('An unavailable background check does not block choosing a file or permit upload; an explicit retry rechecks before one submission.');

    const recovery = await mockedContext(browser, { width: 1440, height: 1024 }, { loseFirstUpload: true });
    const recoveryPage = await open(recovery.context);
    await recoveryPage.getByTestId('trial-file-input').setInputFiles({ name: 'browser-fixture.step', mimeType: 'application/octet-stream',
      buffer: Buffer.from('ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n') });
    await recoveryPage.getByTestId('trial-quantity').fill('10');
    await recoveryPage.getByTestId('trial-submit').click();
    await recoveryPage.getByTestId('trial-recover').waitFor();
    assert.equal(recovery.state.uploadEvents.length, 1);
    assert.equal(recovery.state.startEvents.length, 0);
    assert.equal(await recoveryPage.getByTestId('trial-file-input').isDisabled(), true);
    await recoveryPage.getByTestId('trial-recover').click();
    await recoveryPage.getByTestId('trial-current-order').waitFor();
    await recoveryPage.getByTestId('trial-progress').waitFor();
    assert.equal(recovery.state.uploadEvents.length, 2);
    assert.equal(new Set(recovery.state.uploadEvents).size, 1, 'Lost upload acknowledgement must reuse its original event.');
    assert.equal(recovery.state.startEvents.length, 1);
    assert.equal(recovery.state.unknown.length, 0, recovery.state.unknown.join(', '));
    await recoveryPage.screenshot({ path: path.join(output, 'recovered-upload.png'), fullPage: true });
    await recovery.context.close();
    report.checks.push('Lost upload acknowledgement requires explicit recovery with the same upload event and starts processing once.');

    const completed = await mockedContext(browser, { width: 1440, height: 1100 }, { completed: true });
    await completed.context.addInitScript(id => localStorage.setItem('fabflow-trial-last-order', id), BATCH);
    const completedPage = await completed.context.newPage();
    completedPage.on('pageerror', error => report.page_errors.push(error.message));
    await completedPage.goto(target, { waitUntil: 'networkidle' });
    await completedPage.getByTestId('trial-model').waitFor();
    await completedPage.waitForFunction(() => document.querySelector('[data-testid="customer-original-assembly"]')?.getAttribute('data-state') === 'ready', null, { timeout: 45000 });
    const nativeCanvas = completedPage.getByTestId('customer-original-canvas');
    assert.equal(await nativeCanvas.getAttribute('data-visible-solid-count'), '2');
    assert.equal(await nativeCanvas.getAttribute('data-output-unit'), 'mm');
    assert.equal(await nativeCanvas.getAttribute('data-source-sha256'), SOURCE_SHA);
    assert.equal(await nativeCanvas.locator('canvas').count(), 1, 'The actual native-geometry renderer must mount its canvas.');
    assert(completed.state.geometryReads >= 1);
    await completedPage.getByTestId('customer-original-reset').click();
    await fits(completedPage, 'Completed native model');
    await completedPage.screenshot({ path: path.join(output, 'desktop-completed.png'), fullPage: true });
    report.checks.push('Saved completed order restores its owner-bound two-solid mm model through the actual WebGL geometry renderer.');

    const current = completedPage.getByTestId('trial-current-order');
    await current.getByRole('button', { name: /^零件清单/ }).click();
    const partView = completedPage.getByTestId('trial-parts');
    await partView.waitFor();
    assert((await partView.innerText()).includes('SUS304'));
    assert((await partView.innerText()).includes('2.00 mm'));
    assert((await partView.innerText()).includes('1.20 kg'));
    await partView.getByRole('button', { name: /合成侧板/ }).click();
    assert((await partView.innerText()).includes('300.00 g'));
    assert((await partView.innerText()).includes('200.00 × 2.00 × 40.00 mm'));
    await fits(completedPage, 'Completed parts and parameters');
    await completedPage.screenshot({ path: path.join(output, 'desktop-parts.png'), fullPage: true });
    report.checks.push('Part selection displays the bound material, 2mm thickness, finished dimensions and per-piece estimated weight.');

    await current.getByRole('button', { name: '报价草稿', exact: true }).click();
    const quoteView = completedPage.getByTestId('trial-quote');
    await quoteView.waitFor();
    assert.equal(await quoteView.getAttribute('data-amount-scope'), 'order_total');
    assert((await quoteView.innerText()).includes('¥1,234.50'));
    assert((await quoteView.innerText()).includes('15.00 kg'));
    assert.equal(await quoteView.getByRole('link', { name: /查看客户报价单/ }).getAttribute('href'), runPrefix + '/customer-quote.html');
    assert.equal(await quoteView.getByRole('link', { name: /打印.*PDF/ }).getAttribute('href'), runPrefix + '/customer-print.html');
    const visibleText = await completedPage.locator('body').innerText();
    for (const phrase of ['材料成本', '加工成本', '内部成本', '内部核价', '利润']) assert(!visibleText.includes(phrase), 'Customer view exposed internal costing label ' + phrase);
    await quoteView.getByRole('button', { name: /在本页预览客户报价/ }).click();
    const customerQuote = completedPage.frameLocator('iframe[title="客户报价草稿预览"]').locator('body');
    await customerQuote.waitFor();
    assert((await customerQuote.innerText()).includes('¥1,234.50'));
    assert(completed.state.artifactReads >= 1);
    assert.equal(completed.state.uploadEvents.length, 0);
    assert.equal(completed.state.startEvents.length, 0);
    assert.equal(completed.state.unknown.length, 0, completed.state.unknown.join(', '));
    await fits(completedPage, 'Completed customer quotation');
    await completedPage.screenshot({ path: path.join(output, 'desktop-quote.png'), fullPage: true });
    await completed.context.close();
    report.checks.push('Completed customer quote displays the saved order total, bound quote/print URLs and inline customer document without internal costs or new paid work.');

    const failureCases = [
      ['network', '原图连接未完成'], ['json', '原图数据未完整接收'],
      [401, '当前访问凭据已失效'], [403, '当前浏览器无法访问'],
      [404, '原图预览资料暂不可用'], [429, '原图服务正忙'], [503, '原图服务正忙'],
      ['source', '当前来源尚无可显示的完整原图'],
    ];
    const interrupted = await mockedContext(browser, { width: 1440, height: 1100 },
      { completed: true, geometryFailures: failureCases.map(([failure]) => failure) });
    await interrupted.context.addInitScript(id => localStorage.setItem('fabflow-trial-last-order', id), BATCH);
    const interruptedPage = await interrupted.context.newPage();
    interruptedPage.on('pageerror', error => report.page_errors.push(error.message));
    await interruptedPage.goto(target, { waitUntil: 'networkidle' });
    for (const [index, [, expected]] of failureCases.entries()) {
      await until(() => interrupted.state.geometryReads === index + 1, 'Manual preview recovery did not make exactly one geometry request.');
      await interruptedPage.waitForFunction(message => document.querySelector('[data-testid="customer-original-error"]')?.textContent?.includes(message), expected);
      const message = await interruptedPage.getByTestId('customer-original-error').innerText();
      for (const privateText of ['Failed to fetch', 'SyntaxError', 'synthetic-preview-body', runPrefix])
        assert(!message.includes(privateText), 'Preview error exposed raw diagnostics or response content.');
      assert.equal(await interruptedPage.getByTestId('customer-original-solid-count').count(), 0);
      await interruptedPage.waitForTimeout(300);
      assert.equal(interrupted.state.geometryReads, index + 1, 'A failed large geometry request must not retry automatically.');
      if (index === 0) await interruptedPage.screenshot({ path: path.join(output, 'desktop-preview-network-error.png'), fullPage: true });
      await interruptedPage.getByTestId('customer-original-retry').click();
    }
    await interruptedPage.waitForFunction(() => document.querySelector('[data-testid="customer-original-assembly"]')?.getAttribute('data-state') === 'ready', null, { timeout: 45000 });
    assert.equal(interrupted.state.geometryReads, failureCases.length + 1);
    assert.equal(await interruptedPage.getByTestId('customer-original-canvas').getAttribute('data-visible-solid-count'), '2');
    assert.equal(await interruptedPage.getByTestId('customer-original-canvas').locator('canvas').count(), 1);
    assert.equal(interrupted.state.uploadEvents.length, 0);
    assert.equal(interrupted.state.startEvents.length, 0);
    assert.equal(interrupted.state.unknown.length, 0, interrupted.state.unknown.join(', '));
    await interruptedPage.screenshot({ path: path.join(output, 'desktop-preview-recovered.png'), fullPage: true });
    await interrupted.context.close();
    report.checks.push('Network failure, incomplete JSON, denied/missing/busy responses and a mismatched source show safe Chinese errors; only manual retries request geometry and recover the complete native model without uploads or paid work.');

    const partial = await mockedContext(browser, { width: 1440, height: 1024 }, { completed: true, partial: true });
    await partial.context.addInitScript(id => localStorage.setItem('fabflow-trial-last-order', id), BATCH);
    const partialPage = await partial.context.newPage();
    partialPage.on('pageerror', error => report.page_errors.push(error.message));
    await partialPage.goto(target, { waitUntil: 'networkidle' });
    const partialCurrent = partialPage.getByTestId('trial-current-order');
    await partialCurrent.getByRole('button', { name: '报价草稿', exact: true }).click();
    const partialQuote = partialPage.getByTestId('trial-quote');
    await partialQuote.waitFor();
    assert.equal(await partialQuote.getAttribute('data-amount-scope'), 'known_subtotal');
    const partialText = await partialQuote.innerText();
    assert(partialText.includes('已知分项小计'));
    assert(/1\s*项.*未计价/.test(partialText));
    assert(!partialText.includes('订单估价草稿'));
    assert.equal(partial.state.uploadEvents.length, 0);
    assert.equal(partial.state.startEvents.length, 0);
    assert.equal(partial.state.unknown.length, 0, partial.state.unknown.join(', '));
    await fits(partialPage, 'Partial quotation scope');
    await partialPage.screenshot({ path: path.join(output, 'desktop-partial.png'), fullPage: true });
    await partial.context.close();
    report.checks.push('Legacy partial quotation with an unpriced line is labelled as a known subtotal, never as a complete order total.');
    assert.deepEqual(report.page_errors, []);
    report.passed = true;
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
  }
  console.log(JSON.stringify({ passed: report.passed, checks: report.checks, production_quota_used: false, output }, null, 2));
}
main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
