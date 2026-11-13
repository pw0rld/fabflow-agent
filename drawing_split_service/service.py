"""Persistent single-worker jobs. Reads never dispatch model requests."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import fcntl
import io
import json
import os
import re
import secrets
import tempfile
import threading
from .contract import ContractError, normalize

MAX_UPLOAD=20*1024*1024
JOB_PATTERN=re.compile(r'SPL-[a-f0-9]{32}')
REQUEST_PATTERN=re.compile(r'[A-Za-z0-9_-]{16,128}')
TERMINAL={'succeeded','partial','failed','interrupted'}
STAGES={'queued':'等待识别','preparing':'正在检查原始图片','locating':'AI 正在定位视图、展开图和表格','extracting':'AI 正在读取分区参数','analyzing':'等待模型整理结果','cropping':'正在从原图裁取分区','completed':'识别结果已保存，待核对','failed':'本轮未完成，原图已保留','interrupted':'本轮被中断，原图已保留'}

class ServiceError(ValueError):
    def __init__(self,message,code='invalid_request',status=400):
        super().__init__(message);self.code=code;self.status=status


def now():return datetime.now(timezone.utc).isoformat()
def sha(data):return hashlib.sha256(data).hexdigest()

def atomic_json(path,value):
    data=(json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2)+'\n').encode()
    with tempfile.NamedTemporaryFile(dir=path.parent,prefix='.write-',delete=False) as stream:
        temporary=Path(stream.name)
        try:
            stream.write(data);stream.flush();os.fsync(stream.fileno());temporary.chmod(0o600);os.replace(temporary,path)
        finally:temporary.unlink(missing_ok=True)

def no_links(path):
    if any(item.is_symlink() for item in (path,*path.parents)):
        raise ServiceError('存储路径不安全。','storage_invalid',500)


def source_format(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):return 'png','image/png'
    if data.startswith(b'\xff\xd8\xff'):return 'jpg','image/jpeg'
    if data.startswith((b'GIF87a',b'GIF89a')):return 'gif','image/gif'
    if data[:4]==b'RIFF' and data[8:12]==b'WEBP':return 'webp','image/webp'
    raise ServiceError('自动分图目前支持 JPG、PNG、WebP 和 GIF 图片。','unsupported_image',415)


def filename(value):
    value=str(value or 'drawing').replace('\\','/').split('/')[-1]
    return ''.join(char for char in value if ord(char)>=32 and char not in '\x7f')[:180] or 'drawing'


class SplitService:
    def __init__(self,data_dir,adapter,*,public_base='api/',max_pending=8,start_worker=True):
        path=Path(data_dir).absolute();no_links(path);path.mkdir(parents=True,mode=0o700,exist_ok=True)
        self.root=path.resolve();self.root.chmod(0o700)
        self.requests=self.root/'.requests';self.requests.mkdir(mode=0o700,exist_ok=True);no_links(self.requests)
        lock_file=self.root/'.service.lock';no_links(lock_file)
        self.process_lock=lock_file.open('a+b');lock_file.chmod(0o600)
        try:fcntl.flock(self.process_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.process_lock.close();raise ServiceError('任务目录已由另一服务占用。','store_busy',503) from None
        self.adapter=adapter;self.base=public_base.rstrip('/')+'/';self.lock=threading.RLock()
        self.max_pending=max_pending;self.pending=set();self.closed=False
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='drawing-split') if start_worker else None
        self._recover()

    def _dir(self,job_id):
        if not isinstance(job_id,str) or not JOB_PATTERN.fullmatch(job_id):raise ServiceError('任务不存在。','not_found',404)
        path=self.root/job_id;no_links(path)
        if not path.is_dir():raise ServiceError('任务不存在。','not_found',404)
        return path

    def _load(self,job_id):
        path=self._dir(job_id)/'job.json';no_links(path)
        try:record=json.loads(path.read_text())
        except (OSError,ValueError):raise ServiceError('任务记录无法读取。','record_invalid',500) from None
        return record

    def _save(self,record):
        record['updated_at']=now();atomic_json(self._dir(record['job_id'])/'job.json',record)

    def _url(self,job_id,tail=''):return self.base+'jobs/'+job_id+tail
    def _file_url(self,job_id,name):return self._url(job_id,'/files/'+name)

    def _recover(self):
        for path in self.root.glob('SPL-*'):
            if not path.is_dir() or path.is_symlink() or not JOB_PATTERN.fullmatch(path.name):continue
            try:
                record=self._load(path.name)
                if record.get('status') in {'queued','running'}:
                    record.update(status='interrupted',stage='interrupted',message=STAGES['interrupted'],error={'code':'service_restarted','message':'服务重启，本轮没有自动重新调用模型。可显式重试。'})
                    self._save(record)
            except ServiceError:continue

    def _public(self,record):
        keys=('job_id','status','stage','message','attempt','created_at','updated_at','source','manifest','warnings','error','model')
        result={key:record.get(key) for key in keys}
        original=self._file_url(record['job_id'],record['original_file'])
        result.update(status_url=self._url(record['job_id']),result_url=self._url(record['job_id']),source_url=record.get('source',{}).get('image') or original,original_url=original)
        return result

    def get(self,job_id):
        with self.lock:return self._public(self._load(job_id))

    def get_review(self,job_id):
        from .review import get_review
        return get_review(self,job_id)

    def save_review(self,job_id,payload):
        from .review import save_review
        return save_review(self,job_id,payload)

    def export_review(self,job_id,revision,attempt):
        from .review import export_review
        return export_review(self,job_id,revision,attempt)

    def _request_file(self,request_id):
        if not isinstance(request_id,str) or not REQUEST_PATTERN.fullmatch(request_id):raise ServiceError('请求编号无效。','invalid_request_id')
        return self.requests/(sha(request_id.encode())+'.json')

    def create(self,data,name,request_id):
        if not isinstance(data,bytes) or not data or len(data)>MAX_UPLOAD:raise ServiceError('图片大小须为 1 字节至 20 MB。','too_large',413)
        extension,mime=source_format(data);name=filename(name);digest=sha(data)
        with self.lock:
            marker=self._request_file(request_id);no_links(marker)
            if marker.exists():
                saved=json.loads(marker.read_text())
                if saved.get('operation')!='create' or saved.get('sha256')!=digest or saved.get('filename')!=name:raise ServiceError('请求编号已用于其他文件。','request_conflict',409)
                return self._public(self._load(saved['job_id']))
            if self.closed or len(self.pending)>=self.max_pending:raise ServiceError('识别队列繁忙，请稍后再试。','queue_busy',429)
            job_id='SPL-'+secrets.token_hex(16);directory=self.root/job_id;directory.mkdir(mode=0o700)
            original='source.'+extension;path=directory/original;path.write_bytes(data);path.chmod(0o600)
            record={'job_id':job_id,'status':'queued','stage':'queued','message':STAGES['queued'],'attempt':1,'created_at':now(),'updated_at':now(),'original_file':original,
                    'source':{'filename':name,'sha256':digest,'size':len(data),'image':self._file_url(job_id,original),'width':None,'height':None},'manifest':None,'warnings':[],'error':None,'model':None,
                    'files':{original:{'path':original,'sha256':digest,'mime':mime}},'retry_requests':{}}
            self._save(record)
            atomic_json(marker,{'operation':'create','job_id':job_id,'sha256':digest,'filename':name})
            self._enqueue(job_id,1)
            return self._public(record)

    def retry(self,job_id,request_id):
        with self.lock:
            self._request_file(request_id)
            record=self._load(job_id);key=sha(request_id.encode())
            if key in record.get('retry_requests',{}):return self._public(record)
            if record['status'] not in TERMINAL:raise ServiceError('本轮仍在执行。','job_running',409)
            if self.closed or len(self.pending)>=self.max_pending:raise ServiceError('识别队列繁忙，请稍后再试。','queue_busy',429)
            record.setdefault('retry_requests',{})[key]=record['attempt']+1
            record.update(attempt=record['attempt']+1,status='queued',stage='queued',message=STAGES['queued'],manifest=None,error=None,warnings=[])
            self._save(record);self._enqueue(job_id,record['attempt']);return self._public(record)

    def _enqueue(self,job_id,attempt):
        self.pending.add(job_id)
        if self.executor:self.executor.submit(self._run,job_id,attempt)

    def _progress(self,job_id,attempt,stage,message=None):
        if stage not in STAGES:stage='analyzing'
        with self.lock:
            record=self._load(job_id)
            if record['attempt']!=attempt or record.get('status') not in {'queued','running'}:return
            record.update(status='running',stage=stage,message=STAGES[stage]);self._save(record)
    def _register(self,record,name,path,mime):
        relative=path.relative_to(self._dir(record['job_id'])).as_posix()
        record['files'][name]={'path':relative,'sha256':file_hash(path),'mime':mime}

    def _prepare(self,job_id,attempt):
        from PIL import Image,ImageOps
        import warnings
        record=self._load(job_id);directory=self._dir(job_id);original=directory/record['original_file']
        no_links(original)
        if file_hash(original)!=record['source']['sha256']:raise ServiceError('原图校验失败。','source_changed',409)
        canonical=directory/'canonical.png';registered=record['files'].get('canonical.png')
        if registered:
            no_links(canonical)
            if not canonical.is_file() or file_hash(canonical)!=registered['sha256']:raise ServiceError('原图规范版本校验失败。','source_changed',409)
            return canonical,record['source']['width'],record['source']['height']
        Image.MAX_IMAGE_PIXELS=24_000_000
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(original) as check:
                if check.format not in {'JPEG','PNG','WEBP','GIF'}:raise ValueError('format')
                width,height=check.size
                if width<1 or height<1 or width>20000 or height>20000 or width*height>24_000_000:raise ValueError('dimensions')
                check.verify()
            with Image.open(original) as image:
                image.seek(0);orientation=image.getexif().get(274,1);frames=2 if getattr(image,'is_animated',False) else 1
                image.seek(0)
                frame=ImageOps.exif_transpose(image).convert('RGBA' if image.mode=='RGBA' else 'RGB')
                width,height=frame.size
                frame.save(canonical,format='PNG',optimize=False);canonical.chmod(0o600)
        with self.lock:
            record=self._load(job_id)
            record['source'].update(image=self._file_url(job_id,'canonical.png'),original_image=self._file_url(job_id,record['original_file']),width=width,height=height,canonical_sha256=file_hash(canonical),orientation_normalized=orientation not in {None,1},first_frame_only=frames>1)
            if frames>1:record['warnings'].append('多帧图片本轮仅读取第一帧，原始文件已保留。')
            self._register(record,'canonical.png',canonical,'image/png');self._save(record)
        return canonical,width,height

    def _run(self,job_id,attempt):
        stage='preparing'
        try:
            self._progress(job_id,attempt,stage)
            try:canonical,width,height=self._prepare(job_id,attempt)
            except ServiceError:raise
            except Exception:raise ServiceError('图片无法完整解码或分辨率超出限制，原件已保留。','image_invalid',422) from None
            stage='locating';self._progress(job_id,attempt,stage)
            raw=self.adapter.analyze(canonical,width,height,lambda phase,message=None:self._progress(job_id,attempt,phase,message))
            directory=self._dir(job_id);attempt_dir=directory/'attempts'/('a'+str(attempt).zfill(3));attempt_dir.mkdir(parents=True,mode=0o700,exist_ok=True);attempt_dir.chmod(0o700)
            private_raw=attempt_dir/'model-output.json';private_raw.write_text(json.dumps(raw,ensure_ascii=False,default=repr,allow_nan=True));private_raw.chmod(0o600)
            with self.lock:record=self._load(job_id)
            prefix='a'+str(attempt).zfill(3)+'-'
            manifest=normalize(raw,width=width,height=height,source=dict(record['source']),files_base=self._file_url(job_id,''),filename_prefix=prefix)
            stage='cropping';self._progress(job_id,attempt,stage)
            from PIL import Image
            successful=[];new_files=[]
            with Image.open(canonical) as image:
                image.load()
                for region in manifest['regions']:
                    name=prefix+'region-'+region['id']+'.png';path=attempt_dir/name
                    try:
                        image.crop(tuple(region['bbox'])).save(path,format='PNG',optimize=False);path.chmod(0o600)
                        successful.append(region);new_files.append((name,path,'image/png'))
                    except Exception:
                        manifest['analysis_complete']=False;manifest['warnings'].append(region['title']+'：裁片未能保存，原件已保留。')
            ids={region['id'] for region in successful};manifest['regions']=successful
            for key in ('parameters','process_notes','bend_rows'):
                kept=[]
                for item in manifest[key]:
                    item['evidence']=[ev for ev in item.get('evidence',[]) if ev['region_id'] in ids]
                    if item['evidence']:kept.append(item)
                manifest[key]=kept
            for key in manifest['relationships']:
                manifest['relationships'][key]=[value for value in manifest['relationships'][key] if value in ids]
            manifest['attempt']=attempt
            result_name=prefix+'result.json';result_path=attempt_dir/result_name;atomic_json(result_path,manifest);new_files.append((result_name,result_path,'application/json; charset=utf-8'))
            with self.lock:
                record=self._load(job_id)
                if record['attempt']!=attempt:return
                for name,path,mime in new_files:self._register(record,name,path,mime)
                info=getattr(self.adapter,'model_info',None)
                if isinstance(info,dict):record['model']={key:str(info[key])[:100] for key in ('provider','model') if isinstance(info.get(key),str)}
                status='succeeded' if manifest['analysis_complete'] else 'partial'
                message='图中未发现可用的工程图分区。' if manifest['document_kind']=='not_drawing' else '分图与参数已保存，待工程核对。' if status=='succeeded' else '已保留本轮可用分区，部分参数仍未完成。'
                record.update(status=status,stage='completed',message=message,manifest=manifest,warnings=list(dict.fromkeys(record['warnings']+manifest['warnings'])),error=None)
                self._save(record)
        except ContractError as error:self._fail(job_id,attempt,'model_result_invalid',str(error))
        except ServiceError as error:self._fail(job_id,attempt,error.code,str(error))
        except TimeoutError:self._fail(job_id,attempt,'model_timeout','模型响应超时，原图已保留，可稍后显式重试。')
        except Exception as error:
            # Exception strings can contain provider endpoints or credentials.
            self._fail(job_id,attempt,'analysis_failed','本轮识别未完成，原图已保留。请重试或手工框选。',error_type=type(error).__name__)
        finally:
            with self.lock:
                # A new retry may already own this job's queue slot.
                if self._load(job_id)['attempt']==attempt:self.pending.discard(job_id)

    def _fail(self,job_id,attempt,code,message,error_type=None):
        with self.lock:
            record=self._load(job_id)
            if record['attempt']!=attempt:return
            record.update(status='failed',stage='failed',message=STAGES['failed'],error={'code':code,'message':message})
            self._save(record)
            path=self._dir(job_id)/('error-a'+str(attempt).zfill(3)+'.json')
            atomic_json(path,{'code':code,'error_type':error_type,'created_at':now()})

    def file(self,job_id,name):
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,100}',str(name)):raise ServiceError('文件不存在。','not_found',404)
        with self.lock:
            record=self._load(job_id);entry=record['files'].get(name)
            if not entry:raise ServiceError('文件不存在。','not_found',404)
            directory=self._dir(job_id);path=directory/entry['path'];no_links(path)
            if not path.resolve().is_relative_to(directory) or not path.is_file() or file_hash(path)!=entry['sha256']:raise ServiceError('文件校验失败。','file_changed',409)
            return path,entry['mime']

    def close(self):
        with self.lock:self.closed=True
        if self.executor:self.executor.shutdown(wait=True,cancel_futures=True)
        if not self.process_lock.closed:
            fcntl.flock(self.process_lock,fcntl.LOCK_UN);self.process_lock.close()


def file_hash(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()
