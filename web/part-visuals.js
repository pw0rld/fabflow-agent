/* Part drawing views. All image URLs remain inside the current task. */
(() => {
  'use strict';
  const NS='http://www.w3.org/2000/svg', selections=new Map();
  let scope='';
  const node=(tag,classes,text)=>{const n=document.createElement(tag);if(classes)n.className=classes;if(text!==undefined)n.textContent=text;return n;};
  const svg=(tag,attrs={})=>{const n=document.createElementNS(NS,tag);for(const [key,value] of Object.entries(attrs))n.setAttribute(key,String(value));return n;};
  const choice=(text,click,selected=false)=>{const b=node('button','button part-visual-choice',text);b.type='button';b.setAttribute('aria-pressed',String(selected));b.addEventListener('click',click);return b;};
  const list=value=>Array.isArray(value)?value:[];
  function sameTaskUrl(raw,taskId,runId,kind){
    if(typeof raw!=='string'||!raw.startsWith('/workbench/api/tasks/')||raw.includes('\\'))return '';
    try{
      const u=new URL(raw,location.origin),prefix='/workbench/api/tasks/'+encodeURIComponent(taskId)+'/';
      if(u.origin!==location.origin||u.hash||!u.pathname.startsWith(prefix))return '';
      const rest=u.pathname.slice(prefix.length);
      if(kind==='page'&&/^reading-pages\/[A-Za-z0-9_-]+\/[1-9][0-9]*\.png$/.test(rest)&&u.searchParams.get('run_id')===runId&&[...u.searchParams].length===1)return u.pathname+u.search;
      if(kind==='attachment'&&/^attachments\/[A-Za-z0-9_-]+$/.test(rest)&&!u.search)return u.pathname;
    }catch(_error){}
    return '';
  }
  function bounds(raw){
    if(!Array.isArray(raw)||raw.length!==4||!raw.every(v=>typeof v==='number'&&Number.isFinite(v)))return null;
    const [x,y,w,h]=raw;
    return x>=0&&y>=0&&w>0&&h>0&&x+w<=1000&&y+h<=1000?[x,y,w,h]:null;
  }
  function hotspots(raw){
    return list(raw).slice(0,16).flatMap((p,index)=>{
      if(!p||typeof p.label!=='string'||!Array.isArray(p.polygon)||p.polygon.length<3||p.polygon.length>64)return [];
      if(!p.polygon.every(v=>Array.isArray(v)&&v.length===2&&v.every(n=>typeof n==='number'&&Number.isFinite(n)&&n>=0&&n<=1000)))return [];
      return [{id:String(index),label:p.label.slice(0,200),polygon:p.polygon}];
    });
  }
  function drawingViews(part,task,runId){
    const attachments=new Map(list(task.attachments).map(a=>[a.id,a]));
    const views=list(part.visualization?.views).slice(0,24).flatMap((v,index)=>{
      if(!v||!attachments.has(v.reference)||!Number.isInteger(v.page)||v.page<1)return [];
      const attachment=attachments.get(v.reference),pageUrl=sameTaskUrl(v.image_url,task.id,runId,'page');
      const attachmentUrl=v.page===1&&/^image\/(png|jpeg|webp|gif|bmp)$/.test(attachment.mime||'')?sameTaskUrl(v.image_url,task.id,runId,'attachment'):'';
      const imageUrl=pageUrl||(attachmentUrl===attachment.url?attachmentUrl:''),bbox=bounds(v.bbox);
      if(!imageUrl||!bbox)return [];
      const expected='/workbench/api/tasks/'+encodeURIComponent(task.id)+'/reading-pages/'+encodeURIComponent(v.reference)+'/'+v.page+'.png';
      if(pageUrl&&new URL(pageUrl,location.origin).pathname!==expected)return [];
      const provenance=['ai','prepared','source'].includes(v.provenance)?v.provenance:'ai';
      return [{id:String(v.id||index),label:typeof v.label==='string'?v.label.slice(0,200):'第 '+v.page+' 页',reference:v.reference,page:v.page,imageUrl,bbox,hotspots:hotspots(v.hotspots),provenance}];
    });
    if(views.length)return views;
    const used=new Set();
    return list(part.evidence).flatMap(e=>{
      const a=attachments.get(e?.reference);
      if(!a||used.has(a.id)||!/^image\/(png|jpeg|webp|gif|bmp)$/.test(a.mime||''))return [];
      const imageUrl=sameTaskUrl(a.url,task.id,runId,'attachment');if(!imageUrl)return [];
      used.add(a.id);return [{id:a.id,label:a.name||'原图',reference:a.id,page:1,imageUrl,bbox:[0,0,1000,1000],hotspots:[]}];
    });
  }
  function sourceLink(part,task){
    for(const e of list(part.evidence)){
      const a=list(task.attachments).find(f=>f.id===e?.reference);
      const href=a&&sameTaskUrl(a.url,task.id,'','attachment');
      if(href){const link=node('a','button link','打开本件原图');link.href=href;link.target='_blank';link.rel='noopener';return link;}
    }
    return null;
  }
  function createSource(container,view,selected,onSelect){
    const frame=node('div','part-visual-frame');
    frame.setAttribute('aria-busy','true');
    const pending=node('div','part-visual-message','正在加载零件图纸…');frame.append(pending);container.append(frame);
    const picture=svg('svg',{viewBox:view.bbox.join(' '),preserveAspectRatio:'none',role:view.hotspots.length?'group':'img','aria-label':view.label+'，图纸原始视图'});
    picture.append(svg('image',{href:view.imageUrl,x:0,y:0,width:1000,height:1000,preserveAspectRatio:'none'}));
    const marks=svg('g',{'class':'part-visual-marks'});
    for(const hot of view.hotspots){
      const mark=svg('polygon',{points:hot.polygon.map(p=>p.join(',')).join(' '),tabindex:0,role:'button','aria-label':hot.label,'aria-pressed':String(selected===hot.id),'data-hotspot':hot.id});
      mark.append(svg('title'));mark.firstChild.textContent=hot.label;
      const select=()=>onSelect(hot.id);mark.addEventListener('click',select);mark.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();select();}});marks.append(mark);
    }
    picture.append(marks);
    const image=new Image();
    image.onload=()=>{
      if(!frame.isConnected)return;
      const ratio=image.naturalWidth/image.naturalHeight*view.bbox[2]/view.bbox[3];
      frame.style.aspectRatio=String(ratio);frame.style.setProperty('--part-visual-ratio',String(ratio));frame.classList.add('loaded');frame.replaceChildren(picture);frame.setAttribute('aria-busy','false');
    };
    image.onerror=()=>{
      if(!frame.isConnected)return;
      frame.classList.add('failed');frame.replaceChildren(node('div','part-visual-message','零件视图暂时无法加载，请打开原图查看。'));frame.setAttribute('aria-busy','false');
    };
    image.src=view.imageUrl;
    if(view.hotspots.length){
      const controls=node('div','part-visual-options');controls.setAttribute('aria-label','选择图中部位');
      for(const hot of view.hotspots)controls.append(choice(hot.label,()=>onSelect(hot.id),selected===hot.id));
      container.append(controls);
    }
    const cropped=view.bbox.some((n,i)=>n!==[0,0,1000,1000][i]);
    const provenance=cropped||view.hotspots.length?view.provenance==='prepared'?' · 已整理示例，待核对':' · AI 定位，待核对':'';
    container.append(node('p','small part-visual-caption',`第 ${view.page} 页 · ${cropped?'图纸局部视图':'图纸整页'}${provenance}`));
  }
  const components=[['lid','盖板'],['body','盒体'],['bung','螺纹凸台'],['clamp','夹扣'],['seal','密封位置']];
  function createSchematic(container,saved,onChange){
    container.append(node('p','part-visual-example','已整理示例 · 装配关系示意'));
    const scene=svg('svg',{viewBox:'0 0 540 370',role:'group','aria-label':'盖板、盒体和密封位置的分层结构示意，不按比例，非制造模型','class':'part-visual-structure'}),groups={};
    const group=name=>{const g=svg('g',{'class':'part-visual-component','data-component':name});groups[name]=g;scene.append(g);return g;};
    const path=(g,cls,d)=>g.append(svg('path',{'class':cls,d}));
    const body=group('body');
    path(body,'pv-back','M176 119 L319 38 L429 101 L286 182 Z');
    path(body,'pv-side','M319 38 L429 101 L429 248 L319 185 Z');
    path(body,'pv-floor','M176 266 L319 185 L429 248 L286 329 Z');
    const opening=svg('path',{'class':'pv-opening',d:'M176 119 L319 38 L319 185 L176 266 Z M182 122 L313 48 L313 181 L182 256 Z','fill-rule':'evenodd'});body.append(opening);
    path(body,'pv-edge','M286 182 V329 M176 119 L286 182 L429 101 M176 266 L286 329');
    const bung=group('bung');
    path(bung,'pv-side','M405 126 C415 110 440 121 449 140 L464 149 C455 130 430 119 420 135 Z');
    path(bung,'pv-back','M405 126 C390 152 420 187 440 172 L455 181 C435 197 405 162 420 135 Z');
    bung.append(svg('ellipse',{'class':'pv-back',cx:443,cy:157,rx:18,ry:27,transform:'rotate(-30 443 157)'}),svg('ellipse',{'class':'pv-hole',cx:443,cy:157,rx:12,ry:21,transform:'rotate(-30 443 157)'}));
    path(bung,'pv-edge','M434 137 Q454 149 449 177 M429 139 Q448 151 444 179');
    const clamp=group('clamp');
    path(clamp,'pv-side','M224 270 L250 256 L267 275 L282 282 L275 305 L255 315 L240 293 L219 289 Z');
    path(clamp,'pv-edge','M228 276 L246 266 L257 284 L272 289 L267 300 L257 305 L245 285 L228 284 Z');
    clamp.append(svg('ellipse',{'class':'pv-hole',cx:263,cy:293,rx:5,ry:9,transform:'rotate(-30 263 293)'}));
    path(clamp,'pv-side','M229 85 L247 75 L254 85 L247 89 L241 83 L232 89 Z');
    const guides=svg('g',{'class':'pv-guides'}),top=svg('path'),bottom=svg('path');guides.append(top,bottom);scene.append(guides);
    const seal=group('seal');path(seal,'pv-seal','M176 119 L319 38 L319 185 L176 266 Z');
    const lid=group('lid');
    path(lid,'pv-side','M176 119 L319 38 L325 41 L182 122 Z M319 38 L325 41 L325 188 L319 185 Z');
    path(lid,'pv-lid','M176 119 L319 38 L319 185 L176 266 Z');
    path(lid,'pv-edge','M181 123 L314 48 L314 181 L181 256 Z');
    container.append(scene);
    const controls=node('div','part-visual-options'),description=node('p','small part-visual-caption');description.setAttribute('aria-live','polite');
    const buttons=new Map();
    const update=()=>{
      for(const [name,g] of Object.entries(groups))g.setAttribute('aria-pressed',String(name===saved.component));
      for(const [name,b] of buttons)b.setAttribute('aria-pressed',String(name===saved.component));
      const gap=saved.distance/100;
      lid.setAttribute('transform',`translate(${-106*gap},${-29*gap})`);seal.setAttribute('transform',`translate(${-45*gap},${-12*gap})`);
      top.setAttribute('d',`M${176-106*gap} ${119-29*gap} L176 119`);bottom.setAttribute('d',`M${176-106*gap} ${266-29*gap} L176 266`);
      description.textContent=components.find(([id])=>id===saved.component)[1]+' · 不按比例 · 非制造模型';
    };
    const select=name=>{saved.component=name;update();onChange();};
    for(const [name,label] of components){
      groups[name].setAttribute('tabindex','0');groups[name].setAttribute('role','button');groups[name].setAttribute('aria-label',label);
      groups[name].addEventListener('click',()=>select(name));groups[name].addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();select(name);}});
      const b=choice(label,()=>select(name));buttons.set(name,b);controls.append(b);
    }
    container.append(controls);
    const label=node('label','part-visual-range','分离距离'),output=node('output','small'),range=node('input');range.type='range';range.min='0';range.max='100';range.value=String(saved.distance);range.setAttribute('aria-label','分离距离');
    range.addEventListener('input',()=>{saved.distance=Number(range.value);output.textContent=saved.distance===0?'合拢':saved.distance+'%';update();onChange();});
    output.textContent=saved.distance===0?'合拢':saved.distance+'%';label.append(output,range);container.append(label,description);update();
  }
  function mountOne(host,part,task,runId){
    const key=JSON.stringify([task.id,runId,part.key]),views=drawingViews(part,task,runId);
    const example=part.visualization?.schematic?.kind==='prepared-example'&&part.visualization?.schematic?.template==='sealed-box-v1';
    if(!selections.has(key))selections.set(key,{mode:'source',view:views[0]?.id,hotspots:{},component:'lid',distance:80});
    const saved=selections.get(key);if(!example)saved.mode='source';if(!views.some(v=>v.id===saved.view))saved.view=views[0]?.id;
    host.className='part-visual';
    const draw=()=>{
      host.replaceChildren(node('h4','part-visual-title','零件视图'));
      if(example){const tabs=node('div','part-visual-options');tabs.setAttribute('aria-label','显示方式');for(const [id,label] of [['source','原图视图'],['structure','分层结构示意']])tabs.append(choice(label,()=>{saved.mode=id;draw();},saved.mode===id));host.append(tabs);}
      if(saved.mode==='structure'){createSchematic(host,saved,()=>{});return;}
      if(views.length>1){const buttons=node('div','part-visual-options');buttons.setAttribute('aria-label','选择零件图纸视图');for(const v of views)buttons.append(choice(v.label,()=>{saved.view=v.id;draw();},saved.view===v.id));host.append(buttons);}
      const view=views.find(v=>v.id===saved.view);
      if(view)createSource(host,view,saved.hotspots[view.id],id=>{saved.hotspots[view.id]=id;const all=host.querySelectorAll('[data-hotspot]');all.forEach(m=>m.setAttribute('aria-pressed',String(m.dataset.hotspot===id)));const controls=host.querySelector('[aria-label="选择图中部位"]');if(controls)[...controls.children].forEach((b,i)=>b.setAttribute('aria-pressed',String(view.hotspots[i].id===id)));});
      else host.append(node('p','small part-visual-caption','本件尚无可显示的页面视图，可先打开原图查看。'));
      const link=sourceLink(part,task);if(link)host.append(link);
    };
    draw();
  }
  window.HylPartVisuals={mount(root,{task,runId}={}){
    const next=task?.id&&runId?JSON.stringify([task.id,runId]):'';
    if(next!==scope){selections.clear();scope=next;}
    if(!next)return;
    const parts=new Map(list(task.recognition?.parts).map(p=>[String(p.key),p]));
    for(const host of root.querySelectorAll('[data-part-visual]')){const part=parts.get(host.dataset.partVisual);if(part)mountOne(host,part,task,runId);}
  }};
})();
