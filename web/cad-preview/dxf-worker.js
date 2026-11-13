/* DXF preview worker. The MIT parser is vendored at a pinned version. */
'use strict';
importScripts('./vendor/dxf/dxf-parser-1.1.2.js');
const MAX_ENTITIES=50000,MAX_POINTS=400000,MAX_BYTES=25*1024*1024;
const identity=[1,0,0,1,0,0];
function mul(a,b){return [a[0]*b[0]+a[2]*b[1],a[1]*b[0]+a[3]*b[1],a[0]*b[2]+a[2]*b[3],a[1]*b[2]+a[3]*b[3],a[0]*b[4]+a[2]*b[5]+a[4],a[1]*b[4]+a[3]*b[5]+a[5]];}
function num(n){if(typeof n!=='number'||!Number.isFinite(n)||Math.abs(n)>1e12)throw new Error('DXF 含无效或过大的坐标，无法安全预览。');return n;}
function pt(p,m){const x=num(p?.x),y=num(p?.y);return [num(m[0]*x+m[2]*y+m[4]),num(m[1]*x+m[3]*y+m[5])];}
function sourceTypes(text){const lines=text.split(/\r\n|\r|\n/);if(lines.length>4000000)throw new Error('DXF 内容过于复杂。');const counts={};let section='',entity='';for(let i=0;i+1<lines.length;i+=2){const code=lines[i].trim(),v=lines[i+1].trim();if(code==='0'&&v==='SECTION'){section='';entity='SECTION';}else if(entity==='SECTION'&&code==='2'){section=v;entity='';}else if(code==='0'&&v==='ENDSEC')section='';else if(code==='0'&&['ENTITIES','BLOCKS'].includes(section)&&!['BLOCK','ENDBLK','VERTEX','SEQEND'].includes(v)){counts[v]=(counts[v]||0)+1;if(Object.values(counts).reduce((a,b)=>a+b,0)>MAX_ENTITIES)throw new Error('DXF 实体超过 50,000 个，请提供简化图纸。');}}return counts;}
function cleanText(t){return String(t||'').slice(0,2000).replace(/\\U\+([0-9a-f]{4})/gi,(_,n)=>String.fromCharCode(parseInt(n,16))).replace(/\\P/gi,'\n').replace(/\\~/g,' ').replace(/%%[dD]/g,'°').replace(/%%[pP]/g,'±').replace(/%%[cC]/g,'⌀').replace(/\\[a-zA-Z][^;\n]*;/g,'').replace(/\\[LlOoKk]/g,'').replace(/[{}]/g,'');}
function makeDrawing(dxf,rawTypes){
 const paths=[],texts=[],warnings=new Set();let points=0,entities=0;let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
 const supported=new Set(['LINE','LWPOLYLINE','POLYLINE','CIRCLE','ARC','ELLIPSE','SPLINE','TEXT','MTEXT','INSERT','DIMENSION','POINT','SOLID','3DFACE','ATTDEF']);
 for(const [t,n] of Object.entries(rawTypes))if(!supported.has(t))warnings.add(`${n} 个 ${t} 实体暂未显示`);
 const bounds=p=>{minX=Math.min(minX,p[0]);minY=Math.min(minY,p[1]);maxX=Math.max(maxX,p[0]);maxY=Math.max(maxY,p[1]);};
 const path=(ps,m,color)=>{if(ps.length<2)return;points+=ps.length;if(points>MAX_POINTS)throw new Error('DXF 曲线过于复杂，请提供简化图纸。');const out=ps.map(p=>pt(p,m));out.forEach(bounds);paths.push({points:out,color});};
 const color=e=>{const layer=dxf.tables?.layer?.layers?.[e.layer];const c=e.color||layer?.color;if(!Number.isInteger(c)||c<=0||c>=0xffffff)return '#d9e9f4';let rgb=[c>>16&255,c>>8&255,c&255];if(rgb[0]*0.213+rgb[1]*0.715+rgb[2]*0.072<95)rgb=rgb.map(v=>Math.min(255,v+110));return '#'+rgb.map(v=>v.toString(16).padStart(2,'0')).join('');};
 const curve=(center,rx,ry,start,delta,rotation=0)=>{num(rx);num(ry);num(start);num(delta);num(rotation);if(rx<=0||ry<=0||Math.abs(delta)>Math.PI*2+0.001)throw new Error('DXF 圆弧尺寸无效。');const n=Math.max(8,Math.min(160,Math.ceil(Math.abs(delta)*24)));return Array.from({length:n+1},(_,i)=>{const a=start+delta*i/n,x=rx*Math.cos(a),y=ry*Math.sin(a);return {x:center.x+x*Math.cos(rotation)-y*Math.sin(rotation),y:center.y+x*Math.sin(rotation)+y*Math.cos(rotation)};});};
 function poly(v,closed){const out=[];for(let i=0;i<v.length;i++){const a=v[i];out.push(a);if(i===v.length-1&&!closed)break;const b=v[(i+1)%v.length],bulge=a.bulge||0;if(bulge){num(bulge);const dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy);if(d){const cx=(a.x+b.x)/2-dy*(1-bulge*bulge)/(4*bulge),cy=(a.y+b.y)/2+dx*(1-bulge*bulge)/(4*bulge),r=Math.hypot(a.x-cx,a.y-cy);out.push(...curve({x:cx,y:cy},r,r,Math.atan2(a.y-cy,a.x-cx),4*Math.atan(bulge)).slice(1));}}}if(closed&&out.length)out.push(v[0]);return out;}
 function spline(e){const cp=e.controlPoints||[],k=e.knotValues||[],p=e.degreeOfSplineCurve;if(!Number.isInteger(p)||p<1||p>10||cp.length<p+1||k.length!==cp.length+p+1||cp.length>10000){warnings.add('部分 SPLINE 样条参数不支持，未显示');return null;}cp.forEach(v=>{num(v.x);num(v.y);});k.forEach(num);if(k.some((v,i)=>i&&v<k[i-1]))throw new Error('DXF 样条节点顺序无效。');if(e.rational)warnings.add('有理样条以控制点近似显示，请以原文件为准');const a=k[p],b=k[cp.length];if(b<=a)return null;const count=Math.min(1024,Math.max(64,cp.length*8));return Array.from({length:count+1},(_,j)=>{const u=j===count?b:a+(b-a)*j/count;let s=cp.length-1;if(u<b){s=p;while(s<cp.length-1&&u>=k[s+1])s++;}const d=Array.from({length:p+1},(_,i)=>({...cp[s-p+i]}));for(let r=1;r<=p;r++)for(let i=p;i>=r;i--){const idx=s-p+i,den=k[idx+p-r+1]-k[idx],alpha=den?(u-k[idx])/den:0;d[i]={x:(1-alpha)*d[i-1].x+alpha*d[i].x,y:(1-alpha)*d[i-1].y+alpha*d[i].y};}return d[p];});}
 function draw(e,m,depth,parents){
  if(++entities>MAX_ENTITIES)throw new Error('展开后的 DXF 实体过多，请提供简化图纸。');if(depth>12)throw new Error('DXF 块嵌套过深，无法预览。');const layer=dxf.tables?.layer?.layers?.[e.layer];if(e.paperSpace||e.inPaperSpace||e.visible===false||layer?.visible===false||layer?.frozen)return;
  const c=color(e);if(e.extrusionDirection&&((e.extrusionDirection.x||0)!==0||(e.extrusionDirection.y||0)!==0||(e.extrusionDirection.z??1)!==1))warnings.add('包含非默认坐标平面的实体，当前按 XY 平面近似显示');
  switch(e.type){
   case 'LINE':path(e.vertices||[],m,c);break;
   case 'LWPOLYLINE':case 'POLYLINE':path(poly(e.vertices||[],e.shape),m,c);if(e.is3dPolyline||e.is3dPolygonMesh||e.isPolyfaceMesh)warnings.add('三维多段线按 XY 平面显示');break;
   case 'CIRCLE':case 'ARC':{let delta=e.type==='CIRCLE'?Math.PI*2:e.endAngle-e.startAngle;while(delta<0)delta+=Math.PI*2;path(curve(e.center,e.radius,e.radius,e.startAngle||0,delta),m,c);break;}
   case 'ELLIPSE':{const a=e.majorAxisEndPoint,r=Math.hypot(a.x,a.y);let delta=e.endAngle-e.startAngle;while(delta<0)delta+=Math.PI*2;path(curve(e.center,r,r*e.axisRatio,e.startAngle,delta||Math.PI*2,Math.atan2(a.y,a.x)),m,c);break;}
   case 'SPLINE':{const p=spline(e);if(p)path(p,m,c);break;}
   case 'SOLID':case '3DFACE':{const p=e.points||e.vertices||[];path([...p,p[0]].filter(Boolean),m,c);break;}
   case 'POINT':{const p=e.position||e;path([{x:p.x-0.15,y:p.y},{x:p.x+0.15,y:p.y}],m,c);break;}
   case 'ATTDEF':warnings.add('块属性定义未作为实际标注显示');break;
   case 'TEXT':case 'MTEXT':{const p=e.startPoint||e.position,h=num(e.textHeight||e.height||1);if(h<=0)break;const at=pt(p,m),text=cleanText(e.text);if(!text)break;const scale=Math.hypot(m[0],m[1]),height=num(h*scale),rotation=num((e.rotation||0)*Math.PI/180+Math.atan2(m[1],m[0]));texts.push({point:at,text,height,rotation,color:c,align:e.halign===1?'center':e.halign===2?'right':'left'});bounds(at);bounds([num(at[0]+height*Math.min(100,text.length)*0.6),num(at[1]+height)]);if(texts.length>10000)throw new Error('DXF 标注过多，无法预览。');break;}
   case 'DIMENSION':case 'INSERT':{const name=e.name||e.block,block=dxf.blocks?.[name];if(!block){warnings.add(`块 ${String(name||'未命名').slice(0,80)} 未找到，相关实体未显示`);break;}if(parents.includes(name))throw new Error('DXF 块存在循环引用。');if(block.xrefPath)warnings.add('外部参照不会加载，预览可能不完整');const sx=e.xScale??1,sy=e.yScale??1,angle=(e.rotation||0)*Math.PI/180,p=e.position||{x:0,y:0},bp=block.position||{x:0,y:0};[sx,sy,angle,p.x,p.y,bp.x,bp.y].forEach(num);const t=mul(m,[Math.cos(angle)*sx,Math.sin(angle)*sx,-Math.sin(angle)*sy,Math.cos(angle)*sy,p.x,p.y]);const rows=e.rowCount||1,cols=e.columnCount||1;if(!Number.isInteger(rows)||!Number.isInteger(cols)||rows<1||cols<1||rows*cols>1000)throw new Error('DXF 块阵列过大。');for(let r=0;r<rows;r++)for(let col=0;col<cols;col++){const tm=mul(t,[1,0,0,1,-bp.x+col*(e.columnSpacing||0),-bp.y+r*(e.rowSpacing||0)]);for(const child of block.entities||[])draw(child,tm,depth+1,[...parents,name]);}break;}
   default:warnings.add(`${e.type} 实体暂未显示`);
  }
 }
 for(const e of dxf.entities||[])draw(e,identity,0,[]);
 if(!paths.length&&!texts.length)throw new Error('该 DXF 未包含可显示的二维实体，请提供截图或其他格式。');
 if(![minX,minY,maxX,maxY].every(Number.isFinite))throw new Error('DXF 范围无效。');
 return {paths,texts,bounds:[minX,minY,maxX,maxY],warnings:[...warnings],entities,points};
}
self.onmessage=event=>{try{const bytes=event.data.bytes;if(!(bytes instanceof ArrayBuffer)||!bytes.byteLength||bytes.byteLength>MAX_BYTES)throw new Error('DXF 文件为空或超过 25 MiB。');const text=new TextDecoder('utf-8').decode(bytes);if(text.startsWith('AutoCAD Binary DXF'))throw new Error('暂不支持二进制 DXF，请导出 ASCII DXF 或上传截图。');const types=sourceTypes(text);self.postMessage({type:'status',message:'正在整理二维图元…'});const dxf=new self.DxfParser().parseSync(text);if(!dxf)throw new Error('DXF 文件无法解析。');self.postMessage({type:'ready',drawing:makeDrawing(dxf,types)});}catch(error){self.postMessage({type:'error',message:error.message||'DXF 预览未完成。'});}};
