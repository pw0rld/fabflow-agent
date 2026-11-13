// Generated fixed demo controller. No model, business API or host integration calls.
import { createProcessDemo } from './process-demo'

export function mountAssembly(root) {
      const source = JSON.parse(root.querySelector('#hy-assembly-source').textContent);
      const supplement = JSON.parse(root.querySelector('#hy-engineering-supplement').textContent);
      const validPlans = ['unknown','folded','welded'];
      let state = { step:'home', part:'body', plan:'unknown', doc:'bom', seams:'corners' };
      let selectedFiles = [];
      const fileInput = root.querySelector('#hy-upload-files');
      const acceptedExtensions = new Set(['dwg','dxf','step','stp','pdf','png','jpg','jpeg','webp']);
      const saved = null;
      function restore(value) {
        if (!value || typeof value !== 'object') return;
        if (['home','input','engineering','output'].includes(value.step)) state.step=value.step;
        if (['body','lid','bracket'].includes(value.part)) state.part=value.part;
        if (validPlans.includes(value.plan)) state.plan=value.plan;
        if (['bom','cut','bend','drill','weld','route','cards'].includes(value.doc)) state.doc=value.doc;
        if (['corners','base'].includes(value.seams)) state.seams=value.seams;
      }
      restore(saved);
      const names = {body:'① 机箱主体',lid:'② 上盖板',bracket:'③ 安装支架'};
      const labels = {bom:'物料图表',cut:'激光切割图',bend:'折弯图',drill:'钻孔图',weld:'焊接作业图',route:'工序与物料汇总',cards:'工序图与作业卡'};
      const processes = createProcessDemo(root, {
        onPart: part => { state.part=part; render(); },
        onDocument: doc => { state.doc=doc; state.step='output'; render(); }
      });
      function persist() {}
      function sourceSvg(selected) {
        const y = n => 500-n;
        const art = source.groups.map(group=>`<g data-object="${group.part}" class="${selected===group.part?'hy-on':''}">${group.lines.map(row=>`<line class="hy-geometry" x1="${row[0]}" y1="${y(row[1])}" x2="${row[2]}" y2="${y(row[3])}"/>`).join('')}${(group.circles||[]).map(row=>`<circle class="hy-geometry" cx="${row[0]}" cy="${y(row[1])}" r="${row[2]}"/>`).join('')}</g>`).join('');
        const notes = source.notes.map(row=>`<text class="hy-annotation" x="${row[0]}" y="${y(row[1])}" font-size="${row[2]}">${row[3]}</text>`).join('');
        const markers = [[22,109,'1'],[22,150,'2'],[82,58,'3']].map(row=>`<text x="${row[0]}" y="${y(row[1])}" font-size="24">${row[2]}</text>`).join('');
        return `<svg class="hy-original" viewBox="0 0 740 510" role="img" aria-label="模拟总装：盖板俯视、总装正视与侧视。总装320乘220乘80毫米，材料SS304，主体制造方案、孔定位和焊接要求待补。">${art}${notes}${markers}</svg>`;
      }
      function rows(plan=state.plan) {
        const base = [['上盖板','1.5',1],['安装支架','2',2]];
        if(plan==='folded')return [['一体盒体','1.5',1],...base];
        if(plan==='welded')return [['底板','1.5',1],['长侧板','1.5',2],['短侧板','1.5',2],...base];
        return [];
      }
      function thumb(name) {
        const path=name==='一体盒体'?'M8 15L30 6L52 16L30 26ZM8 15V32L30 42L52 32V16M30 26V42':name==='安装支架'?'M15 5H20V30H50V35H15Z':'M8 15L38 5L54 20L24 32Z';
        return `<svg class="hy-thumb" viewBox="0 0 60 44" role="img" aria-label="${name}示意"><path d="${path}" fill="var(--hy-soft)" stroke="currentColor" stroke-width="1.5"/></svg>`;
      }
      function dim(x1,y1,x2,y2,label,tx,ty) {
        const horizontal=y1===y2;
        return `<path class="hy-measure" d="M${x1} ${y1}L${x2} ${y2}M${x1-(horizontal?0:5)} ${y1-(horizontal?5:0)}l${horizontal?0:10} ${horizontal?10:0}M${x2-(horizontal?0:5)} ${y2-(horizontal?5:0)}l${horizontal?0:10} ${horizontal?10:0}"/><text x="${tx}" y="${ty}" text-anchor="middle">${label}</text>`;
      }
      function sheet(kind,art) {
        return `<svg class="hy-technical" data-drawing-kind="${kind}" viewBox="0 0 560 360" role="img" aria-label="${labels[kind]}模拟草稿，非生产图">${art}</svg>`;
      }
      function sheetMeta(code) {
        return `<div class="hy-sheetmeta"><span>HY-D02 / ${code} / REV A</span><span>单位 mm · 示意，不按比例 · DEMO</span></div>`;
      }
      function bendDrawing() {
        const b=supplement.bracket;
        return sheet('bend',`<path class="hy-surface" d="M110 60H120V240Q120 250 130 250H410V260H130Q110 260 110 240Z"/>
          <path class="hy-feature hy-picked" d="M120 240Q120 250 130 250"/>
          <path class="hy-feature" d="M150 205A45 45 0 0 1 195 250"/>
          <text x="205" y="223">${b.angle_degrees}°</text><path class="hy-reference" d="M195 170L137 239"/>
          <text x="205" y="167">B01</text><text x="155" y="290">R${b.inner_radius_mm}</text>
          ${dim(110,315,410,315,String(b.outside_legs_mm[0]),260,343)}
          ${dim(72,60,72,260,String(b.outside_legs_mm[1]),40,166)}
          <text x="300" y="110">宽 ${b.width_mm}</text><text x="300" y="145">t2</text>`);
      }
      function drillDrawing() {
        const b=supplement.bracket;
        const holes=[b.hole_edge_mm,b.hole_edge_mm+b.hole_pitch_mm];
        const marks=holes.map((x,index)=>{
          const cx=100+x*6,cy=280-b.hole_offset_mm*6;
          return `<circle class="hy-hole" cx="${cx}" cy="${cy}" r="19.5"/>
            <path class="hy-reference" d="M${cx-34} ${cy}H${cx+34}M${cx} ${cy-34}V${cy+34}"/>
            <text x="${cx}" y="247" text-anchor="middle">H0${index+1}</text>`;
        }).join('');
        return sheet('drill',`<rect class="hy-surface" x="100" y="100" width="360" height="180"/>${marks}
          ${dim(100,60,220,60,String(b.hole_edge_mm),160,40)}
          ${dim(220,60,340,60,String(b.hole_pitch_mm),280,40)}
          ${dim(100,315,460,315,'60',280,344)}
          ${dim(65,100,65,280,String(b.width_mm),35,198)}
          ${dim(503,190,503,280,String(b.hole_offset_mm),528,244)}
          <path class="hy-measure" d="M100 100V48M220 100V48M340 100V48M100 280V325M460 280V325M460 190H512M460 280H512"/>
          <path class="hy-feature" d="M430 125L356 178"/><text x="438" y="113" text-anchor="middle">Ø6.5</text>
          <text x="84" y="303">A</text>`);
      }
      function weldDrawing(plan) {
        const base=plan==='welded';
        const picked=state.seams==='base'&&base?'base':'corners';
        const seam=(d,group,dashed=false)=>`<path class="hy-feature ${picked===group?'hy-picked':''}" d="${d}"${dashed?' stroke-dasharray="7 5"':''}/>`;
        return sheet('weld',`<path class="hy-surface" d="M130 95L270 175V290L130 210Z"/>
          <path class="hy-surface" d="M270 175L440 115V230L270 290Z"/>
          <path class="hy-feature" d="M130 95L300 40L440 115M140 98L300 52L427 115L270 162Z"/>
          ${seam('M130 95V210M270 175V290M440 115V230','corners')}
          ${seam('M300 40V155','corners',true)}
          ${base?seam('M130 210L270 290L440 230','base')+seam('M130 210L300 155L440 230','base',true):''}
          <path class="hy-measure" d="M92 152L130 152M290 317L270 278M477 196L440 184M347 35L300 70"/>
          <text x="52" y="156">W01</text><text x="288" y="340">W02</text><text x="478" y="201">W03</text><text x="350" y="34">W04</text>
          ${base?'<text x="40" y="311">W05–W08</text><path class="hy-measure" d="M135 296L190 245"/>':''}`);
      }
      function inspector() {
        const target=root.querySelector('#hy-inspector');
        root.querySelector('#hy-inspector-title').textContent=state.part==='body'?'主体制造方案':state.part==='lid'?'盖板参数':'支架参数';
        if(state.part==='body') target.innerHTML=`<span class="hy-pill">图纸参数</span><p>SS304 · t1.5</p><p class="hy-sub">总装包络：320 × 220 × 80 mm；本件尺寸待补。</p><div class="hy-block"><label class="hy-label" for="hy-fabrication">采用的工程方案</label><select id="hy-fabrication"><option value="unknown">尚未决定</option><option value="folded">一体折弯盒体</option><option value="welded">底板＋侧板拼焊</option></select></div><div class="hy-warning">${state.plan==='unknown'?'主体制造方案待确认。':state.plan==='folded'?'待补：内 R、展开依据、零件尺寸链。':'待补：板件尺寸、接缝、连接要求。'}</div>`;
        else if(state.part==='lid') target.innerHTML='<span class="hy-pill">图纸参数</span><p>SS304 · 320 × 220 × 1.5 mm</p><div class="hy-block"><h3>关联资料</h3><p class="hy-sub">切割图草稿</p><p class="hy-sub">折弯：不适用；加工与装配条件待校核。</p></div><button type="button" class="cursor-interaction" data-preview-cut style="margin-top:14px">查看切割图 →</button>';
        else target.innerHTML='<span class="hy-pill">图纸参数</span><p>SS304 · t2 · 示意 2 件</p><p class="hy-sub">每件 2 × Ø6.5 通孔</p><div class="hy-warning">孔定位基准与尺寸未标注。</div><div class="hy-block"><h3>待补参数</h3><p class="hy-sub">孔距、边距、加工面、折弯几何。</p><p class="hy-sub">折弯图、钻孔图：已提供模拟补图示例。</p></div>';
        const select=target.querySelector('select');if(select)select.value=state.plan;
      }
      function decomposition() {
        const r=rows();
        root.querySelector('#hy-decomposition').innerHTML=r.length?`<p style="margin-top:8px">${r.map(row=>row[0]+' ×'+row[2]).join('　·　')}</p><p class="hy-sub">按当前演示方案：${r.length} 类 · ${r.reduce((n,row)=>n+row[2],0)} 件/套 · 10 套共 ${r.reduce((n,row)=>n+row[2]*10,0)} 件</p><p class="hy-notice">几何与工艺状态：待补充</p>`:'<p class="hy-sub">主体拆分方案待确认，制造零件数量待定。</p>';
      }
      function output() {
        root.querySelector('#hy-doc-title').textContent=labels[state.doc];
        const preview=root.querySelector('#hy-doc-preview');
        const status=root.querySelector('#hy-doc-state');
        const previewPlan=state.plan==='unknown'?'folded':state.plan;
        root.querySelector('#hy-output-summary').textContent='工序图、作业卡与原样例图纸 · 模拟草稿待校核';
        if(state.doc==='route'||state.doc==='cards'){
          status.textContent='模拟草稿 · 待校核';preview.innerHTML='<div data-process-output data-process-ui></div>';return;
        }
        if(state.doc==='bom'){
          const r=rows(previewPlan);status.textContent='模拟草稿 · 待校核';
          preview.innerHTML=`<div class="hy-status-line"><h3>${previewPlan==='folded'?'一体折弯方案':'分板拼焊方案'}</h3><span class="hy-pill">${r.length} 类 · ${r.reduce((n,row)=>n+row[2]*source.sets,0)} 件</span></div>
            <div class="hy-table-scroll"><table data-output-bom><thead><tr><th>示意</th><th>制造零件</th><th>材料 / 板厚</th><th>每套</th><th>10 套</th></tr></thead><tbody>${r.map(row=>`<tr><td>${thumb(row[0])}</td><td>${row[0]}</td><td>SS304 / ${row[1]} mm</td><td>${row[2]}</td><td>${row[2]*source.sets}</td></tr>`).join('')}</tbody></table></div>
            <p class="hy-source-note">工程拆分：${state.plan==='unknown'?'一体折弯为预览示例，尚未采用。':'当前采用的模拟方案；几何与工艺待校核。'} 数量按 10 套汇总。</p>${sheetMeta('BOM')}`;
        } else if(state.doc==='cut') {
          status.textContent='模拟草稿 · 待校核';
          preview.innerHTML='<div class="hy-status-line"><h3>上盖板 / SS304 · t1.5 · 10 件</h3><span class="hy-pill">尺寸来源：总装标注</span></div><svg class="hy-cut" data-drawing-kind="cut" viewBox="0 0 480 350" role="img" aria-label="盖板切割草稿，闭合矩形320乘220毫米，无附加孔，模拟非生产图"><rect x="80" y="50" width="320" height="220" fill="none" stroke="currentColor" stroke-width="2"/><path d="M80 300H400M50 50V270" stroke="currentColor" fill="none"/><text x="240" y="325" text-anchor="middle" font-size="19" fill="currentColor">320 mm</text><text x="24" y="165" transform="rotate(-90 24 165)" text-anchor="middle" font-size="19" fill="currentColor">220 mm</text><text x="240" y="165" text-anchor="middle" font-size="17" fill="currentColor">DEMO · 待工程校核</text></svg><p class="hy-sub">用途：激光排版；内容：平面轮廓、材料、板厚、数量。</p>'+sheetMeta('LID-CUT');
        } else if(state.doc==='bend') {
          status.textContent='模拟草稿 · 待校核';
          preview.innerHTML=`<h3>安装支架 / B01</h3>${bendDrawing()}<div class="hy-feature-row"><span class="hy-tag">B01 · 90° · 向上</span><span class="hy-pill">SS304 · t2 · 20 件</span></div><p class="hy-source-note">原图：SS304、t2。模拟补图：外边长 60/40、宽 30、内角 90°、内 R2。</p><div class="hy-status-line"><span class="hy-sub">待核对：零件尺寸链、角度口径、内 R、展开依据。</span><button type="button" class="cursor-interaction" data-fix="bracket">查看支架参数 →</button></div>${sheetMeta('BRACKET-BEND')}`;
        } else if(state.doc==='drill') {
          status.textContent='模拟草稿 · 待校核';
          preview.innerHTML=`<h3>安装支架 / H01、H02</h3>${drillDrawing()}<div class="hy-feature-row"><span class="hy-pill">2 × Ø6.5 通孔 / 件</span><span class="hy-pill">板厚 2 · 20 件 · 共 40 孔</span></div><p class="hy-sub">加工视图：底面局部，成形前示例；A 为定位基准。</p><p class="hy-source-note">原图：孔径、通孔要求、板厚。模拟补图：边距 20、孔距 20、横向偏距 15。</p><div class="hy-status-line"><span class="hy-sub">待核对：定位基准、孔位尺寸、公差、加工状态。</span><button type="button" class="cursor-interaction" data-fix="bracket">查看支架参数 →</button></div>${sheetMeta('BRACKET-DRILL')}`;
        } else {
          status.textContent='模拟草稿 · 待校核';
          const base=previewPlan==='welded';
          preview.innerHTML=`<h3>盒体装配 / ${base?'分板拼焊':'一体折弯'}示例</h3>${weldDrawing(previewPlan)}
            <div class="hy-feature-row"><button type="button" class="cursor-interaction" data-seams="corners" aria-pressed="${state.seams!=='base'||!base}">W01–W04 · 角部接头</button>${base?`<button type="button" class="cursor-interaction" data-seams="base" aria-pressed="${state.seams==='base'}">W05–W08 · 底板周边</button>`:''}</div>
            <p class="hy-sub">焊缝形式：连续（演示假设）；背面接头以虚线表示。</p><p class="hy-source-note">原图要求：外露焊缝焊后修整。模拟补图：接头位置、编号、连续焊缝形式${state.plan==='unknown'?'；主体方案尚未采用':''}。</p>
            <div class="hy-status-line"><span class="hy-sub">待核对：实际接头、焊接方法、尺寸、质量要求。</span><button type="button" class="cursor-interaction" data-fix="body">查看主体方案 →</button></div>${sheetMeta('BODY-WELD')}`;
        }
      }
      function render(){
        root.querySelectorAll('[data-page]').forEach(el=>el.hidden=el.dataset.page!==state.step);
        root.querySelector('.hy-steps').hidden=state.step==='home';
        root.querySelector('[data-home]').hidden=state.step==='home';
        root.querySelector('#hy-footer-context').textContent=state.step==='home'?'工程拆图工作台':'HY-D02 · 总装拆图样例 · 固定模拟结果';
        root.querySelectorAll('[data-step]').forEach(el=>{if(el.dataset.step===state.step)el.setAttribute('aria-current','step');else el.removeAttribute('aria-current');});
        root.querySelectorAll('[data-part]').forEach(el=>el.setAttribute('aria-pressed',String(el.dataset.part===state.part)));
        root.querySelector('[data-part="body"] small').textContent=state.plan==='unknown'?'制造构成待定':state.plan==='folded'?'方案：一体折弯':'方案：分板拼焊';
        root.querySelectorAll('[data-doc]').forEach(el=>el.setAttribute('aria-pressed',String(el.dataset.doc===state.doc)));
        root.querySelector('[data-drawing="input"]').innerHTML=sourceSvg(null);
        root.querySelector('[data-drawing="engineering"]').innerHTML=sourceSvg(state.part);
        root.querySelector('#hy-selected-label').textContent=names[state.part];
        inspector();decomposition();output();processes.render(state.part,state.doc);
      }
      function renderFiles(rejected=0){
        const list=root.querySelector('#hy-selected-files');
        list.replaceChildren();
        selectedFiles.forEach((file,index)=>{
          const row=document.createElement('li'),info=document.createElement('div'),name=document.createElement('span'),meta=document.createElement('small'),remove=document.createElement('button');
          info.className='hy-file-info';name.textContent=file.name;
          meta.textContent=(file.size<1024?file.size+' B':file.size<1048576?(file.size/1024).toFixed(1)+' KB':(file.size/1048576).toFixed(1)+' MB')+' · 已选择，暂未上传';
          remove.type='button';remove.textContent='移除';remove.dataset.removeFile=String(index);remove.setAttribute('aria-label','移除 '+file.name);
          info.append(name,meta);row.append(info,remove);list.append(row);
        });
        const status=selectedFiles.length?'已选择 '+selectedFiles.length+' 份文件，仅保留在当前页面；尚未上传或解析。':'当前演示版仅展示文件选择，暂未接入上传与解析。';
        root.querySelector('#hy-upload-status').textContent=status+(rejected?' 有 '+rejected+' 份文件格式不支持，请选择 CAD、PDF 或图片。':'');
      }
      function selectFiles(files){
        let rejected=0;
        for(const file of files){
          if(!acceptedExtensions.has(file.name.split('.').pop().toLowerCase())){rejected++;continue;}
          if(!selectedFiles.some(item=>item.name===file.name&&item.size===file.size&&item.lastModified===file.lastModified))selectedFiles.push(file);
        }
        fileInput.value='';renderFiles(rejected);
      }
      function onDragOver(event){
        if(!Array.from(event.dataTransfer?.types||[]).includes('Files'))return;
        event.preventDefault();
        if(state.step==='home'&&event.target.closest('[data-dropzone]'))root.querySelector('[data-dropzone]').dataset.dragging='true';
      }
      function onDragLeave(event){
        const zone=root.querySelector('[data-dropzone]');
        if(!zone.contains(event.relatedTarget))delete zone.dataset.dragging;
      }
      function onDrop(event){
        if(!event.dataTransfer?.files?.length)return;
        event.preventDefault();delete root.querySelector('[data-dropzone]').dataset.dragging;
        if(state.step==='home'&&event.target.closest('[data-dropzone]'))selectFiles(event.dataTransfer.files);
      }
      root.addEventListener('dragover',onDragOver);
      root.addEventListener('dragleave',onDragLeave);
      root.addEventListener('drop',onDrop);
      const onClick = event => {
        const button=event.target.closest('button');if(!button)return;
        if(event.defaultPrevented||button.closest('[data-process-ui]'))return;
        if(button.hasAttribute('data-choose-files')){fileInput.click();return;}
        if(button.dataset.removeFile!==undefined){selectedFiles.splice(Number(button.dataset.removeFile),1);renderFiles();return;}
        if(button.hasAttribute('data-home'))state.step='home';
        if(button.hasAttribute('data-open-demo')){state={step:'input',part:'body',plan:'unknown',doc:'bom',seams:'corners'};processes.reset();}
        if(button.dataset.step||button.dataset.go)state.step=button.dataset.step||button.dataset.go;
        if(button.dataset.part){state.part=button.dataset.part;processes.selectOriginalPart(state.part);}
        if(button.dataset.doc)state.doc=button.dataset.doc;
        if(['corners','base'].includes(button.dataset.seams))state.seams=button.dataset.seams;
        if(button.dataset.fix){state.part=button.dataset.fix;state.step='engineering';processes.selectOriginalPart(state.part);root.querySelector('#hy-original-details').open=true;}
        if(button.hasAttribute('data-preview-cut')){state.doc='cut';state.step='output';}
        render();persist();
      };
      const onChange = event => {if(event.target===fileInput){selectFiles(fileInput.files);return;}if(event.target.id==='hy-fabrication'&&validPlans.includes(event.target.value)){state.plan=event.target.value;render();persist();}};
      root.addEventListener('click', onClick);
      root.addEventListener('change', onChange);
      render();
      root.dataset.uiReady = 'true';
      root.setAttribute('aria-busy', 'false');
      return () => {
        root.removeEventListener('click', onClick);
        root.removeEventListener('change', onChange);
        root.removeEventListener('dragover', onDragOver);
        root.removeEventListener('dragleave', onDragLeave);
        root.removeEventListener('drop', onDrop);
        processes.destroy();
        selectedFiles = [];
      };
}
