(() => {
  'use strict';
  const API = '/workbench/api/orders';
  const content = document.querySelector('#content');
  const dialog = document.querySelector('#order-dialog');
  const dialogContent = document.querySelector('#dialog-content');
  const clone = value => JSON.parse(JSON.stringify(value));
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const id = prefix => prefix + '-' + crypto.randomUUID();
  const state = { order: null, draft: null, dirty: false, step: 0, busy: false, token: 0, jobs: [], selected: new Set(), uploads: [], timer: null, pendingRestore: null, error: '', quotes: [], policy: { pricing_method: '', rate_percent: '' }, sharedFees: [], newJobResults: false };
  const modeLabels = { make: '自制零件', buy: '外购件', assembly: '装配件' };
  const stepNames = ['上传资料', '分件与 BOM', '工艺与价格', '生成报价'];
  const jobStatus = { queued: '等待处理', running: '正在处理', completed: '已完成', succeeded: '已完成', failed: '部分或全部处理失败', cancelled: '已停止', stopped: '已停止', stopping: '正在停止', pending: '等待处理' };
  const terminal = value => ['completed','succeeded','failed','cancelled','stopped'].includes(value);
  const key = orderId => 'haoyunlai:order-draft:' + orderId;
  const endpoint = (suffix = '') => API + '/' + encodeURIComponent(state.order.id) + suffix;
  const safeUrl = raw => {
    if (typeof raw !== 'string' || !raw) return '';
    try { const url = new URL(raw, location.origin); return url.origin === location.origin && ['http:','https:'].includes(url.protocol) ? url.href : ''; } catch (_) { return ''; }
  };
  const link = (url, label, css = 'button') => { const safe = safeUrl(url); return safe ? `<a class="${css}" href="${escape(safe)}" target="_blank" rel="noopener">${escape(label)}</a>` : ''; };
  const options = (items, selected) => items.map(([value,label]) => `<option value="${escape(value)}"${String(selected ?? '') === String(value) ? ' selected' : ''}>${escape(label)}</option>`).join('');
  const numberInput = (value, attrs = '') => `<input class="input" type="number" min="0" step="any" inputmode="decimal" value="${escape(value ?? '')}" ${attrs}>`;
  const nullableNumber = value => value === '' ? null : value;
  const structure = value => ({ parts: clone(value.parts || []), lines: clone(value.lines || []), bom: clone(value.bom || []), routes: clone(value.routes || {}) });
  const partById = partId => state.draft?.parts.find(part => part.id === partId);
  const partName = partId => partById(partId)?.name || partId || '未选零件';
  const partOptions = selected => options([['','请选择零件'], ...state.draft.parts.map(part => [part.id, part.name || part.id])], selected);
  function toast(message) { const element = document.querySelector('#toast'); element.textContent = message; element.hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => { element.hidden = true; }, 4500); }
  async function request(url, config = {}) {
    const headers = new Headers(config.headers || {});
    if (!['GET','HEAD'].includes((config.method || 'GET').toUpperCase())) headers.set('X-Workbench-Request','1');
    const response = await fetch(url, { credentials: 'same-origin', ...config, headers });
    let payload; try { payload = await response.json(); } catch (_) { payload = {}; }
    if (!response.ok) { const error = new Error(payload.error?.message || payload.error || payload.message || `请求未完成（${response.status}），请保留草稿后重试。`); error.status = response.status; throw error; }
    return payload;
  }
  function persist() {
    if (!state.order || !state.draft || state.pendingRestore) return;
    try { localStorage.setItem(key(state.order.id), JSON.stringify({ revision: state.order.revision, draft: state.draft, dirty: state.dirty, step: state.step, policy: state.policy, sharedFees: state.sharedFees })); } catch (_) { /* Editing remains usable when browser storage is unavailable. */ }
  }
  function dirty() { state.dirty = true; persist(); updateSaveState(); }
  function updateSaveState() { const element = document.querySelector('[data-save-state]'); if (element) { element.textContent = state.dirty ? '有未保存修改 · 已保留在当前浏览器' : `已保存 · 版本 ${state.order?.revision ?? ''}`; element.classList.toggle('dirty', state.dirty); } }
  function showError(error) { state.error = String(error?.message || error); const element = document.querySelector('#order-error'); if (element) { element.hidden = false; element.querySelector('[data-error-text]').textContent = state.error; } else toast(state.error); }
  function clearError() { state.error = ''; const element = document.querySelector('#order-error'); if (element) element.hidden = true; }
  function eventPayload(action, body) {
    const storageKey = `haoyunlai:order-event:${state.order?.id || 'create'}:${action}`;
    const fingerprint = JSON.stringify(body);
    let pending; try { pending = JSON.parse(localStorage.getItem(storageKey)); } catch (_) { /* no pending event */ }
    if (!pending || pending.fingerprint !== fingerprint) { pending = { fingerprint, event_id: id('EVT') }; try { localStorage.setItem(storageKey, JSON.stringify(pending)); } catch (_) {} }
    return { payload: { ...body, event_id: pending.event_id }, clear: () => { try { localStorage.removeItem(storageKey); } catch (_) {} } };
  }
  async function mutation(action, suffix, body) {
    const event = eventPayload(action, { expected_revision: state.order.revision, ...body });
    const token = state.token;
    const result = await request(endpoint(suffix), { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(event.payload) });
    if (token !== state.token) throw new DOMException('订单已切换。', 'AbortError');
    event.clear(); return result;
  }
  function acceptOrder(value, keepDraft = false) {
    const order = value.order || value;
    if (state.order && order.id !== state.order.id) throw new DOMException('订单已切换。', 'AbortError');
    if (!order?.id || !Number.isInteger(order.revision)) throw new Error('服务器返回的订单记录不完整，请重新打开订单。');
    state.order = order;
    state.quotes = order.quotes || [];
    if (!keepDraft) { state.draft = structure(order); state.dirty = false; }
    persist();
  }
  async function saveDraft() {
    if (state.pendingRestore) throw new Error('请先选择保留本地草稿或使用服务器记录。');
    if (!state.dirty) return state.order;
    for (const part of state.draft.parts) for (const ref of part.source_refs || []) {
      if (!(part.document_ids || []).includes(ref.document_id)) throw new Error('请为“' + (part.name || '未命名零件') + '”的来源行选择已关联文件，或移除空白来源行。');
      if (ref.page != null && (!Number.isInteger(ref.page) || ref.page <= 0)) throw new Error('来源页码须为正整数，未限定页时请留空。');
      if (ref.region != null && (typeof ref.region !== 'string' || ref.region.length > 500)) throw new Error('来源区域说明须为不超过500字的文字。');
    }
    for (const row of state.draft.bom) { const parent = partById(row.parent_id); if (parent && parent.mode !== 'assembly') throw new Error('有 BOM 子件的父件必须选择装配件，请核对“' + (parent.name || parent.id) + '”的供货方式。'); }
    const response = await mutation('structure', '/structure', structure(state.draft));
    acceptOrder(response); return state.order;
  }
  function setBusy(value) { document.querySelectorAll('#order-step,.orders-page .steps,.order-toolbar').forEach(element => { element.inert = value; }); }
  async function perform(action) {
    if (state.busy) return;
    state.busy = true; setBusy(true); clearError();
    const token = state.token;
    try { await action(); } catch (error) { if (token === state.token && error.name !== 'AbortError') showError(error); }
    finally { if (token !== state.token) return; state.busy = false; setBusy(false); updateSaveState(); }
  }
  function newPart(base = {}) { return { id: id('P'), name: '', part_number: '', version: '', mode: 'make', purchase_price: null, assembly_unit_cost: null, assembly_setup_fee: null, cost_inclusions: '', cost_confirmed: false, document_ids: [], task_id: null, ...base }; }
  function newLine(partId = '') { return { id: id('L'), part_id: partId, quantity: null, unit: 'piece' }; }
  function routeRows(partId) { return state.draft.routes[partId] || []; }
  function documentLink(document) { return document.url || endpoint('/documents/' + encodeURIComponent(document.id)); }
  function sourceLabel(refs) {
    return (refs || []).map(ref => typeof ref === 'string' ? ref : [ref.document_name || ref.document_id || '', ref.page != null ? `第 ${ref.page} 页` : '', ref.evidence || ref.reason || ''].filter(Boolean).join(' · ')).join('；');
  }
  function renderShell() {
    const order = state.order;
    document.querySelector('#breadcrumb').textContent = '批量订单 / ' + (order.name || order.id);
    content.className = 'orders-page';
    content.innerHTML = `<div class="page-head"><div><h1>${escape(order.name || '未命名订单')}</h1><p class="subtitle">${escape(order.inquiry || '将多份图纸整理为零件、订单数量和装配清单，集中核对后报价。')}</p></div><div class="actions"><a class="button" href="#">返回订单列表</a><span class="badge warning">模拟订单</span></div></div>
      <div id="order-error" class="alert error order-error" role="alert"${state.error ? '' : ' hidden'}><span data-error-text>${escape(state.error)}</span><div class="actions"><button class="button link" data-action="dismiss-error">收起提示</button><button class="button link" data-action="reload-order">重新同步服务器记录</button></div></div>
      <div id="restore-banner"></div><div id="order-jobs" class="order-jobs" aria-live="polite" hidden></div>
      <nav class="steps" aria-label="订单步骤">${stepNames.map((name,index) => `<button class="step${state.step === index ? ' active' : ''}" data-step="${index}"${state.step === index ? ' aria-current="step"' : ''}><span>${index + 1}</span>${name}</button>`).join('')}</nav>
      <div id="order-step"></div>
      <div class="order-toolbar"><span class="order-save-state" data-save-state></span><div class="actions"><button class="button" data-action="save" data-mutation>保存本步</button>${state.step < 3 ? '<button class="button primary" data-action="next" data-mutation>保存并继续</button>' : ''}</div></div>`;
    renderRestore(); renderStep(); renderJobs(); updateSaveState(); setBusy(state.busy); content.setAttribute('aria-busy','false');
  }
  function renderRestore() {
    const element = document.querySelector('#restore-banner'); if (!element) return;
    element.innerHTML = state.pendingRestore ? `<div class="alert draft-banner"><span>浏览器中有版本 ${escape(state.pendingRestore.revision)} 的未保存草稿，服务器当前为版本 ${escape(state.order.revision)}。请选择要继续编辑的内容；本地草稿不会自动覆盖服务器。</span><div class="actions"><button class="button" data-action="restore-draft">保留本地草稿</button><button class="button" data-action="discard-draft">使用服务器记录</button></div></div>` : '';
  }
  function renderStep() {
    const element = document.querySelector('#order-step');
    if (!element) return;
    element.innerHTML = [uploadView,structureView,routesView,quoteView][state.step](); element.inert = state.busy;
    document.querySelectorAll('[data-step]').forEach(button => { button.classList.toggle('active', Number(button.dataset.step) === state.step); if (Number(button.dataset.step) === state.step) button.setAttribute('aria-current','step'); else button.removeAttribute('aria-current'); });
  }
  function uploadView() {
    return `<section class="panel"><div class="panel-head"><h2>1. 上传订单资料</h2><span class="badge">${state.order.documents?.length || 0} 份已保存</span></div><p class="small">可一次选择多张图片、PDF、DXF、DWG 或 STEP。每单最多 20 份，每份不超过 25 MB。文件逐份保存；处理中关闭页面，任务仍会在后台继续。</p><div class="upload-zone"><strong>选择这一单的图纸和询价文件</strong><div><input type="file" multiple data-upload-files accept=".pdf,.jpg,.jpeg,.png,.webp,.tif,.tiff,.bmp,.dxf,.dwg,.step,.stp"></div><p class="small">确认资料齐全后，点击下方“识别零件建议”。自动识别结果需人工采用。</p></div><div id="upload-queue">${uploadQueueView()}</div><div class="order-docs">${(state.order.documents || []).map(document => `<div class="order-doc"><div><strong>${escape(document.name)}</strong><div class="small">${formatSize(document.size)}</div></div><div class="actions">${link(documentLink(document),'打开原件')}</div></div>`).join('') || '<p class="orders-empty">尚未上传文件</p>'}</div><div class="footer"><span class="small">上传失败时可单独重试，无需重传成功文件。</span><button class="button primary" data-action="discover" data-mutation${state.order.documents?.length ? '' : ' disabled'}>识别零件建议</button></div></section>${discoveryView()}`;
  }
  function formatSize(size) { return Number(size) > 1024 * 1024 ? (Number(size) / (1024 * 1024)).toFixed(1) + ' MB' : Math.ceil(Number(size || 0) / 1024) + ' KB'; }
  function uploadQueueView() {
    return state.uploads.map((item,index) => `<div class="order-doc"><div><strong>${escape(item.file.name)}</strong><div class="small">${escape(item.status === 'done' ? '上传成功' : item.status === 'uploading' ? '正在上传…' : item.error || '等待上传')}</div></div>${item.status === 'error' ? `<button class="button" data-action="retry-upload" data-index="${index}" data-mutation>重试这一份</button>` : ''}</div>`).join('');
  }
  function candidates() { const value = state.order.discovery?.parts || state.order.discovery?.candidates || []; return Array.isArray(value) ? value : []; }
  function candidateKey(candidate,index) { return String(candidate.key || candidate.id || index); }
  function discoveryView() {
    const discovery = state.order.discovery || {};
    const parts = candidates();
    return `<section class="panel"><div class="panel-head"><h2>零件识别建议</h2><span class="badge">${escape(jobStatus[discovery.status] || discovery.status || '尚未识别')}</span></div><p class="small">建议不会自动成为订单零件。选择后可分别采用为新零件，或合并到已有零件；数量仍需按询价确认。</p>${state.newJobResults ? '<div class="note">后台结果已更新。为保护当前草稿，点击“刷新识别结果”后查看；未保存的结构输入会保留。<button class="button link" data-action="refresh-discovery">刷新识别结果</button></div>' : ''}${(discovery.warnings || []).length ? `<ul class="order-warning-list small">${discovery.warnings.map(value => `<li>${escape(value.message || value)}</li>`).join('')}</ul>` : ''}<div class="order-candidates">${parts.map((candidate,index) => { const candidateId = candidateKey(candidate,index); return `<div class="candidate-card"><label class="check"><input type="checkbox" data-candidate="${escape(candidateId)}"${state.selected.has(candidateId) ? ' checked' : ''}><strong>${escape(candidate.name || candidate.part_name || candidate.part_number || '未命名零件建议')}</strong></label><p>${escape([candidate.part_number && '图号：' + candidate.part_number,candidate.version && '版本：' + candidate.version,candidate.reason || (typeof candidate.evidence === 'string' ? candidate.evidence : '')].filter(Boolean).join(' · '))}</p><p class="source-ref">${escape(sourceLabel(candidateRefs(candidate)) || (candidate.document_ids || []).map(docId => state.order.documents.find(document => document.id === docId)?.name || docId).join('、'))}</p></div>`; }).join('') || '<p class="orders-empty">可以先手工分件，也可以上传后启动后台识别。</p>'}</div>${parts.length ? `<div class="candidate-actions"><div class="actions"><button class="button" data-action="adopt-candidates">采用为新零件</button></div><label class="field"><span>合并到已有零件</span><select class="input" data-merge-target>${partOptions('')}</select></label><button class="button" data-action="merge-candidates">合并所选建议</button></div>` : ''}</section>`;
  }
  function sourceRows(part) {
    const documentOptions = (state.order.documents || []).filter(document => (part.document_ids || []).includes(document.id)).map(document => [document.id,document.name]);
    return (part.source_refs || []).map((ref,index) => `<div class="source-page-row"><label class="field"><span>来源文件</span><select class="input" data-ref-part="${escape(part.id)}" data-source-index="${index}" data-source-property="document_id">${options([['','请选择已关联文件'],...documentOptions],ref.document_id)}</select></label><label class="field"><span>页码</span><input class="input" type="number" min="1" step="1" inputmode="numeric" placeholder="全部" value="${escape(ref.page ?? '')}" data-ref-part="${escape(part.id)}" data-source-index="${index}" data-source-property="page"></label><label class="field"><span>区域说明</span><input class="input" maxlength="500" placeholder="例如：右下角支架视图" value="${escape(ref.region || '')}" data-ref-part="${escape(part.id)}" data-source-index="${index}" data-source-property="region"></label><button class="button danger" data-action="remove-source-ref" data-part-id="${escape(part.id)}" data-index="${index}">移除</button></div>`).join('') || '<p class="small">未限定来源页；按所关联的整份文件准备资料。</p>';
  }
  function sourceEditor(part) {
    return `<details class="source-ref-editor" data-source-editor="${escape(part.id)}"><summary>来源页与区域（可选） · ${(part.source_refs || []).length} 条</summary><p class="small">多零件共用一份 PDF 时，可指定本件所在页。页码留空表示未限定页；同一文件没有其他明确页码时使用整份文件。区域仅作说明，不自动裁图。</p><div data-source-rows>${sourceRows(part)}</div><button class="button" data-action="add-source-ref" data-part-id="${escape(part.id)}"${part.document_ids?.length ? '' : ' disabled'}>添加来源页</button></details>`;
  }
  function updateSourceEditor(part) {
    const editor = document.querySelector(`[data-source-editor="${CSS.escape(part.id)}"]`);
    if (!editor) return;
    editor.querySelector('summary').textContent = `来源页与区域（可选） · ${(part.source_refs || []).length} 条`;
    editor.querySelector('[data-source-rows]').innerHTML = sourceRows(part);
    editor.querySelector('[data-action="add-source-ref"]').disabled = !part.document_ids?.length;
  }
  function structureView() {
    return `${discoveryView()}<section class="panel"><div class="panel-head"><h2>2. 零件与文件归属</h2><button class="button" data-action="add-part">手工添加零件</button></div><p class="small">同一份图纸可属于多个零件。同一零件的不同视图应归为一个零件；拆成多个零件后，请分别填写名称和图号。</p>${state.draft.parts.map(part => `<article class="part-card" data-part-card="${escape(part.id)}"><div class="part-head"><h3>${escape(part.name || '未命名零件')}</h3><div class="actions"><button class="button" data-action="split-part" data-id="${escape(part.id)}">拆出一个零件</button><button class="button danger" data-action="remove-part" data-id="${escape(part.id)}">移除</button></div></div><div class="field-grid"><label class="field"><span>零件名称</span><input class="input" data-part="${escape(part.id)}" data-property="name" value="${escape(part.name)}"></label><label class="field"><span>图号 / 料号</span><input class="input" data-part="${escape(part.id)}" data-property="part_number" value="${escape(part.part_number)}"></label><label class="field"><span>版本</span><input class="input" data-part="${escape(part.id)}" data-property="version" value="${escape(part.version)}"></label><label class="field"><span>供货方式</span><select class="input" data-part="${escape(part.id)}" data-property="mode">${options(Object.entries(modeLabels),part.mode)}</select></label>${part.mode === 'buy' ? `<label class="field"><span>外购单价（元 / 件）</span>${numberInput(part.purchase_price,`data-part="${escape(part.id)}" data-property="purchase_price"`)}<small>未知请留空，不默认零。</small></label>` : ''}</div><details class="part-files"><summary>文件归属 · ${(part.document_ids || []).length} 份</summary><div class="checks">${(state.order.documents || []).map(document => `<label class="check"><input type="checkbox" data-part-document="${escape(part.id)}" value="${escape(document.id)}"${part.document_ids?.includes(document.id) ? ' checked' : ''}><span>${escape(document.name)}</span></label>`).join('') || '<span class="small">请先上传资料</span>'}</div>${part.source_refs?.length ? `<p class="source-ref">识别来源：${escape(sourceLabel(part.source_refs))}</p>` : ''}</details>${sourceEditor(part)}</article>`).join('') || '<p class="orders-empty">采用识别建议，或手工添加第一个零件。</p>'}</section>
      <section class="panel"><div class="panel-head"><h2>客户订购数量</h2><button class="button" data-action="add-line">添加订购行</button></div><p class="small order-table-note">例如 A 产品 10 件、B 产品 30 件，分别添加两行。装配产品按“套”填写，BOM 填每套消耗量。</p><div class="table-wrap"><table class="order-grid-table"><thead><tr><th>产品 / 零件</th><th>订购数量</th><th>单位</th><th>操作</th></tr></thead><tbody>${state.draft.lines.map(line => `<tr><td class="name-col"><select class="input" data-line="${escape(line.id)}" data-property="part_id">${partOptions(line.part_id)}</select></td><td>${numberInput(line.quantity,`data-line="${escape(line.id)}" data-property="quantity" aria-label="订购数量"`)}</td><td><select class="input" data-line="${escape(line.id)}" data-property="unit">${options([['piece','件'],['set','套']],line.unit)}</select></td><td><button class="button danger" data-action="remove-line" data-id="${escape(line.id)}">移除</button></td></tr>`).join('') || '<tr><td colspan="4" class="orders-empty">尚未填写订购产品与数量</td></tr>'}</tbody></table></div></section>
      <section class="panel"><div class="panel-head"><h2>每套装配清单（BOM）</h2><button class="button" data-action="add-bom">添加装配关系</button></div><p class="small order-table-note">父件为装配产品，子件填写每套用量。普通独立零件不需要填写；可保留多层装配关系。</p><div class="table-wrap"><table class="order-grid-table"><thead><tr><th>装配父件</th><th>组成子件</th><th>每套用量</th><th>操作</th></tr></thead><tbody>${state.draft.bom.map(row => `<tr><td class="name-col"><select class="input" data-bom="${escape(row.id)}" data-property="parent_id">${partOptions(row.parent_id)}</select></td><td class="name-col"><select class="input" data-bom="${escape(row.id)}" data-property="part_id">${partOptions(row.part_id)}</select></td><td>${numberInput(row.quantity,`data-bom="${escape(row.id)}" data-property="quantity" aria-label="每套用量"`)}</td><td><button class="button danger" data-action="remove-bom" data-id="${escape(row.id)}">移除</button></td></tr>`).join('') || '<tr><td colspan="4" class="orders-empty">这一单尚无装配关系</td></tr>'}</tbody></table></div></section>`;
  }
  function issuesView() {
    const issues = state.order.issues || [];
    return issues.length ? `<section class="alert"><strong>仍需核对</strong><ul class="order-issues">${issues.map(issue => `<li>${escape(issue.message || issue.reason || issue)}</li>`).join('')}</ul></section>` : '';
  }
  function taskStatusView(part) {
    const status = state.order.part_status?.[part.id];
    if (!status) return '<p class="small">建立单品任务后，可打开核对材料、尺寸和费率。</p>';
    return `<p class="${status.ready ? 'note' : 'alert'}">${status.ready ? '单品参数已具备核算条件' : '单品仍有待补充或待核对项'}</p>${status.issues?.length ? `<ul class="small">${status.issues.map(issue => `<li>${escape(issue.message || issue)}</li>`).join('')}</ul>` : ''}`;
  }
  function costFields(part) {
    const prefix = `data-part="${escape(part.id)}"`;
    const fields = part.mode === 'buy' ? `<label class="field"><span>外购单价（元 / 件）</span>${numberInput(part.purchase_price, `${prefix} data-property="purchase_price"`)}</label>` : `<label class="field"><span>直接装配加工费（元 / 套）</span>${numberInput(part.assembly_unit_cost, `${prefix} data-property="assembly_unit_cost"`)}</label><label class="field"><span>本批装配准备费（元 / 批）</span>${numberInput(part.assembly_setup_fee, `${prefix} data-property="assembly_setup_fee"`)}</label>`;
    const parentRows = state.draft.bom.filter(row => row.parent_id === part.id);
    return `<div class="field-grid">${fields}<label class="field full"><span>费用包含说明</span><textarea class="input" ${prefix} data-property="cost_inclusions">${escape(part.cost_inclusions || '')}</textarea><small>${part.mode === 'assembly' ? '仅填装配加工与准备费，子件材料和加工成本由 BOM 另计，避免重复。' : '明确是否包含税运、采购或包装费用，未知费用不要补零。'}</small></label></div><label class="order-confirm-check"><input type="checkbox" data-cost-confirm="${escape(part.id)}"${part.cost_confirmed ? ' checked' : ''}><span>已核对以上${part.mode === 'buy' ? '外购单价' : '装配加工与准备费'}及费用包含项。</span></label>${part.mode === 'buy' && parentRows.length ? '<p class="alert error">外购件不能包含 BOM 子件，请调整供货方式或装配关系。</p>' : ''}`;
  }
  function routesView() {
    return `${issuesView()}<section class="panel"><div class="panel-head"><h2>3. 工艺与单品价格</h2><div class="actions"><button class="button" data-action="refresh-discovery" data-mutation>刷新单品状态</button><button class="button primary" data-action="prepare" data-mutation>保存并建立单品任务</button></div></div><p class="small">确认订单数量和 BOM 后，系统汇总各零件需求量，并建立或关联单品任务。材料、板厚、费率等仍在单品工作台核对；这里不会自动确认隐藏技术参数。</p>${demandView()}<div class="footer"><span class="small">建议工序仅供技术人员核对；顺序可调整。</span><button class="button" data-action="confirm-visible-routes">确认本页已展示工序</button></div></section>${state.draft.parts.map(part => `<section class="route-card"><div class="route-head"><div><h3>${escape(part.name || '未命名零件')}</h3><span class="small">${escape(modeLabels[part.mode] || part.mode)}${part.task_id ? ' · 已关联单品任务' : ''}</span></div>${part.task_id ? link('/workbench/#task/' + encodeURIComponent(part.task_id) + '/confirm','打开单品参数与价格') : '<span class="badge">尚未建立任务</span>'}</div>${part.mode === 'make' ? taskStatusView(part) : costFields(part)}${part.mode !== 'buy' ? `<div>${routeRows(part.id).map((route,index) => `<div class="route-line"><span>${index + 1}</span><label class="field"><input class="input" aria-label="工序名称" data-route="${escape(route.id)}" data-part-id="${escape(part.id)}" value="${escape(route.operation)}"><small>${escape(route.reason || '')}</small></label><div class="actions"><span class="badge ${route.status === 'confirmed' ? 'success' : 'warning'}">${route.status === 'confirmed' ? '已核对' : '待核对'}</span><button class="button" data-action="route-up" data-part-id="${escape(part.id)}" data-id="${escape(route.id)}"${index === 0 ? ' disabled' : ''} aria-label="上移工序">↑</button><button class="button" data-action="route-down" data-part-id="${escape(part.id)}" data-id="${escape(route.id)}"${index === routeRows(part.id).length - 1 ? ' disabled' : ''} aria-label="下移工序">↓</button><button class="button danger" data-action="remove-route" data-part-id="${escape(part.id)}" data-id="${escape(route.id)}">移除</button></div></div>`).join('') || '<p class="small">尚无工序。可手工添加，再由技术人员集中核对。</p>'}</div><div class="route-suggestions">${['激光下料','去毛刺','折弯','焊接','装配','表面处理','检验包装'].map(operation => `<button class="button" data-action="add-route" data-part-id="${escape(part.id)}" data-operation="${escape(operation)}">＋ ${escape(operation)}</button>`).join('')}<button class="button" data-action="add-route" data-part-id="${escape(part.id)}" data-operation="">＋ 其他工序</button></div>` : ''}</section>`).join('')}`;
  }
  function demandView() {
    const demands = state.order.demands || {};
    return `<div class="order-demand">${Object.entries(demands).map(([partId,quantity]) => `<div class="demand-item">${escape(partName(partId))} <strong>${escape(typeof quantity === 'object' ? quantity.quantity : quantity)} ${partById(partId)?.mode === 'assembly' ? '套' : '件'}</strong></div>`).join('') || '<p class="small">保存并建立单品任务后显示汇总需求量。</p>'}</div>${state.dirty ? '<p class="small">以上为最近一次保存后的需求量；当前草稿修改尚未计入。</p>' : ''}`;
  }
  function quoteView() {
    return `${issuesView()}<section class="panel"><h2>4. 生成整单报价</h2><p class="small">各零件单价由已核对的单品成本计算。整单共用费用只在本单收取一次；请核对它与单品准备费或工序费的包含项。</p>${demandView()}<div class="field-grid section-gap"><label class="field"><span>定价方式</span><select class="input" data-policy="pricing_method">${options([['','请选择'],['cost_plus','成本加成'],['gross_margin','目标毛利率']],state.policy.pricing_method)}</select></label><label class="field"><span>比例（%）</span>${numberInput(state.policy.rate_percent,'data-policy="rate_percent" aria-label="定价比例"')}<small>例如填写 20 表示 20%，不设默认比例。</small></label></div><p class="note section-gap">报价口径：人民币、未税、不含运费。生成结果为模拟报价草稿，待审核。定价与共用费用输入暂存当前浏览器，生成报价时一同冻结。</p><div class="panel-head section-gap"><h3>整单共用费用（可选）</h3><button class="button" data-action="add-shared-fee">添加共用费用</button></div><div class="table-wrap"><table class="order-grid-table"><thead><tr><th>费用名称</th><th>整单金额（元）</th><th>包含项说明</th><th>操作</th></tr></thead><tbody>${state.sharedFees.map(fee => `<tr><td><input class="input" data-shared="${escape(fee.id)}" data-property="name" value="${escape(fee.name)}" aria-label="共用费用名称"></td><td>${numberInput(fee.value,`data-shared="${escape(fee.id)}" data-property="value" aria-label="共用费用金额"`)}</td><td><input class="input" data-shared="${escape(fee.id)}" data-property="inclusions" value="${escape(Array.isArray(fee.inclusions) ? fee.inclusions.join('；') : fee.inclusions)}" aria-label="包含项说明"></td><td><button class="button danger" data-action="remove-shared-fee" data-id="${escape(fee.id)}">移除</button></td></tr>`).join('') || '<tr><td colspan="4" class="small">未添加共用费用；不会自动补收。</td></tr>'}</tbody></table></div><label class="order-confirm-check"><input type="checkbox" data-quote-confirm><span>已核对当前订购数量、BOM、单品参数和共用费用包含项，本次生成模拟报价草稿。</span></label><div class="actions"><button class="button" data-action="refresh-discovery" data-mutation>刷新参数核对状态</button><button class="button primary" data-action="quote" data-mutation>保存并生成整单报价草稿</button>${link(endpoint('/exports/bom.csv'),'导出 BOM')}${link(endpoint('/exports/routes.csv'),'导出工艺清单')}</div></section><section class="order-quotes" id="order-quotes">${quotesView()}</section>`;
  }
  function quotesView() {
    return state.quotes.length ? state.quotes.slice().reverse().map(quote => `<article class="panel"><div class="panel-head"><h3>${escape(quote.quote_id || quote.id || '整单报价草稿')}</h3><span class="badge warning">模拟 · 待审核</span></div>${quote.amounts ? `<p class="amount">¥${escape(quote.amounts.total_price ?? quote.amounts.quote_total ?? quote.amounts.total ?? quote.amounts.total_amount ?? '待查看')}</p>` : ''}${quote.source_current === false ? '<p class="alert">订单或单品参数已变化，这份历史报价不代表当前输入，请重新核对后生成新报价。</p>' : ''}<div class="quote-link-list">${link(quote.links?.quote_url,'查看整单报价')}${link(quote.links?.cost_url,'查看成本明细')}${link(quote.links?.bom_url,'下载 BOM')}${link(quote.links?.routes_url,'下载工艺清单')}<button class="button" data-action="load-quote" data-id="${escape(quote.id || quote.quote_id)}">查看报价链接</button></div></article>`).join('') : '<div class="panel orders-empty">尚未生成整单报价</div>';
  }
  function renderJobs() {
    const container = document.querySelector('#order-jobs'); if (!container) return;
    const jobs = state.jobs.slice(-4).reverse();
    container.hidden = !jobs.length;
    container.innerHTML = `<strong>后台处理进度</strong><p class="small">可以离开本页，已保存资料保留；重新打开后继续查看。进度更新不会覆盖当前表单草稿。</p>${jobs.map(job => `<div class="job-row"><div><strong>${escape(job.label || job.name || (job.kind === 'part_read' ? '单品参数读图' : '零件识别'))}</strong> <span class="badge ${job.status === 'failed' ? 'danger' : terminal(job.status) ? 'success' : 'warning'}">${escape(jobStatus[job.status] || job.status)}</span><div class="small">${escape(job.message || job.progress?.label || job.progress?.message || (typeof job.error === 'string' ? job.error : job.error?.message) || job.phase || '')}</div>${job.progress?.completed != null && job.progress?.total ? `<div class="small">${escape(job.progress.completed)} / ${escape(job.progress.total)} 份已处理</div>` : ''}</div>${!terminal(job.status) ? `<button class="button" data-action="stop-job" data-id="${escape(job.id || job.job_id)}" data-mutation>停止</button>` : ''}</div>`).join('')}${state.newJobResults ? '<button class="button" data-action="refresh-discovery">查看新的识别建议</button>' : ''}`;
  }
  async function pollJobs(token) {
    if (!state.order || token !== state.token) return;
    try {
      const response = await request(endpoint('/jobs'));
      if (token !== state.token) return;
      const previous = JSON.stringify(state.jobs.map(job => [job.id || job.job_id,job.status]));
      state.jobs = response.jobs || [];
      const current = JSON.stringify(state.jobs.map(job => [job.id || job.job_id,job.status]));
      if (previous !== current && state.jobs.some(job => terminal(job.status))) state.newJobResults = true;
      renderJobs();
    } catch (_) { const container = document.querySelector('#order-jobs'); if (container && !container.hidden) { const paragraph = document.createElement('p'); paragraph.className = 'small'; paragraph.textContent = '进度连接暂时中断，将自动重连；当前输入保留。'; if (!container.querySelector('[data-offline]')) { paragraph.dataset.offline = ''; container.append(paragraph); } } }
    finally { if (token === state.token) state.timer = setTimeout(() => pollJobs(token), document.hidden ? 12000 : 4000); }
  }
  async function refreshDiscovery() {
    const token = state.token;
    const response = await request(endpoint());
    if (token !== state.token) return;
    if (response.revision !== state.order.revision) {
      if (state.dirty || state.pendingRestore) {
        throw new Error(`服务器订单已更新为版本 ${response.revision}，当前草稿仍基于版本 ${state.order.revision}。本地输入已保留，请点击“重新同步服务器记录”并选择如何保留草稿，避免覆盖其他修改。`);
      }
      acceptOrder(response);
      state.newJobResults = false; renderStep(); renderJobs(); updateSaveState(); return;
    }
    state.order.discovery = response.discovery;
    state.order.documents = response.documents;
    state.order.issues = response.issues;
    state.order.part_status = response.part_status;
    state.order.demands = response.demands;
    state.order.quotes = response.quotes || [];
    state.quotes = state.order.quotes;
    state.newJobResults = false; persist(); renderStep(); renderJobs(); updateSaveState();
  }
  async function uploadOne(item) {
    const token = state.token;
    item.expectedRevision ??= state.order.revision;
    item.status = 'uploading'; item.error = ''; document.querySelector('#upload-queue').innerHTML = uploadQueueView();
    try {
      const response = await request(endpoint('/documents?name=' + encodeURIComponent(item.file.name)), { method: 'POST', headers: { 'Content-Type': item.file.type || 'application/octet-stream', 'X-Event-Id': item.eventId, 'X-Expected-Revision': String(item.expectedRevision ?? state.order.revision) }, body: item.file });
      if (token !== state.token) return;
      acceptOrder(response, true); item.status = 'done';
    } catch (error) { if (token !== state.token) return; item.status = 'error'; item.error = error.message; }
    if (state.step === 0) renderStep(); updateSaveState();
  }
  function adoptedParts() { return candidates().filter((candidate,index) => state.selected.has(candidateKey(candidate,index))); }
  function candidateRefs(candidate) {
    if (candidate.source_refs?.length) return clone(candidate.source_refs);
    const evidence = Array.isArray(candidate.evidence) ? candidate.evidence : candidate.evidence && typeof candidate.evidence === 'object' ? [candidate.evidence] : [];
    return evidence.filter(item => item.reference || item.document_id).map(item => ({document_id:item.document_id || item.reference,...(item.page != null ? {page:item.page} : {}),...(item.region ? {region:item.region} : {})}));
  }
  function applyCandidates(mergeTarget) {
    const selected = adoptedParts(); if (!selected.length) throw new Error('请先勾选需要采用的零件建议。');
    let target = mergeTarget ? partById(mergeTarget) : null;
    if (mergeTarget && !target) throw new Error('请选择合并到哪个已有零件。');
    for (const candidate of selected) {
      if (!target) {
        const part = newPart({ name: candidate.name || candidate.part_name || candidate.part_number || '待核对零件', part_number: candidate.part_number || '', version: candidate.version || '', mode: ['make','buy','assembly'].includes(candidate.mode) ? candidate.mode : 'make', document_ids: clone(candidate.document_ids || []), source_refs: candidateRefs(candidate), discovery_key: candidate.key || candidate.id || null });
        state.draft.parts.push(part);
        const candidateRoutes = candidate.suggested_route || candidate.routes || candidate.process_route || [];
        state.draft.routes[part.id] = candidateRoutes.map(route => ({ id: id('R'), operation: typeof route === 'string' ? route : route.operation || '', reason: typeof route === 'string' ? '识别建议，待技术核对' : route.reason || '', status: 'pending_review' }));
      } else {
        target.document_ids = [...new Set([...(target.document_ids || []), ...(candidate.document_ids || [])])];
        target.source_refs = [...(target.source_refs || []), ...candidateRefs(candidate)];
      }
    }
    state.selected.clear(); dirty(); renderStep(); toast(mergeTarget ? '已合并到本地草稿，请核对文件归属后保存。' : '已采用为零件草稿，订购数量需另行填写。');
  }
  function confirmDialog(title, body, confirmAction, label) {
    dialogContent.innerHTML = `<h2>${escape(title)}</h2><p>${escape(body)}</p><div class="footer"><button class="button" data-action="close-dialog">取消</button><button class="button primary" data-action="${escape(confirmAction)}">${escape(label || '确认')}</button></div>`;
    dialog.showModal();
  }
  function showCreate() {
    dialogContent.innerHTML = `<form id="create-order" class="order-create"><h2>新建批量订单</h2><p class="small">同一个客户询价中的多个产品和零件，放在一个订单里整理。</p><label class="field"><span>订单名称</span><input class="input" name="name" required maxlength="160" placeholder="例如：支架与护罩试制订单"></label><label class="field"><span>询价说明（可选）</span><textarea class="input" name="inquiry" maxlength="4000" placeholder="填写已知要求；未知数量可稍后补充。"></textarea></label><p class="note">本次创建模拟订单，报价草稿仍需人工审核。</p><p data-create-error class="alert error" hidden></p><div class="footer"><button class="button" type="button" data-action="close-dialog">取消</button><button class="button primary" type="submit">创建并上传资料</button></div></form>`;
    dialog.showModal(); dialog.querySelector('input').focus();
  }
  async function route() {
    const token = ++state.token; clearTimeout(state.timer); state.order = null; state.draft = null; state.jobs = []; state.selected.clear(); state.uploads = []; state.newJobResults = false; state.pendingRestore = null; state.error = ''; state.busy = false;
    const orderId = decodeURIComponent(location.hash.slice(1).split('/')[0] || '');
    content.setAttribute('aria-busy','true'); content.innerHTML = '<div class="loading">正在加载订单…</div>';
    try {
      if (!orderId) {
        const response = await request(API); if (token !== state.token) return;
        document.querySelector('#breadcrumb').textContent = '批量订单'; content.className = 'orders-page';
        content.innerHTML = `<div class="page-head"><div><h1>批量订单</h1><p class="subtitle">一单多产品、多份图纸，按零件与装配关系汇总报价。</p></div><button class="button primary" data-action="create-order">新建批量订单</button></div><section class="panel">${(response.orders || []).map(order => `<div class="order-list-item"><div><strong>${escape(order.name || order.id)}</strong><div class="small">${escape(order.id)} · ${order.parts?.length ?? order.part_count ?? 0} 个零件 · ${escape(order.status || '资料整理中')}</div></div><a class="button" href="#${encodeURIComponent(order.id)}">打开订单</a></div>`).join('') || '<div class="orders-empty">暂无批量订单，先创建一单并上传图纸。</div>'}</section>`;
      } else {
        const response = await request(API + '/' + encodeURIComponent(orderId)); if (token !== state.token) return;
        state.order = response.order || response; state.draft = structure(state.order); state.dirty = false; state.quotes = state.order.quotes || []; state.step = 0; state.policy = {pricing_method:'',rate_percent:''}; state.sharedFees = [];
        let stored; try { stored = JSON.parse(localStorage.getItem(key(orderId))); } catch (_) {}
        if (stored?.draft) {
          state.step = Math.min(3, Math.max(0, Number(stored.step) || 0)); state.policy = stored.policy || state.policy; state.sharedFees = stored.sharedFees || [];
          if (stored.dirty && stored.revision !== state.order.revision) state.pendingRestore = stored;
          else if (stored.dirty) { state.draft = stored.draft; state.dirty = true; }
        }
        renderShell(); pollJobs(token);
      }
    } catch (error) { if (token === state.token) content.innerHTML = `<section class="panel"><h1>订单暂时无法打开</h1><p class="alert error">${escape(error.message)}</p><a class="button" href="#">返回列表</a><button class="button" data-action="reload-order">重试</button></section>`; }
    finally { if (token === state.token) content.setAttribute('aria-busy','false'); }
  }
  content.addEventListener('input', event => {
    const input = event.target; if (!state.draft) return;
    if (input.dataset.part) { const part = partById(input.dataset.part); if (part) { if (['purchase_price','assembly_unit_cost','assembly_setup_fee','cost_inclusions','mode'].includes(input.dataset.property)) { part.cost_confirmed = false; const checkbox = document.querySelector(`[data-cost-confirm="${CSS.escape(part.id)}"]`); if (checkbox) checkbox.checked = false; } part[input.dataset.property] = ['purchase_price','assembly_unit_cost','assembly_setup_fee'].includes(input.dataset.property) ? nullableNumber(input.value) : input.value; dirty(); } }
    else if (input.dataset.line) { const row = state.draft.lines.find(value => value.id === input.dataset.line); if (row) { row[input.dataset.property] = input.dataset.property === 'quantity' ? nullableNumber(input.value) : input.value; dirty(); } }
    else if (input.dataset.bom) { const row = state.draft.bom.find(value => value.id === input.dataset.bom); if (row) { row[input.dataset.property] = input.dataset.property === 'quantity' ? nullableNumber(input.value) : input.value; dirty(); } }
    else if (input.dataset.sourceIndex != null) {
      const part = partById(input.dataset.refPart); const ref = part?.source_refs?.[Number(input.dataset.sourceIndex)];
      if (ref) {
        const property = input.dataset.sourceProperty;
        if (property === 'page') { if (input.value === '') delete ref.page; else ref.page = Number(input.value); }
        else ref[property] = input.value;
        dirty();
      }
    }
    else if (input.dataset.route) { const row = routeRows(input.dataset.partId).find(value => value.id === input.dataset.route); if (row) { row.operation = input.value; row.status = 'pending_review'; dirty(); const badge = input.closest('.route-line').querySelector('.badge'); badge.textContent = '待核对'; badge.className = 'badge warning'; } }
    else if (input.dataset.policy) { state.policy[input.dataset.policy] = input.value; persist(); }
    else if (input.dataset.shared) { const fee = state.sharedFees.find(value => value.id === input.dataset.shared); if (fee) { fee[input.dataset.property] = input.value; persist(); } }
  });
  content.addEventListener('change', event => {
    const input = event.target;
    if (input.dataset.candidate != null) { if (input.checked) state.selected.add(input.dataset.candidate); else state.selected.delete(input.dataset.candidate); }
    if (input.dataset.costConfirm) { const part = partById(input.dataset.costConfirm); if (part) { part.cost_confirmed = input.checked; dirty(); } }
    if (input.dataset.partDocument) {
      const part = partById(input.dataset.partDocument);
      if (part) {
        part.document_ids = input.checked ? [...new Set([...(part.document_ids || []),input.value])] : (part.document_ids || []).filter(value => value !== input.value);
        if (!input.checked) part.source_refs = (part.source_refs || []).filter(ref => ref.document_id !== input.value);
        const files = input.closest('.part-files');
        files.querySelector('summary').textContent = `文件归属 · ${part.document_ids.length} 份`;
        const source = files.querySelector('.source-ref');
        if (source) { if (part.source_refs?.length) source.textContent = '识别来源：' + sourceLabel(part.source_refs); else source.remove(); }
        updateSourceEditor(part); dirty();
      }
    }
    if (input.dataset.part && input.dataset.property === 'mode') renderStep();
    if (input.matches('[data-upload-files]') && input.files.length) {
      const files = Array.from(input.files); input.value = '';
      perform(async () => { const token = state.token; await saveDraft(); for (const file of files) { if (token !== state.token) break; const item = {file,eventId:id('UPLOAD'),status:'pending'}; state.uploads.push(item); await uploadOne(item); } });
    }
  });
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-action],[data-step]'); if (!button || button.disabled) return;
    if (button.dataset.step != null) { if (state.busy) return; state.step = Number(button.dataset.step); persist(); renderStep(); const toolbar = content.querySelector('.order-toolbar'); if (toolbar) { const next = toolbar.querySelector('[data-action="next"]'); if (state.step === 3) next?.remove(); else if (!next) toolbar.querySelector('.actions').insertAdjacentHTML('beforeend','<button class="button primary" data-action="next" data-mutation>保存并继续</button>'); } return; }
    const action = button.dataset.action;
    if (action === 'create-order') return showCreate();
    if (action === 'close-dialog') return dialog.close();
    if (action === 'dismiss-error') return clearError();
    if (action === 'reload-order') { persist(); return route(); }
    if (!state.order) return;
    if (action === 'restore-draft' || action === 'discard-draft') {
      if (action === 'restore-draft') { state.draft = state.pendingRestore.draft; state.dirty = true; }
      state.pendingRestore = null; persist(); renderRestore(); renderStep(); updateSaveState(); return;
    }
    const local = callback => { if (state.busy) return; try { clearError(); callback(); } catch (error) { showError(error); } };
    if (action === 'add-source-ref') return local(() => { const part = partById(button.dataset.partId); (part.source_refs ||= []).push({document_id:''}); dirty(); updateSourceEditor(part); document.querySelector(`[data-source-editor="${CSS.escape(part.id)}"]`).open = true; });
    if (action === 'remove-source-ref') return local(() => { const part = partById(button.dataset.partId); part.source_refs.splice(Number(button.dataset.index),1); dirty(); updateSourceEditor(part); });
    if (action === 'add-part') return local(() => { state.draft.parts.push(newPart()); dirty(); renderStep(); });
    if (action === 'split-part') return local(() => { const original = partById(button.dataset.id); const part = newPart({...clone(original),id:id('P'),name:(original.name || '零件') + '（拆分）',task_id:null}); state.draft.parts.push(part); state.draft.routes[part.id] = routeRows(original.id).map(route => ({...route,id:id('R'),status:'pending_review'})); dirty(); renderStep(); toast('已拆出新零件草稿，请修改名称、文件归属和数量。'); });
    if (action === 'remove-part') return local(() => { const partId = button.dataset.id; const part = partById(partId); confirmDialog('移除零件？',`移除“${part.name || '未命名零件'}”及其订购行、BOM 关系和本页工序草稿；原图纸与既有单品任务保留。`,'confirm-remove-part','移除零件'); dialog.dataset.partId = partId; });
    if (action === 'confirm-remove-part') return local(() => { const partId = dialog.dataset.partId; state.draft.parts = state.draft.parts.filter(part => part.id !== partId); state.draft.lines = state.draft.lines.filter(line => line.part_id !== partId); state.draft.bom = state.draft.bom.filter(row => row.parent_id !== partId && row.part_id !== partId); delete state.draft.routes[partId]; dialog.close(); dirty(); renderStep(); });
    if (action === 'add-line') return local(() => { state.draft.lines.push(newLine()); dirty(); renderStep(); });
    if (action === 'remove-line') return local(() => { state.draft.lines = state.draft.lines.filter(line => line.id !== button.dataset.id); dirty(); renderStep(); });
    if (action === 'add-bom') return local(() => { state.draft.bom.push({id:id('BOM'),parent_id:'',part_id:'',quantity:null,unit:'piece'}); dirty(); renderStep(); });
    if (action === 'remove-bom') return local(() => { state.draft.bom = state.draft.bom.filter(row => row.id !== button.dataset.id); dirty(); renderStep(); });
    if (action === 'adopt-candidates') return local(() => applyCandidates());
    if (action === 'merge-candidates') return local(() => { const target = document.querySelector('[data-merge-target]').value; if (!target) throw new Error('请选择合并到哪个已有零件。'); applyCandidates(target); });
    if (action === 'add-route') return local(() => { (state.draft.routes[button.dataset.partId] ||= []).push({id:id('R'),operation:button.dataset.operation || '',reason:'手工补充，待技术核对',status:'pending_review'}); dirty(); renderStep(); });
    if (['route-up','route-down','remove-route'].includes(action)) return local(() => { const rows = routeRows(button.dataset.partId); const index = rows.findIndex(row => row.id === button.dataset.id); if (action === 'remove-route') rows.splice(index,1); else { const target = index + (action === 'route-up' ? -1 : 1); if (target >= 0 && target < rows.length) { [rows[index],rows[target]] = [rows[target],rows[index]]; rows.forEach(row => { row.status = 'pending_review'; }); } } dirty(); renderStep(); });
    if (action === 'confirm-visible-routes') return local(() => { const visible = [...document.querySelectorAll('[data-route]')].map(input => ({partId:input.dataset.partId,id:input.dataset.route})); if (!visible.length) throw new Error('本页没有可确认的工序，请先添加。'); if (visible.some(ref => !routeRows(ref.partId).find(row => row.id === ref.id)?.operation.trim())) throw new Error('请填写空白工序名称后再确认。'); visible.forEach(ref => { routeRows(ref.partId).find(row => row.id === ref.id).status = 'confirmed'; }); dirty(); renderStep(); toast('仅本页已展示工序标记为已核对，请保存。'); });
    if (action === 'add-shared-fee') return local(() => { state.sharedFees.push({id:id('FEE'),name:'',value:null,inclusions:''}); persist(); renderStep(); });
    if (action === 'remove-shared-fee') return local(() => { state.sharedFees = state.sharedFees.filter(fee => fee.id !== button.dataset.id); persist(); renderStep(); });
    if (action === 'save' || action === 'next') return perform(async () => { await saveDraft(); if (action === 'next') state.step = Math.min(3,state.step + 1); persist(); renderShell(); toast('本步已保存。'); });
    if (action === 'discover') return perform(async () => { await saveDraft(); const response = await mutation('discover','/discover',{}); acceptOrder(response); state.newJobResults = false; renderShell(); toast('后台识别已启动，可以离开本页。'); clearTimeout(state.timer); pollJobs(state.token); });
    if (action === 'refresh-discovery') return perform(refreshDiscovery);
    if (action === 'retry-upload') return perform(async () => { const item = state.uploads[Number(button.dataset.index)]; if (item) await uploadOne(item); });
    if (action === 'stop-job') return perform(async () => { await mutation('stop-'+button.dataset.id,'/jobs/'+encodeURIComponent(button.dataset.id)+'/stop',{}); clearTimeout(state.timer); await pollJobs(state.token); toast('停止请求已提交，已完成部分会保留。'); });
    if (action === 'prepare') return perform(async () => { await saveDraft(); const response = await mutation('prepare','/prepare',{}); acceptOrder(response); renderShell(); toast('单品任务已建立或关联，按汇总需求数量核对。'); });
    if (action === 'quote') return perform(async () => {
      if (!document.querySelector('[data-quote-confirm]')?.checked) throw new Error('请先勾选本次报价核对确认。');
      if (!state.policy.pricing_method || state.policy.rate_percent === '' || !Number.isFinite(Number(state.policy.rate_percent)) || Number(state.policy.rate_percent) < 0) throw new Error('请选择定价方式并填写有效比例。');
      const shared = state.sharedFees.map(fee => ({...fee,inclusions:Array.isArray(fee.inclusions) ? fee.inclusions.join('；') : String(fee.inclusions || '').trim()}));
      if (shared.some(fee => !fee.name.trim() || fee.value == null || fee.value === '' || !Number.isFinite(Number(fee.value)) || Number(fee.value) < 0 || !fee.inclusions.length)) throw new Error('共用费用需填写名称、金额和包含项说明；不收取的行可移除。');
      await saveDraft(); const quote = await mutation('quote','/quotes',{policy:{...state.policy,tax_excluded:true,freight_excluded:true},shared_fees:shared}); const result = quote.quote || quote; const current = await request(endpoint()); acceptOrder(current); if (!state.quotes.some(entry => (entry.id || entry.quote_id) === (result.id || result.quote_id))) state.quotes.push(result); renderStep(); toast('模拟整单报价草稿已生成。');
    });
    if (action === 'load-quote') return perform(async () => { const quote = await request(endpoint('/quotes/' + encodeURIComponent(button.dataset.id))); const value = quote.quote || quote; const index = state.quotes.findIndex(entry => (entry.id || entry.quote_id) === button.dataset.id); if (index >= 0) state.quotes[index] = value; else state.quotes.push(value); document.querySelector('#order-quotes').innerHTML = quotesView(); });
  });
  dialog.addEventListener('submit', event => {
    if (event.target.id !== 'create-order') return; event.preventDefault();
    const form = event.target; const data = new FormData(form); const body = {name:String(data.get('name') || '').trim(),inquiry:String(data.get('inquiry') || '').trim(),simulated:true};
    if (!body.name) return;
    const button = form.querySelector('[type="submit"]'); button.disabled = true;
    const pending = eventPayload('create',body);
    request(API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(pending.payload)}).then(response => { pending.clear(); dialog.close(); location.hash = encodeURIComponent((response.order || response).id); }).catch(error => { const element = form.querySelector('[data-create-error]'); element.textContent = error.message; element.hidden = false; }).finally(() => { button.disabled = false; });
  });
  window.addEventListener('hashchange',route);
  window.addEventListener('pagehide',persist);
  route();
})();
