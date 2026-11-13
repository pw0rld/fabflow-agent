/* Production workbench: all records and amounts come from the business API. */
(() => {
  'use strict';
  const API = '/workbench/api';
  const content = document.querySelector('#content');
  const dialog = document.querySelector('#review-dialog');
  const activeRuns = new Set(['starting', 'running', 'unknown', 'stopping']);
  const commonOperations = ['激光下料', '去毛刺', '折弯', '焊接', '压铆/组装', '喷粉', '镀锌', '检验包装'];
  const inclusionChoices = ['准备费已包含在工序报价中，不另收准备费。','材料费用按材料用量和材料单价单独计入。','工序费用按各行所选单位计入。'];
  const statusNames = {missing:'待补充',unreadable:'读不清',pending_review:'待核对',verified:'已核对',conflict:'有冲突'};
  const unitNames = {'piece':'件','kg/piece':'kg/件','CNY/kg':'元/kg','CNY/piece':'元/件','CNY/order':'元/单','mm':'mm'};
  const state = {
    page:'tasks', task:null, tasks:[], tab:'requirements', busy:false, error:null,
    drafts:new Map(), selectedFiles:[], uploadInquiry:'', simulated:true,
    attachment:0, zoom:100, quote:null, quoteView:'quote', search:'', filter:'all',
    policy:new Map(), pending:new Map(), timer:null, prices:[], priceEditor:null, priceMatches:null,
    selectedPrices:{}, conflict:null, listError:null, loading:false, pendingRoute:false, routeToken:0,
    sync:new Map(), observedRuns:new Map(), pendingRecords:new Map(), listSync:{at:null,error:null,refreshing:false}, formFocus:new Map(), detailsOpen:new Map(),
    templates:null, templateLoading:false, templateUis:new Map(), templatePreview:null
  };
  const esc = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
  const clone = value => value == null ? value : JSON.parse(JSON.stringify(value));
  const equal = (left,right) => JSON.stringify(left) === JSON.stringify(right);
  const taskId = () => state.task?.id;
  const fieldValue = name => state.task?.fields?.[name]?.value ?? null;
  const drafts = () => {
    if (!state.drafts.has(taskId())) state.drafts.set(taskId(),{});
    return state.drafts.get(taskId());
  };
  const value = name => Object.hasOwn(drafts(),name) ? drafts()[name] : fieldValue(name);
  const dirty = () => Object.keys(drafts()).length > 0;
  const policy = () => {
    if (!state.policy.has(taskId())) state.policy.set(taskId(),{pricing_method:'',rate_percent:'',conditions:false});
    return state.policy.get(taskId());
  };
  const button = (label,action,classes='',attributes='') => `<button type="button" class="button ${classes}" data-action="${action}" ${state.busy?'disabled':''} ${attributes}>${label}</button>`;
  const safeUrl = raw => {
    if (typeof raw !== 'string' || !raw.startsWith('/') || raw.startsWith('//')) return '';
    const url = new URL(raw,location.origin);
    return url.origin === location.origin && (url.pathname.startsWith('/workbench/') || url.pathname.startsWith('/quote-preview/')) ? url.pathname+url.search+url.hash : '';
  };
  const fileLink = (raw,label,classes='button',download=false) => {
    const url = safeUrl(raw);
    return url ? `<a class="${classes}" href="${esc(url)}" target="_blank" rel="noopener" ${download?'download':''}>${label}</a>` : '';
  };
  const money = raw => {
    if (raw === null || raw === undefined || raw === '') return '待核算';
    const [whole,fraction='00'] = String(raw).split('.');
    return '¥'+whole.replace(/\B(?=(\d{3})+(?!\d))/g,',')+'.'+fraction.padEnd(2,'0');
  };
  const date = raw => {
    if (!raw) return '—';
    const parsed = new Date(raw);
    return Number.isNaN(parsed.valueOf()) ? String(raw) : parsed.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
  };
  const timestamp = raw => {
    if(raw===null||raw===undefined||raw==='')return null;
    const parsed=new Date(typeof raw==='string'&&/^\d{10,}$/.test(raw)?Number(raw):raw).valueOf();
    return Number.isFinite(parsed)?parsed:null;
  };
  const clockTime = raw => {
    const parsed=timestamp(raw);
    return parsed===null?'尚无记录':new Date(parsed).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});
  };
  function duration(milliseconds) {
    const seconds=Math.max(0,Math.floor(milliseconds/1000));
    if(seconds<60)return `${seconds} 秒`;
    const minutes=Math.floor(seconds/60);
    return minutes<60?`${minutes} 分 ${seconds%60} 秒`:`${Math.floor(minutes/60)} 小时 ${minutes%60} 分`;
  }
  function relativeTime(raw) {
    const parsed=timestamp(raw);
    if(parsed===null)return '尚无记录';
    const delta=Date.now()-parsed;
    return delta<5000?'刚刚':duration(delta)+'前';
  }
  function updateClocks() {
    document.querySelectorAll('[data-run-start]').forEach(element=>{
      const started=timestamp(element.dataset.runStart);
      const end=timestamp(element.dataset.runEnd);
      element.textContent=started===null?'开始时间待确认':duration((end??Date.now())-started);
    });
    document.querySelectorAll('[data-relative-time]').forEach(element=>{element.textContent=relativeTime(element.dataset.relativeTime);});
  }
  const runLabel = run => run?.progress?.label || ({starting:'准备读图',running:'AI 正在读取图纸',unknown:'读图状态待确认',waiting:'等待补充资料',completed:'本轮读图已结束',failed:'本轮读图未完成',stopping:'正在停止读图',stopped:'读图已停止'}[run?.status]||'读图状态待确认');
  function observeRun(task) {
    if(!task?.run)return;
    const previous=state.observedRuns.get(task.id);
    const current={status:task.run.status,started_at:task.run.started_at};
    state.observedRuns.set(task.id,current);
    if(!previous||!activeRuns.has(previous.status)||activeRuns.has(current.status)||previous.started_at!==current.started_at)return;
    const name=task.product_name||task.fields?.product_name?.value||'询价任务';
    if(current.status==='completed')toast(`${name}：读图已完成，返回任务核对参数。`);
    else if(current.status==='waiting')toast(`${name}：需要补充资料，请返回任务查看。`);
    else if(current.status==='failed')toast(`${name}：读图未完成，请返回任务查看原因。`);
    else if(current.status==='stopped')toast(`${name}：后台读图已停止。`);
  }
  function markSynced(id) {state.sync.set(id,{at:Date.now(),error:null,refreshing:false});}
  function toast(message) {
    const element = document.querySelector('#toast');
    element.textContent = message; element.hidden = false;
    clearTimeout(toast.timer); toast.timer = setTimeout(() => {element.hidden=true;},5000);
  }
  async function request(path,options={}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(),45000);
    try {
      const response = await fetch(API+path,{credentials:'same-origin',...options,signal:controller.signal,headers:{'X-Workbench-Request':'1',...(options.body instanceof File || options.body instanceof Blob ? {} : {'Content-Type':'application/json'}),...options.headers}});
      const type = response.headers.get('content-type') || '';
      const result = type.includes('application/json') ? await response.json() : null;
      if (!response.ok) {
        const error = new Error(result?.error?.message || (response.status===401?'登录已失效，请重新登录后继续。':`请求未完成（${response.status}），请稍后重试。`));
        error.code=result?.error?.code; error.status=response.status; error.details=result?.error?.details;
        throw error;
      }
      if (!result) throw new Error('服务暂未返回可用结果，请稍后重试。');
      return result;
    } catch(error) {
      if (error.name==='AbortError') throw new Error('等待服务响应超时，已填写内容仍保留。请先刷新任务状态，再重试原操作。');
      if (error instanceof TypeError) throw new Error('暂时无法连接服务，已填写内容仍保留。请检查网络后重试。');
      throw error;
    } finally {clearTimeout(timeout);}
  }
  // Unresolved requests retain their original ID/payload in pending. A later
  // deliberate operation receives a new ID even when its visible values match.
  function eventId() {return 'web-'+crypto.randomUUID();}
  async function mutate(path,payload,key=path) {
    const semantic={...payload};delete semantic.expected_revision;
    const signature=key+':'+JSON.stringify(semantic);
    if(!state.pending.has(signature))state.pending.set(signature,{...payload,event_id:eventId(key,payload)});
    const body=state.pending.get(signature);
    try {
      const result=await request(path,{method:'POST',body:JSON.stringify(body)});
      state.pending.delete(signature);return result;
    } catch(error) {if(error.status&&error.status<500)state.pending.delete(signature);throw error;}
  }
  function takeTask(result) {
    state.task = result.task || result;
    if (!state.task?.id) throw new Error('任务返回不完整，请刷新后重试。');
    state.pendingRecords.delete(state.task.id);
    observeRun(state.task);markSynced(state.task.id);
    schedulePoll();
  }
  function businessStatus(task=state.task) {
    if (!task) return '待接收';
    if (task.quotes?.some(item=>item.source_current)) return '已生成草稿';
    if (task.quotes?.length) return '需重新核算';
    if (task.validation?.ready||task.ready) return '可核算';
    const status = task.validation?.status || task.status;
    const names={received:'资料已接收',needs_customer:'待补需求',needs_technical:'待补技术',needs_review:'待核对',ready:'可核算',awaiting_customer:'待补需求',awaiting_technical:'待补技术',pending_review:'待核对',conflict:'待核对/冲突'};
    return names[status] || (typeof status==='string' && /[\u4e00-\u9fff]/.test(status) ? status : '待确认参数');
  }
  function title(name,subtitle='',actions='') {return `<div class="page-head"><div><h1>${esc(name)}</h1><div class="subtitle">${esc(subtitle)}</div></div><div class="actions">${actions}</div></div>`;}
  function steps(current) {return `<nav class="steps" aria-label="报价步骤">${[['upload','上传资料'],['confirm','确认参数'],['quote','查看报价']].map(([key,label],index)=>`<a href="#task/${encodeURIComponent(taskId()||'new')}/${key}" class="step ${current===key?'active':''}"><span>${index+1}</span>${label}</a>`).join('')}</nav>`;}
  function errorView() {
    if (!state.error) return '';
    return `<div class="alert error" role="alert"><strong>操作未完成</strong><div>${esc(state.error.message)}</div><div class="actions">${taskId()?button('刷新任务状态','refresh-task'):button('重新加载','refresh-list')}${state.conflict?button('核对最新资料与未保存输入','review-conflict'):''}${button('关闭提示','dismiss-error','link')}</div></div>`;
  }
  function runView() {
    const run=state.task?.run;
    if (!run) return '';
    return `<section class="run-panel" data-run-panel aria-label="后台读图任务">${runPanelContent(run)}</section>`;
  }
  function runElapsed(run) {
    const end=run.finished_at||run.ended_at||run.completed_at;
    const started=timestamp(run.started_at);
    if(started===null||!activeRuns.has(run.status)&&!end)return '';
    return `<span>${activeRuns.has(run.status)?'已运行':'本轮历时'} <strong data-run-start="${esc(run.started_at)}" ${end?`data-run-end="${esc(end)}"`:''}>${duration((timestamp(end)??Date.now())-started)}</strong></span>`;
  }
  function reviewView(run) {
    const review=run.review;
    if(!review||review.status==='not_needed')return '';
    const labels={pending:'待回查',starting:'正在准备回查',running:'正在回查',unknown:'回查状态待确认',completed:'回查已结束',failed:'回查未完成',stopped:'回查已停止'};
    const fields=Array.isArray(review.fields)?review.fields.map(name=>state.task.definitions?.[name]?.label||name):[];
    return `<div class="run-review"><strong>关键标注回查（最多一次）</strong><span>${esc(labels[review.status]||'状态待确认')}${fields.length?' · '+esc(fields.join('、')):''}</span>${review.status==='failed'?'<span class="small">初次识别结果保留，请结合原图核对。</span>':''}</div>`;
  }
  function runPanelContent(run) {
    const active=activeRuns.has(run.status),progress=run.progress||{},sync=state.sync.get(taskId())||{};
    const events=Array.isArray(progress.events)?progress.events:[];
    const activity=progress.last_activity_at;
    const retry=progress.retry;
    return `<div class="run-panel-head"><div><div class="small">${active?'服务器后台任务':'本轮后台任务'}</div><h2>${active?'<span class="spinner" aria-hidden="true"></span>':''}${esc(runLabel(run))}</h2></div><span class="badge ${run.status==='failed'?'danger':active||run.status==='waiting'?'warning':'success'}">${active?'后台处理中':run.status==='failed'?'未完成':run.status==='stopped'?'已停止':run.status==='waiting'?'待补资料':'已结束'}</span></div>
      ${active?'<p class="run-background-note">服务器后台执行，可离开页面，返回任务查看结果。</p>':'<p class="small">读图信息仅为候选，请在参数页核对后采用。</p>'}
      <div class="run-metrics"><span>开始时间 <strong>${esc(clockTime(run.started_at))}</strong></span>${runElapsed(run)}<span>最近活动 <strong data-relative-time="${esc(activity??'')}">${relativeTime(activity)}</strong></span></div>
      <div class="run-connection ${sync.error?'is-disconnected':''}" role="status"><span>${sync.error?'连接暂时中断，当前显示最近一次状态；后台任务不会因关闭页面而取消。':sync.refreshing?'正在刷新页面状态…':`状态连接正常${sync.at?` · <span data-relative-time="${sync.at}">${relativeTime(sync.at)}</span>刷新`:''}`}</span>${run.checked_at?`<span class="small">服务器检查 ${esc(clockTime(run.checked_at))}</span>`:''}</div>
      ${!activity&&run.last_progress_at?`<div class="small">阶段更新于 ${esc(clockTime(run.last_progress_at))}；尚无模型活动时间记录。</div>`:''}
      ${retry?`<div class="small">服务重试记录${Number.isFinite(retry.attempt)?`：第 ${esc(retry.attempt)} 次`:''}${Number.isFinite(retry.delay_ms)?`，延迟 ${duration(retry.delay_ms)}`:''}。</div>`:''}
      ${reviewView(run)}
      ${run.error?`<div class="alert error">${esc(run.error)}</div>`:''}${(run.warnings||[]).map(warning=>`<div class="alert">${esc(warning)}</div>`).join('')}
      ${state.pendingRecords.has(taskId())?'<div class="alert run-record-update">已有新的已保存资料。本页输入保持原样，载入后再核对。'+button('载入最新参数','refresh-task','link')+'</div>':''}
      ${events.length?`<details class="run-history"><summary>查看本轮进度记录（${events.length} 项）</summary><ol>${events.map(event=>`<li><span>${esc(event.label||'状态已更新')}</span>${event.at!==null&&event.at!==undefined?`<time>${esc(clockTime(event.at))}</time>`:''}</li>`).join('')}</ol></details>`:''}
      <div class="run-panel-actions"><a class="button" href="#tasks">${active?'返回列表，后台继续':'返回询价列表'}</a><div class="actions">${sync.error?button('重新获取状态','refresh-task'):''}${active?button('停止后台任务','stop-read','danger'):button(run.status==='failed'?'重试读图':'重新读图','read','link')}</div></div>`;
  }
  function filteredTasks() {
    const query=state.search.trim().toLowerCase();
    return state.tasks.filter(task=>!query||[task.id,task.product_name,task.inquiry,task.customer_note].some(item=>String(item||'').toLowerCase().includes(query))).filter(task=>state.filter==='all'||(state.filter==='quotes'?task.quotes?.length||task.quote_count:!(task.validation?.ready||task.ready)&&!task.quotes?.length));
  }
  function taskRuntime(task) {
    const run=task.run;
    if(!run)return '';
    return `<div class="task-runtime"><strong>${esc(runLabel(run))}</strong>${runElapsed(run)}${activeRuns.has(run.status)?'<span class="small">服务器后台执行</span>':''}</div>`;
  }
  function taskRows() {
    const tasks=filteredTasks();
    return tasks.map(task=>`<tr data-task-row="${esc(task.id)}"><td><strong>${esc(task.product_name||task.fields?.product_name?.value||'待确认产品')}</strong><div class="small">${esc(task.id)}</div>${task.simulated?'<span class="badge warning">模拟资料</span>':''}</td><td class="nowrap">${esc(task.quantity??task.fields?.quantity?.value??'待补')} ${task.quantity||task.fields?.quantity?.value?'件':''}</td><td><span class="badge ${task.validation?.ready||task.ready?'success':'warning'}">${esc(businessStatus(task))}</span>${taskRuntime(task)}</td><td class="nowrap small">${esc(date(task.updated_at))}</td><td><div class="actions"><a class="button link" href="#task/${encodeURIComponent(task.id)}/confirm">继续处理</a>${task.quotes?.length||task.quote_count?`<a class="button link" href="#task/${encodeURIComponent(task.id)}/quote">查看报价</a>`:''}</div></td></tr>`).join('')||`<tr><td colspan="5"><div class="empty"><h3>${state.search||state.filter!=='all'?'没有符合条件的任务':'还没有询价任务'}</h3><p>请新建询价，上传同一零件的图纸或资料。</p>${button('新建询价','new','primary')}</div></td></tr>`;
  }
  function listSyncView() {
    const running=state.tasks.filter(task=>activeRuns.has(task.run?.status)).length;
    if(state.listSync.error)return '<div class="alert">页面连接暂时中断，任务状态待确认；离开页面不会取消后台任务。正在重新获取状态。</div>';
    return `<div class="list-sync-line"><span>${running?`${running} 个任务在服务器后台处理，可离开页面，稍后返回查看结果。`:'显示最近保存的任务状态。'}</span><span class="small">${state.listSync.refreshing?'正在刷新状态…':state.listSync.at?`页面 <span data-relative-time="${state.listSync.at}">${relativeTime(state.listSync.at)}</span>刷新`:''}</span></div>`;
  }
  function taskList() {
    return title('询价任务','上传图纸、集中确认参数，在同一任务内形成报价草稿。',button('新建询价','new','primary'))+
      `<section class="panel"><div class="toolbar"><label class="visually-hidden" for="task-search">搜索询价</label><input id="task-search" class="input" type="search" data-search placeholder="搜索产品、询价编号或备注" value="${esc(state.search)}"><label class="visually-hidden" for="task-filter">筛选状态</label><select id="task-filter" class="input" data-filter><option value="all" ${state.filter==='all'?'selected':''}>全部任务</option><option value="todo" ${state.filter==='todo'?'selected':''}>待处理</option><option value="quotes" ${state.filter==='quotes'?'selected':''}>已有报价</option></select>${button('刷新','refresh-list')}</div><div data-list-sync>${listSyncView()}</div>${state.listError?`<div class="alert error">${esc(state.listError)}</div>`:''}<div class="table-wrap"><table><thead><tr><th>询价 / 产品</th><th>数量</th><th>业务状态 / 后台进度</th><th>更新</th><th>操作</th></tr></thead><tbody data-task-rows>${taskRows()}</tbody></table></div></section>`;
  }
  function attachmentsView() {
    return `<div class="file-list">${(state.task?.attachments||[]).map(file=>`<div class="file-row"><span>${esc(file.name)}<span class="small"> · 已上传</span></span>${fileLink(file.url,'打开文件','button link')}</div>`).join('')}${state.selectedFiles.map((file,index)=>`<div class="file-row"><span>${esc(file.name)}<span class="small"> · 待上传</span></span>${button('移除','remove-file','link',`data-index="${index}"`)}</div>`).join('')}</div>`;
  }
  function uploadPage() {
    const existing=!!taskId();
    return title(existing?'补充询价资料':'新建询价','同一零件可上传多页图纸；加工数量在下一步单独确认。')+(existing?steps('upload'):'')+
      (existing?runView():'')+`<section class="panel"><div class="upload-zone" data-dropzone><h2>上传待报价图纸</h2><div class="small">图片、PDF 或 CAD 文件</div><input type="file" data-files multiple accept=".jpg,.jpeg,.png,.webp,.gif,.bmp,.tif,.tiff,.pdf,.dxf,.dwg,.step,.stp" aria-label="选择待报价图纸" ${state.busy?'disabled':''}><div class="small">也可拖放文件到这里。CAD 或不清晰的图纸可能需要补充截图。</div></div>${attachmentsView()}<div class="field-grid single section-gap"><label class="field"><span>客户询价与补充说明</span><textarea class="input" data-inquiry placeholder="例如产品用途、客户备注、已知加工要求…" ${existing?'readonly':''}>${esc(existing?state.task.inquiry:state.uploadInquiry)}</textarea>${existing?'<small>新要求可在参数确认页补录；原始询价保留。</small>':'<small>尚不明确的信息可以留空，不影响先接收图纸。</small>'}</label></div>${!existing?`<label class="check section-gap"><input type="checkbox" data-simulated ${state.simulated?'checked':''}>本次使用模拟资料</label>`:''}<div class="footer"><span class="small">${existing?'已上传资料不会因重试而删除。':'一笔询价对应一个产品，不按文件数量推算件数。'}</span><div class="actions">${button(existing?'保存所选附件':'创建询价并上传','upload','',!state.selectedFiles.length&&existing?'disabled':'')}${button(existing?'开始读图':'上传并开始读图','upload-read','primary')}</div></div></section>`;
  }
  function evidence(name) {
    const field=state.task.fields?.[name]||{};
    const items=Array.isArray(field.evidence)?field.evidence:field.evidence?[field.evidence]:[];
    const texts=items.map(item=>typeof item==='string'?item:[item.label,item.text,item.source_name,item.page?`第 ${item.page} 页`:null,item.region].filter(Boolean).join(' · ')).filter(Boolean);
    return texts.length?`<details class="evidence"><summary>查看依据</summary>${texts.map(text=>`<p>${esc(text)}</p>`).join('')}</details>`:'';
  }
  function fieldMeta(name) {
    return `<div data-field-meta="${esc(name)}">${fieldMetaContent(name)}</div>`;
  }
  function fieldObservations(name) {
    return (Array.isArray(state.task.run?.observations)?state.task.run.observations:[]).filter(item=>item&&item.name===name&&typeof item.raw_text==='string');
  }
  function observationView(name,items) {
    if(!items.length)return '';
    const labels={observed:'已识别，待核对',unit_unknown:'单位待确认',conflict:'标注待消解',unreadable:'无法辨认'};
    return `<section class="field-observations" aria-label="${esc(state.task.definitions?.[name]?.label||name)}图纸原始标注"><div class="observation-heading">图纸原始标注 · ${items.length} 处</div><div class="small">原文仅供核对，不会自动填入参数。</div><ul>${items.map(item=>{
      const source=item.evidence||{};
      const file=(state.task.attachments||[]).find(attachment=>attachment.id===source.reference);
      const location=[file?.name||'来源待核对',source.page!==null&&source.page!==undefined?`第 ${source.page} 页`:null,typeof source.region==='string'?source.region:null].filter(Boolean).join(' · ');
      return `<li><div class="observation-line"><q>${esc(item.raw_text||'（标注无法辨认）')}</q><span class="observation-status ${item.status==='conflict'?'danger':''}">${esc(labels[item.status]||'待核对')}</span></div><div class="observation-source">${esc(location)}${file?fileLink(file.url,'打开原图','observation-link'):''}</div>${item.reason?`<div class="small observation-reason">${esc(item.reason)}</div>`:''}</li>`;
    }).join('')}</ul></section>`;
  }
  function fieldMetaContent(name) {
    const field=state.task.fields?.[name]||{},status=field.status||'missing',items=fieldObservations(name);
    const hasValue=field.value!==null&&field.value!==undefined&&field.value!==''&&(!Array.isArray(field.value)||field.value.length>0);
    let label=statusNames[status]||'待核对';
    if(status!=='verified') {
      if(status==='conflict'||items.some(item=>item.status==='conflict'))label='标注待消解';
      else if(items.some(item=>item.status==='unit_unknown'))label='已识别，单位待确认';
      else if(items.length&&items.every(item=>item.status==='unreadable'))label='标注无法辨认';
      else if(items.length)label='已识别，待核对';
      else if(!hasValue&&status==='missing'&&state.task.run?.status==='completed')label='本轮未找到';
    }
    return `<div class="field-meta"><span class="small">${Object.hasOwn(drafts(),name)?'有未保存输入':''}</span><span class="badge ${status==='verified'?'success':status==='conflict'||items.some(item=>item.status==='conflict')?'danger':'warning'}">${esc(label)}</span></div>${observationView(name,items)}${evidence(name)}`;
  }
  function patchFieldMetadata() {
    content.querySelectorAll('[data-field-meta]').forEach(element=>{
      const open=element.querySelector('.evidence')?.open;
      element.innerHTML=fieldMetaContent(element.dataset.fieldMeta);
      if(open&&element.querySelector('.evidence'))element.querySelector('.evidence').open=true;
    });
  }
  function fieldInput(name,definition) {
    const raw=value(name), unit=unitNames[definition.unit]||definition.unit||'';
    let control='';
    if (definition.kind==='list') control=`<textarea class="input" data-field="${esc(name)}" placeholder="每行一项，未知可留空">${esc(Array.isArray(raw)?raw.join('\n'):raw||'')}</textarea>`;
    else {
      const numeric=['quantity','decimal','length'].includes(definition.kind);
      const list=name==='material_grade'?'grades':name==='surface_treatment'?'surfaces':name==='material_family'?'families':'';
      control=`<div class="input-wrap"><input class="input" data-field="${esc(name)}" ${list?`list="${list}"`:''} type="${numeric?'number':'text'}" ${numeric?`min="${name==='quantity'?'1':'0'}" step="${name==='quantity'?'1':'any'}" inputmode="decimal"`:''} value="${esc(raw??'')}" placeholder="待补充">${unit?`<span class="unit">${esc(unit)}</span>`:''}</div>`;
    }
    return `<label class="field ${definition.kind==='list'?'full':''}" data-field-container="${esc(name)}"><span>${esc(definition.label)}${definition.required?' <span class="inline-muted">*</span>':''}</span>${control}${fieldMeta(name)}</label>`;
  }
  function drawingView() {
    const files=state.task.attachments||[];
    if (state.attachment>=files.length) state.attachment=0;
    const file=files[state.attachment];
    const url=safeUrl(file?.url);
    const image=file && /^image\/(?!tiff)/.test(file.mime||'');
    const pdf=file?.mime==='application/pdf';
    const cad=url&&/\.(dxf|dwg|step|stp)$/i.test(file?.name||'');
    const preview=cad?`/workbench/cad-preview/index.html?task=${encodeURIComponent(taskId())}&source=${encodeURIComponent(file.id)}`:'';
    const key=cad?JSON.stringify([taskId(),file.id,files.map(item=>[item.id,item.name,item.url])]):'';
    return `<section class="panel drawing-panel" ${cad?`data-preview-key="${esc(key)}"`:''}><div class="panel-head"><h2>图纸资料</h2><a class="button link" href="#task/${encodeURIComponent(taskId())}/upload">补充文件</a></div><div class="drawing-toolbar"><label class="visually-hidden" for="attachment-select">切换图纸</label><select class="input" id="attachment-select" data-attachment ${files.length?'':'disabled'}>${files.length?files.map((item,index)=>`<option value="${index}" ${index===state.attachment?'selected':''}>${esc(item.name)}</option>`).join(''):'<option>暂无附件</option>'}</select>${image?`${button('−','zoom-out','',`aria-label="缩小图纸"`)}<span class="small">${state.zoom}%</span>${button('+','zoom-in','',`aria-label="放大图纸"`)}`:''}${cad?`<a class="button link" href="${esc(preview)}" target="_blank" rel="noopener">大图预览</a>`:''}</div><div class="drawing-canvas">${url&&image?`<img src="${esc(url)}" alt="${esc(file.name)}" style="width:${state.zoom}%;max-width:none">`:url&&(pdf||cad)?`<iframe title="${esc(file.name)}${cad?' · CAD 预览':''}" src="${esc(preview||url)}"></iframe>`:`<div class="drawing-placeholder"><h3>${file?esc(file.name):'还没有图纸'}</h3><p>${file?'该格式暂不能在此预览，可打开原文件，或补充清晰截图供读图。':'先上传图纸，也可以先补充已知参数。'}</p>${fileLink(file?.url,'打开原文件')}</div>`}</div><div class="small section-gap">AI 识别内容为候选。请结合原图核对，读不清或无法读取时补充资料。</div></section>`;
  }
  function requirementsForm() {
    const definitions=state.task.definitions||{};
    return `<div class="note">本页显示的参数将随本批提交一起核对。空白跳过，已有值不会被清空。</div><form data-stage="requirements"><div class="field-grid">${Object.entries(definitions).filter(([,definition])=>definition.stage==='customer').map(([name,definition])=>fieldInput(name,definition)).join('')}</div><div class="footer"><span class="small" data-save-state>${dirty()?'有未保存输入':'显示的是已保存资料'}</span><div class="actions">${button('保存本批输入','save-requirements')}${button('保存并继续技术补录','save-requirements-next','primary')}</div></div></form><datalist id="grades"><option>Q235B</option><option>Q355B</option><option>SUS304</option><option>6061</option></datalist><datalist id="surfaces"><option>不处理</option><option>喷粉</option><option>镀锌</option><option>电镀锌</option><option>热浸镀锌</option></datalist><datalist id="families"><option>碳钢</option><option>不锈钢</option><option>铝合金</option></datalist>`;
  }
  function routeValue() {const route=value('process_route');return Array.isArray(route)?route:[];}
  function ratesValue() {const rates=value('operation_rates');return Array.isArray(rates)?rates:[];}
  function inclusionValue() {const raw=value('fee_inclusions');return Array.isArray(raw)?raw:typeof raw==='string'?raw.split(/\r?\n/).map(line=>line.trim()).filter(Boolean):[];}
  function feeOverlap() {
    const positive=raw=>raw!==''&&raw!==null&&raw!==undefined&&Number.isFinite(Number(raw))&&Number(raw)>0;
    const rates=ratesValue().filter(rate=>routeValue().includes(rate.operation)&&rate.unit==='CNY/order'&&positive(rate.value));
    return positive(value('setup_fee'))&&rates.length?{setup_fee:value('setup_fee'),rates}:null;
  }
  function feeConflictContent() {
    const overlap=feeOverlap(),resolution=drafts()._feeResolution;
    if(!overlap)return resolution?`<div class="note fee-resolution" role="status"><strong>本页已取消另收准备费：${esc(money(resolution.from))} → ¥0.00 / 整单</strong><div>工序金额和计费单位保持原值；保存后才会更新本单。</div><p>请核对下方已有包含项说明。设为 0 只表示不另收，不代表准备费已经包含在其他费用中。</p>${button('核对包含说明','edit-fee-inclusions','link')}</div>`:'';
    return `<div class="alert fee-conflict" role="status"><strong>这两处都在按整单收费，请先核对</strong><ul><li>另收准备费 / 开机费：<strong>${esc(money(overlap.setup_fee))} / 整单</strong></li>${overlap.rates.map(rate=>`<li>${esc(rate.operation)}：<strong>${esc(money(rate.value))} / 整单</strong>（元/单）</li>`).join('')}</ul><p>当前版本不能同时计入正数的按单工序费和另收准备费。请核对准备费是否已经包含，或修改实际计费方式。</p><div class="actions">${button('本单不另收准备费，设为 0','cancel-setup-fee')}${button('修改工序计费','edit-operation-billing','',`data-operation-target="${esc(overlap.rates[0].operation)}"`)}${button('修改准备费','edit-setup-fee','link')}</div><div class="small">选择只修改本页草稿，保存后生效；不会自动改成按件计费。</div></div>`;
  }
  function inclusionForm() {
    const items=inclusionValue(),notes=items.filter(item=>!inclusionChoices.includes(item));
    const pendingNotes=notes.length&&['pending_review','conflict'].includes(state.task.fields?.fee_inclusions?.status);
    return `<div class="fee-inclusions" data-field-container="fee_inclusions"><span class="field-label">费用包含范围 *</span><div class="small">选择实际适用的口径即可，也可展开补充自己的说明。</div><div class="checks inclusion-checks">${inclusionChoices.map((item,index)=>`<label class="check"><input type="checkbox" data-inclusion="${index}" ${items.includes(item)?'checked':''}>${esc(item)}</label>`).join('')}</div><details class="form-details" data-fee-notes ${pendingNotes?'open':''}><summary>补充包含项说明${notes.length?` · 已有 ${notes.length} 条`:''}${pendingNotes?' · 待核对':''}</summary><label class="field"><span class="visually-hidden">补充包含项说明</span><textarea class="input" data-field="fee_inclusions" placeholder="已有口径不适用时填写，每行一项">${esc(notes.join('\n'))}</textarea></label></details>${fieldMeta('fee_inclusions')}</div>`;
  }
  const templateFields=['process_route','operation_rates','material_rate','setup_fee','fee_inclusions','material_usage_per_piece'];
  const templateLabels={process_route:'采用工序',operation_rates:'各工序价格',material_rate:'材料单价',setup_fee:'另收准备费 / 开机费',fee_inclusions:'费用包含说明',material_usage_per_piece:'每件材料用量',product_family:'产品族 / 工艺模板',material_form:'材料形态'};
  function templateUi() {
    if(!state.templateUis.has(taskId()))state.templateUis.set(taskId(),{selected:'',name:'',includeUsage:false,notice:''});
    return state.templateUis.get(taskId());
  }
  function templatePanelContent() {
    const ui=templateUi(),entries=(state.templates||[]).filter(item=>item.enabled!==false);
    return `<p class="small">保存常用工序、费率和费用口径，下次预览后一起带入。数量、板厚和尺寸仍按本单核对。</p><div class="template-controls"><label class="field"><span class="visually-hidden">选择常用模板</span><select class="input" data-template-select ${state.templateLoading?'disabled':''}><option value="">${state.templateLoading?'正在读取模板…':entries.length?'选择一个模板':'暂无可用模板'}</option>${entries.map(entry=>`<option value="${esc(entry.id)}" ${ui.selected===entry.id?'selected':''}>${esc(entry.name)}${entry.simulated?' · 模拟':''}</option>`).join('')}</select></label><div class="actions">${button('预览并带入','preview-template','',!ui.selected||state.templateLoading?'disabled':'')}${button('刷新','refresh-templates','link',state.templateLoading?'disabled':'')}</div></div>${ui.notice?`<div class="note template-notice" role="status">${esc(ui.notice)}</div>`:''}<details class="template-save form-details" data-section="template-save"><summary>将本单存为模板</summary><p class="small">仅保存已经保存并核对的工艺和计价参数；有未保存修改时，请先保存本批输入。</p><label class="field"><span>模板名称</span><input class="input" data-template-name value="${esc(ui.name)}" maxlength="80" placeholder="例如：碳钢支架 · 按件加工"></label><label class="check template-usage"><input type="checkbox" data-template-usage ${ui.includeUsage?'checked':''}>同时保存单件材料用量（仅适合同类零件）</label><div class="actions">${button('保存模板','save-template')}</div></details>${ui.selected?`<div class="template-disable">${button('停用所选模板','disable-template','link')}</div>`:''}`;
  }
  function templatePanel() {return `<details class="form-details template-panel" data-section="templates"><summary>常用模板 · 可选</summary><div data-template-panel>${templatePanelContent()}</div></details>`;}
  function patchTemplatePanel() {
    const panel=content.querySelector('[data-template-panel]');if(!panel)return;
    const saveOpen=panel.querySelector('[data-section="template-save"]')?.open;
    panel.innerHTML=templatePanelContent();
    if(saveOpen)panel.querySelector('[data-section="template-save"]').open=true;
  }
  async function loadTemplates() {
    if(state.templateLoading)return;
    const id=taskId(),token=state.routeToken;state.templateLoading=true;patchTemplatePanel();
    try {
      const result=await request('/templates');state.templates=result.templates||[];
      if(taskId()===id&&state.routeToken===token&&!state.templates.some(item=>item.enabled!==false&&item.id===templateUi().selected))templateUi().selected='';
    } finally {state.templateLoading=false;patchTemplatePanel();}
  }
  function technicalDraftPending() {return Object.keys(drafts()).some(name=>state.task.definitions?.[name]?.stage==='technical'||['_context','_priceSelections','_templateSelection','_feeResolution'].includes(name));}
  async function saveTemplate() {
    const ui=templateUi(),name=ui.name.trim();
    if(!name)throw new Error('请先填写模板名称。');
    if(technicalDraftPending())throw new Error('请先保存本批输入，再存模板；未保存或未核对的参数不会收入模板。');
    const result=await mutate('/templates',{name,source_task_id:taskId(),expected_revision:state.task.revision,include_material_usage:ui.includeUsage});
    const entry=result.template||result.entry||result;
    state.templates=[entry,...(state.templates||[]).filter(item=>item.id!==entry.id)];ui.selected=entry.id;ui.name='';ui.includeUsage=false;ui.notice=`模板“${entry.name}”已保存，本单参数保持原值。`;
    toast('模板已保存，下次可预览后带入。');
  }
  function closeTemplatePreview() {
    const preview=state.templatePreview;state.templatePreview=null;dialog.close();
    if(preview?.taskId===taskId())content.querySelector(`[data-action="${preview.disableId?'disable-template':'preview-template'}"]`)?.focus({preventScroll:true});
  }
  function templateDisplay(name,raw) {
    const shown=displayValue(raw);
    if(raw===null||raw===undefined||raw==='')return shown;
    return ['material_rate','setup_fee','material_usage_per_piece'].includes(name)?`${shown} ${unitNames[state.task.definitions?.[name]?.unit]||''}`.trim():shown;
  }
  async function previewTemplate() {
    const id=taskId(),revision=state.task.revision,token=state.routeToken,selected=templateUi().selected;
    if(!selected)throw new Error('请先选择一个模板。');
    state.templateLoading=true;patchTemplatePanel();
    try {
      const preview=await request(`/tasks/${encodeURIComponent(id)}/template-preview`,{method:'POST',body:JSON.stringify({template_id:selected,expected_revision:revision})});
      if(taskId()!==id||state.task.revision!==revision||state.routeToken!==token)return;
      const values=Object.fromEntries(Object.entries(preview.values||{}).filter(([name])=>templateFields.includes(name)));
      const context=Object.fromEntries(Object.entries(preview.pricing_context||{}).filter(([name])=>['product_family','material_form'].includes(name)));
      const route=values.process_route,rates=values.operation_rates;
      if((route!==undefined||rates!==undefined)&&(!Array.isArray(route)||!route.length||!Array.isArray(rates)||rates.length!==route.length||new Set(route).size!==route.length||new Set(rates.map(rate=>rate.operation)).size!==rates.length||!route.every(operation=>rates.some(rate=>rate.operation===operation&&rate.value!==null&&rate.value!==undefined&&rate.value!==''&&rate.unit)))) {
        preview.can_apply=false;preview.warnings=[...(preview.warnings||[]),'模板工序和费率不完整，请先补齐并核对后另存模板；本单工序价格不会被自动沿用。'];
      }
      state.templatePreview={...preview,values,pricing_context:context,taskId:id,revision,token,draftSnapshot:clone(drafts())};
      const rows=[...Object.entries(values).map(([name,raw])=>({name,current:value(name),raw})),...Object.entries(context).map(([name,raw])=>({name,current:contextValue(name),raw}))];
      const applicability=preview.applicability||{};
      document.querySelector('#dialog-content').innerHTML=`<h2>预览模板：${esc(preview.template?.name||'常用模板')}</h2><p class="small">以下字段将替换为模板参数，当前未保存输入也列在左侧。未列出的参数保持原值；带入后仍需核对并保存。</p>${preview.simulated?'<div class="alert">此模板包含模拟参数，仅用于演示。</div>':''}<div class="table-wrap template-preview-table"><table><thead><tr><th>参数</th><th>当前本单 / 草稿</th><th>模板参数</th></tr></thead><tbody>${rows.map(({name,current,raw})=>`<tr><td>${esc(templateLabels[name]||name)}</td><td class="conflict-value">${esc(templateDisplay(name,current))}</td><td class="conflict-value">${esc(templateDisplay(name,raw))}</td></tr>`).join('')}</tbody></table></div>${Object.keys(applicability).length?`<div class="template-applicability"><strong>适用范围</strong><p>${esc(Object.entries(applicability).map(([name,raw])=>`${({material_grade:'材料牌号',material_family:'材料类别',material_form:'材料形态',surface_treatment:'表面处理',drawing_units:'图纸单位',thickness:'板厚',thickness_min:'最小板厚',thickness_max:'最大板厚',qty_min:'最少数量',qty_max:'最多数量',product_family:'产品族',note:'说明',currency:'币种',tax_included:'含税',freight_included:'含运费'}[name]||name)}：${displayValue(raw)}`).join('；'))}</p></div>`:''}${(preview.warnings||[]).map(warning=>`<div class="alert">${esc(warning)}</div>`).join('')}<div class="footer"><span class="small">不会自动保存或生成报价</span><div class="actions">${button('取消','cancel-template-preview')}${button('带入本单草稿','apply-template','primary',preview.can_apply===false||!rows.length?'disabled':'')}</div></div>`;
      dialog.showModal();
    } finally {state.templateLoading=false;patchTemplatePanel();}
  }
  function applyTemplate() {
    const preview=state.templatePreview;
    if(!preview||preview.can_apply===false)return;
    if(preview.taskId!==taskId()||preview.revision!==state.task.revision||preview.token!==state.routeToken||!equal(preview.draftSnapshot,drafts())){dialog.close();state.templatePreview=null;throw new Error('本单资料或输入已更新，请重新预览模板后再带入。');}
    const draft=drafts(),refs=clone(draft._priceSelections||{});
    for(const [name,raw] of Object.entries(preview.values)) {
      if(raw===null||raw===undefined||raw===''||Array.isArray(raw)&&!raw.length)continue;
      draft[name]=clone(raw);
      if(name==='operation_rates'||name==='process_route')delete refs.operations;else delete refs[name];
    }
    const selected=clone(preview.price_selections||{});
    draft._priceSelections={...refs,...selected};
    if(!Object.keys(draft._priceSelections).length)delete draft._priceSelections;
    if(Object.keys(preview.pricing_context).length)draft._context={...(draft._context||{}),...clone(preview.pricing_context)};
    draft._templateSelection={id:preview.template.id,version:preview.template.version};delete draft._feeResolution;
    const ui=templateUi();ui.notice=`已将“${preview.template.name}”带入草稿，核对后保存。`;
    if(inclusionValue().some(item=>!inclusionChoices.includes(item)))state.detailsOpen.set(`${taskId()}:fee-notes`,true);
    dialog.close();state.templatePreview=null;render();
    const notes=content.querySelector('[data-fee-notes]');if(notes&&inclusionValue().some(item=>!inclusionChoices.includes(item)))notes.open=true;
    toast('已带入草稿，核对后保存。');
  }
  function confirmDisableTemplate() {
    const entry=(state.templates||[]).find(item=>item.id===templateUi().selected&&item.enabled!==false);if(!entry)return;
    state.templatePreview={disableId:entry.id,taskId:taskId()};
    document.querySelector('#dialog-content').innerHTML=`<h2>停用模板“${esc(entry.name)}”？</h2><p>停用后不再出现在可选模板中，已保存的任务和报价保留。</p><div class="footer">${button('取消','cancel-template-preview')}${button('确认停用','confirm-disable-template','danger')}</div>`;dialog.showModal();
  }
  async function disableTemplate() {
    const selection=state.templatePreview;if(!selection?.disableId||selection.taskId!==taskId())return;
    await mutate(`/templates/${encodeURIComponent(selection.disableId)}/disable`,{});
    state.templates=(state.templates||[]).map(item=>item.id===selection.disableId?{...item,enabled:false}:item);templateUi().selected='';templateUi().notice='所选模板已停用，已保存任务和报价保留。';state.templatePreview=null;dialog.close();toast('模板已停用。');
  }
  function technicalForm() {
    const definitions=state.task.definitions||{};
    const route=routeValue();
    const choices=[...new Set([...commonOperations,...route])];
    const named=(name,label)=>definitions[name]?fieldInput(name,{...definitions[name],label}):'';
    const advanced=Object.entries(definitions).filter(([name,definition])=>definition.stage==='technical'&&!['material_usage_per_piece','material_rate','process_route','operation_rates','setup_fee','fee_inclusions'].includes(name));
    return `${templatePanel()}<div class="note">先确认材料费用和加工费用，再保存。未知可留空，不会按零计价。</div><form data-stage="technical">
      <section class="technical-section" data-section="material"><h3>材料费用</h3><div class="field-grid">${named('material_usage_per_piece','每件材料用量')}${named('material_rate','材料单价')}</div></section>
      <section class="technical-section" data-section="fees" id="technical-fees" tabindex="-1"><h3>加工费用</h3><div class="field" data-field-container="process_route"><span class="field-label">采用工序 *</span><div class="checks">${choices.map(operation=>`<label class="check"><input type="checkbox" data-operation="${esc(operation)}" ${route.includes(operation)?'checked':''}>${esc(operation)}</label>`).join('')}</div><div class="actions"><label class="visually-hidden" for="custom-operation">补充工序名称</label><input class="input" id="custom-operation" placeholder="补充其他工序名称" style="flex:1;min-width:120px">${button('添加工序','add-operation')}</div>${fieldMeta('process_route')}</div>
      <div class="section-gap" data-field-container="operation_rates"><span class="field-label">各工序价格 *</span>${route.length?`<div class="rate-row small"><span>工序</span><span>金额</span><span>计费方式</span></div>${route.map(operation=>{const rate=ratesValue().find(item=>item.operation===operation)||{};return `<div class="rate-row" data-operation-row="${esc(operation)}"><span>${esc(operation)}</span><input class="input" type="number" inputmode="decimal" min="0" step="any" data-rate="${esc(operation)}" aria-label="${esc(operation)}费率" placeholder="待补" value="${esc(rate.value??'')}"><select class="input" data-rate-unit="${esc(operation)}" aria-label="${esc(operation)}计费单位"><option value="">待确认</option>${[['CNY/piece','元/件'],['CNY/order','元/单']].map(([unit,label])=>`<option value="${unit}" ${rate.unit===unit?'selected':''}>${label}</option>`).join('')}${rate.unit&&!['CNY/piece','CNY/order'].includes(rate.unit)?`<option selected value="${esc(rate.unit)}">${esc(rate.unit)}（当前不支持）</option>`:''}</select></div>`;}).join('')}`:'<div class="small">选择工序后填写价格。</div>'}${fieldMeta('operation_rates')}<p class="small">元/件按数量计入；元/单整单计入一次。其他计费单位需人工核对。</p></div>
      <div class="setup-fee-input">${named('setup_fee','另收准备费 / 开机费（整单）')}<p class="small">确实不另收时填 0；不知道时留空。</p></div><div data-fee-conflict aria-live="polite">${feeConflictContent()}</div>${inclusionForm()}</section>
      ${advanced.length?`<details class="form-details technical-advanced" data-section="advanced"><summary>其他技术信息 · 按需填写${advanced.some(([name])=>['pending_review','conflict'].includes(state.task.fields?.[name]?.status))?' · 有待核对项':''}</summary><div class="field-grid section-gap">${advanced.map(([name,definition])=>fieldInput(name,definition)).join('')}</div></details>`:''}
      <details class="form-details price-advanced" data-section="prices" ${state.priceMatches?'open':''}><summary>从价格库选价 · 可选</summary><p class="small">已有价格可直接在上方填写；需要查询时再补匹配条件。</p><div class="field-grid"><label class="field"><span>产品族 / 工艺模板</span><input class="input" data-context="product_family" value="${esc(contextValue('product_family'))}" placeholder="按实际产品或模板填写"></label><label class="field"><span>材料形态</span><input class="input" data-context="material_form" value="${esc(contextValue('material_form'))}" placeholder="例如板材；未知可留空"></label></div><div class="actions section-gap">${button('查询适用价格','match-prices','link')}</div>${priceMatchesView()}</details>
      <div class="footer"><span class="small" data-save-state>${dirty()?'有未保存输入':'显示的是已保存资料'}</span><div class="actions">${button('保存本批输入','save-technical')}${button('保存并查看报价','save-technical-next','primary')}</div></div></form>`;
  }
  function contextValue(key) {return drafts()._context?.[key]??state.task.pricing_context?.[key]??'';}
  function issueTarget(issue) {
    if(!issue||typeof issue!=='object')return null;
    const field=issue.target?.field||issue.field;
    const definition=state.task.definitions?.[field];
    if(!issue.target&&!definition)return null;
    const stage=issue.target?.stage||(definition?.stage==='technical'?'technical':'requirements');
    return {stage:stage==='technical'?'technical':'requirements',section:issue.target?.section||(['setup_fee','fee_inclusions','operation_rates','process_route'].includes(field)?'fees':['material_rate','material_usage_per_piece'].includes(field)?'material':'advanced'),field};
  }
  function issueItems() {
    const validation=state.task.validation||{},overlap=feeOverlap();
    let feeFound=false;
    const items=[...(validation.issues||[]),...(validation.open_questions||[])].map(item=>{
      const message=typeof item==='string'?item:item.question||item.text||item.message||item.reason||item.label;
      if(!message)return null;
      const fee=typeof item==='object'&&item.code==='fee_overlap'||/按单工序费.*准备费/.test(message);
      if(fee){feeFound=true;return {message:overlap?`准备费 ${money(overlap.setup_fee)} 与 ${overlap.rates.map(rate=>`${rate.operation} ${money(rate.value)}/单`).join('、')} 同时按整单收费。`:value('setup_fee')===''?'准备费尚未填写；空白会保留已保存费用，请明确核对后填写。':'本页费用已调整，保存后重新校验。',target:{stage:'technical',section:'fees',field:'setup_fee'}};}
      return {message,target:issueTarget(item)};
    }).filter(Boolean);
    if(overlap&&!feeFound)items.unshift({message:`准备费 ${money(overlap.setup_fee)} 与 ${overlap.rates.map(rate=>`${rate.operation} ${money(rate.value)}/单`).join('、')} 同时按整单收费。`,target:{stage:'technical',section:'fees',field:'setup_fee'}});
    return items;
  }
  function issuesView() {
    const issues=issueItems();
    return `<div data-issues>${issues.length?`<details class="alert issue-list" open><summary>仍需补充或核对 ${issues.length} 项</summary><ul>${issues.map(issue=>`<li><span>${esc(issue.message)}</span>${issue.target?button('去处理','go-to-issue','link',`data-issue-stage="${esc(issue.target.stage)}" data-issue-section="${esc(issue.target.section)}" data-issue-field="${esc(issue.target.field||'')}"`):''}</li>`).join('')}</ul></details>`:state.task.validation?.ready?'<div class="note">已保存资料满足当前核算校验要求。请在报价页选择本单定价方式。</div>':''}</div>`;
  }
  function patchFees() {
    const panel=content.querySelector('[data-fee-conflict]');
    if(panel)panel.innerHTML=feeConflictContent();
    const issues=content.querySelector('[data-issues]');
    if(issues){const open=issues.querySelector('details')?.open;issues.outerHTML=issuesView();if(open===false)content.querySelector('[data-issues] details')?.removeAttribute('open');}
  }
  function focusField(field,section='fees',operation=null) {
    const candidates=Array.from(content.querySelectorAll('[data-field],[data-rate-unit],[data-field-container],[data-section]'));
    let target=operation?candidates.find(element=>element.dataset.rateUnit===operation):candidates.find(element=>element.dataset.field===field);
    if(!target&&field==='operation_rates')target=content.querySelector('[data-rate-unit]');
    if(!target)target=candidates.find(element=>element.dataset.fieldContainer===field)||candidates.find(element=>element.dataset.section===section);
    if(!target)return;
    let ancestor=target.parentElement;
    while(ancestor){if(ancestor.tagName==='DETAILS')ancestor.open=true;ancestor=ancestor.parentElement;}
    const highlight=target.closest('.field,.rate-row,.fee-inclusions')||target;
    content.querySelectorAll('.is-targeted').forEach(element=>element.classList.remove('is-targeted'));
    highlight.classList.add('is-targeted');highlight.scrollIntoView({block:'center',behavior:'smooth'});
    const control=target.matches('input,select,textarea')?target:target.querySelector('input,select,textarea')||target;
    if(!control.hasAttribute('tabindex')&&!control.matches('input,select,textarea'))control.setAttribute('tabindex','-1');
    control.focus({preventScroll:true});setTimeout(()=>highlight.classList.remove('is-targeted'),6000);
  }
  function goToIssue(control) {
    state.tab=control.dataset.issueStage==='technical'?'technical':'requirements';
    state.page='confirm';history.replaceState(null,'',`#task/${encodeURIComponent(taskId())}/confirm`);render();
    focusField(control.dataset.issueField,control.dataset.issueSection);
  }
  function formFocusKey() {return `${taskId()}:${state.page}:${state.tab}`;}
  function rememberFormFocus(element=document.activeElement) {
    const key=element?.dataset?.field?['field',element.dataset.field]:element?.dataset?.rate?['rate',element.dataset.rate]:element?.dataset?.rateUnit?['rateUnit',element.dataset.rateUnit]:element?.dataset?.context?['context',element.dataset.context]:null;
    if(key&&state.page==='confirm')state.formFocus.set(formFocusKey(),{key,selection:typeof element.selectionStart==='number'?[element.selectionStart,element.selectionEnd]:null});
  }
  function confirmPage() {
    return title(fieldValue('product_name')||'确认询价参数',state.task.simulated?'模拟资料 · 需求与技术分批确认':'结合资料核对，每批信息一次提交。',`<span class="badge ${state.task.validation?.ready?'success':'warning'}">${esc(businessStatus())}</span>`)+steps('confirm')+runView()+issuesView()+`<div class="split">${drawingView()}<section class="panel"><div class="tabs" aria-label="参数批次">${button('需求参数','requirements',state.tab==='requirements'?'active':'')}${button('工艺与计价','technical',state.tab==='technical'?'active':'')}</div>${state.tab==='requirements'?requirementsForm():technicalForm()}</section></div>`;
  }
  function quotePage() {
    const quotes=state.task.quotes||[];
    const quote=state.quote;
    const amounts=quote?.amounts||{};
    const links=quote?.links||{};
    const embedUrl=safeUrl(state.quoteView==='cost'?links.cost_embed_url:links.quote_embed_url);
    const p=policy();
    return title('报价草稿','报价由已核对参数计算，保留每次输入、价格与策略快照。',`<span class="badge warning">模拟报价 · 待审核</span>`)+steps('quote')+runView()+issuesView()+
      `<section class="panel"><h2>本单定价口径</h2>${dirty()?'<div class="alert">参数页有未保存输入，请先保存并核对，避免使用旧资料生成报价。</div>':''}<div class="field-grid"><label class="field"><span>定价方式</span><select class="input" data-policy="pricing_method"><option value="">请选择本单方式</option><option value="cost_plus" ${p.pricing_method==='cost_plus'?'selected':''}>成本加成率</option><option value="gross_margin" ${p.pricing_method==='gross_margin'?'selected':''}>目标毛利率</option></select></label><label class="field"><span>${p.pricing_method==='gross_margin'?'目标毛利率':'加成比例'}</span><div class="input-wrap"><input class="input" type="number" min="0" step="any" inputmode="decimal" data-policy="rate_percent" value="${esc(p.rate_percent)}" placeholder="本单明确填写"><span class="unit">%</span></div></label></div><label class="check section-gap"><input type="checkbox" data-policy="conditions" ${p.conditions?'checked':''}>确认本次采用人民币，未计税费、不含运费</label><div class="footer"><span class="small">定价比例不沿用其他询价；草稿需商务审核后对外使用。</span>${button('核算并生成新草稿','generate','primary',!state.task.validation?.ready||dirty()?'disabled':'')}</div></section>`+
      (quotes.length?`<section class="panel"><div class="panel-head"><h2>报价版本</h2><select class="input" data-quote-select style="width:auto;max-width:100%" aria-label="选择报价版本">${quotes.map(item=>`<option value="${esc(item.id||item.quote_id)}" ${(quote?.quote_id||quote?.id)===(item.id||item.quote_id)?'selected':''}>${esc(item.id||item.quote_id)}${item.source_current?' · 当前资料':' · 历史版本'}</option>`).join('')}</select></div>${!quote?'<div class="small">正在加载报价…</div>':`<div class="small">${esc(quote.quote_id||quote.id)} · ${quote.status==='draft'?'待审核草稿':esc(quote.status||'待审核')}</div>`}</section>`:'')+
      (quote?`${quote.source_current===false?'<div class="alert">当前资料已修改。本稿保留当时的参数和金额，需重新核算才能反映当前资料。</div>':''}<div class="quote-totals"><div class="panel"><div class="small">报价单价 / 件</div><div class="amount">${money(amounts.unit_price)}</div></div><div class="panel"><div class="small">报价总额 · ${esc(quote.quantity??'—')} 件</div><div class="amount">${money(amounts.total_price)}</div></div><div class="panel"><div class="small">单件成本 · 内部可见</div><div class="amount">${money(amounts.unit_cost)}</div></div></div><section class="panel"><div class="panel-head"><div class="actions">${button('报价单','quote-view',state.quoteView==='quote'?'primary':'')}${button('内部成本明细','cost-view',state.quoteView==='cost'?'primary':'')}</div><div class="actions">${fileLink(links.quote_url,'打开报价单')}${fileLink(links.quote_download,'下载报价单','button',true)}</div></div><div class="actions small">${fileLink(links.cost_url,'打开成本明细','button link')}${fileLink(links.cost_download,'下载成本明细','button link',true)}${fileLink(links.record_url,'查看询价记录','button link')}</div><p class="small">交期、付款条件和报价有效期待约定。成本明细仅供内部使用。</p>${embedUrl?`<iframe class="quote-frame" title="${state.quoteView==='cost'?'内部成本明细':'模拟报价草稿'}" src="${esc(embedUrl)}"></iframe>`:'<p class="small">请点击上方链接打开或下载文件。</p>'}</section>`:!quotes.length?'<section class="panel empty"><h2>还没有报价草稿</h2><p>补齐资料并确认计价信息后，在上方填写本单定价口径。</p></section>':'');
  }
  function priceMatchesView() {
    if (!state.priceMatches) return '';
    const names={unique:'找到适用价格，待核对',ambiguous:'存在多条候选，请核对选择',missing_conditions:'缺少匹配条件',not_found:'未找到适用价格',expired:'匹配价格已过期',unsupported:'当前计费方式不支持'};
    return state.priceMatches.results.map((result,index)=>`<div class="price-match"><h3>${esc(result.operation||({material:'材料价格',setup:'准备费',operation:'工序价格'}[result.kind]||'价格'))}</h3><div class="small">${names[result.status]||'需人工核对'}${result.missing?.length?' · '+esc(result.missing.join('、')):''}</div>${(result.candidates||[]).map((entry,candidate)=>`<label class="check"><input type="radio" name="match-${index}" data-price-select="${index}:${candidate}" ${state.selectedPrices[index]===candidate?'checked':''} ${!['unique','ambiguous'].includes(result.status)?'disabled':''}><span>${esc(entry.name||entry.operation||entry.entry_id||entry.id)} · ${esc(entry.rate??'待补')} ${esc(unitNames[entry.unit]||entry.unit||'')}<small class="small"> · ${esc(entry.source||'来源待核对')} · v${esc(entry.version)}${entry.simulated?' · 模拟':''}</small><small class="small"> · ${esc(entry.valid_from)} 至 ${esc(entry.valid_to)} · ${esc((entry.inclusions||[]).join('；'))}</small></span></label>`).join('')}</div>`).join('')+button('将所选价格带入表单待核对','apply-price-selection','link');
  }
  function pricesPage() {
    return title('价格管理','按材料、工序与准备费维护适用条件和价格版本。',button('新建价格草稿','new-price','primary'))+`<section class="panel"><div class="alert">价格必须明确来源、适用范围和计费单位。发布后可参与匹配，修改价格保留历史版本。</div><div class="table-wrap"><table><thead><tr><th>价格项目</th><th>适用条件</th><th>金额 / 单位</th><th>状态</th><th>操作</th></tr></thead><tbody>${state.prices.map((entry,index)=>`<tr><td><strong>${esc(entry.name||entry.operation||entry.entry_id||entry.id)}</strong><div class="small">${esc(({material:'材料',operation:'工序',setup:'准备费'}[entry.kind]||entry.kind))} · v${esc(entry.version)}${entry.simulated?' · 模拟':''}</div></td><td>${esc([entry.product_family,entry.material_grade,entry.material_form].filter(Boolean).join(' · ')||'见价格详情')}<div class="small">${esc(entry.thickness_min)}–${esc(entry.thickness_max)} mm · ${esc(entry.valid_from)} 至 ${esc(entry.valid_to)}</div><div class="small">${esc(entry.source||'来源待补')}</div></td><td>${esc(entry.rate??'待补')}<div class="small">${esc(unitNames[entry.unit]||entry.unit||'')}</div></td><td><span class="badge ${entry.status==='published'?'success':'warning'}">${esc(({draft:'草稿',published:'已发布',disabled:'已停用'}[entry.status]||entry.status||'待核对'))}</span></td><td>${button('查看 / 编辑','edit-price','link',`data-index="${index}"`)}</td></tr>`).join('')||'<tr><td colspan="5"><div class="empty">暂无价格。可先录入有来源的模拟价格草稿。</div></td></tr>'}</tbody></table></div></section>${state.priceEditor?priceEditorView():''}`;
  }
  function priceEditorView() {
    const entry=state.priceEditor;
    const field=(key,label,type='text',unit='')=>`<label class="field"><span>${label}</span><div class="input-wrap"><input class="input" data-price-field="${key}" type="${type}" ${type==='number'?'min="0" step="any"':''} value="${esc(entry[key]??'')}" placeholder="请填写">${unit?`<span class="unit">${unit}</span>`:''}</div></label>`;
    const units=entry.kind==='material'?[['CNY/kg','元/kg']]:entry.kind==='setup'?[['CNY/order','元/单']]:[['CNY/piece','元/件'],['CNY/order','元/单']];
    return `<section class="panel" id="price-editor"><div class="panel-head"><h2>${entry.entry_id?'价格详情 · v'+esc(entry.version):'新建价格草稿'}</h2>${button('关闭','close-price','link')}</div>${entry.entry_id?'<div class="note">价格内容修改后保存为新草稿版本，已生成报价保留原快照。发布与停用操作作用于当前所示版本。</div>':''}<div class="field-grid"><label class="field"><span>价格类别</span><select class="input" data-price-field="kind"><option value="material" ${entry.kind==='material'?'selected':''}>材料</option><option value="operation" ${entry.kind==='operation'?'selected':''}>工序</option><option value="setup" ${entry.kind==='setup'?'selected':''}>准备费</option></select></label>${field('name','价格名称')}${field('rate','原始费率','number')}<label class="field"><span>计费单位</span><select class="input" data-price-field="unit"><option value="">请选择</option>${units.map(([unit,label])=>`<option value="${unit}" ${entry.unit===unit?'selected':''}>${label}</option>`).join('')}</select></label>${field('material_grade','材料牌号')}${field('material_form','材料形态')}${field('thickness_min','最小板厚','number','mm')}${field('thickness_max','最大板厚','number','mm')}${entry.kind!=='material'?field('product_family','产品族 / 工艺模板'):''}${entry.kind==='operation'?field('operation','精确工序名称'):''}${field('qty_min','最小数量','number','件')}${field('qty_max','最大数量（无上限可留空）','number','件')}${field('valid_from','有效期起','date')}${field('valid_to','有效期止','date')}<label class="field full"><span>价格来源</span><textarea class="input" data-price-field="source" placeholder="填写实际供应商报价、工厂费率表或模拟资料的来源">${esc(entry.source||'')}</textarea></label><label class="field full"><span>包含项与计费口径</span><textarea class="input" data-price-field="inclusions" placeholder="每行一项；明确已包含的人工、设备、耗材或准备等内容">${esc(Array.isArray(entry.inclusions)?entry.inclusions.join('\n'):entry.inclusions||'')}</textarea></label></div><label class="check section-gap"><input type="checkbox" data-price-field="simulated" ${entry.simulated?'checked':''}>该价格来自模拟资料</label><p class="small">人民币；价格未计税费、不含运费。发布草稿前请核对来源与适用条件。</p><div class="footer"><span class="small">${entry.entry_id?esc(entry.entry_id)+' · '+esc(({draft:'草稿',published:'已发布',disabled:'已停用'}[entry.status]||entry.status)):'尚未保存'}</span><div class="actions">${entry.entry_id&&entry.status==='draft'?button('发布本版本','publish-price'):''}${entry.entry_id&&entry.status==='published'?button('停用本版本','disable-price','danger'):''}${button(entry.entry_id?'另存为新草稿版本':'保存价格草稿','save-price','primary')}</div></div></section>`;
  }
  function render() {
    content.setAttribute('aria-busy',String(state.busy||state.loading));
    const focus=document.activeElement;
    const key=focus?.dataset?.field?['field',focus.dataset.field]:focus?.dataset?.rate?['rate',focus.dataset.rate]:focus?.dataset?.rateUnit?['rateUnit',focus.dataset.rateUnit]:focus?.dataset?.context?['context',focus.dataset.context]:focus?.dataset?.policy?['policy',focus.dataset.policy]:focus?.id?['id',focus.id]:null;
    const selection=typeof focus?.selectionStart==='number'?[focus.selectionStart,focus.selectionEnd]:null;
    content.querySelectorAll('details[data-section],details[data-fee-notes]').forEach(element=>state.detailsOpen.set(`${taskId()}:${element.dataset.section||'fee-notes'}`,element.open));
    const html=errorView()+(state.loading?'<div class="loading">正在加载资料…</div>':state.page==='tasks'?taskList():state.page==='prices'?pricesPage():state.page==='upload'?uploadPage():state.task?state.page==='quote'?quotePage():confirmPage():'<div class="empty">任务尚未加载，请返回询价列表。</div>');
    const existing=content.querySelector('[data-preview-key]');
    const template=document.createElement('template');template.innerHTML=html;
    const incoming=template.content.querySelector('[data-preview-key]');
    if(existing&&incoming&&existing.dataset.previewKey===incoming.dataset.previewKey){
      // Keep the CAD iframe connected: detaching/reinserting it reloads WASM and
      // discards the user's view. Rebuild the adjacent form and page notices.
      const split=existing.parentElement,nextSplit=incoming.parentElement;
      Array.from(content.children).filter(node=>node!==split).forEach(node=>node.remove());
      Array.from(template.content.children).forEach(node=>{if(node!==nextSplit)content.insertBefore(node,split);});
      Array.from(split.children).filter(node=>node!==existing).forEach(node=>node.remove());
      Array.from(nextSplit.children).filter(node=>node!==incoming).forEach(node=>split.append(node));
    }else content.replaceChildren(template.content);
    content.querySelectorAll('details[data-section],details[data-fee-notes]').forEach(element=>{const saved=state.detailsOpen.get(`${taskId()}:${element.dataset.section||'fee-notes'}`);if(saved!==undefined)element.open=saved;});
    document.querySelector('#breadcrumb').textContent=state.page==='prices'?'价格管理':state.page==='tasks'?'询价任务':state.task?`询价任务 / ${fieldValue('product_name')||state.task.id}`:'新建询价';
    document.querySelectorAll('[data-nav]').forEach(link=>link.classList.toggle('active',link.dataset.nav===(state.page==='prices'?'prices':'tasks')));
    const savedFocus=state.page==='confirm'?state.formFocus.get(formFocusKey()):null;
    for(const bookmark of [{key,selection},savedFocus].filter(item=>item?.key)) {
      const [kind,name]=bookmark.key;
      const element=kind==='id'?document.getElementById(name):Array.from(content.querySelectorAll('input,select,textarea')).find(item=>item.dataset[kind]===name);
      if (element&&element.getClientRects().length) {element.focus({preventScroll:true}); if(bookmark.selection&&typeof element.setSelectionRange==='function')try{element.setSelectionRange(...bookmark.selection);}catch(_){/* Numeric controls have no selection range. */}break;}
    }
    updateClocks();
  }
  function patchRuntimeUI() {
    if(!['upload','confirm','quote'].includes(state.page)||!state.task?.run)return;
    let panel=content.querySelector('[data-run-panel]');
    if(!panel){
      panel=document.createElement('section');panel.className='run-panel';panel.dataset.runPanel='';panel.setAttribute('aria-label','后台读图任务');
      const steps=content.querySelector('.steps');
      if(steps)steps.insertAdjacentElement('afterend',panel);else return;
    }
    const historyOpen=panel.querySelector('.run-history')?.open;
    const focused=panel.contains(document.activeElement)?document.activeElement?.dataset?.action:null;
    panel.innerHTML=runPanelContent(state.task.run);
    if(historyOpen&&panel.querySelector('.run-history'))panel.querySelector('.run-history').open=true;
    if(focused)panel.querySelector(`[data-action="${focused}"]`)?.focus({preventScroll:true});
    updateClocks();
  }
  function patchTaskList() {
    if(state.page!=='tasks')return;
    const rows=content.querySelector('[data-task-rows]'),sync=content.querySelector('[data-list-sync]');
    if(rows)rows.innerHTML=taskRows();
    if(sync)sync.innerHTML=listSyncView();
    updateClocks();
  }
  async function loadTasks() {
    const result=await request('/tasks');state.tasks=result.tasks||[];state.listError=null;
    state.tasks.forEach(observeRun);state.listSync={at:Date.now(),error:null,refreshing:false};
    schedulePoll();
  }
  async function loadTask(id,guard=null) {const result=await request('/tasks/'+encodeURIComponent(id));if(guard===null||guard===state.routeToken)takeTask(result);}
  async function loadQuote(id,expectedTask=taskId()) {
    if (!id) {state.quote=null;return;}
    const result=await request('/tasks/'+encodeURIComponent(expectedTask)+'/quotes/'+encodeURIComponent(id));
    if(taskId()===expectedTask)state.quote=result;
  }
  async function route() {
    if(state.busy){state.pendingRoute=true;return;}
    if(state.templatePreview){dialog.close();state.templatePreview=null;}
    const token=++state.routeToken;
    clearTimeout(state.timer); state.error=null; state.conflict=null; state.loading=true; render();
    const parts=location.hash.replace(/^#/,'').split('/');
    try {
      if (parts[0]==='prices') {state.page='prices';await loadPrices();}
      else if(parts[0]==='new') {state.page='upload';state.task=null;state.quote=null;state.priceMatches=null;}
      else if(parts[0]==='task'&&parts[1]&&parts[1]!=='new') {
        const id=decodeURIComponent(parts[1]);
        if(taskId()!==id){state.tab='requirements';state.attachment=0;state.zoom=100;state.priceMatches=null;state.selectedPrices={};state.quote=null;state.selectedFiles=[];}
        state.page=['upload','confirm','quote'].includes(parts[2])?parts[2]:'confirm';
        await loadTask(id,token);
        if(token!==state.routeToken)return;
        if(state.page==='quote')await loadQuote(state.task.quotes?.[0]?.id||state.task.quotes?.[0]?.quote_id);
      } else {state.page='tasks';await loadTasks();}
    } catch(error) {state.error=error;}
    finally {if(token===state.routeToken){state.loading=false;render();schedulePoll();}}
  }
  function schedulePoll() {
    clearTimeout(state.timer);
    const list=state.page==='tasks';
    if(list?!state.tasks.some(task=>activeRuns.has(task.run?.status))&&!state.listSync.error:!['upload','confirm','quote'].includes(state.page)||!activeRuns.has(state.task?.run?.status))return;
    const id=taskId(),token=state.routeToken;
    state.timer=setTimeout(async()=>{
      if(token!==state.routeToken||(!list&&taskId()!==id))return;
      if(state.busy){schedulePoll();return;}
      if(list){state.listSync.refreshing=true;const sync=content.querySelector('[data-list-sync]');if(sync)sync.innerHTML=listSyncView();}
      else {state.sync.set(id,{...(state.sync.get(id)||{}),refreshing:true});patchRuntimeUI();}
      try {
        const result=await request(list?'/tasks':'/tasks/'+encodeURIComponent(id));
        if(token!==state.routeToken||state.busy||(!list&&taskId()!==id))return;
        if(list){
          state.tasks=result.tasks||[];state.tasks.forEach(observeRun);
          state.listSync={at:Date.now(),error:null,refreshing:false};patchTaskList();
        } else {
          if(result.revision>=state.task.revision){
            if(result.revision>state.task.revision)state.pendingRecords.set(id,result);
            state.task.run=clone(result.run);observeRun(result);markSynced(id);patchRuntimeUI();patchFieldMetadata();
          }
        }
      } catch(error){
        if(token!==state.routeToken)return;
        if(list){state.listSync={...state.listSync,error:error.message,refreshing:false};const sync=content.querySelector('[data-list-sync]');if(sync)sync.innerHTML=listSyncView();}
        else {state.sync.set(id,{...(state.sync.get(id)||{}),error:error.message,refreshing:false});patchRuntimeUI();}
      } finally {if(token===state.routeToken)schedulePoll();}
    },2500);
  }
  function parseField(name,raw) {
    const definition=state.task.definitions[name];
    if(definition.kind==='quantity') {
      if(!/^\d+$/.test(String(raw))||!Number.isSafeInteger(Number(raw))||Number(raw)<1)throw new Error(`${definition.label}请填写大于零的整数。`);
      return Number(raw);
    }
    if(definition.kind==='list')return Array.isArray(raw)?raw:String(raw).split(/\r?\n/).map(line=>line.trim()).filter(Boolean);
    return typeof raw==='string'?raw.trim():raw;
  }
  function fieldPresented(name) {
    const visible=element=>{
      if(!element?.getClientRects().length)return false;
      let ancestor=element.parentElement;
      while(ancestor){if(ancestor.tagName==='DETAILS'&&!ancestor.open)return false;ancestor=ancestor.parentElement;}
      return true;
    };
    if(name==='fee_inclusions') {
      const notes=content.querySelector('[data-field="fee_inclusions"]');
      return visible(notes)||inclusionValue().every(item=>inclusionChoices.includes(item));
    }
    const element=Array.from(content.querySelectorAll('[data-field-container]')).find(item=>item.dataset.fieldContainer===name);
    return visible(element);
  }
  async function saveStage(stage,next=false) {
    if(state.conflict)throw new Error('请先刷新任务状态并核对最新资料与未保存输入，再提交。');
    const targetStage=stage==='requirements'?'customer':'technical';
    const values={};
    const incompleteRates=stage==='technical'?ratesValue().filter(rate=>routeValue().includes(rate.operation)&&((rate.value!==''&&rate.value!==null&&rate.value!==undefined&&!rate.unit)||((rate.value===''||rate.value===null||rate.value===undefined)&&rate.unit))):[];
    for(const [name,definition] of Object.entries(state.task.definitions||{})) {
      if(definition.stage!==targetStage)continue;
      const edited=Object.hasOwn(drafts(),name)||name==='operation_rates'&&Object.hasOwn(drafts(),'process_route');
      const openQuestion=(state.task.validation?.open_questions||[]).some(item=>item&&typeof item==='object'&&item.field===name);
      if(!edited&&(!fieldPresented(name)||state.task.fields?.[name]?.status==='verified'&&!openQuestion))continue;
      let raw=value(name);
      if(definition.kind==='rates')raw=(raw||[]).filter(rate=>routeValue().includes(rate.operation)&&rate.value!==''&&rate.value!==null&&rate.value!==undefined&&rate.unit);
      if(raw===null||raw===undefined||raw===''||Array.isArray(raw)&&!raw.length)continue;
      values[name]=parseField(name,raw);
    }
    const pricing_context={product_family:contextValue('product_family'),material_form:contextValue('material_form')};
    const snapshot=clone(drafts());
    const body={expected_revision:state.task.revision,values};
    if(stage==='technical')body.pricing_context=pricing_context;
    if(stage==='technical'&&drafts()._priceSelections)body.price_selections=drafts()._priceSelections;
    if(stage==='technical'&&drafts()._templateSelection)body.template_selection=drafts()._templateSelection;
    takeTask(await mutate(`/tasks/${encodeURIComponent(taskId())}/${stage}`,body));
    const remaining=drafts();
    for(const [name,saved] of Object.entries(snapshot))if((state.task.definitions?.[name]?.stage===targetStage||stage==='technical'&&name.startsWith('_'))&&!(name==='operation_rates'&&incompleteRates.length)&&equal(remaining[name],saved))delete remaining[name];
    if(stage==='technical'&&snapshot._templateSelection)templateUi().notice='本单采用的模板参数已保存，可继续核对或查看报价。';
    state.priceMatches=null;state.selectedPrices={};
    toast(incompleteRates.length?'有效信息已保存；未完整的工序费率输入仍保留，请补齐金额和单位。':'本批信息已保存。');
    if(next&&stage==='requirements')state.tab='technical';
    else if(next&&!incompleteRates.length){location.hash=`task/${encodeURIComponent(taskId())}/quote`;}
  }
  async function upload(startRead) {
    if(!taskId()) {
      if(!state.selectedFiles.length&&!state.uploadInquiry.trim())throw new Error('请先选择图纸，或填写客户询价说明。');
      takeTask(await mutate('/tasks',{inquiry:state.uploadInquiry.trim()||'仅收到附件，询价原文未提供。',simulated:state.simulated},'create:'+state.uploadInquiry+':'+state.selectedFiles.map(file=>file.name+file.size).join('|')));
      history.replaceState(null,'',`#task/${encodeURIComponent(taskId())}/upload`);
    }
    while(state.selectedFiles.length) {
      const file=state.selectedFiles[0];
      const payload={task:taskId(),name:file.name,size:file.size,lastModified:file.lastModified};
      const signature='upload:'+JSON.stringify(payload);
      if(!state.pending.has(signature))state.pending.set(signature,{revision:state.task.revision,event_id:eventId('upload',payload)});
      const pending=state.pending.get(signature);
      try {
        takeTask(await request(`/tasks/${encodeURIComponent(taskId())}/attachments?name=${encodeURIComponent(file.name)}`,{method:'POST',body:file,headers:{'Content-Type':file.type||'application/octet-stream','X-Expected-Revision':String(pending.revision),'X-Event-Id':pending.event_id}}));
        state.pending.delete(signature);
      } catch(error) {if(error.status&&error.status<500)state.pending.delete(signature);throw error;}
      state.selectedFiles.shift();render();
    }
    toast('询价资料已保存。');
    if(startRead){await read();state.page='confirm';location.hash=`task/${encodeURIComponent(taskId())}/confirm`;}
  }
  async function read() {
    if(!state.task.attachments?.length)throw new Error('请先上传待识别图纸。');
    takeTask(await mutate(`/tasks/${encodeURIComponent(taskId())}/read`,{expected_revision:state.task.revision}));
    toast(activeRuns.has(state.task.run?.status)?'已开始读图，可等待结果后核对参数。':'读图请求已提交，请查看运行状态。');
  }
  async function generate() {
    if(dirty())throw new Error('参数页有未保存输入，请先保存并核对。');
    const p=policy();
    if(!p.pricing_method||p.rate_percent==='')throw new Error('请选择本单定价方式并填写比例。');
    if(!p.conditions)throw new Error('请确认本次人民币、未计税费、不含运费的报价口径。');
    const body={expected_revision:state.task.revision,policy:{pricing_method:p.pricing_method,rate_percent:p.rate_percent,tax_excluded:true,freight_excluded:true}};
    const result=await mutate(`/tasks/${encodeURIComponent(taskId())}/quotes`,body);
    state.quote=result;state.quoteView='quote';
    state.task.quotes=[{...result,id:result.quote_id||result.id},...(state.task.quotes||[]).filter(item=>(item.quote_id||item.id)!==(result.quote_id||result.id))];
    try {await loadTask(taskId());}
    catch(error){state.error=new Error('报价草稿已生成，任务状态刷新暂未完成。可直接查看本稿，稍后刷新任务状态。');}
    toast('模拟报价草稿已生成，可直接打开或下载。');
  }
  async function refreshTask() {
    const before=clone(state.task);
    await loadTask(taskId());
    const differences=Object.keys(drafts()).filter(name=>!name.startsWith('_')&&!equal(before.fields?.[name]?.value,state.task.fields?.[name]?.value));
    if(differences.length){state.conflict=differences;state.error=new Error('已载入最新资料，未提交输入仍保留。请核对差异后再提交。');}
    else {state.error=null;state.conflict=null;toast('已刷新任务状态，未提交输入仍保留。');}
    if(state.page==='quote'&&state.quote)await loadQuote(state.quote.quote_id||state.quote.id);
  }
  function reviewConflict() {
    const names=state.conflict||Object.keys(drafts()).filter(name=>!name.startsWith('_'));
    document.querySelector('#dialog-content').innerHTML=`<h2>核对未保存输入</h2><p class="small">最新资料与当前表单分别如下。返回后可继续修改；未保存输入仍保留。</p><div class="table-wrap"><table><thead><tr><th>字段</th><th>最新已保存</th><th>本页未保存输入</th></tr></thead><tbody>${names.map(name=>`<tr><td>${esc(state.task.definitions[name]?.label||name)}</td><td class="conflict-value">${esc(displayValue(fieldValue(name)))}</td><td class="conflict-value">${esc(displayValue(drafts()[name]))}</td></tr>`).join('')}</tbody></table></div><div class="footer">${button('改用最新已保存值','use-remote')}${button('已核对，保留本页输入继续','keep-local','primary')}</div>`;
    dialog.showModal();
  }
  function displayValue(raw){return raw===null||raw===undefined||raw===''?'待补充':Array.isArray(raw)?raw.map(item=>typeof item==='object'?`${item.operation}：${item.value} ${unitNames[item.unit]||item.unit}`:item).join('\n'):String(raw);}
  async function loadPrices(){const result=await request('/prices');state.prices=result.entries||result.prices||[];}
  async function matchPrices() {
    if(dirty())throw new Error('请先保存本批参数和价格匹配条件，再查询适用价格。');
    state.priceMatches=await request(`/tasks/${encodeURIComponent(taskId())}/price-matches`);state.selectedPrices={};
  }
  function applyPriceSelection() {
    const refs=clone(drafts()._priceSelections||{});
    let count=0;
    for(const [index,candidate] of Object.entries(state.selectedPrices)) {
      const result=state.priceMatches.results[Number(index)],entry=result.candidates[candidate];
      const ref={entry_id:entry.entry_id||entry.id,version:entry.version};
      if(!['unique','ambiguous'].includes(result.status))throw new Error('该价格暂不能采用，请补充条件或选择当前有效价格。');
      if(result.kind==='material'){drafts().material_rate=String(entry.rate);refs.material_rate=ref;}
      if(result.kind==='setup'){drafts().setup_fee=String(entry.rate);refs.setup_fee=ref;}
      if(result.kind==='operation'){
        const operation=result.operation||entry.operation;
        const rates=clone(ratesValue()).filter(rate=>rate.operation!==operation);
        rates.push({operation,value:String(entry.rate),unit:entry.unit});drafts().operation_rates=rates;
        refs.operations={...(refs.operations||{}),[operation]:ref};
      }
      count++;
    }
    if(!count)throw new Error('请先选择有依据的价格候选。');
    drafts()._priceSelections=refs;
    toast('所选价格已带入表单，尚未保存；请核对后提交本批技术信息。');
  }
  function setRate(operation,key,newValue) {
    const rates=clone(ratesValue());
    const index=rates.findIndex(rate=>rate.operation===operation);
    if(index<0)rates.push({operation,value:'',unit:'',[key]:newValue});else rates[index][key]=newValue;
    drafts().operation_rates=rates;
    if(drafts()._priceSelections?.operations)delete drafts()._priceSelections.operations[operation];
  }
  function selectFiles(files) {
    const supported=/\.(jpg|jpeg|png|webp|gif|bmp|tif|tiff|pdf|dxf|dwg|step|stp)$/i;
    const rejected=[];
    for(const file of files){if(file.size>25*1024*1024){rejected.push(file.name+'（超过25 MiB）');continue;}if(!supported.test(file.name)){rejected.push(file.name);continue;}state.selectedFiles.push(file);}
    if(rejected.length)state.error=new Error('暂不支持这些文件：'+rejected.join('、'));
    render();
  }
  async function savePrice() {
    const editor=state.priceEditor;
    const fields=['kind','name','rate','unit','material_grade','material_form','thickness_min','thickness_max','valid_from','valid_to','source'];
    const payload={currency:'CNY',tax_included:false,freight_included:false,simulated:!!editor.simulated};
    for(const key of fields){payload[key]=String(editor[key]??'').trim();if(!payload[key])throw new Error('请补齐价格名称、金额、单位、材料、板厚、有效期和来源。');}
    payload.inclusions=(Array.isArray(editor.inclusions)?editor.inclusions:String(editor.inclusions||'').split(/\r?\n/)).map(item=>item.trim()).filter(Boolean);
    if(!payload.inclusions.length)throw new Error('请填写价格包含项与计费口径。');
    if(payload.kind!=='material'){
      payload.product_family=String(editor.product_family||'').trim();
      if(!payload.product_family)throw new Error('请填写该价格适用的产品族或工艺模板。');
    }
    if(payload.kind==='operation'){
      payload.operation=String(editor.operation||'').trim();
      if(!payload.operation)throw new Error('请填写与技术表一致的精确工序名称。');
    }
    for(const key of ['qty_min','qty_max']){
      const raw=editor[key];
      if(raw===null||raw===undefined||raw===''){payload[key]=key==='qty_min'?1:null;continue;}
      if(!/^\d+$/.test(String(raw))||!Number.isSafeInteger(Number(raw))||Number(raw)<1)throw new Error('数量范围请填写大于零的整数。');
      payload[key]=Number(raw);
    }
    const path=editor.entry_id?`/prices/${encodeURIComponent(editor.entry_id)}/versions`:'/prices';
    const result=await mutate(path,payload);
    state.priceEditor=clone(result.entry||result);await loadPrices();toast('价格草稿已保存，核对后可发布。');
  }
  async function changePriceStatus(action) {
    const editor=state.priceEditor;
    if(editor._edited)throw new Error('价格内容有未保存修改，请先另存为新草稿版本。');
    const result=await mutate(`/prices/${encodeURIComponent(editor.entry_id)}/${encodeURIComponent(editor.version)}/${action}`,{});
    state.priceEditor=clone(result.entry||result);await loadPrices();toast(action==='publish'?'本价格版本已发布。':'本价格版本已停用。');
  }
  document.addEventListener('input',event=>{
    const element=event.target;
    if(element.hasAttribute('data-template-name'))templateUi().name=element.value;
    if(element.dataset.field){
      drafts()[element.dataset.field]=element.dataset.field==='fee_inclusions'?[...inclusionValue().filter(item=>inclusionChoices.includes(item)),...element.value.split(/\r?\n/).map(line=>line.trim()).filter(Boolean)]:element.value;
      if(drafts()._priceSelections)delete drafts()._priceSelections[element.dataset.field];
      if(element.dataset.field==='setup_fee'&&(element.value.trim()===''||!Number.isFinite(Number(element.value))||Number(element.value)!==0))delete drafts()._feeResolution;
    }
    if(element.dataset.rate)setRate(element.dataset.rate,'value',element.value);
    if(element.dataset.context)drafts()._context={...(drafts()._context||{}),[element.dataset.context]:element.value};
    if(element.dataset.policy)policy()[element.dataset.policy]=element.type==='checkbox'?element.checked:element.value;
    if(element.hasAttribute('data-inquiry'))state.uploadInquiry=element.value;
    if(element.dataset.priceField){state.priceEditor[element.dataset.priceField]=element.type==='checkbox'?element.checked:element.value;state.priceEditor._edited=true;}
    if(element.hasAttribute('data-search')){state.search=element.value;render();}
    if(element.dataset.field==='setup_fee'||element.dataset.rate)patchFees();
    content.querySelectorAll('[data-save-state]').forEach(item=>item.textContent=dirty()?'有未保存输入':'显示的是已保存资料');
  });
  document.addEventListener('change',event=>{
    const element=event.target;
    if(element.hasAttribute('data-template-select')){templateUi().selected=element.value;patchTemplatePanel();}
    if(element.hasAttribute('data-template-usage'))templateUi().includeUsage=element.checked;
    if(element.hasAttribute('data-files'))selectFiles(Array.from(element.files||[]));
    if(element.hasAttribute('data-simulated'))state.simulated=element.checked;
    if(element.hasAttribute('data-filter')){state.filter=element.value;render();}
    if(element.hasAttribute('data-attachment')){state.attachment=Number(element.value);state.zoom=100;render();}
    if(element.dataset.operation){const route=routeValue();drafts().process_route=element.checked?[...new Set([...route,element.dataset.operation])]:route.filter(operation=>operation!==element.dataset.operation);if(!element.checked&&drafts()._priceSelections?.operations)delete drafts()._priceSelections.operations[element.dataset.operation];render();}
    if(element.dataset.rateUnit)setRate(element.dataset.rateUnit,'unit',element.value);
    if(element.dataset.rateUnit)patchFees();
    if(element.hasAttribute('data-inclusion')){
      const item=inclusionChoices[Number(element.dataset.inclusion)],items=inclusionValue().filter(value=>value!==item);
      drafts().fee_inclusions=element.checked?[...items,item]:items;
      if(inclusionValue().some(value=>!inclusionChoices.includes(value))){const details=content.querySelector('[data-fee-notes]');if(details)details.open=true;}
      content.querySelectorAll('[data-save-state]').forEach(item=>item.textContent='有未保存输入');
    }
    if(element.dataset.policy==='pricing_method')render();
    if(element.dataset.priceField==='kind'){state.priceEditor.kind=element.value;state.priceEditor.unit='';state.priceEditor._edited=true;render();}
    if(element.dataset.priceSelect){const[index,candidate]=element.dataset.priceSelect.split(':').map(Number);state.selectedPrices[index]=candidate;}
    if(element.hasAttribute('data-quote-select'))execute(async()=>{await loadQuote(element.value);});
  });
  document.addEventListener('submit',event=>{if(event.target.closest('[data-stage]'))event.preventDefault();});
  async function templateRead(operation) {
    const id=taskId(),token=state.routeToken;
    try{await operation();}catch(error){if(taskId()===id&&state.routeToken===token){state.error=error;render();}}
  }
  document.addEventListener('toggle',event=>{if(event.target.matches?.('[data-section="templates"]')&&event.target.open&&state.templates===null)templateRead(loadTemplates);},true);
  dialog.addEventListener('cancel',event=>{if(state.templatePreview){event.preventDefault();closeTemplatePreview();}});
  document.addEventListener('focusout',event=>rememberFormFocus(event.target));
  document.addEventListener('dragover',event=>{const zone=event.target.closest('[data-dropzone]');if(zone){event.preventDefault();zone.classList.add('dragover');}});
  document.addEventListener('dragleave',event=>event.target.closest('[data-dropzone]')?.classList.remove('dragover'));
  document.addEventListener('drop',event=>{const zone=event.target.closest('[data-dropzone]');if(zone){event.preventDefault();zone.classList.remove('dragover');if(!state.busy)selectFiles(Array.from(event.dataTransfer.files));}});
  async function execute(operation) {
    if(state.busy)return;
    state.busy=true;state.error=null;render();
    try{await operation();}
    catch(error){state.error=error;if(error.status===409){state.conflict=Object.keys(drafts()).filter(name=>!name.startsWith('_'));state.error.message='资料已被其他操作更新，当前输入仍保留。请先刷新并核对差异，再提交。';}}
    finally{state.busy=false;if(state.pendingRoute){state.pendingRoute=false;route();}else render();}
  }
  document.addEventListener('click',event=>{
    const control=event.target.closest('[data-action]');if(!control||control.disabled)return;
    const action=control.dataset.action;
    if(action==='refresh-templates'){templateRead(loadTemplates);return;}
    if(action==='preview-template'){templateRead(previewTemplate);return;}
    if(action==='cancel-template-preview'){closeTemplatePreview();return;}
    if(action==='apply-template'){try{applyTemplate();}catch(error){state.error=error;render();}return;}
    if(action==='disable-template'){confirmDisableTemplate();return;}
    if(action==='new'){state.selectedFiles=[];state.uploadInquiry='';state.simulated=true;location.hash='new';return;}
    if(action==='requirements'||action==='technical'){state.tab=action;render();return;}
    if(action==='go-to-issue'){goToIssue(control);return;}
    if(action==='edit-operation-billing'){focusField('operation_rates','fees',control.dataset.operationTarget);return;}
    if(action==='edit-setup-fee'){focusField('setup_fee');return;}
    if(action==='edit-fee-inclusions'){focusField('fee_inclusions');return;}
    if(action==='cancel-setup-fee'){
      if(!feeOverlap())return;
      drafts()._feeResolution={from:String(value('setup_fee'))};drafts().setup_fee='0';
      if(drafts()._priceSelections)delete drafts()._priceSelections.setup_fee;
      const input=content.querySelector('[data-field="setup_fee"]');if(input)input.value='0';
      patchFees();patchFieldMetadata();focusField('fee_inclusions');
      content.querySelectorAll('[data-save-state]').forEach(item=>item.textContent='有未保存输入');
      toast('准备费已在本页设为 0。请核对包含说明，再保存本批输入。');return;
    }
    if(action==='dismiss-error'){state.error=null;render();return;}
    if(action==='zoom-in'||action==='zoom-out'){state.zoom=Math.min(300,Math.max(50,state.zoom+(action==='zoom-in'?25:-25)));render();return;}
    if(action==='remove-file'){state.selectedFiles.splice(Number(control.dataset.index),1);render();return;}
    if(action==='add-operation'){const input=document.querySelector('#custom-operation');const operation=input.value.trim();if(operation){drafts().process_route=[...new Set([...routeValue(),operation])];render();}return;}
    if(action==='quote-view'||action==='cost-view'){state.quoteView=action==='quote-view'?'quote':'cost';render();return;}
    if(action==='review-conflict'){reviewConflict();return;}
    if(action==='keep-local'){state.conflict=null;state.error=null;dialog.close();render();return;}
    if(action==='use-remote'){for(const name of state.conflict||[])delete drafts()[name];state.conflict=null;state.error=null;dialog.close();render();return;}
    if(action==='new-price'){state.priceEditor={kind:'material',unit:'',qty_min:'1',qty_max:'',simulated:true};render();return;}
    if(action==='close-price'){state.priceEditor=null;render();return;}
    if(action==='edit-price'){state.priceEditor=clone(state.prices[Number(control.dataset.index)]);render();return;}
    execute(async()=>{
      if(action==='refresh-list')await loadTasks();
      else if(action==='refresh-task')await refreshTask();
      else if(action==='upload'||action==='upload-read')await upload(action==='upload-read');
      else if(action==='read')await read();
      else if(action==='stop-read')takeTask(await mutate(`/tasks/${encodeURIComponent(taskId())}/read/stop`,{expected_revision:state.task.revision}));
      else if(action==='save-requirements'||action==='save-requirements-next')await saveStage('requirements',action.endsWith('-next'));
      else if(action==='save-technical'||action==='save-technical-next')await saveStage('technical',action.endsWith('-next'));
      else if(action==='generate')await generate();
      else if(action==='match-prices')await matchPrices();
      else if(action==='apply-price-selection')applyPriceSelection();
      else if(action==='save-price')await savePrice();
      else if(action==='publish-price')await changePriceStatus('publish');
      else if(action==='disable-price')await changePriceStatus('disable');
      else if(action==='save-template')await saveTemplate();
      else if(action==='confirm-disable-template')await disableTemplate();
    });
  });
  window.addEventListener('hashchange',route);
  window.addEventListener('beforeunload',event=>{if([...state.drafts.values()].some(draft=>Object.keys(draft).length)||state.selectedFiles.length){event.preventDefault();event.returnValue='';}});
  setInterval(updateClocks,1000);
  route();
})();
