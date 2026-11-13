/* Drawing recognition only. Explicit user actions start or stop model work. */
(() => {
  'use strict';
  const API='/workbench/api', main=document.querySelector('#content');
  const active=new Set(['starting','running','unknown','stopping']);
  const state={view:'new',id:null,task:null,tasks:[],busy:false,loading:false,error:'',syncError:'',token:0,timer:null,preview:null,page:1,drafts:new Map(),pending:new Map(),open:new Set()};
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const draft=()=>{const key=state.id||'new';if(!state.drafts.has(key))state.drafts.set(key,{files:[],note:'',uploads:new Map()});return state.drafts.get(key);};
  const event=()=>'web-'+crypto.randomUUID();
  const url=raw=>typeof raw==='string'&&raw.startsWith('/workbench/')&&!raw.startsWith('//')?raw:'';
  const link=(raw,label,classes='button link')=>url(raw)?`<a class="${classes}" href="${esc(url(raw))}" target="_blank" rel="noopener">${esc(label)}</a>`:'';
  const button=(label,action,classes='',extra='')=>`<button type="button" class="button ${classes}" data-action="${action}" ${state.busy?'disabled':''} ${extra}>${esc(label)}</button>`;
  const date=raw=>raw?new Date(raw).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
  const value=raw=>Array.isArray(raw)?raw.map(v=>typeof v==='object'?JSON.stringify(v):v).join('、'):typeof raw==='object'?JSON.stringify(raw):String(raw??'未明确');
  const unit=raw=>({'piece':'件'}[raw]||raw||'');
  const label=run=>({starting:'正在准备图纸',running:'AI 正在识别图纸',unknown:'正在确认识别状态',stopping:'正在停止识别',completed:'识别已完成',failed:'本轮识别未完成',stopped:'识别已停止',waiting:'需要补充清晰图纸'}[run?.status]||'等待开始识别');
  function toast(text){const el=document.querySelector('#toast');el.textContent=text;el.hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>{el.hidden=true;},4500);}
  async function api(path,options={}){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),45000);
    try{
      const response=await fetch(API+path,{...options,credentials:'same-origin',signal:controller.signal,headers:{'X-Workbench-Request':'1',...(options.body instanceof Blob?{}:{'Content-Type':'application/json'}),...options.headers}});
      const data=await response.json();
      if(!response.ok){const error=new Error(data?.error?.message||`请求未完成（${response.status}）`);error.status=response.status;throw error;}
      return data.task||data;
    }catch(error){if(error.name==='AbortError')throw new Error('连接等待超时，已上传资料会保留。请刷新状态后继续。');throw error;}
    finally{clearTimeout(timer);}
  }
  async function post(path,body){
    const key=path+JSON.stringify(body);
    if(!state.pending.has(key))state.pending.set(key,{...body,event_id:event()});
    try{const result=await api(path,{method:'POST',body:JSON.stringify(state.pending.get(key))});state.pending.delete(key);return result;}
    catch(error){if(error.status&&error.status<500)state.pending.delete(key);throw error;}
  }
  function rememberOpen(){main.querySelectorAll('details[data-detail]').forEach(el=>{if(el.open)state.open.add(el.dataset.detail);else state.open.delete(el.dataset.detail);});}
  function restoreOpen(){main.querySelectorAll('details[data-detail]').forEach(el=>{el.open=state.open.has(el.dataset.detail);});}
  function filesView(){
    const saved=state.task?.attachments||[],selected=draft().files;
    return `<div class="file-list">${saved.map(f=>`<div class="file-row"><div class="file-name">${esc(f.name)}<span class="small"> · 已上传</span></div>${link(f.url,'打开文件')}</div>`).join('')}${selected.map((f,i)=>`<div class="file-row"><div class="file-name">${esc(f.name)}<span class="small"> · 待上传</span></div>${button('移除','remove-file','link',`data-index="${i}"`)}</div>`).join('')}</div>`;
  }
  function uploadView(compact=false){
    const running=active.has(state.task?.run?.status),d=draft();
    return `<section class="card"><h2>${compact?'补充图纸':'上传待识别图纸'}</h2><div class="upload-zone" data-drop><strong>选择文件，或拖放到这里</strong><p class="small">图片、PDF 或 CAD，可一次选择多份。单个文件不超过 25 MB。</p><label class="button"><span>选择图纸</span><input type="file" data-files multiple accept=".png,.jpg,.jpeg,.webp,.gif,.bmp,.tif,.tiff,.pdf,.dxf,.dwg,.step,.stp" ${state.busy||running?'disabled':''}></label></div>${filesView()}<details data-detail="note-${esc(state.id||'new')}"><summary>识别说明（可选）</summary><textarea data-note rows="3" placeholder="例如：重点查看板厚、材料和密封要求" ${state.busy||running?'disabled':''}>${esc(d.note)}</textarea></details>${(d.files.some(f=>/\.(dwg|step|stp)$/i.test(f.name)))?'<p class="small">CAD 可预览；识别遇到无法读取的内容时，可补充 PDF 或清晰截图。</p>':''}<div class="actions">${button(state.busy?'正在提交…':compact?'上传并重新识别':'开始识别','start','primary',running?'disabled':'')}${state.id?'<a class="button link" href="#tasks">返回识别记录</a>':''}</div></section>`;
  }
  function progressView(){
    const run=state.task?.run;if(!run)return '';
    const working=active.has(run.status),events=run.progress?.events||[];
    return `<section class="progress-panel" aria-label="识别进度"><div class="progress-head"><div><h2>${working?'<span class="spinner" aria-hidden="true"></span>':''}${esc(label(run))}</h2><p class="small">${working?'识别在服务器后台进行，可以离开页面，稍后从识别记录查看结果。':'识别内容为 AI 提取结果，请结合原图核对。'}</p></div><span class="badge ${run.status==='failed'?'danger':working?'warning':'success'}">${working?'后台处理中':run.status==='completed'?'已完成':'待处理'}</span></div>${working?`<div class="progress-meta"><span>${esc(run.progress?.label||'正在处理已上传图纸')}</span><span>开始于 ${esc(date(run.started_at))}</span></div>`:''}${run.error?`<div class="alert error" role="alert">${esc(run.error)}</div>`:''}${state.syncError?`<div class="alert">暂时无法刷新状态，后台识别会继续。${button('刷新状态','refresh','link')}</div>`:''}${events.length?`<details class="run-history" data-detail="progress-${esc(run.id)}"><summary>查看识别进度记录</summary><ol>${events.map(e=>`<li>${esc(e.label)}${e.at?`<span class="small"> · ${esc(date(e.at))}</span>`:''}</li>`).join('')}</ol></details>`:''}<div class="actions"><a class="button link" href="#tasks">${working?'返回记录，后台继续':'查看所有识别记录'}</a>${working?button('停止识别','stop','link',run.status==='stopping'?'disabled':''):button(run.status==='failed'?'重试识别':'重新识别','retry','link')}</div></section>`;
  }
  function previewView(){
    const files=state.task?.attachments||[];
    const file=files.find(f=>f.id===state.preview)||files[0];
    if(!file)return '<section class="card preview-panel"><div class="empty">上传后可在这里查看原图</div></section>';
    state.preview=file.id;const original=url(file.url),pdf=file.mime==='application/pdf'||/\.pdf$/i.test(file.name),cad=/\.(dxf|dwg|step|stp)$/i.test(file.name),image=/^image\/(png|jpeg|webp|gif|bmp)/.test(file.mime||'');
    const target=cad?`/workbench/cad-preview/index.html?task=${encodeURIComponent(state.id)}&source=${encodeURIComponent(file.id)}`:original+(pdf&&state.page>1?'#page='+state.page:'');
    return `<section class="card preview-panel"><div class="preview-toolbar"><div><h2>原图预览</h2><select data-preview aria-label="选择预览图纸">${files.map(f=>`<option value="${esc(f.id)}" ${f.id===file.id?'selected':''}>${esc(f.name)}</option>`).join('')}</select></div>${link(original,'打开原文件')}</div><div class="preview-canvas">${image?`<img src="${esc(original)}" alt="${esc(file.name)}">`:pdf||cad?`<iframe title="${esc(file.name)}" src="${esc(target)}"></iframe>`:`<div class="empty">此格式请打开原文件查看${link(original,'打开原文件')}</div>`}</div><p class="small">${esc(file.name)}${pdf?` · 第 ${state.page} 页`:''}</p></section>`;
  }
  function evidenceView(e){
    if(!e)return '';
    if(e.reference==='inquiry')return `<span class="small">来源：识别说明 · ${esc(e.region)}</span>`;
    const file=(state.task.attachments||[]).find(f=>f.id===e.reference);
    return file?`<button type="button" class="evidence-link" data-action="evidence" data-reference="${esc(file.id)}" data-page="${esc(e.page||1)}">${esc(file.name)} · 第 ${esc(e.page||1)} 页 · ${esc(e.region)}</button>`:'';
  }
  function annotationView(o,withEvidence=true){
    return `<article class="annotation"><div class="annotation-head"><strong>${esc(o.label||o.source_name||'图纸标注')}</strong>${o.status!=='observed'?`<span class="badge warning">${esc(({unit_unknown:'单位待核对',conflict:'标注有差异',unreadable:'尚未读清'})[o.status]||'待核对')}</span>`:''}</div><div class="annotation-text">${esc(o.raw_text)}</div>${o.reason?`<p class="small">${esc(o.reason)}</p>`:''}${withEvidence?evidenceView(o.evidence):''}</article>`;
  }
  function partView(part,index,runId){
    const rows=Array.isArray(part.observations)?part.observations:[];
    const dimension=o=>/dimension|overall_size|尺寸|孔径|直径|外形|length|width|height/i.test(`${o.source_name||''} ${o.label||''}`);
    const groups=[
      ['材料',rows.filter(o=>['material_family','material_grade'].includes(o.name))],
      ['板厚',rows.filter(o=>o.name==='thickness')],
      ['尺寸',rows.filter(dimension)],
      ['表面处理',rows.filter(o=>o.name==='surface_treatment')],
      ['技术要求',rows.filter(o=>o.name==='process_route'||(o.name==='engineering_open_items'&&!dimension(o)))],
    ];
    const available=new Set((state.task?.attachments||[]).map(f=>f.id));
    const source=(part.evidence||[]).find(e=>available.has(e.reference));
    return `<article class="part-card" data-part-key="${esc(part.key)}"><div class="part-head"><div><div class="small">零件 ${index+1}</div><h3>${esc(part.name||'未命名零件')}</h3><div class="part-identifiers"><span>图号：${esc(part.part_number||'未明确')}</span><span>版本：${esc(part.version||'未明确')}</span></div></div>${source?button('查看图纸','evidence','link',`data-reference="${esc(source.reference)}" data-page="${esc(source.page||1)}"`):''}</div><div data-part-visual="${esc(part.key)}"></div><div class="part-summary">${groups.map(([title,items])=>`<div class="part-summary-item" data-part-field="${esc(title)}"><div class="small">${title}</div>${items.length?items.map(o=>`<div class="part-summary-text">${esc(o.raw_text)}${o.status&&o.status!=='observed'?` <span class="badge warning">${esc(({unit_unknown:'单位待核对',conflict:'标注有差异',unreadable:'尚未读清'})[o.status]||'待核对')}</span>`:''}</div>`).join(''):'<div class="part-summary-text muted">未明确</div>'}</div>`).join('')}</div><details class="part-annotations" data-detail="part-${esc(runId)}-${esc(part.key)}"><summary>查看本件全部标注 · ${rows.length} 条</summary>${rows.map(o=>annotationView(o,false)).join('')||'<p class="small">本件尚无明确标注。</p>'}</details></article>`;
  }
  function resultsView(){
    const r=state.task?.recognition||{},candidates=r.candidates||[],observations=r.observations||[],parts=Array.isArray(r.parts)?r.parts:[];
    const questions=[...new Set([...(r.questions||[]),...(r.warnings||[])])];
    const files=[...(state.task?.attachments||[]),{id:'inquiry',name:'识别说明'}];
    const summary=`<p class="small">${r.partial?'首轮结果已保留，后台仍在继续核对。':parts.length?'以下是整份资料的概览，具体参数请查看对应零件。':'仅展示本轮 AI 识别内容，尚未经人工核对。'}</p>${candidates.length?`<div class="summary-grid">${candidates.map(c=>`<div class="summary-item"><div class="small">${esc(c.label)}</div><strong>${esc(value(c.value))}${c.unit?' '+esc(unit(c.unit)):''}</strong></div>`).join('')}</div>`:`<div class="empty">${active.has(state.task?.run?.status)?'正在读取图纸，结果会自动出现在这里。':state.task?.run?.status==='completed'?'尚未提取到明确参数，请查看原始标注和待核对问题。':'开始识别后，这里会显示材料、板厚、尺寸等图纸信息。'}</div>`}`;
    const partsPanel=parts.length?`<div class="card parts-panel"><h2>识别出的零件 · ${parts.length}</h2><p class="small">${r.partial?'已读出的零件先显示，后台仍在继续识别和核对。':'按零件整理图纸中的原始信息，尚未经人工核对。'}</p><div class="parts-list">${parts.map((part,index)=>partView(part,index,r.run_id)).join('')}</div></div>`:r.parts_supported?`<div class="card parts-panel"><h2>识别出的零件 · 0</h2><p class="small">${r.partial?'暂未读出明确零件，后台仍在继续识别。':'本轮未识别出有明确依据的零件，请查看原图和原始标注。'}</p></div>`:r.status==='completed'?`<div class="alert legacy-parts">本次结果尚未按零件整理，可重新识别。${button('重新识别','retry','link')}</div>`:'';
    return `<section class="results-panel">${r.source_current===false&&r.run_id?'<div class="alert">附件有更新，本轮结果尚未覆盖全部附件，可重新识别。</div>':''}${partsPanel}${parts.length?`<details class="card overall-summary" data-detail="overall-${esc(r.run_id)}"><summary>整份资料概览</summary>${summary}</details>`:`<div class="card"><h2>${r.partial?'正在整理的识别内容':'识别摘要'}</h2>${summary}</div>`}${questions.length?`<details class="card questions" data-detail="questions-${esc(r.run_id)}"><summary>待核对问题 · ${questions.length} 项</summary><ul>${questions.map(q=>`<li>${esc(q)}</li>`).join('')}</ul></details>`:''}<div class="card"><h2>图纸原始标注 <span class="small">${observations.length} 条</span></h2><p class="small">按文件展开查看，可点击来源定位到原图页码。</p>${files.map(f=>{const rows=observations.filter(o=>o.evidence?.reference===f.id);if(!rows.length)return '';return `<details class="annotation-group" data-detail="file-${esc(r.run_id)}-${esc(f.id)}"><summary>${esc(f.name)} <span class="small">${rows.length} 条</span></summary>${rows.map(o=>annotationView(o)).join('')}</details>`;}).join('')||'<p class="small">暂无本轮原始标注。</p>'}</div></section>`;
  }
  function listView(){
    return `<div class="page-head"><div><h1>识别记录</h1><p class="subtitle">查看已有结果，或继续等待后台识别。</p></div><a class="button primary" href="#new">上传新图纸</a></div><div class="task-list">${state.tasks.map(t=>`<a class="task-row card" href="#task/${encodeURIComponent(t.id)}"><div class="task-info"><strong class="task-title">${esc(t.recognition_name||'图纸识别记录')}</strong><div class="task-meta small">${esc(t.attachment_count??0)} 份附件 · ${esc(date(t.updated_at))}</div></div><span class="badge task-status ${t.run?.status==='completed'?'success':t.run?.status==='failed'?'danger':'warning'}">${esc(label(t.run))}</span><span class="small task-open">查看结果 →</span></a>`).join('')||'<div class="card empty">还没有识别记录，上传图纸即可开始。</div>'}</div>`;
  }
  function render(){
    rememberOpen();
    document.querySelector('#breadcrumb').textContent=state.view==='tasks'?'识别记录':state.view==='new'?'上传图纸':'查看识别结果';
    document.querySelectorAll('[data-nav]').forEach(el=>el.classList.toggle('active',el.dataset.nav===(state.view==='tasks'?'tasks':'new')));
    const error=state.error?`<div class="alert error" role="alert"><strong>操作未完成</strong><p>${esc(state.error)}</p>${button('刷新状态','refresh','link')}</div>`:'';
    const header=state.view==='task'?`<div class="page-head"><div><h1>图纸识别</h1><p class="subtitle">${state.task?.attachments?.length||0} 份资料 · ${state.task?.recognition?.observations?.length||0} 条原始标注</p></div><a class="button" href="#new">识别新图纸</a></div>`:`<div class="page-head"><div><h1>让图纸信息，一目了然</h1><p class="subtitle">上传图纸，AI 提取材料、板厚、尺寸与技术备注。</p></div></div><nav class="steps" aria-label="识别流程"><span class="step active">1 上传图纸</span><span class="step">2 AI 识别</span><span class="step">3 查看结果</span></nav>`;
    main.innerHTML=error+(state.loading&&!state.task?'<div class="loading">正在加载…</div>':state.view==='tasks'?listView():state.view==='new'?header+uploadView():header+progressView()+`<div class="recognition-grid"><div id="drawing-preview">${previewView()}</div>${resultsView()}</div><details class="card" data-detail="add-files"><summary>补充或更换图纸</summary>${uploadView(true)}</details>`);
    window.HylPartVisuals?.mount(main,{task:state.view==='task'?state.task:null,runId:state.task?.recognition?.run_id});
    main.setAttribute('aria-busy',state.loading?'true':'false');restoreOpen();
  }
  function schedule(){
    clearTimeout(state.timer);
    const needed=state.view==='tasks'&&state.tasks.some(t=>active.has(t.run?.status))||state.view==='task'&&active.has(state.task?.run?.status);
    if(!needed)return;
    const token=state.token;
    state.timer=setTimeout(async()=>{
      if(token!==state.token)return;
      if(state.busy){schedule();return;}
      try{
        if(state.view==='tasks')state.tasks=(await api('/tasks')).tasks;
        else{const previous=state.task?.run?.status;state.task=await api('/tasks/'+encodeURIComponent(state.id));if(active.has(previous)&&state.task.run?.status==='completed')toast('识别已完成，可以查看结果。');}
        state.syncError='';render();
      }catch(error){state.syncError=error.message;if(state.view==='task')render();}
      finally{if(token===state.token)schedule();}
    },2000);
  }
  async function route(){
    if(state.busy){history.replaceState(null,'',state.id?'#task/'+state.id:state.view==='tasks'?'#tasks':'#new');toast('正在保存上传资料，请稍候；识别开始后可离开页面。');return;}
    clearTimeout(state.timer);const token=++state.token;
    const parts=location.hash.slice(1).split('/');
    state.id=null;state.task=null;state.error='';state.syncError='';state.preview=null;state.page=1;
    if(parts[0]==='task'&&/^[A-Za-z0-9_-]+$/.test(parts[1]||'')){state.view='task';state.id=parts[1];history.replaceState(null,'','#task/'+state.id);}
    else if(parts[0]==='tasks')state.view='tasks';
    else{state.view='new';history.replaceState(null,'','#new');}
    state.loading=true;render();
    try{
      const data=state.view==='task'?await api('/tasks/'+encodeURIComponent(state.id)):state.view==='tasks'?await api('/tasks'):null;
      if(token!==state.token)return;
      if(state.view==='task')state.task=data;else if(data)state.tasks=data.tasks;
    }catch(error){if(token===state.token)state.error=error.message;}
    finally{if(token===state.token){state.loading=false;render();schedule();}}
  }
  function addFiles(files){
    if(state.busy||active.has(state.task?.run?.status))return;
    const d=draft();for(const f of files){if(f.size>25*1024*1024){state.error=f.name+' 超过 25 MB，请缩小文件后上传。';continue;}if(!d.files.some(x=>x.name===f.name&&x.size===f.size&&x.lastModified===f.lastModified))d.files.push(f);}render();
  }
  async function begin(retry=false){
    let d=draft();
    if(!state.id&&!d.files.length)throw new Error('请先选择待识别图纸。');
    if(!state.id){
      state.task=await post('/tasks',{inquiry:d.note.trim(),simulated:false});state.id=state.task.id;state.view='task';state.drafts.set(state.id,d);state.drafts.delete('new');history.replaceState(null,'','#task/'+state.id);
    }
    while(d.files.length){
      const f=d.files[0];if(!d.uploads.has(f))d.uploads.set(f,{event_id:event(),revision:state.task.revision});
      const pending=d.uploads.get(f);
      try{state.task=await api('/tasks/'+encodeURIComponent(state.id)+'/attachments?name='+encodeURIComponent(f.name),{method:'POST',body:f,headers:{'Content-Type':f.type||'application/octet-stream','X-Event-Id':pending.event_id,'X-Expected-Revision':String(pending.revision)}});d.uploads.delete(f);d.files.shift();render();}
      catch(error){if(error.status&&error.status<500)d.uploads.delete(f);throw error;}
    }
    state.task=await api('/tasks/'+encodeURIComponent(state.id));
    if(!state.task.attachments?.length)throw new Error('请先上传图纸。');
    if(!active.has(state.task.run?.status))state.task=await post('/tasks/'+encodeURIComponent(state.id)+'/read',{expected_revision:state.task.revision});
    toast('已开始后台识别，可从识别记录查看结果。');schedule();
  }
  async function action(name,element){
    if(name==='evidence'){
      state.preview=element.dataset.reference;state.page=Math.max(1,Number(element.dataset.page)||1);
      document.querySelector('#drawing-preview').innerHTML=previewView();return;
    }
    if(state.busy)return;
    if(name==='remove-file'){draft().files.splice(Number(element.dataset.index),1);render();return;}
    state.busy=true;state.error='';render();
    try{
      if(name==='start'||name==='retry')await begin(name==='retry');
      else if(name==='stop'&&state.task)state.task=await post('/tasks/'+encodeURIComponent(state.id)+'/read/stop',{expected_revision:state.task.revision});
      else if(name==='refresh'){if(state.id)state.task=await api('/tasks/'+encodeURIComponent(state.id));else if(state.view==='tasks')state.tasks=(await api('/tasks')).tasks;}
    }catch(error){state.error=error.message||'操作未完成，请稍后再试。';}
    finally{state.busy=false;render();schedule();}
  }
  main.addEventListener('click',e=>{const target=e.target.closest('[data-action]');if(target){e.preventDefault();action(target.dataset.action,target);}});
  main.addEventListener('input',e=>{if(e.target.matches('[data-note]'))draft().note=e.target.value;});
  main.addEventListener('change',e=>{if(e.target.matches('[data-files]'))addFiles(e.target.files);if(e.target.matches('[data-preview]')){state.preview=e.target.value;state.page=1;document.querySelector('#drawing-preview').innerHTML=previewView();}});
  main.addEventListener('dragover',e=>{if(e.target.closest('[data-drop]'))e.preventDefault();});
  main.addEventListener('drop',e=>{if(e.target.closest('[data-drop]')){e.preventDefault();addFiles(e.dataTransfer.files);}});
  window.addEventListener('hashchange',route);window.addEventListener('beforeunload',()=>clearTimeout(state.timer));
  route();
})();
