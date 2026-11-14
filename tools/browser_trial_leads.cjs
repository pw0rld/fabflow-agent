'use strict';
// Remote browser acceptance with synthetic fixtures only. Every customer and
// staff API is intercepted; no upload, SMS, model or production data is used.
if (process.platform !== 'linux') throw Error('Run on the remote build server only.');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
process.env.PLAYWRIGHT_BROWSERS_PATH ||= '/srv/dsh/build-runtime/ms-playwright';
const { chromium } = require(process.env.FABFLOW_PLAYWRIGHT_MODULE || '/srv/dsh/build-runtime/node_modules/playwright');
const origin = new URL(process.env.TRIAL_ORIGIN || 'https://8.135.55.251');
const output = process.env.LEADS_BROWSER_OUT;
assert(['http:', 'https:'].includes(origin.protocol));
assert(output && path.isAbsolute(output) && !fs.existsSync(output), 'Set a new private LEADS_BROWSER_OUT directory.');
fs.mkdirSync(output, { recursive: true, mode: 0o700 });
const CUSTOMER = '/workbench/customer/api';
const ADMIN = '/ortho2cad-test/customer-upload-admin/api/trial-leads';
const BATCH = 'BATCH-' + 'a'.repeat(32), RUN = 'RUN-' + 'b'.repeat(32);
const LEAD = 'LEAD-' + 'c'.repeat(32), OTHER = 'LEAD-' + 'd'.repeat(32), APP = 'APP-' + 'e'.repeat(32);
const SHA = 'f'.repeat(64), PHONE = '19900000001', OTHER_PHONE = '19900000009';
const runPrefix = `${CUSTOMER}/orders/${BATCH}/runs/${RUN}`;
const report = { passed: false, checks: [], page_errors: [], blocked_requests: [],
  api_mocked: true, real_uploads: 0, provider_requests: 0, production_quota_used: false };
const base = { id: LEAD, revision: 1, company: '合成钣金体验工厂', contact_name: '测试联系人',
  contact_method: 'phone', contact_value: PHONE, contact_masked: '199****0001', contact_revealed: true,
  manufacturing_type: 'sheet_metal', verification_status: 'unverified', contact_consent: true,
  can_contact: true, status: 'new', assignee: '', note: '', created_at: '2026-11-14T01:00:00Z',
  updated_at: '2026-11-14T01:00:00Z', applications: [], audit: [] };
const binding = { batch_id: BATCH, run_id: RUN, name: 'flow-fixture.step', status: 'completed', part_count: 1 };
const trial = { id: 'TRIAL-' + '1'.repeat(32), ...binding, order_binding: binding,
  created_at: '2026-11-14T01:30:00Z', updated_at: '2026-11-14T01:45:00Z' };
const unrequestedTrial = { ...trial, id: 'TRIAL-' + '2'.repeat(32), name: 'no-request-fixture.step',
  status: 'running', part_count: 0,
  order_binding: { ...binding, name: 'no-request-fixture.step', status: 'running', part_count: 0 } };
const application = { id: APP, interest: 'factory_trial', note: '合成需求：希望接入本厂工序费率。',
  status: 'submitted', follow_up_status: 'new', contact_consent: true, can_contact: true,
  created_at: '2026-11-14T02:00:00Z', order_binding: binding };
const summary = { status: 'complete', complete: true, unit: 'kg', role: 'cross_check', source: 'saved_line_weights',
  includes_packaging: false, order_sets: 10, line_count: 1, known_weight_lines: 1,
  missing_estimated_lines: 0, estimated_set_net_weight_kg: '0.06', estimated_total_net_weight_kg: '0.60',
  known_set_net_weight_kg: '0.06', known_total_net_weight_kg: '0.60', low_confidence_lines: 0,
  low_confidence_total_net_weight_kg: '0.00' };
