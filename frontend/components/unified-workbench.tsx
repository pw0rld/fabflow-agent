'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, Box, Check, ChevronRight, FileArchive, FileImage, FileUp, FolderOpen, LoaderCircle, RefreshCw, Upload } from 'lucide-react'
import CadWorkbench, { type CadEntryAction, type CadWorkflowState } from './cad-workbench'
import ImageBomPanel, { type ImageBomStage } from './image-bom-panel'
import ImageEngineeringPanel from './image-engineering-panel'
import { sourceFingerprint, type ImageBomSource } from '../lib/image-bom-demo'
import './unified-workbench.css'

type Flow = 'step' | 'image'
type Phase = 'idle' | 'upload' | 'processing' | 'review' | 'error'
type Recent = { flow: Flow; id: string; filename: string; sample: boolean; updatedAt: string }
type ImageAction = { requestId: string; type: 'load-file' | 'open-sample' | 'restore-job'; file?: File; jobId?: string }
type ImageStage = 'source' | 'engineering' | ImageBomStage
const CHANNEL = 'hyl-unified-v1'
const IMAGE_PATH = '/ortho2cad-test/drawing-split/'
const RECENT_KEY = 'hyl-unified-recent-v1'
const stepId = (value: unknown): value is string => typeof value === 'string' && /^CAD-[a-f0-9]{32}$/.test(value)
const imageId = (value: unknown): value is string => typeof value === 'string' && /^SPL-[a-f0-9]{32}$/.test(value)
const idFor = (flow: Flow, value: unknown): value is string => flow === 'step' ? stepId(value) : imageId(value)
const short = (value: unknown, limit = 300) => typeof value === 'string' ? value.slice(0, limit) : ''
const randomId = () => crypto.randomUUID()

function storedRecent(): Recent[] {
  try {
    const raw = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]')
    const rows: Recent[] = (Array.isArray(raw) ? raw : []).filter(row => row && (row.flow === 'step' || row.flow === 'image') && idFor(row.flow, row.id)).slice(0, 8).map(row => ({ flow: row.flow, id: row.id, filename: short(row.filename, 180) || '已保存图纸', sample: row.sample === true, updatedAt: short(row.updatedAt, 40) }))
    const previous = JSON.parse(localStorage.getItem('hyl-drawing-split-last-job-v1') || 'null')
    if (previous && imageId(previous.job_id) && !rows.some(row => row.id === previous.job_id)) rows.push({ flow: 'image', id: previous.job_id, filename: short(previous.filename, 180) || '上次图片识别', sample: false, updatedAt: '' })
    return rows.slice(0, 8)
  } catch { return [] }
}

function locationFor(flow: Flow | null, sourceId: string | null, sample: boolean, view?: 'source' | 'manufacturing') {
  const url = new URL(window.location.href)
  for (const key of ['flow', 'document', 'job', 'sample', 'view']) url.searchParams.delete(key)
  if (flow) url.searchParams.set('flow', flow)
  if (flow && sourceId && idFor(flow, sourceId)) url.searchParams.set(flow === 'step' ? 'document' : 'job', sourceId)
  else if (flow && sample) url.searchParams.set('sample', flow)
  if (flow === 'step' && view === 'manufacturing') url.searchParams.set('view', view)
  window.history.replaceState({}, '', url)
}

