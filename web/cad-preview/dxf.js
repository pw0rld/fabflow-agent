/* Browser-only 2D preview. Geometry is visual only, never quotation input. */
export async function mount({element,bytes,signal,status=()=>{},warning=()=>{}}){
 if(signal?.aborted)throw new DOMException('Aborted','AbortError');
 if(!(bytes instanceof ArrayBuffer)||!bytes.byteLength)throw new Error('DXF 文件为空。');
 let worker=new Worker(new URL('./dxf-worker.js',import.meta.url)),disposed=false,timer;
 let cleanup=()=>{};
 const abort=()=>{disposed=true;clearTimeout(timer);worker?.terminate();worker=null;cleanup();};
 const drawing=await new Promise((resolve,reject)=>{
  const onAbort=()=>{abort();reject(new DOMException('Aborted','AbortError'));};
  signal?.addEventListener('abort',onAbort,{once:true});
  cleanup=()=>signal?.removeEventListener('abort',onAbort);
  timer=setTimeout(()=>{abort();reject(new Error('DXF 预览超过 90 秒，请提供简化文件或截图。'));},90000);
  worker.onerror=()=>{abort();reject(new Error('DXF 解析器未能启动或文件无法处理。'));};
  worker.onmessage=event=>{if(disposed)return;const data=event.data;if(data.type==='status'){status(data.message);return;}clearTimeout(timer);worker.terminate();worker=null;cleanup();if(data.type==='error'){reject(new Error(data.message));return;}if(data.type==='ready')resolve(data.drawing);};
  status('正在解析 DXF 图元…');const workerBytes=bytes.slice(0);worker.postMessage({bytes:workerBytes},[workerBytes]);
 });
 if(signal?.aborted||disposed)throw new DOMException('Aborted','AbortError');
 const canvas=document.createElement('canvas');canvas.className='cad-dxf-canvas';canvas.setAttribute('role','img');canvas.setAttribute('aria-label','DXF 二维图纸预览，可拖动平移并滚轮缩放');canvas.style.cssText='display:block;width:100%;height:100%;touch-action:none;cursor:grab;background:#102331';element.replaceChildren(canvas);
 const context=canvas.getContext('2d');if(!context)throw new Error('浏览器不支持二维图纸预览。');
 let width=1,height=1,scale=1,fitScale=1,offsetX=0,offsetY=0,drag=null;
 function draw(){if(disposed)return;const dpr=Math.min(devicePixelRatio||1,2);context.setTransform(dpr,0,0,dpr,0,0);context.fillStyle='#102331';context.fillRect(0,0,width,height);context.save();context.translate(offsetX,offsetY);context.scale(scale,-scale);context.lineWidth=1/scale;context.lineJoin='round';for(const p of drawing.paths){context.strokeStyle=p.color;context.beginPath();p.points.forEach(([x,y],i)=>i?context.lineTo(x,y):context.moveTo(x,y));context.stroke();}for(const t of drawing.texts){context.save();context.translate(...t.point);context.rotate(t.rotation);context.scale(1,-1);context.font=`${Math.max(t.height,0.01)}px sans-serif`;context.fillStyle=t.color;context.textAlign=t.align;context.textBaseline='alphabetic';t.text.split('\n').slice(0,100).forEach((line,i)=>context.fillText(line,0,i*t.height*1.3));context.restore();}context.restore();}
 function reset(){const [x0,y0,x1,y1]=drawing.bounds,w=Math.max(x1-x0,0.001),h=Math.max(y1-y0,0.001);fitScale=Math.min(width/w,height/h)*0.88;scale=fitScale;offsetX=width/2-(x0+x1)/2*scale;offsetY=height/2+(y0+y1)/2*scale;draw();}
 function zoom(factor,x=width/2,y=height/2){if(!Number.isFinite(factor)||factor<=0||disposed)return;const next=Math.max(fitScale/100,Math.min(fitScale*1000,scale*factor)),ratio=next/scale;offsetX=x-(x-offsetX)*ratio;offsetY=y-(y-offsetY)*ratio;scale=next;draw();}
 function resize(){const r=element.getBoundingClientRect();width=Math.max(1,r.width);height=Math.max(1,r.height);const dpr=Math.min(devicePixelRatio||1,2);canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);reset();}
 const wheel=e=>{e.preventDefault();const r=canvas.getBoundingClientRect();zoom(Math.exp(-Math.max(-100,Math.min(100,e.deltaY))*0.004),e.clientX-r.left,e.clientY-r.top);};
 const down=e=>{if(e.button!==0)return;drag={x:e.clientX,y:e.clientY};canvas.setPointerCapture(e.pointerId);canvas.style.cursor='grabbing';};
 const move=e=>{if(!drag)return;offsetX+=e.clientX-drag.x;offsetY+=e.clientY-drag.y;drag={x:e.clientX,y:e.clientY};draw();};
 const up=()=>{drag=null;canvas.style.cursor='grab';};
 canvas.addEventListener('wheel',wheel,{passive:false});canvas.addEventListener('pointerdown',down);canvas.addEventListener('pointermove',move);canvas.addEventListener('pointerup',up);canvas.addEventListener('pointercancel',up);
 const observer=new ResizeObserver(resize);observer.observe(element);resize();
 cleanup=()=>{observer.disconnect();canvas.removeEventListener('wheel',wheel);canvas.removeEventListener('pointerdown',down);canvas.removeEventListener('pointermove',move);canvas.removeEventListener('pointerup',up);canvas.removeEventListener('pointercancel',up);signal?.removeEventListener('abort',abort);canvas.remove();};
 signal?.addEventListener('abort',abort,{once:true});
 warning(drawing.warnings.length?drawing.warnings.join('；')+'。预览可能不完整，请核对原文件。':'');
 status(`二维预览已就绪 · ${drawing.entities} 个图元 · 拖动平移，滚轮缩放`);
 return {dispose:abort,reset,zoom};
}