const order = (completed = false) => ({ id: BATCH, name: 'flow-fixture.step', status: completed ? 'completed' : 'uploaded',
  revision: completed ? 3 : 1, created_at: '2026-11-14T02:00:00Z', updated_at: '2026-11-14T02:00:00Z', order_sets: 10,
  files: [{ id: 'F0001', name: 'flow-fixture.step', kind: 'step', status: completed ? 'ready' : 'uploaded' }],
  progress: { total: 1, done: completed ? 1 : 0, message: '合成浏览器流程，仅用于验收' }, error: null,
  can_start: !completed, can_retry: false, retry_limit: 1, retry_remaining: 1,
  quote: completed ? { run_id: RUN, total_price: '120.00', currency: 'CNY', status: 'pending_review', simulated: true,
    unpriced_lines: 0, quoted_lines: 1, weight_summary: summary, quote_url: runPrefix + '/customer-quote.html',
    print_url: runPrefix + '/customer-print.html', manufacturing_url: runPrefix + '/manufacturing',
    pricing_status: 'estimated', costing_mode: 'browser-fixture' } : null });
const part = { id: 'L0001', part_name: '合成测试底板', source: 'flow-fixture.step', quantity_total: 10,
  quantity_per_set: 1, material: { text: 'SUS304', source: 'cad' }, thickness_mm: '2.00', dimensions_mm: [80, 50, 2],
  estimated_net_weight_kg: '0.06', estimated_total_net_weight_kg: '0.60', blockers: [],
  processes: [{ id: 'laser', name: '激光切割', status: 'candidate' }],
  geometry_url: runPrefix + '/customer-parts/L0001', thumbnail_url: runPrefix + '/customer-parts/L0001/thumbnail' };
const mesh = { positions: [0,0,0,80,0,0,80,50,0,0,50,0,0,0,2,80,0,2,80,50,2,0,50,2],
  indices: [0,2,1,0,3,2,4,5,6,4,6,7,0,1,5,0,5,4,1,2,6,1,6,5,2,3,7,2,7,6,3,0,4,3,4,7] };
const geometryPayload = { batch_id: BATCH, run_id: RUN, line_id: 'L0001', source_sha256: SHA,
  geometry: { source_sha256: SHA, output_unit: 'mm', solids: [{ id: 'S0001', name: part.part_name, mesh }] },
  groups: [{ id: 'L0001', name: part.part_name, solid_ids: ['S0001'] }], selected_id: 'L0001', up_axis: 'z',
  representative_solid_id: 'S0001', representative_solid_ids: ['S0001'], occurrences: [{ id: 'O0001', solid_ids: ['S0001'] }],
  order_sets: 10, line_snapshot: { id: 'L0001', part_name: part.part_name, document: part.source,
    quantity_per_set: 1, quantity_total: 10, material_raw: 'SUS304', estimated_net_weight_kg: '0.06' } };
const image = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 80"><rect x="9" y="17" width="82" height="46" rx="3" fill="#9cbedb" stroke="#527b9f"/><text x="50" y="43" text-anchor="middle" font-size="9" fill="#36546e">SYNTHETIC</text></svg>';

const copy = value => JSON.parse(JSON.stringify(value));
function masked(value) { return { ...copy(value), contact_value: value.contact_masked, contact_revealed: false,
  applications: undefined, trials: undefined, audit: undefined, note: undefined }; }
