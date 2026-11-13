// Curated demonstration data only. No uploaded content, factory rates or AI calls.
const definitions = [
  ['shear','数剪（落料）','下料','cut','分钟'],
  ['punch_setup','普冲（装夹）','冲压','setup','分钟'],
  ['punch','普冲（成型）','冲压','punch','分钟'],
  ['cnc_setup','数控冲床（装夹）','冲压','setup','分钟'],
  ['cnc','数控冲床（冲）','冲压','punch','分钟'],
  ['bend_setup','数控折弯（装夹）','折弯','setup','分钟'],
  ['bend','数控折弯（折）','折弯','bend','分钟'],
  ['countersink','倒沉孔','孔加工','hole','分钟'],
  ['extrude','拉伸孔','孔加工','hole','分钟'],
  ['tap','攻丝','孔加工','hole','分钟'],
  ['rivet','压铆','连接','rivet','分钟'],
  ['deburr','去毛刺','修整','finish','分钟'],
  ['weld','焊接','连接','weld','分钟'],
  ['spot','点焊','连接','weld','分钟'],
  ['fusion','熔焊','连接','weld','分钟'],
  ['reshape','整形','修整','reshape','分钟'],
  ['file','锉刀','修整','finish','分钟'],
  ['ream','扩孔','孔加工','hole','分钟'],
  ['grind','钳打磨','修整','finish','分钟'],
  ['paint','喷涂（油漆/塑粉）','表面与标识','surface','平方米'],
  ['galvanize','镀锌','表面与标识','surface','平方分米'],
  ['silk','丝印','表面与标识','mark','分钟'],
  ['assemble','装配','装配与辅助','assembly','分钟'],
  ['aux','辅助','装配与辅助','aux','分钟'],
  ['laser','激光切割','下料','cut','计费口径待定'],
  ['drill','钻孔','孔加工','hole','分钟'],
  ['polish','抛光','修整','finish','分钟']
];
export const PROCESS_CATALOG = definitions.map(([id,name,category,kind,unit],i)=>({id,name,category,kind,unit,fromQuote:i<24}));
const byId=Object.fromEntries(PROCESS_CATALOG.map(p=>[p.id,p]));
const statusNames={needed:'需要',pending:'待确认',na:'不适用'};
const step=(op,status='needed')=>({op,status});
const examples = {
  ss304:{name:'SS304 机箱',code:'HY-D02',description:'原总装样例的工序补充；新增路线与标注均为模拟，主体制造方案仍待确认。',sets:10,parts:[
    {id:'body',name:'机箱主体',material:'SS304',thickness:1.5,qty:10,quantityNote:'结构部件；制造构成待定',width:320,height:220,allowed:['laser','bend_setup','bend','weld','spot','fusion','deburr','grind','polish','reshape','file','assemble','aux'],route:[step('weld','pending'),step('grind','pending'),step('assemble')],accessories:[]},
    {id:'lid',name:'上盖板',material:'SS304',thickness:1.5,qty:10,width:320,height:220,allowed:['laser','shear','deburr','polish','file','aux'],route:[step('laser'),step('deburr')],accessories:[]},
    {id:'bracket',name:'安装支架',material:'SS304',thickness:2,qty:20,width:60,height:30,allowed:['laser','bend_setup','bend','drill','deburr','polish','file','aux'],route:[step('laser'),step('deburr'),step('bend_setup'),step('bend','pending'),step('drill','pending')],accessories:[]}
  ]},
  spcc:{name:'SPCC 设备壳体',code:'HY-P01',description:'独立模拟样例；下列零件和几何为演示编排，不是从 HY-D02 总装中识别出来的。',sets:10,parts:[
    {id:'housing',name:'折弯壳体',material:'SPCC',thickness:1.5,qty:10,width:300,height:200,allowed:['shear','laser','punch_setup','punch','cnc_setup','cnc','bend_setup','bend','rivet','weld','spot','fusion','deburr','reshape','file','grind','paint','assemble','aux'],route:[step('cnc_setup'),step('cnc'),step('deburr'),step('bend_setup'),step('bend'),step('rivet'),step('weld','pending'),step('grind'),step('paint'),step('assemble')],accessories:[{name:'M4 压铆螺母',perPiece:4},{name:'M4 装配螺钉',perPiece:4}]},
    {id:'panel',name:'丝印面板',material:'SPCC',thickness:1.5,qty:10,width:300,height:200,allowed:['shear','laser','cnc_setup','cnc','countersink','deburr','file','paint','silk','aux'],route:[step('laser'),step('countersink','pending'),step('deburr'),step('paint'),step('silk')],accessories:[]},
    {id:'rail',name:'镀锌安装条',material:'SPCC',thickness:2,qty:20,width:120,height:35,allowed:['shear','laser','drill','tap','deburr','file','galvanize','aux'],route:[step('shear'),step('drill'),step('tap'),step('deburr'),step('galvanize')],accessories:[]}
  ]},
  holes:{name:'孔加工安装板',code:'HY-P02',description:'独立孔加工样例；H01—H04 是不同特征，参数均为模拟，拉伸孔的成型条件待补。',sets:10,parts:[
    {id:'plate',name:'多工艺安装板',material:'Q235',thickness:3,qty:10,width:120,height:80,allowed:['shear','laser','punch_setup','punch','cnc_setup','cnc','drill','countersink','extrude','tap','ream','deburr','file','grind','reshape','aux'],route:[step('shear'),step('drill'),step('ream'),step('tap'),step('countersink','pending'),step('extrude','pending'),step('deburr'),step('aux','pending')],accessories:[]}
  ]}
};
const esc=value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const btn=(text,attrs='',selected=false)=>`<button type="button" ${attrs} aria-pressed="${selected}">${esc(text)}</button>`;

