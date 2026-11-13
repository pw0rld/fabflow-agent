// This viewer only reads the attachment API. It never starts AI or saves fields.
const stage=document.querySelector('#stage'),loading=document.querySelector('#loading');
const statusNode=document.querySelector('#status'),warningNode=document.querySelector('#warning');
const controls=['zoom-in','zoom-out','reset'].map(id=>document.getElementById(id));
const formats={dxf:['DXF · 二维图','./dxf.js'],dwg:['DWG · 二维图','./dwg.js'],step:['STEP · 三维模型','./step.js'],stp:['STEP · 三维模型','./step.js']};
const limit=25*1024*1024;
let active=null,controller=null,generation=0;
const status=message=>{statusNode.textContent=message;document.body.dataset.stage=message;};
function stop(){controller?.abort();controller=null;active?.dispose();active=null;}
function abortable(promise,signal){
  return new Promise((resolve,reject)=>{
    const abort=()=>reject(signal.reason||new DOMException('预览已停止。','AbortError'));
    if(signal.aborted){abort();return;}
    signal.addEventListener('abort',abort,{once:true});
    promise.then(value=>{signal.removeEventListener('abort',abort);resolve(value);},error=>{signal.removeEventListener('abort',abort);reject(error);});
  });
}
async function checked(response){if(!response.ok)throw Error(response.status===401?'登录已失效，请回到工作台重新登录。':response.status===404?'原附件不存在，请返回任务重新选择。':'无法读取附件，请稍后重新预览。');return response;}
async function start(){
  stop();const turn=++generation;controller=new AbortController();const signal=controller.signal;
  document.body.classList.remove('failed');document.body.dataset.ready='false';
  stage.replaceChildren();warningNode.textContent='';loading.hidden=false;controls.forEach(node=>node.disabled=true);
  document.querySelector('#retry').hidden=true;status('正在读取附件信息…');
  const warnings=new Set(),warning=message=>{if(turn===generation){warnings.add(message);warningNode.textContent=[...warnings].join('；');}};
  const timer=setTimeout(()=>{if(turn===generation)controller?.abort(new Error('预览处理超时，请下载原件查看，或换用较小的文件。'));},90000);
  try{
    const query=new URLSearchParams(location.search),task=query.get('task'),source=query.get('source');
    const valid=/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/;
    if(!valid.test(task||'')||!valid.test(source||'')||query.size!==2)throw Error('预览链接无效，请从任务附件重新打开。');
    const base='/workbench/api/tasks/'+task;
    const taskInfo=await (await checked(await fetch(base,{signal,credentials:'same-origin'}))).json();
    const file=taskInfo.attachments?.find(item=>item.id===source);
    if(!file)throw Error('当前任务中找不到这个附件。');
    const original=document.querySelector('#original');original.href=base+'/attachments/'+source+'?download=1';original.hidden=false;
    const extension=file.name?.split('.').pop().toLowerCase(),format=formats[extension];
    if(!format)throw Error('此格式尚不支持 CAD 预览，请下载原件查看。');
    const fileLimit=['step','stp'].includes(extension)?20*1024*1024:limit;
    if(!Number.isFinite(file.size)||file.size<=0||file.size>fileLimit)throw Error(`此格式支持预览不超过 ${fileLimit/1024/1024} MiB 的附件。`);
    document.title=file.name+' · CAD 预览';document.querySelector('#format').textContent=format[0];
    document.querySelector('#hint').textContent=['step','stp'].includes(extension)?'拖动旋转 · 滚轮缩放 · 右键平移':'滚轮缩放 · 拖动平移';
    status('正在下载预览资料…');
    const response=await checked(await fetch(base+'/attachments/'+source,{signal,credentials:'same-origin'}));
    if(Number(response.headers.get('Content-Length'))>limit)throw Error('文件过大，请下载原件查看。');
    const bytes=await response.arrayBuffer();
    if(!bytes.byteLength||bytes.byteLength>limit)throw Error('文件为空或过大，无法预览。');
    status('正在加载预览组件…');
    const module=await abortable(import(format[1]),signal);
    if(signal.aborted)throw signal.reason;
    const result=await module.mount({element:stage,bytes,signal,status,warning});
    if(signal.aborted||turn!==generation){result.dispose();return;}
    active=result;loading.hidden=true;controls.forEach(node=>node.disabled=false);
    document.body.dataset.ready='true';status('预览已就绪');
  }catch(error){
    if(turn!==generation)return;
    active?.dispose();active=null;stage.replaceChildren();document.body.classList.add('failed');
    status(signal.aborted?(signal.reason?.message||'预览已停止，可重新预览。'):(error?.message||'无法生成预览，请下载原件查看。'));
    document.querySelector('#retry').hidden=false;
  }finally{clearTimeout(timer);}
}
document.querySelector('#zoom-in').addEventListener('click',()=>active?.zoom(1.25));
document.querySelector('#zoom-out').addEventListener('click',()=>active?.zoom(.8));
document.querySelector('#reset').addEventListener('click',()=>active?.reset());
document.querySelector('#retry').addEventListener('click',start);
window.addEventListener('pagehide',()=>{generation++;stop();});
window.addEventListener('pageshow',event=>{if(event.persisted)start();});
start();