async function mocked(browser, viewport, options = {}) {
  const context = await browser.newContext({ viewport, ignoreHTTPSErrors: true, reducedMotion: 'reduce', serviceWorkers: 'block' });
  context.setDefaultTimeout(35000);
  const state = { profile: null, created: false, started: false, uploads: [], starts: [], profileEvents: [],
    interestEvents: [], updates: [], applications: [], geometryReads: 0,
    staff: [copy({ ...base, application_count: 1, latest_application: application, applications: [application],
      trial_count: 1, latest_trial: trial, trials: [trial] }),
      copy({ ...base, id: OTHER, company: '合成暂不联系工厂', contact_consent: false, can_contact: false,
        contact_value: OTHER_PHONE, contact_masked: '199****0009', contact_revealed: false, status: 'do_not_contact',
        application_count: 0, latest_application: null, applications: [], trial_count: 1,
        latest_trial: unrequestedTrial, trials: [unrequestedTrial] })] };
  const knownProfile = new Map(), knownInterest = new Map(), knownUpdate = new Map();
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), method = request.method();
    if (['data:', 'blob:', 'about:'].includes(url.protocol)) return route.continue();
    const reject = () => { report.blocked_requests.push(method + ' ' + url.origin + url.pathname); return route.abort(); };
    if (url.origin !== origin.origin) return reject();
    const reply = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (url.pathname.startsWith(CUSTOMER + '/')) {
      const suffix = url.pathname.slice(CUSTOMER.length);
      if (suffix === '/session' && method === 'POST') return reply({ ready: true });
      if (suffix === '/quota' && method === 'GET') return reply({ limit: 10, used: 2, remaining: 8, scope: 'global',
        max_files: 1, max_bytes: 20 * 1024 * 1024, blocked: false, accepted_extensions: ['.step', '.stp'] });
      if (suffix === '/trial/profile' && method === 'GET') return reply({ enabled: true, verification_available: false, profile: state.profile });
      if (suffix === '/trial/profile' && method === 'POST') {
        const value = request.postDataJSON(); assert(value.event_id); state.profileEvents.push(value.event_id);
        if (knownProfile.has(value.event_id)) return reply({ profile: knownProfile.get(value.event_id), replayed: true });
        assert.equal(value.expected_revision, state.profile?.revision || 0);
        assert.equal(value.verification_status, undefined, 'Client cannot claim verification.');
        state.profile = { ...copy(base), ...value, id: LEAD, revision: (state.profile?.revision || 0) + 1,
          verification_status: 'unverified', contact_masked: '199****0001', can_contact: value.contact_consent };
        knownProfile.set(value.event_id, copy(state.profile));
        return reply({ profile: state.profile });
      }
      if (suffix === '/trial/applications' && method === 'GET') return reply({ applications: state.applications });
      if (suffix === '/trial/applications' && method === 'POST') {
        assert(state.profile); const value = request.postDataJSON(); assert(value.event_id);
        assert.equal(value.batch_id, BATCH); assert.equal(value.run_id, RUN); assert.equal(value.contact_consent, true);
        state.interestEvents.push(value.event_id);
        if (knownInterest.has(value.event_id)) return reply({ ...knownInterest.get(value.event_id), replayed: true });
        state.profile = { ...state.profile, revision: state.profile.revision + 1, contact_consent: true, can_contact: true };
        const saved = { ...copy(application), interest: value.interest, note: value.note };
        state.applications.push(saved); const result = { profile: state.profile, application: saved, replayed: false };
        knownInterest.set(value.event_id, copy(result));
        if (options.loseInterest && state.interestEvents.length === 1) return route.abort('failed');
        return reply(result, 201);
      }
      if (suffix === '/orders' && method === 'GET') return reply({ orders: state.created ? [order(state.started)] : [] });
      if (suffix === '/orders' && method === 'POST') {
        assert(state.profile, 'No upload before profile completion.'); assert.equal(url.searchParams.get('name'), 'flow-fixture.step');
        assert(request.headers()['x-event-id']); state.uploads.push(request.headers()['x-event-id']); state.created = true; return reply(order(), 201);
      }
      if (suffix === `/orders/${BATCH}/start` && method === 'POST') {
        assert.equal(request.postDataJSON().order_sets, 10); state.starts.push(request.postDataJSON().event_id); state.started = true; return reply(order(true), 202);
      }
      if (suffix === `/orders/${BATCH}` && method === 'GET') return reply(order(state.started));
      if (method === 'GET' && url.pathname === runPrefix + '/manufacturing') return reply({ batch_id: BATCH, run_id: RUN,
        status: 'pending_review', parts: [part], summary: { parts_count: 1, quantity_total: 10 } });
      if (method === 'GET' && url.pathname === runPrefix + '/customer-parts/L0001') { state.geometryReads += 1; return reply(geometryPayload); }
      if (method === 'GET' && url.pathname === runPrefix + '/customer-parts/L0001/thumbnail')
        return route.fulfill({ status: 200, contentType: 'image/svg+xml', body: image });
      if (method === 'GET' && [runPrefix + '/customer-quote.html', runPrefix + '/customer-print.html'].includes(url.pathname))
        return route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><html><body><h1>合成测试报价草稿</h1><p>¥120.00</p></body></html>' });
      return reject();
    }
    if (url.pathname === ADMIN || url.pathname.startsWith(ADMIN + '/')) {
      if (options.unauthorized) return reply({ error: { message: 'Synthetic internal authentication required.' } }, 401);
      if (url.pathname === ADMIN && method === 'GET') {
        const rows = state.staff.filter(row => (!url.searchParams.get('status') || row.status === url.searchParams.get('status'))
          && (url.searchParams.get('contactable') !== '1' || row.can_contact));
        return reply({ leads: rows.map(masked), total: rows.length, limit: 50, offset: 0 });
      }
      const match = /^\/(LEAD-[a-f0-9]{32})$/.exec(url.pathname.slice(ADMIN.length));
      const lead = match && state.staff.find(row => row.id === match[1]);
      if (!lead) return reply({ error: { message: 'Synthetic lead not found.' } }, 404);
      if (method === 'GET') return reply({ lead });
      if (method === 'POST') {
        const value = request.postDataJSON(); assert(value.event_id); state.updates.push(value.event_id);
        if (knownUpdate.has(value.event_id)) return reply({ lead: knownUpdate.get(value.event_id), replayed: true });
        assert.equal(value.expected_revision, lead.revision);
        Object.assign(lead, { ...value, revision: lead.revision + 1, updated_at: '2026-11-14T03:00:00Z' });
        lead.can_contact = lead.contact_consent && lead.status !== 'do_not_contact';
        lead.audit.push({ id: 'synthetic-audit', action: 'staff_updated', created_at: lead.updated_at, details: { note: value.note } });
        knownUpdate.set(value.event_id, copy(lead));
        if (options.loseUpdate && state.updates.length === 1) return route.abort('failed');
        return reply({ lead, replayed: false });
      }
      return reject();
    }
    if (!['GET', 'HEAD'].includes(method)) return reject();
    const allowed = ['/try/', '/workbench/leads/', '/favicon.ico'].includes(url.pathname)
      || url.pathname.startsWith('/workbench/_next/') || url.pathname.startsWith('/workbench/cad-preview/')
      || /^\/workbench\/(?:icon[^/]*\.(?:png|svg)|apple-icon\.png)$/.test(url.pathname);
    return allowed ? route.continue() : reject();
  });
  return { context, state };
}
async function pageFor(context, pathname) {
  const page = await context.newPage(); page.on('pageerror', error => report.page_errors.push(error.message));
  const response = await page.goto(new URL(pathname, origin).href, { waitUntil: 'networkidle' }); assert.equal(response.status(), 200);
  return page;
}
async function fits(page, label) {
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), label + ' has horizontal overflow.');
}
async function main() {
  const browser = await chromium.launch({ headless: true, chromiumSandbox: false,
    args: ['--disable-dev-shm-usage', '--renderer-process-limit=1', '--no-first-run', '--no-proxy-server',
      '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  try {
    const customer = await mocked(browser, { width: 1440, height: 1080 }, { loseInterest: true });
    const page = await pageFor(customer.context, '/try/');
    await page.getByTestId('trial-profile-form').waitFor();
    assert.equal(await page.getByTestId('trial-contact-consent').isChecked(), false);
    await page.getByTestId('trial-file-input').setInputFiles({ name: 'flow-fixture.step', mimeType: 'application/octet-stream',
      buffer: Buffer.from('ISO-10303-21;\nHEADER;ENDSEC;DATA;ENDSEC;END-ISO-10303-21;\n') });
    await page.getByTestId('trial-company').fill('合成钣金体验工厂');
    await page.getByTestId('trial-manufacturing-type').selectOption('sheet_metal');
    await page.getByTestId('trial-contact-method').selectOption('phone');
    await page.getByTestId('trial-contact-value').fill(PHONE);
    await page.getByTestId('trial-profile-save').click();
    await page.getByTestId('trial-profile-saved').waitFor();
    assert((await page.getByTestId('trial-contact-profile').innerText()).includes('未验证'));
    assert.equal(customer.state.profile.verification_status, 'unverified'); assert.equal(customer.state.profile.contact_consent, false);
    assert.equal(customer.state.uploads.length, 0); assert.equal(customer.state.starts.length, 0);
    await page.getByTestId('trial-quantity').fill('10');
    await page.getByTestId('trial-submit').click();
    await page.getByTestId('trial-current-order').waitFor();
    await page.waitForFunction(() => document.querySelector('[data-testid="customer-original-assembly"]')?.getAttribute('data-state') === 'ready', null, { timeout: 60000 });
    assert.equal(customer.state.uploads.length, 1); assert.equal(customer.state.starts.length, 1);
    assert(customer.state.geometryReads >= 1);
    await page.getByTestId('trial-interest-open').first().click();
    await page.getByTestId('trial-interest-note').fill(application.note);
    assert.equal(await page.getByTestId('trial-interest-consent').isChecked(), false);
    await page.getByTestId('trial-interest-consent').check();
    await page.getByTestId('trial-interest-submit').click();
    await page.getByTestId('trial-interest-retry').waitFor();
    assert.equal(customer.state.interestEvents.length, 1);
    await page.getByTestId('trial-interest-retry').click();
    await page.getByTestId('trial-interest-success').waitFor();
    assert((await page.getByTestId('trial-interest-success').innerText()).includes(APP));
    assert.equal(customer.state.interestEvents.length, 2); assert.equal(new Set(customer.state.interestEvents).size, 1);
    assert.equal(customer.state.applications.length, 1);
    assert.equal(customer.state.uploads.length, 1); assert.equal(customer.state.starts.length, 1);
    assert.equal(await page.getByTestId('trial-current-order').count(), 1);
    await page.getByTestId('trial-applications-toggle').click();
    assert((await page.getByTestId('trial-application-history').innerText()).includes(APP));
    const body = await page.locator('body').innerText();
    for (const text of ['手机号已验证', '验证成功', '内部成本', '材料成本', '利润']) assert(!body.includes(text), 'Unexpected public claim: ' + text);
    await fits(page, 'Customer trial flow');
    await page.screenshot({ path: path.join(output, 'customer-request-saved.png'), fullPage: true, timeout: 60000, animations: 'disabled' });
    await customer.context.close();
    report.checks.push('An unverified company profile saves without contact opt-in, retains the chosen STEP, then one mocked upload/start restores the native model; a lost application reply recovers with the same event without extra uploads, starts or SMS.');

    const admin = await mocked(browser, { width: 1440, height: 1080 }, { loseUpdate: true });
    const staffPage = await pageFor(admin.context, '/workbench/leads/');
    await staffPage.getByTestId('trial-lead-row').first().waitFor();
    const listText = await staffPage.getByTestId('trial-leads-list').innerText();
    assert(!listText.includes(PHONE)); assert(!listText.includes(OTHER_PHONE));
    assert(listText.includes('解析已完成')); assert(listText.includes('正在解析'));
    assert(listText.includes('no-request-fixture.step'), 'Experience appears without a factory trial application.');
    assert.equal(await staffPage.getByTestId('trial-lead-latest-trial').count(), 2);
    await staffPage.getByTestId('trial-lead-row').first().click();
    await staffPage.getByTestId('trial-lead-company').waitFor();
    assert((await staffPage.getByTestId('trial-lead-contact').innerText()).includes(PHONE));
    const trialText = await staffPage.getByTestId('trial-lead-trials').innerText();
    assert(trialText.includes('flow-fixture.step')); assert(trialText.includes('解析已完成'));
    assert(trialText.includes('1 项零件')); assert(trialText.includes(BATCH)); assert(trialText.includes(RUN));
    assert.equal(await staffPage.getByTestId('trial-lead-trials').locator('a').count(), 0, 'Trial records do not expose raw CAD links.');
    await staffPage.getByTestId('trial-lead-status').selectOption('trialing');
    await staffPage.getByTestId('trial-lead-assignee').fill('合成负责同事');
    await staffPage.getByTestId('trial-lead-note').fill('合成内部备注：下次确认工序费率。');
    await staffPage.getByTestId('trial-lead-save').click();
    await staffPage.getByTestId('trial-lead-recover').waitFor();
    await staffPage.getByTestId('trial-lead-recover').click();
    await staffPage.getByTestId('trial-lead-saved').waitFor();
    assert.equal(admin.state.updates.length, 2); assert.equal(new Set(admin.state.updates).size, 1);
    assert.equal(admin.state.staff[0].revision, 2);
    assert.equal(admin.state.staff[0].status, 'trialing');
    await fits(staffPage, 'Desktop lead details');
    await staffPage.screenshot({ path: path.join(output, 'staff-followup-saved.png'), fullPage: true, timeout: 60000, animations: 'disabled' });
    await staffPage.getByTestId('trial-lead-row').last().click();
    await staffPage.waitForFunction(() => document.querySelector('[data-testid="trial-lead-company"]')?.textContent === '合成暂不联系工厂');
    assert((await staffPage.getByTestId('trial-lead-consent').innerText()).includes('暂不安排'));
    assert(!(await staffPage.getByTestId('trial-lead-contact').innerText()).includes(OTHER_PHONE));
    const noRequestTrialText = await staffPage.getByTestId('trial-lead-trials').innerText();
    assert(noRequestTrialText.includes('no-request-fixture.step')); assert(noRequestTrialText.includes('正在解析'));
    assert(noRequestTrialText.includes('不代表同意销售联系'));
    assert.equal(await staffPage.getByTestId('trial-lead-trial').count(), 1);
    assert.equal(await staffPage.getByTestId('trial-lead-application').count(), 0);
    assert.equal(admin.state.staff[1].contact_consent, false);
    await staffPage.getByTestId('trial-leads-contactable-filter').check();
    await staffPage.waitForFunction(() => document.querySelectorAll('[data-testid="trial-lead-row"]').length === 1);
    assert(!(await staffPage.getByTestId('trial-leads-list').innerText()).includes('合成暂不联系工厂'));
    await admin.context.close();
    report.checks.push('Staff lists show latest automatic trial stages; trial details exist without applications and expose no raw CAD links. Contact permission remains independent of experience, with masked no-consent records excluded by the contactable filter. Follow-up saves recover idempotently.');

    const mobile = await mocked(browser, { width: 390, height: 844 });
    const mobilePage = await pageFor(mobile.context, '/workbench/leads/');
    await mobilePage.getByTestId('trial-lead-row').first().waitFor();
    await mobilePage.getByTestId('trial-lead-row').first().click();
    await mobilePage.getByTestId('trial-lead-company').waitFor();
    await fits(mobilePage, 'Mobile lead details');
    await mobilePage.screenshot({ path: path.join(output, 'mobile-lead-details.png'), fullPage: true, timeout: 60000, animations: 'disabled' });
    await mobile.context.close();
    report.checks.push('The internal lead list, contact permission and follow-up form fit a mobile viewport.');

    const anonymous = await mocked(browser, { width: 1440, height: 1000 }, { unauthorized: true });
    const deniedPage = await pageFor(anonymous.context, '/workbench/leads/');
    await deniedPage.getByTestId('trial-leads-list-error').waitFor();
    assert((await deniedPage.locator('body').innerText()).includes('需要内部账号'));
    assert.equal(await deniedPage.locator('input[type="password"]').count(), 0);
    assert.equal(await deniedPage.getByTestId('trial-lead-row').count(), 0);
    await anonymous.context.close();
    report.checks.push('An unauthorized staff API response shows an internal-account prompt without lead records or a replacement password form.');
    assert.deepEqual(report.page_errors, []); assert.deepEqual(report.blocked_requests, []);
    report.passed = true;
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
  }
  console.log(JSON.stringify(report, null, 2));
}
main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