type UnifiedProps = { embedded?: boolean; onOpenQuotation?(id?: string): void }
export default function UnifiedWorkbench({ embedded = false, onOpenQuotation }: UnifiedProps = {}) {
  const Container = embedded ? 'div' : 'main'
  const [flow, setFlow] = useState<Flow | null>(null)
  const [phase, setPhase] = useState<Phase>('idle')
  const [filename, setFilename] = useState('')
  const [sourceId, setSourceId] = useState<string | null>(null)
  const [sample, setSample] = useState(false)
  const [status, setStatus] = useState('')
  const [error, setError] = useState('')
  const [dragging, setDragging] = useState(false)
  const [packageUploading, setPackageUploading] = useState(false)
  const packageUpload = useRef(false)
  const [recent, setRecent] = useState<Recent[]>([])
  const [nativeAction, setNativeAction] = useState<CadEntryAction | null>(null)
  const [imageAction, setImageAction] = useState<ImageAction | null>(null)
  const [runKey, setRunKey] = useState('initial')
  const [childReady, setChildReady] = useState(false)
  const [handoffAccepted, setHandoffAccepted] = useState(false)
  const [imageStage, setImageStage] = useState<ImageStage>('source')
  const [imageSource, setImageSource] = useState<ImageBomSource | null>(null)
  const [imageSourceStatus, setImageSourceStatus] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const [imageSourceError, setImageSourceError] = useState('')
  const [imageSourceRefresh, setImageSourceRefresh] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const frameRef = useRef<HTMLIFrameElement>(null)
  const live = useRef({ flow, runKey, filename, sample, sourceId })
  live.current = { flow, runKey, filename, sample, sourceId }
  const imageActionRef = useRef<ImageAction | null>(null)
  imageActionRef.current = imageAction
  const sentImageRequest = useRef<string | null>(null)
  const acceptedImageRequest = useRef<string | null>(null)
  const readyRef = useRef(false)
  const readyTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const remember = useCallback((entry: Omit<Recent, 'updatedAt'>) => {
    const row: Recent = { ...entry, filename: short(entry.filename, 180), updatedAt: new Date().toISOString() }
    const rows = [row, ...storedRecent().filter(old => old.id !== entry.id)].slice(0, 8)
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(rows)) } catch { /* No file content is cached. */ }
    setRecent(rows)
  }, [])

  function prepare(next: Flow, name: string, isSample: boolean, id: string | null = null) {
    const key = randomId()
    if (readyTimer.current) clearTimeout(readyTimer.current)
    sentImageRequest.current = null; acceptedImageRequest.current = null; readyRef.current = false
    live.current = { flow: next, runKey: key, filename: name, sample: isSample, sourceId: id }
    setRunKey(key); setFlow(next); setPhase('processing'); setFilename(name); setSourceId(id); setSample(isSample)
    setStatus(next === 'step' ? '正在打开原生 CAD 工作区…' : '正在连接图纸识别工作区…')
    setError(''); setChildReady(false); setHandoffAccepted(false); setNativeAction(null); setImageAction(null)
    setImageStage('source'); setImageSource(null); setImageSourceStatus('idle'); setImageSourceError('')
    const view = next === 'step' && id && new URL(window.location.href).searchParams.get('view') === 'manufacturing' ? 'manufacturing' : undefined
    locationFor(next, id, isSample, view)
    return key
  }

  function chooseFile(file?: File) {
    if (!file || packageUpload.current) return
    if (/\.(zip|rar)$/i.test(file.name)) { void uploadPackage(file); return }
    if (file.size < 1 || file.size > 20 * 1024 * 1024) { setError('请选择非空、大小不超过 20 MB 的图纸。'); return }
    const next: Flow | null = /\.(step|stp)$/i.test(file.name) ? 'step' : /\.(jpe?g|png|webp|gif)$/i.test(file.name) || /^image\/(jpeg|png|webp|gif)$/.test(file.type) ? 'image' : null
    if (!next) { setError(/\.pdf$/i.test(file.name) ? '本次演示支持 STEP 和图片。PDF 可先将所需页面导出为图片，也可随 ZIP / RAR 作为报价参考附件上传。' : '请选择 ZIP / RAR 订单资料包、STEP/STP 或 JPG、PNG、WebP、GIF 图片。'); return }
    const key = prepare(next, file.name, false)
    if (next === 'step') setNativeAction({ id: key, type: 'file', file })
    else setImageAction({ requestId: key, type: 'load-file', file })
  }

  async function uploadPackage(file: File) {
    if (packageUpload.current) return
    if (!file.size || file.size > 50 * 1024 * 1024) { setError('请选择非空、大小不超过 50 MB 的 ZIP / RAR 订单资料包。'); return }
    packageUpload.current = true; setPackageUploading(true); setError('')
    try {
      const response = await fetch(`/workbench/api/quote-batches?name=${encodeURIComponent(file.name)}`, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/octet-stream', 'X-Workbench-Request': '1', 'X-Event-Id': crypto.randomUUID() }, body: file })
      let data: { id?: string; error?: { message?: string } | string }
      try { data = await response.json() } catch { throw new Error(response.status === 413 ? '压缩包超过服务器允许的大小，请缩小后重试。' : '暂未收到上传结果，请重试。') }
      if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : data.error?.message || '订单资料包上传未完成。')
      if (!data.id || !/^BATCH-[a-f0-9]{32}$/.test(data.id)) throw new Error('上传结果无效，请打开整单报价工作台重新上传。')
      if (onOpenQuotation) onOpenQuotation(data.id)
      else window.location.assign(`/workbench/quotation/?id=${encodeURIComponent(data.id)}`)
    } catch (failure) { setError(failure instanceof Error ? failure.message : '订单资料包上传未完成。') }
    finally { packageUpload.current = false; setPackageUploading(false) }
  }

  function openSample(next: Flow) {
    const key = prepare(next, next === 'step' ? 'Laser4DIY 机箱 · STEP 示例' : '钣金箱体 · 图片示例', true)
    if (next === 'step') setNativeAction({ id: key, type: 'sample' })
    else setImageAction({ requestId: key, type: 'open-sample' })
  }

  function resume(entry: Recent, update = true) {
    if (!idFor(entry.flow, entry.id)) return
    const key = prepare(entry.flow, entry.filename, entry.sample, entry.id)
    if (entry.flow === 'image') setImageAction({ requestId: key, type: 'restore-job', jobId: entry.id })
    if (update) setStatus('正在读取已保存结果，不重新上传图纸。')
  }

  const back = useCallback(() => {
    if (readyTimer.current) clearTimeout(readyTimer.current)
    live.current = { flow: null, runKey: 'home', filename: '', sample: false, sourceId: null }
    imageActionRef.current = null; readyRef.current = false; sentImageRequest.current = null; acceptedImageRequest.current = null
    setFlow(null); setPhase('idle'); setStatus(''); setError(''); setSourceId(null); setFilename(''); setSample(false); setNativeAction(null); setImageAction(null); setChildReady(false); setHandoffAccepted(false)
    setImageStage('source'); setImageSource(null); setImageSourceStatus('idle'); setImageSourceError('')
    setRecent(storedRecent()); locationFor(null, null, false)
  }, [])

  // Read the latest saved review on every downstream navigation. The child can
  // save a new review revision without changing its job ID or bridge phase.
  useEffect(() => {
    if (flow !== 'image' || phase !== 'review' || (!imageId(sourceId) && !sample)) {
      setImageSource(null); setImageSourceStatus('idle'); return
    }
    const controller = new AbortController(), expectedRun = runKey
    let active = true
    const timer = setTimeout(() => controller.abort(), 20000)
    setImageSourceStatus('loading'); setImageSourceError('')
    const url = imageId(sourceId) ? `${IMAGE_PATH}api/jobs/${sourceId}/review` : `${IMAGE_PATH}sample-manifest.json`
    void fetch(url, { credentials: 'same-origin', cache: 'no-store', signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error('暂未读取到最新的图纸校正结果，请稍后重试。')
      const data = await response.json()
      if (!active || controller.signal.aborted || live.current.runKey !== expectedRun || live.current.flow !== 'image') return
      if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('图纸结果格式无效，请返回图纸与参数查看。')
      const manifest = imageId(sourceId) ? data.manifest : data
      if (!manifest || typeof manifest !== 'object' || !Array.isArray(manifest.regions) || !Array.isArray(manifest.parameters)) throw new Error('本轮暂无可整理的分图和参数。')
      if (imageId(sourceId) && (data.job_id !== sourceId || !Number.isInteger(data.attempt) || !Number.isInteger(data.revision))) throw new Error('图纸结果版本不完整，请重新读取最新结果。')
      if (!imageId(sourceId) && manifest.mode !== 'predefined_sample_regions') throw new Error('样例资料无效。')
      setImageSource({ jobId: imageId(sourceId) ? sourceId : null, attempt: imageId(sourceId) ? data.attempt : null, revision: imageId(sourceId) ? data.revision : null, sourceSha256: imageId(sourceId) ? data.source_sha256 : manifest.source?.sha256, manifest })
      setImageSourceStatus('ready')
    }).catch(reason => {
      if (!active || live.current.runKey !== expectedRun || live.current.flow !== 'image') return
      setImageSourceStatus('error'); setImageSourceError(reason instanceof Error && reason.name !== 'AbortError' ? reason.message : '读取最新结果超时，请重试。')
    }).finally(() => clearTimeout(timer))
    return () => { active = false; controller.abort(); clearTimeout(timer) }
  }, [flow, phase, sourceId, sample, runKey, imageSourceRefresh])

  function navigateImageStage(next: ImageStage) {
    setImageStage(next)
    if (next !== 'source') setImageSourceRefresh(value => value + 1)
  }

  const nativeState = useCallback((value: CadWorkflowState) => {
    const current = live.current
    if (current.flow !== 'step' || current.runKey !== runKey) return
    setPhase(value.phase); setStatus(value.phase === 'review' ? '总装已读取，可查看部件并继续整理制造件。' : value.status)
    setError(value.error || '')
    if (value.filename) setFilename(value.filename)
    if (stepId(value.documentId)) {
      setSourceId(value.documentId); locationFor('step', value.documentId, current.sample, value.view)
      remember({ flow: 'step', id: value.documentId, filename: value.filename || current.filename, sample: current.sample })
    }
  }, [remember, runKey])

  const deliver = useCallback(() => {
    const action = imageActionRef.current
    if (live.current.flow !== 'image' || !readyRef.current || !action || sentImageRequest.current === action.requestId || !frameRef.current?.contentWindow) return
    sentImageRequest.current = action.requestId
    frameRef.current.contentWindow.postMessage({ channel: CHANNEL, ...action }, window.location.origin)
  }, [])

  useEffect(() => { deliver() }, [imageAction, childReady, deliver])
  useEffect(() => {
    const receive = (event: MessageEvent) => {
      if (event.origin !== window.location.origin || event.source !== frameRef.current?.contentWindow || live.current.flow !== 'image' || frameRef.current?.dataset.runKey !== live.current.runKey) return
      const data = event.data
      if (!data || typeof data !== 'object' || data.channel !== CHANNEL || typeof data.type !== 'string') return
      const action = imageActionRef.current
      if (data.type === 'ready') { if (readyTimer.current) clearTimeout(readyTimer.current); readyRef.current = true; setChildReady(true); deliver(); return }
      if (data.type === 'request-upload') { inputRef.current?.click(); return }
      if (data.type === 'back') { back(); return }
      if (data.requestId && data.requestId !== action?.requestId) return
      if (data.type === 'accepted' && action && data.requestId === action.requestId) { acceptedImageRequest.current = action.requestId; setHandoffAccepted(true); setError(''); return }
      if (data.type === 'error') { setPhase('error'); setError(short(data.message) || '图纸工作区暂未完成操作。'); return }
      if (data.type !== 'state' || !action || acceptedImageRequest.current !== action.requestId) return
      const nextPhase = ['idle', 'upload', 'processing', 'review', 'error'].includes(data.phase) ? data.phase as Phase : 'processing'
      setPhase(nextPhase)
      const messages: Record<Phase, string> = { idle: '图纸工作区已就绪。', upload: '正在上传图纸，随后自动分图并提取参数。', processing: '后台正在分图与提取参数，可稍后继续查看。', review: '分图和参数已载入，可核对并下载工程资料。', error: '本轮尚未完成，原图和已保存结果会保留。' }
      setStatus(short(data.message) || messages[nextPhase])
      if (typeof data.filename === 'string' && data.filename) setFilename(short(data.filename, 180))
      if (imageId(data.jobId)) { setSourceId(data.jobId); locationFor('image', data.jobId, false); remember({ flow: 'image', id: data.jobId, filename: short(data.filename, 180) || live.current.filename, sample: false }) }
    }
    window.addEventListener('message', receive)
    return () => { window.removeEventListener('message', receive); if (readyTimer.current) clearTimeout(readyTimer.current) }
  }, [back, deliver, remember])

  useEffect(() => {
    const rows = storedRecent(); setRecent(rows)
    const params = new URL(window.location.href).searchParams, kind = params.get('flow')
    if (kind === 'step' && stepId(params.get('document'))) resume(rows.find(row => row.id === params.get('document')) || { flow: 'step', id: params.get('document')!, filename: '已保存的 STEP 图纸', sample: false, updatedAt: '' }, false)
    else if (kind === 'image' && imageId(params.get('job'))) resume(rows.find(row => row.id === params.get('job')) || { flow: 'image', id: params.get('job')!, filename: '已保存的图片识别', sample: false, updatedAt: '' }, false)
    else if ((kind === 'step' || kind === 'image') && params.get('sample') === kind) openSample(kind)
    // Saved identities reopen results; refresh never repeats an uploaded File.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    const getState = () => ({ flow, phase, filename, sourceId, childReady, handoffAccepted, sample, error, imageStage, imageSourceStatus, imageSourceKey: imageSource ? sourceFingerprint(imageSource) : null })
    const target = window as typeof window & { HylUnifiedDemo?: { getState: typeof getState } }
    target.HylUnifiedDemo = { getState }
    return () => { delete target.HylUnifiedDemo }
  }, [flow, phase, filename, sourceId, childReady, handoffAccepted, sample, error, imageStage, imageSourceStatus, imageSource])

  function frameLoaded() {
    if (readyRef.current) return
    if (readyTimer.current) clearTimeout(readyTimer.current)
    readyTimer.current = setTimeout(() => { if (live.current.flow === 'image' && !readyRef.current) setError('图片工作区尚未连接。可先打开工作区完成原有登录，再返回这里。') }, 12000)
  }
  function retryFrame() {
    const action = imageActionRef.current
    if (!action || flow !== 'image') return
    const next: ImageAction = imageId(sourceId) ? { type: 'restore-job', requestId: randomId(), jobId: sourceId } : action
    if (readyTimer.current) clearTimeout(readyTimer.current)
    readyRef.current = false; sentImageRequest.current = null; acceptedImageRequest.current = null
    const key = randomId(); live.current = { ...live.current, runKey: key }; imageActionRef.current = next
    setChildReady(false); setHandoffAccepted(false); setImageAction(next); setRunKey(key); setError(''); setStatus('正在重新连接工作区，已有请求编号保持不变。')
  }
  const step = flow ? flow === 'image' ? imageStage === 'source' ? 2 : 3 : phase === 'review' ? 3 : 2 : 1
  return <Container className={`unified-app${flow ? ' unified-active' : ''}${embedded ? ' unified-embedded' : ''}`} data-testid="unified-workbench">
    {!embedded && <header className="unified-topbar"><a className="unified-brand" href="./" onClick={event => { event.preventDefault(); back() }}><span>H</span><strong>好运来</strong><small>图纸工作台</small></a><span className="unified-demo-tag"><i />演示版</span></header>}
    <div className="unified-flowbar" data-testid="unified-flow"><div><span className="unified-flow-label">工程资料整理</span>{['上传图纸', '拆件与识别', '物料与输出'].map((label, index) => <div className={`unified-flow-step${step === index + 1 ? ' active' : step > index + 1 ? ' done' : ''}`} key={label}><span>{step > index + 1 ? <Check size={12} /> : index + 1}</span>{label}{index < 2 && <ChevronRight size={13} />}</div>)}</div>{flow && <button className="unified-button ghost" data-testid="unified-back" onClick={back}><ArrowLeft size={14} />返回上传</button>}</div>
    {!flow ? <section className="unified-landing" data-testid="unified-landing">
      <div className="unified-package-entry"><div className="unified-package-icon"><FileArchive size={25} /></div><div><h2>整单报价</h2><p>上传 ZIP、RAR 或 STEP，整理拆件与工艺，再核对 BOM 和报价草稿。</p></div><a className="unified-button primary" data-testid="unified-batch-quote" href="/workbench/?workspace=orders&amp;scope=public" onClick={event => { if (onOpenQuotation) { event.preventDefault(); onOpenQuotation() } }}>进入整单报价<ArrowRight size={14} /></a></div>
      {packageUploading && <div className="unified-package-progress" role="status" data-testid="unified-package-uploading"><LoaderCircle size={16} className="unified-spin" />正在上传订单资料包，完成后进入报价条件确认…</div>}
      <div className="unified-intro"><p>从客户图纸开始</p><h1>上传图纸，进入对应的拆图流程。</h1><span>STEP 查看原生结构与制造零件；图片分离视图、展开图和表格，提取对应参数。</span></div>
      <div className={`unified-drop${dragging ? ' dragging' : ''}`} role="button" tabIndex={0} data-testid="unified-drop-zone" aria-label="选择或拖入图纸" aria-disabled={packageUploading} onClick={() => { if (!packageUploading) inputRef.current?.click() }} onKeyDown={event => { if (event.target === event.currentTarget && !packageUploading && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); inputRef.current?.click() } }} onDragOver={event => { event.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); if (event.dataTransfer.files.length > 1) setError('多份订单资料请放入一个 ZIP / RAR 压缩包后上传。'); else chooseFile(event.dataTransfer.files[0]) }}><div className="unified-upload-icon"><FileUp size={28} /></div><h2>选择文件，或拖放到这里</h2><p>ZIP / RAR · STEP / STP · JPG / PNG / WebP / GIF</p><span className="unified-button primary"><Upload size={15} />上传资料</span><small>ZIP / RAR 最大 50 MB · 单份图纸最大 20 MB · 自动识别文件类型</small></div>
      <div className="unified-route-summary"><span><Box size={16} /><strong>三维 CAD</strong>原生拆件、物料与加工草稿</span><span><FileImage size={16} /><strong>工程图片</strong>分图提参、物料与模拟报价</span></div>
      <div className="unified-examples-heading"><h2>先看一个例子</h2><span>使用已准备的样例体验流程</span></div>
      <div className="unified-examples"><button className="unified-example" data-testid="unified-step-sample" onClick={() => openSample('step')}><div className="unified-model-art" aria-hidden="true"><svg viewBox="0 0 260 160"><path d="M45 54 134 17l85 39-88 41z" fill="#bdd0de" /><path d="M45 54v64l86 39V97z" fill="#7295b0" /><path d="m131 97 88-41v65l-88 36z" fill="#8baabd" /><path d="m57 67 58 26v39l-58-26z" fill="#e8f0f5" /><path d="m145 103 57-26v33l-57 27z" fill="#e8f0f5" /><path d="M45 54 134 17l85 39v65l-88 36-86-39zM45 54l86 43 88-41M131 97v60" fill="none" stroke="#476d89" strokeWidth="2" /></svg></div><div className="unified-example-copy"><span className="unified-format">STEP 原生样例</span><h3>钣金机箱总装</h3><p>查看装配、选择部件，再进入制造零件与物料表。</p><span className="unified-example-action">打开 STEP 示例 <ArrowRight size={15} /></span></div></button><button className="unified-example" data-testid="unified-image-sample" onClick={() => openSample('image')}><div className="unified-drawing-art"><img src={IMAGE_PATH + 'assets/source.jpg'} alt="箱体多视图工程图" /></div><div className="unified-example-copy"><span className="unified-format">图片整理样例</span><h3>箱体视图与折弯表</h3><p>查看分图、参数与来源。采用预设分区，不调用模型。</p><span className="unified-example-action">打开图片示例 <ArrowRight size={15} /></span></div></button></div>
      {!!recent.length && <section className="unified-recent"><div className="unified-examples-heading"><h2><FolderOpen size={16} />继续已保存的图纸</h2><span>读取已有结果，不重新上传</span></div>{recent.map(entry => <button className="unified-recent-item" key={entry.id} data-testid={`unified-recent-${entry.id}`} onClick={() => resume(entry)}>{entry.flow === 'step' ? <Box size={17} /> : <FileImage size={17} />}<strong>{entry.filename}</strong><span>{entry.flow === 'step' ? '原生 CAD' : '图片识别'}</span><ArrowRight size={15} /></button>)}</section>}
    </section> : <section className="unified-workspace" data-testid="unified-workspace">
      <div className="unified-document-bar"><div className="unified-document-icon">{flow === 'step' ? <Box size={21} /> : <FileImage size={21} />}</div><div><span>{flow === 'step' ? 'STEP · 原生拆件' : sample ? '图片 · 预设整理示例' : '图片 · 分图与提参'}</span><h1>{filename || '正在打开图纸…'}</h1></div><button className="unified-button" data-testid="unified-new-file" onClick={() => inputRef.current?.click()}><RefreshCw size={14} />更换图纸</button></div>
      <div className={`unified-common-status ${phase}`} role="status" data-testid="unified-status">{phase === 'processing' || phase === 'upload' ? <LoaderCircle size={15} className="unified-spin" /> : phase === 'review' ? <Check size={15} /> : <FileUp size={15} />}<span>{status}</span>{sample && <small>{flow === 'step' ? '公开 STEP 样例' : '预设分区 · 不调用模型'}</small>}</div>
      {flow === 'step' ? <CadWorkbench key={runKey} embedded entryAction={nativeAction} onWorkflowState={nativeState} /> : <>
        <nav className="unified-image-stages" aria-label="图片加工流程">
          {([['source', '图纸与参数'], ['engineering', '加工特征'], ['bom', '物料 BOM'], ['cost', '用料与成本'], ['quote', '报价审核']] as const).map(([value, label], index) => <button key={value} type="button" data-testid={`image-stage-${value}`} className={imageStage === value ? 'active' : ''} aria-current={imageStage === value ? 'step' : undefined} disabled={value !== 'source' && phase !== 'review'} onClick={() => navigateImageStage(value)}><span>{index + 1}</span>{label}{value === 'cost' || value === 'quote' ? <small>演示</small> : null}</button>)}
        </nav>
        <div className="unified-image-shell" hidden={imageStage !== 'source'}>{!childReady && <div className="unified-connecting"><LoaderCircle size={18} className="unified-spin" /><span>正在打开图片工作区…</span></div>}<iframe key={runKey} ref={frameRef} src={IMAGE_PATH + '?embedded=1'} title="图纸分图与参数工作区" data-testid="unified-frame" data-run-key={runKey} onLoad={frameLoaded} onError={() => setError('图片工作区暂时无法连接，已有图纸不会删除。')} allow="fullscreen" /></div>
        {imageStage === 'source' && phase === 'review' && <div className="unified-image-next"><div><strong>查看每个零件的加工特征</strong><span>归并孔槽、折弯、焊接与后处理要求，再整理物料和用料依据。</span></div><button type="button" className="unified-button primary" data-testid="image-next-engineering" onClick={() => navigateImageStage('engineering')}>下一步 · 加工特征<ArrowRight size={15} /></button></div>}
        <div hidden={imageStage === 'source'} className="unified-image-downstream">
          {imageSourceStatus === 'loading' && <div className="unified-source-feedback" role="status"><LoaderCircle size={15} className="unified-spin" />正在读取最新图纸与校正版本…</div>}
          {imageSourceStatus === 'error' && <div className="unified-source-feedback error" role="alert" data-testid="image-source-error"><span>{imageSourceError} 读取完成前暂停计算与导出。</span><button type="button" className="unified-button" onClick={() => setImageSourceRefresh(value => value + 1)}>重新读取</button></div>}
          {phase !== 'review' && <div className="unified-source-feedback">请先完成图纸分图与提参，再整理物料。</div>}
          <div hidden={imageStage !== 'engineering'}><ImageEngineeringPanel source={imageSource} filename={filename} sample={sample} disabled={imageSourceStatus !== 'ready' || phase !== 'review'} onViewSource={() => navigateImageStage('source')} onNext={() => navigateImageStage('bom')} /></div>
          <div hidden={imageStage === 'engineering'}><ImageBomPanel key={runKey} source={imageSource} filename={filename} sample={sample} stage={imageStage === 'source' || imageStage === 'engineering' ? 'bom' : imageStage} disabled={imageSourceStatus !== 'ready' || phase !== 'review'} onStageChange={navigateImageStage} onViewSource={() => navigateImageStage('source')} onViewEngineering={() => navigateImageStage('engineering')} /></div>
        </div>
      </>}
    </section>}
    {error && <div className="unified-error" role="alert" data-testid="unified-error"><span>{error}</span>{flow === 'image' && <button data-testid="unified-frame-retry" onClick={retryFrame}>重新连接工作区</button>}{flow === 'image' && !childReady && <a href={IMAGE_PATH} target="_blank" rel="noopener">打开图片工作区 ↗</a>}<button type="button" onClick={() => setError('')}>知道了</button></div>}
    {!flow && <footer className="unified-footer">一个入口，按图纸类型进入处理流程。识别和工程参数保留人工核对记录。</footer>}
    <input ref={inputRef} type="file" accept=".zip,.rar,.step,.stp,.jpg,.jpeg,.png,.webp,.gif" data-testid="unified-upload" hidden onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; chooseFile(file) }} />
  </Container>
}