export function createProcessDemo(root, callbacks) {
  let model,doc='bom',sourcePart='body';
  const urls=new Set();
  function reset(){model={example:'ss304',part:'body',op:'weld',feature:null,face:'front',exploded:true,routes:Object.fromEntries(Object.entries(examples).map(([key,example])=>[key,Object.fromEntries(example.parts.map(p=>[p.id,p.route.map(s=>({...s}))]))]))};}
  reset();
  const example=()=>examples[model.example];
  const part=()=>example().parts.find(p=>p.id===model.part);
  const route=()=>model.routes[model.example][model.part];
  function selectOriginalPart(id){model.example='ss304';model.part=id;model.op=route()[0]?.op||'laser';model.feature=null;sourcePart=id;}
  function setExample(id){model.example=id;model.part=example().parts[0].id;model.op=route()[0].op;model.feature=null;}
  const inRoute=()=>route().find(s=>s.op===model.op);
  const isReference=()=>!part().allowed.includes(model.op);
  function drawingPart(){
    if(!isReference())return part();
    for(const x of Object.values(examples))for(const p of x.parts)if(p.allowed.includes(model.op))return p;
    return part();
  }
  function measurement(op,p){
    if(op==='rivet')return `4 点/件 · 本批 ${4*p.qty} 点`;
    if(['tap','countersink','extrude','ream'].includes(op))return `指定孔特征 · ${p.qty} 件（位置待校核）`;
    if(op==='drill')return p.id==='bracket'?`2 孔/件 · 本批 ${2*p.qty} 孔`:`孔数与前后工步待核对 · ${p.qty} 件`;
    if(op==='paint'||op==='galvanize'){
      const sqm=2*p.width*p.height*p.qty/1e6;
      return (op==='paint'?sqm.toFixed(3)+' 平方米':(sqm*100).toFixed(2)+' 平方分米')+'（两大面净面积示例）';
    }
    if(byId[op].kind==='setup')return '本批准备；装夹次数、分钟数待核定';
    if(['weld','spot','fusion'].includes(op))return `接头/焊点位置示例 · ${p.qty} 件；长度与工时待补`;
    return `${p.qty} 件 · 工程量及分钟数待核定`;
  }
  function requirements(op){
    return ({
      shear:'下料边界与尺寸待校核；不能将总装投影当作展开轮廓。',laser:'轮廓与孔槽分开；排版、刀缝补偿和机器程序尚未接入。',
      punch:'成型区示意；模具、行程及成型尺寸待工程师确定。',cnc:'蓝色孔槽为冲加工区域；工步、模具和装夹另列。',
      bend:'B01 为 90° 内夹角示例；内 R、方向、展开及模具仍需核对。',
      countersink:'H01 为倒沉孔位置；上口尺寸、角度与配合紧固件待核对。',
      extrude:'H04 为拉伸孔参考剖面；孔径、高度、材料成型条件待补，不是已完成的生产图。',
      tap:'H02 为 M6 螺纹示例；底孔、有效牙深与通盲要求待核对。',
      ream:'H03 为目标 Ø8 示例；预孔、尺寸公差及加工方式待核对。',drill:'孔中心与加工面待校核；钻孔、扩孔、攻丝可引用同一特征，避免重复计孔。',
      rivet:'R01—R04 安装 M4 压铆螺母（演示选型）；安装面、方向和适用板厚待核对。',
      weld:'接头位置为模拟；厂内焊接方法、焊缝尺寸和验收要求待确认。',spot:'S01—S04 为点焊位置示例；搭接、点距和工艺参数待确认。',fusion:'保留报价表“熔焊”原称；与“焊接”的厂内定义及计费边界待确认。',
      deburr:'处理外轮廓与孔口；去除毛刺，保留尺寸与配合要求。',grind:'只修整高亮焊缝区域；焊缝余高、外观及不得减薄区域待确认。',file:'手工修整指定边缘；“锉刀”的厂内适用范围与完成要求待确认。',polish:'高亮表面为处理范围；粗糙度、纹理方向与不处理区待确认。',reshape:'示意校正前后形状；目标平面度、方法和检验基准待确认。',
      paint:'灰色涂层为模拟要求；白色圈为遮蔽区。两大面净面积不等于已确认计价面积。',galvanize:'安装条镀锌示例；计量用平方分米。实际表面积、镀层要求和外协口径待确认。',silk:'POWER、ON/OFF 为模拟标识；位置、方向、颜色和图案版本待校核。',
      assemble:'对应螺钉与连接位置；装配顺序、配合、紧固与检查要求待校核。',aux:'搬运/翻面仅作辅助工作举例；明确是否已包含在其他工序中，避免重复计时。'
    })[op]||'本批装夹作业；定位面、装夹次数、准备时间及加工费包含关系待确认。';
  }
  function features(){
    const p=byId[model.op];
    if(p.kind==='rivet')return ['R01','R02','R03','R04'];
    if(p.kind==='weld')return model.op==='spot'?['S01','S02','S03','S04']:['W01','W02','W03','W04'];
    if(p.kind==='hole')return model.op==='drill'?['H01','H02']:[{countersink:'H01',tap:'H02',ream:'H03',extrude:'H04'}[model.op]];
    if(p.kind==='surface')return ['正面','背面'];
    if(p.kind==='finish')return ['处理区域'];
    if(p.kind==='bend')return ['B01'];
    return [];
  }
  function art(){
    const op=byId[model.op],p=drawingPart(),f=model.feature||features()[0],selected=id=>id===f?'hy-op-picked':'';
    const label=(x,y,text)=>`<text x="${x}" y="${y}">${esc(text)}</text>`;
    const panel='<rect class="hy-op-surface" x="100" y="95" width="360" height="220" rx="3"/>';
    const holes=[[140,135],[420,135],[140,275],[420,275]];
    let image='';
    if(op.kind==='cut'||op.kind==='punch'){
      image=panel+`<path class="hy-op-feature hy-op-picked" d="M100 95H460V315H100Z"/>`;
      if(op.kind==='punch')image+=holes.map(([x,y])=>`<circle class="hy-op-hole" cx="${x}" cy="${y}" r="13"/>`).join('')+'<rect class="hy-op-hole" x="225" y="165" width="110" height="35" rx="16"/>';
      image+=label(100,65,op.id==='punch'?'成型区域参考；几何待补':'平面轮廓示意 · 非展开结果')+label(125,355,`${p.width} × ${p.height} · t${p.thickness} · 模拟标注`);
    }else if(op.kind==='setup'){
      image=panel+'<path class="hy-op-feature hy-op-picked" d="M65 150H130V185H65ZM430 235H495V270H430Z"/><path class="hy-op-guide" d="M280 65V345M75 205H490"/>'+label(85,65,'C01 / C02 · 定位与装夹')+label(150,355,'准备工步与加工工步分别记录');
    }else if(op.kind==='bend'){
      image='<path class="hy-op-surface" d="M155 75H170V265Q170 280 185 280H465V295H185Q155 295 155 265Z"/><path class="hy-op-feature hy-op-picked" d="M170 265Q170 280 185 280"/><path class="hy-op-feature" d="M205 225A55 55 0 0 1 260 280"/>'+label(275,240,'B01 · 90°')+label(100,335,'内角与方向为模拟 · 内 R / 展开待核对');
    }else if(op.kind==='hole'){
      const special=['countersink','tap','ream','extrude'].includes(op.id);
      const ids=special?['H01','H02','H03','H04']:['H01','H02'];
      image=panel+ids.map((id,i)=>{const [x,y]=holes[i];return `<g class="hy-op-feature ${selected(id)}" data-feature-shape="${id}"><circle class="hy-op-hole" cx="${x}" cy="${y}" r="15"/>${i===0&&special?`<circle fill="none" cx="${x}" cy="${y}" r="23"/>`:''}${i===1&&special?`<path d="M${x-18} ${y-12}l36 24m-36-8 36 24"/>`:''}<path class="hy-op-guide" d="M${x-30} ${y}h60m-30-30v60"/></g>${label(x-23,y+45,id)}`;}).join('');
      image+=label(110,65,special?'不同孔特征；高亮为当前工步':'钻孔位置示例');
      if(op.id==='extrude')image+='<path class="hy-op-feature hy-op-picked" d="M145 355H220V330H245V355H325"/>'+label(345,359,'参考剖面 · 高度待补');
      else image+=label(115,365,op.id==='tap'?'H02 · M6（模拟要求）':op.id==='ream'?'H03 · Ø8（模拟目标）':op.id==='countersink'?'H01 · 上口尺寸 / 角度待补':p.id==='bracket'?'2 × Ø6.5 / 件；定位尺寸待核对':'孔数 / 孔径及前后工步待核对');
    }else if(op.kind==='rivet'){
      image=panel+holes.map(([x,y],i)=>`<g class="hy-op-feature ${selected('R0'+(i+1))}" data-feature-shape="R0${i+1}"><circle class="hy-op-hole" cx="${x}" cy="${y}" r="17"/><circle fill="none" cx="${x}" cy="${y}" r="8"/></g>${label(x+24,y+7,'R0'+(i+1))}`).join('')+label(100,65,'压铆面与安装点 · M4 螺母示例')+'<path class="hy-op-feature" d="M510 120V175m-8-10 8 10 8-10M495 205H525V250H495Z"/>'+label(488,280,'压入')+label(110,355,`4 个/件 · 本批 ${p.qty*4} 个 · 安装方向待校核`);
    }else if(op.kind==='weld'){
      image='<path class="hy-op-surface" d="M120 130L285 65L475 145L310 210ZM120 130V270L310 350L475 285V145M310 210V350"/>';
      const edges=['M120 130V270','M310 210V350','M475 145V285','M285 65V205'];
      image+=edges.map((d,i)=>`<path class="hy-op-feature ${selected((op.id==='spot'?'S':'W')+'0'+(i+1))}" data-feature-shape="${(op.id==='spot'?'S':'W')+'0'+(i+1)}" d="${d}" ${i===3?'stroke-dasharray="7 5"':''}/>`).join('');
      if(op.id==='spot')image+=[[120,190],[310,270],[475,205],[285,125]].map(([x,y],i)=>`<circle class="hy-op-hole ${selected('S0'+(i+1))}" cx="${x}" cy="${y}" r="10"/>`).join('');
      image+=label(100,40,op.id==='spot'?'S01—S04 · 焊点示例':'W01—W04 · 接头示例');
    }else if(op.kind==='finish'){
      image=panel+(op.id==='grind'?'<path class="hy-op-feature hy-op-picked" d="M112 105L450 305"/>':op.id==='polish'?'<rect class="hy-op-feature hy-op-picked" x="130" y="125" width="300" height="160"/>':'<path class="hy-op-feature hy-op-picked" d="M100 95H460V315H100Z"/>')+label(100,65,op.id==='grind'?'焊缝修整区域':op.id==='polish'?'指定表面 · 纹理与要求待补':'外缘与孔口修整')+label(100,360,'高亮区域为模拟作业范围');
    }else if(op.kind==='surface'){
      image=panel+`<rect class="hy-op-feature hy-op-picked" x="110" y="105" width="340" height="200" style="fill:#c9dfef"/>`+holes.map(([x,y])=>`<circle class="hy-op-hole" cx="${x}" cy="${y}" r="18"/>`).join('')+label(100,65,(f==='背面'?'背面':'正面')+' · 处理面 / 白色圈为遮蔽示例')+label(100,355,op.id==='paint'?'灰色喷涂示例 · 计量：平方米':'镀锌示例 · 计量：平方分米');
    }else if(op.kind==='mark'){
      image=panel+'<rect class="hy-op-feature hy-op-picked" x="155" y="130" width="240" height="100" stroke-dasharray="6 4"/>'+label(185,170,'POWER')+label(185,207,'ON / OFF')+'<path class="hy-op-guide" d="M100 345V125M100 345H395M155 345V125"/>'+label(165,365,'X40 / Y30 · 模拟定位')+label(100,65,'丝印图案与方向示例');
    }else if(op.kind==='assembly'){
      const offset=model.exploded?-65:0;
      image='<path class="hy-op-surface" d="M130 185L300 125L470 195L300 260ZM130 185V285L300 360L470 290V195M300 260V360"/>'+
        `<g transform="translate(0 ${offset})"><path class="hy-op-surface" d="M120 173L300 110L480 182L300 248Z"/>${[[160,170],[300,130],[440,180],[300,225]].map(([x,y],i)=>`<circle class="hy-op-hole" cx="${x}" cy="${y}" r="8"/>${label(x-8,y-18,String(i+1))}`).join('')}</g>`+label(100,40,model.exploded?'盖板 / 壳体 / 紧固件 · 分解示意':'装配关系示意');
    }else if(op.kind==='reshape'){
      image='<path class="hy-op-feature" d="M110 150Q285 80 460 150M110 280H460"/><path class="hy-op-guide" d="M280 185V245m-8-10 8 10 8-10"/>'+label(110,65,'整形参考 · 非变形计算')+label(110,330,'校正方法、平面度及检查基准待核定');
    }else{
      image='<rect class="hy-op-surface" x="80" y="150" width="140" height="100"/><rect class="hy-op-surface" x="350" y="150" width="140" height="100"/><path class="hy-op-feature" d="M240 200H330m-15-10 15 10-15 10"/>'+label(105,210,'翻面 / 搬运')+label(375,210,'交接 / 检查')+label(90,65,'辅助工作举例；包含关系待确认');
    }
    return `<svg class="hy-op-drawing" data-operation-drawing="${op.id}" viewBox="0 0 600 420" role="img" aria-label="${esc(op.name)}模拟示意，非生产图">${image}<text class="hy-op-watermark" x="100" y="405">DEMO · 模拟补图 · 不按比例 · 待工程校核</text></svg>`;
  }
  function selector(){return `<div class="hy-op-toolbar"><label>工序样例 <select data-process-example aria-label="工序样例">${Object.entries(examples).map(([id,x])=>`<option value="${id}" ${model.example===id?'selected':''}>${x.code} · ${x.name}</option>`).join('')}</select></label><span class="hy-tag">固定模拟 · 未调用 AI</span></div><p class="hy-notice">${esc(example().description)}</p>`;}
  function partButtons(){return `<div class="hy-op-parts" aria-label="工序样例零件">${example().parts.map(p=>btn(p.name,`data-process-part="${p.id}"`,p.id===model.part)+`<small>${esc(p.material)} · t${p.thickness} · ${p.qty} 件${p.quantityNote?' · '+esc(p.quantityNote):''}</small>`).join('')}</div>`;}
  function routeButtons(){return `<div class="hy-op-route" aria-label="本件工艺路线">${route().map((s,i)=>`<button type="button" data-process-op="${s.op}" data-route-state="${s.status}" aria-pressed="${model.op===s.op}"><span>${i+1}. ${esc(byId[s.op].name)}</span><small>${statusNames[s.status]}</small></button>`).join('')}</div>`;}
  function detail(){
    const op=byId[model.op],p=drawingPart(),s=inRoute(),reference=isReference();
    const featureButtons=features().map(f=>btn(f,`data-process-feature="${esc(f)}"`,(model.feature||features()[0])===f)).join('');
    const mutable=s&&!reference;
    return `<div class="hy-op-layout"><div class="hy-panel"><div class="hy-panel-title"><h3>${esc(op.name)}</h3><span class="hy-pill">${reference?'目录参考 · 未关联本件':s?'本件作业示例':'目录预览 · 未加入路线'}</span></div>${art()}<div class="hy-body"><div class="hy-feature-row">${featureButtons}${op.kind==='assembly'?btn(model.exploded?'查看装配状态':'查看分解状态','data-process-explode'):''}</div><p class="hy-notice">${reference?'独立参考示意；本件未采用此工序。':'模拟工序图与作业要求；不代表已从客户 CAD 自动生成。'}</p></div></div>
      <aside class="hy-panel"><div class="hy-panel-title"><h3>工序作业卡</h3><span class="hy-tag">待校核</span></div><div class="hy-body"><dl class="hy-meta"><dt>对象</dt><dd>${esc(p.name)}${reference?'（参考样例）':''}</dd><dt>材料</dt><dd>${esc(p.material)} · t${p.thickness}</dd><dt>工程量</dt><dd data-process-quantity>${esc(measurement(op.id,p))}</dd><dt>计费单位</dt><dd>${esc(op.unit)}</dd><dt>工时 / 费率</dt><dd>待工厂核定</dd></dl><p class="hy-source-note">${esc(requirements(op.id))}</p>
      ${mutable?`<label class="hy-label" style="margin-top:14px">适用状态<select data-process-status aria-label="当前工序适用状态">${Object.entries(statusNames).map(([k,v])=>`<option value="${k}" ${s.status===k?'selected':''}>${v}（模拟）</option>`).join('')}</select></label><div class="hy-feature-row">${btn('上移','data-process-move="-1" '+(route().indexOf(s)===0?'disabled':''))}${btn('下移','data-process-move="1" '+(route().indexOf(s)===route().length-1?'disabled':''))}</div>`:reference?'<p class="hy-notice">本件未配置适用条件，可切换对应样例查看。</p>':btn('加入本件路线（模拟）','data-process-add')}
      <div class="hy-block">${btn('下载当前示意 SVG','data-process-download-svg')}</div></div></aside></div>`;
  }
  function catalog(){return `<details class="hy-op-catalog"><summary>全部工序 · 8 类 24 项 + 3 项补充</summary><p class="hy-notice">点击查看参考。加入路线前保留待确认；目录不表示每件都需要加工。</p><div class="hy-op-catalog-grid">${[...new Set(PROCESS_CATALOG.map(p=>p.category))].map(category=>`<section><h3>${category}</h3><div class="hy-op-catalog-items">${PROCESS_CATALOG.filter(p=>p.category===category).map(p=>btn(p.name+(p.fromQuote?'':' · 补充'),`data-catalog-op="${p.id}"`,model.op===p.id)).join('')}</div></section>`).join('')}</div></details>`;}
  function workspace(){return `${selector()}<div class="hy-op-work"><aside><h3>零件</h3>${partButtons()}</aside><div class="hy-op-main"><h3>${esc(part().name)} · 工艺路线</h3>${routeButtons()}${detail()}</div></div>${catalog()}<div class="hy-bottom"><span>顺序可调整；准备工步单独记录，分钟数和价格待核定。</span>${btn('查看工序汇总 →','data-process-document="route"')}</div>`;}
  function summaryRows(){return example().parts.flatMap(p=>model.routes[model.example][p.id].map((s,i)=>({p,s,index:i+1})));}
  function overview(){
    const rows=summaryRows(),active=rows.filter(x=>x.s.status!=='na');
    return `${selector()}<div class="hy-status-line"><h3>${esc(example().name)} · 10 套</h3><span class="hy-pill">${active.length} 个适用/待确认工步 · ${active.filter(x=>x.s.status==='pending').length} 项待确认</span>${btn('下载工序 CSV','data-process-download-csv')}</div><h3 class="hy-block">物料与辅料（模拟）</h3><div class="hy-table-scroll"><table data-process-materials><thead><tr><th>零件</th><th>材料 / 板厚</th><th>本批数量</th><th>辅料</th></tr></thead><tbody>${example().parts.map(p=>`<tr><td>${esc(p.name)}</td><td>${esc(p.material)} / ${p.thickness} mm</td><td>${p.qty}${p.quantityNote?'（'+esc(p.quantityNote)+'）':''}</td><td>${p.accessories.length?p.accessories.map(a=>`${esc(a.name)} × ${a.perPiece*p.qty}（模拟选型）`).join('<br>'):'本例未配置'}</td></tr>`).join('')}</tbody></table></div><h3 class="hy-block">工序汇总 · 非生产工单</h3><div class="hy-table-scroll"><table data-process-summary><thead><tr><th>零件 / 顺序</th><th>工序</th><th>工程量</th><th>计费单位</th><th>适用状态</th><th>工时依据</th><th>资料</th></tr></thead><tbody>${rows.map(({p,s,index})=>`<tr data-summary-op="${s.op}" data-summary-part="${p.id}" data-summary-state="${s.status}"><td>${esc(p.name)} / ${index}</td><td>${esc(byId[s.op].name)}</td><td>${esc(measurement(s.op,p))}</td><td>${esc(byId[s.op].unit)}</td><td>${statusNames[s.status]}（模拟）</td><td>待核定；不填零</td><td>${btn('查看作业卡',`data-process-open-card="${s.op}" data-card-part="${p.id}"`)}</td></tr>`).join('')}</tbody></table></div><details class="hy-op-catalog"><summary>后续计费结构</summary><p class="hy-sub">材料（净料 / 损耗 / 废料回收）、辅料、加工（准备 / 作业）、模具与分摊、包装 / 运输等分别记录。当前没有采用客户费率、工时或税费公式，不生成真实报价。</p></details>`;
  }
  function paint(){
    root.querySelector('#hy-process-workbench').innerHTML=workspace();
    root.querySelector('#hy-original-details').hidden=model.example!=='ss304';
    if(!root.querySelector('[data-page="engineering"]').hidden||(!root.querySelector('[data-page="output"]').hidden&&['route','cards'].includes(doc)))root.querySelector('#hy-footer-context').textContent=example().code+' · '+example().name+' · 固定模拟结果';
    const output=root.querySelector('[data-process-output]');
    if(output)output.innerHTML=doc==='route'?overview():`${selector()}${partButtons()}${routeButtons()}${detail()}${catalog()}`;
  }
  function download(text,type,name){
    const url=URL.createObjectURL(new Blob([text],{type}));urls.add(url);
    const a=document.createElement('a');a.href=url;a.download=name;root.append(a);a.click();a.remove();
    setTimeout(()=>{URL.revokeObjectURL(url);urls.delete(url);},1000);
  }
  function onClick(event){
    const feature=event.target.closest('[data-feature-shape]');
    if(feature&&feature.closest('[data-process-ui]')){
      event.preventDefault();const id=feature.dataset.featureShape;
      if(!features().includes(id)){
        const holeOperation={H01:'countersink',H02:'tap',H03:'ream',H04:'extrude'}[id];
        if(byId[model.op].kind!=='hole'||!holeOperation)return;
        model.op=holeOperation;
      }
      model.feature=id;paint();return;
    }
    const button=event.target.closest('button');if(!button||!button.closest('[data-process-ui]'))return;
    event.preventDefault();
    if(button.dataset.processPart){model.part=button.dataset.processPart;model.op=route()[0].op;model.feature=null;if(model.example==='ss304'){callbacks.onPart(model.part);return;}}
    if(button.dataset.processOp){model.op=button.dataset.processOp;model.feature=null;}
    if(button.dataset.catalogOp){model.op=button.dataset.catalogOp;model.feature=null;}
    if(button.dataset.processFeature)model.feature=button.dataset.processFeature;
    if(button.hasAttribute('data-process-explode'))model.exploded=!model.exploded;
    if(button.dataset.processMove){const index=route().findIndex(s=>s.op===model.op),next=index+Number(button.dataset.processMove);if(index>=0&&next>=0&&next<route().length)[route()[index],route()[next]]=[route()[next],route()[index]];}
    if(button.hasAttribute('data-process-add')&&!isReference()&&!inRoute()){
      const alternatives=['shear','laser','cnc','punch'];
      if(alternatives.includes(model.op)){
        const remove=new Set([...alternatives,'cnc_setup','punch_setup']);
        model.routes[model.example][model.part]=route().filter(s=>!remove.has(s.op));
        route().unshift(step(model.op,'pending'));
        if(model.op==='cnc'||model.op==='punch')route().unshift(step(model.op==='cnc'?'cnc_setup':'punch_setup','pending'));
      }else route().push(step(model.op,'pending'));
    }
    if(button.dataset.processDocument){callbacks.onDocument(button.dataset.processDocument);return;}
    if(button.dataset.processOpenCard){model.part=button.dataset.cardPart;model.op=button.dataset.processOpenCard;model.feature=null;callbacks.onDocument('cards');return;}
    if(button.hasAttribute('data-process-download-svg')){
      const plain=art(),end=plain.indexOf('>');
      const styles=`<title>${esc(example().code+' / '+drawingPart().name+' / '+byId[model.op].name)} DEMO</title><style>text{font:18px sans-serif;fill:#25364a}.hy-op-surface{fill:#edf4f9;stroke:#47677f;stroke-width:2}.hy-op-feature{fill:none;stroke:#246c9e;stroke-width:2}.hy-op-picked{stroke:#b8781e;stroke-width:5}.hy-op-hole{fill:white;stroke:#246c9e;stroke-width:2}.hy-op-picked .hy-op-hole{stroke:#b8781e;stroke-width:5}.hy-op-guide{fill:none;stroke:#8fa2b3;stroke-dasharray:5 4}.hy-op-watermark{font-size:14px;fill:#617287}</style>`;
      download(plain.slice(0,end).replace('<svg ','<svg xmlns="http://www.w3.org/2000/svg" ')+'> '+styles+plain.slice(end+1),'image/svg+xml',`${example().code}-${model.op}-DEMO.svg`);return;
    }
    if(button.hasAttribute('data-process-download-csv')){
      const csvCell=v=>'"'+String(v).replaceAll('"','""')+'"';
      const rows=[['DEMO 模拟工序表','非生产工单；工时/费率待核定'],['样例','零件','顺序','工序','工程量','计费单位','适用状态','工时依据'],...summaryRows().filter(x=>x.s.status!=='na').map(({p,s,index})=>[example().code,p.name,index,byId[s.op].name,measurement(s.op,p),byId[s.op].unit,statusNames[s.status]+'（模拟）','待核定'])];
      download('\ufeff'+rows.map(row=>row.map(csvCell).join(',')).join('\r\n'),'text/csv;charset=utf-8',`${example().code}-工序-DEMO.csv`);return;
    }
    const open=button.closest('details');const catalogOpen=open?.open;paint();
    if(catalogOpen)root.querySelectorAll('.hy-op-catalog').forEach(el=>el.open=true);
  }
  function onChange(event){
    if(!event.target.closest('[data-process-ui]'))return;
    if(event.target.hasAttribute('data-process-example'))setExample(event.target.value);
    if(event.target.hasAttribute('data-process-status')&&inRoute())inRoute().status=event.target.value;
    paint();
  }
  root.addEventListener('click',onClick);root.addEventListener('change',onChange);
  return {reset,selectOriginalPart,render(partId,documentId){doc=documentId;if(partId!==sourcePart){selectOriginalPart(partId);}paint();},destroy(){root.removeEventListener('click',onClick);root.removeEventListener('change',onChange);for(const url of urls)URL.revokeObjectURL(url);urls.clear();}};
}
