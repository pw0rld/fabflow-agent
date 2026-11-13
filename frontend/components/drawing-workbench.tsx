'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, Check, ChevronRight, FileText, FolderOpen, History, Layers3, LoaderCircle, Sparkles, UploadCloud, X } from 'lucide-react'
import { useRecognition } from '@/hooks/use-recognition'
import RecognitionWorkspace from '@/components/recognition-workspace'
import { DemoRecognition } from '@/components/demo-recognition'
import { MockStages, useMockFlow } from '@/components/mock-stages'
import { DEMO_PARTS, type MockPartIdentity } from '@/lib/demo-data'
import { isRecognitionRunning, readingStatusLabel, recognitionApi, RECOGNITION_LAST_TASK, validTaskId, type RecognitionTaskSummary } from '@/lib/recognition-api'

const stages = ['上传图纸', '识别与拆件', '用料与价格', 'BOM 与报价', '人工审核']
const accepted = /\.(png|jpe?g|webp|gif|bmp|tiff?|pdf|dxf|dwg|step|stp)$/i

export default function DrawingWorkbench() {
  const recognition = useRecognition()
  const inputRef = useRef<HTMLInputElement>(null)
  const submissionStarted = useRef(false)
  const taskRef = useRef(recognition.task)
  taskRef.current = recognition.task
  const busyRef = useRef(recognition.busy)
  busyRef.current = recognition.busy
  const [stage, setStage] = useState(0)
  const [screen, setScreen] = useState<'flow' | 'history'>('flow')
  const [source, setSource] = useState<'real' | 'sample'>('real')
  const [selected, setSelected] = useState<string | null>(null)
  const [files, setFiles] = useState<File[]>([])
  const [note, setNote] = useState('')
  const [quantity, setQuantity] = useState('10')
  const [error, setError] = useState('')
  const [recent, setRecent] = useState<string | null>(null)
  const [records, setRecords] = useState<RecognitionTaskSummary[]>([])
  const [recordsLoading, setRecordsLoading] = useState(false)
  const [recordsError, setRecordsError] = useState('')
  const orderQty = /^\d+$/.test(quantity) && Number(quantity) >= 1 && Number(quantity) <= 100000 ? Number(quantity) : null
  const task = recognition.task
  const running = isRecognitionRunning(task)
  const parts = task?.recognition?.parts || []
  const canContinue = source === 'sample' || Boolean(!recognition.loading && !files.length && task?.run?.status === 'completed' && parts.length && task.recognition?.source_current)
  const identities = useMemo<MockPartIdentity[]>(() => source === 'sample' ? DEMO_PARTS : parts.map(part => {
    const text = (names: string[]) => part.observations.filter(row => names.includes(row.name)).map(row => row.raw_text).join('；') || null
    return { id: part.key, name: part.name, code: part.part_number, version: part.version, material: text(['material_grade', 'material_family']), thickness: text(['thickness']), size: null }
  }), [source, parts])
  const mockFlow = useMockFlow(identities, orderQty || 1)

  function setHash(hash: string) { window.history.replaceState(null, '', hash) }
  function chooseStage(next: number) {
    if (recognition.busy || recognition.loading) return
    if (next > 0 && source === 'real' && files.length) { setError('有尚未提交的新图纸，请先点击“开始识别”。'); return }
    if (next > 1 && (!canContinue || !orderQty)) { setError('请先完成解析并确认有明确零件，订单数量需为 1 至 100000 的整数。'); return }
    if (next === 1 && source === 'real' && !task) return
    setError(''); setScreen('flow'); setStage(next)
    setHash(source === 'sample' ? '#sample' : task && next > 0 ? '#task/' + task.id : '#new')
  }
  function home() {
    if (recognition.busy) return
    setStage(0); setScreen('flow'); setSource('real'); setFiles([]); setError(''); setNote(''); submissionStarted.current = false; setHash('#new')
  }
  async function showHistory() {
    if (recognition.busy) return
    setScreen('history'); setHash('#tasks'); setRecordsLoading(true); setRecordsError('')
    try { setRecords(await recognitionApi.list()) }
    catch (failure) { setRecordsError(failure instanceof Error ? failure.message : '识别记录暂不可读取。') }
    finally { setRecordsLoading(false) }
  }
  async function openRecord(id: string) {
    if (!validTaskId(id) || recognition.busy) return
    setSource('real'); setScreen('flow'); setStage(1); setSelected(null); setFiles([]); setError(''); setHash('#task/' + id)
    await recognition.load(id)
  }
  function sample() {
    if (recognition.busy) return
    setSource('sample'); setScreen('flow'); setStage(1); setSelected(DEMO_PARTS[0].id); setError(''); setHash('#sample')
  }
  useEffect(() => {
    try { const id = localStorage.getItem(RECOGNITION_LAST_TASK); if (validTaskId(id)) setRecent(id) } catch { /* optional last-record hint */ }
    function route() {
      if (busyRef.current) {
        setHash(taskRef.current ? '#task/' + taskRef.current.id : '#new')
        return
      }
      const hash = location.hash.slice(1)
      const match = /^task\/([A-Za-z0-9_-]+)(?:\/.*)?$/.exec(hash)
      if (match) {
        setSource('real'); setScreen('flow'); setStage(1); setSelected(null)
        if (taskRef.current?.id !== match[1]) void recognition.load(match[1])
      } else if (hash === 'tasks') { void showHistory() }
      else if (hash === 'sample') { setSource('sample'); setScreen('flow'); setStage(1); setSelected(DEMO_PARTS[0].id) }
      else { setSource('real'); setScreen('flow'); setStage(0) }
    }
    route(); window.addEventListener('hashchange', route)
    return () => window.removeEventListener('hashchange', route)
    // The route listener uses a ref for the current task, and load is stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recognition.load])
  useEffect(() => { if (task) setRecent(task.id) }, [task?.id])

  function addFiles(incoming: FileList | null) {
    if (!incoming || recognition.busy) return
    const selectedFiles: File[] = []; const rejected: string[] = []
    for (const file of Array.from(incoming)) {
      if (!accepted.test(file.name)) { rejected.push(file.name + '：格式暂不支持'); continue }
      if (!file.size || file.size > 25 * 1024 * 1024) { rejected.push(file.name + '：须大于 0 且不超过 25 MB'); continue }
      selectedFiles.push(file)
    }
    setFiles(current => [...current, ...selectedFiles.filter(file => !current.some(item => item.name === file.name && item.size === file.size && item.lastModified === file.lastModified))])
    setError(rejected.join('；')); submissionStarted.current = false
    if (inputRef.current) inputRef.current.value = ''
  }
  async function start() {
    if (!files.length || recognition.busy) return
    if (!orderQty) { setError('订单数量 / 套数需为 1 至 100000 的整数，仅用于后续模拟展示。'); return }
    setError(''); setSource('real'); setSelected(null)
    if (!submissionStarted.current) { recognition.reset(); submissionStarted.current = true }
    const result = await recognition.start(files, note)
    if (result) { setStage(1); setFiles([]); setHash('#task/' + result.id) }
  }
  const title = source === 'sample' ? '内置机箱样例' : task?.attachments[0]?.name || '新建图纸解析'
  const showFlow = screen === 'flow'
  return <main className="app-shell">
    <header className="topbar"><button className="brand brand-home" onClick={home} aria-label="图智工坊，上传新图纸"><span className="brand-mark"><Layers3 size={18}/></span><span>图智工坊</span><small>图纸驱动报价工作台</small></button><div className="top-actions"><span className={`status-dot ${source === 'sample' ? 'sample-dot' : ''}`}/><span>{source === 'sample' ? '内置演示样例' : '真实图纸解析'}</span><button className="text-button" onClick={() => void showHistory()} disabled={recognition.busy} data-testid="task-history"><History size={16}/>识别记录</button></div></header>
    <div className="flowbar"><div className="flow-title"><strong>{title}</strong><span className={`sample-tag ${source === 'real' && stage < 2 ? 'real-tag' : ''}`}>{source === 'real' && stage < 2 ? '真实解析' : '模拟演示'}</span></div>{stages.map((label, index) => <button key={label} className={`flow-step ${stage === index && showFlow ? 'active' : ''} ${stage > index && showFlow ? 'done' : ''}`} disabled={recognition.busy || recognition.loading || (index === 1 && source === 'real' && (!task || Boolean(files.length))) || (index > 1 && !canContinue)} onClick={() => chooseStage(index)} aria-current={stage === index && showFlow ? 'step' : undefined}><span>{stage > index && showFlow ? <Check size={12}/> : `0${index + 1}`}</span>{label}</button>)}</div>
    {error && <div className="global-alert" role="alert">{error}</div>}
    {screen === 'history' ? <section className="step-page history-page"><div className="history-heading"><div><span className="eyebrow">已保存的真实解析结果</span><h1>识别记录</h1></div><button className="primary-button" onClick={home}><UploadCloud size={16}/>上传新图纸</button></div>{recordsError && <div className="global-alert" role="alert">{recordsError}</div>}{recordsLoading ? <p className="loading-state"><LoaderCircle size={18} className="spin"/>正在读取记录…</p> : records.length ? <div className="history-list">{records.map(record => <button className="history-record" key={record.id} onClick={() => void openRecord(record.id)}><div><strong>{record.recognition_name || '图纸识别记录'}</strong><small>{record.attachment_count} 份附件 · {new Date(record.updated_at).toLocaleString('zh-CN')}</small></div><span className="record-status">{readingStatusLabel(record.run?.status)}</span><ChevronRight size={16}/></button>)}</div> : <div className="empty-state">还没有识别记录，上传图纸即可开始。</div>}</section> : stage === 0 ? <section className="hero">
      <div className="eyebrow"><Sparkles size={15}/>01 · 上传图纸</div><h1>从图纸开始，<em>让零件信息清晰可见</em></h1><p className="hero-copy">上传图片、PDF 或 CAD，AI 提取材料、板厚、尺寸与技术备注。<br/>解析完成后，可继续体验用料、价格与报价的模拟流程。</p>
      <div className="upload-card" onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); addFiles(event.dataTransfer.files) }}><input ref={inputRef} data-testid="drawing-upload" type="file" multiple accept=".png,.jpg,.jpeg,.webp,.gif,.bmp,.tif,.tiff,.pdf,.dxf,.dwg,.step,.stp" disabled={recognition.busy} hidden onChange={event => addFiles(event.target.files)}/><div className="upload-icon"><UploadCloud size={25}/></div><h2>{files.length ? `已选择 ${files.length} 份资料` : '拖放图纸到这里'}</h2><p>支持图片、PDF、CAD · 可一次上传多份 · 单个文件不超过 25 MB</p><button className="primary-button" onClick={() => inputRef.current?.click()} disabled={recognition.busy}><FolderOpen size={17}/>选择图纸</button>{files.length > 0 && <div className="file-chips">{files.map((file, index) => <span key={`${file.name}-${index}`}><FileText size={14}/>{file.name}<button type="button" aria-label={'移除 ' + file.name} disabled={recognition.busy} onClick={() => { setFiles(current => current.filter((_, i) => i !== index)); submissionStarted.current = false }}><X size={12}/></button></span>)}</div>}</div>
      <div className="order-card"><label htmlFor="order-quantity">本次订单数量 / 套数 <input id="order-quantity" data-testid="order-quantity" type="number" min="1" max="100000" value={quantity} onChange={event => setQuantity(event.target.value)}/><small>仅用于后续模拟数量，不作为图纸识别结果</small></label></div>
      <details className="recognition-note"><summary>识别说明（可选）</summary><textarea aria-label="识别说明" value={note} onChange={event => setNote(event.target.value)} placeholder="例如：请重点查看板厚、焊接和密封要求" disabled={recognition.busy}/></details>
      {recognition.error && <div className="upload-error" role="alert">{recognition.error}</div>}
      <div className="landing-actions"><button className="demo-button" onClick={sample} disabled={recognition.busy} data-testid="demo-sample"><Sparkles size={16}/>体验演示样例</button>{files.length > 0 && <button className="recognize-button" onClick={() => void start()} disabled={recognition.busy} data-testid="start-recognition">{recognition.busy ? <LoaderCircle size={16} className="spin"/> : <Sparkles size={16}/>} {recognition.busy ? '正在上传并提交解析…' : '开始识别'}<ChevronRight size={16}/></button>}</div>
      {recent && <button className="resume-button text-button" onClick={() => void openRecord(recent)} disabled={recognition.busy}>{running && task?.id === recent ? '查看正在后台解析的任务' : '继续查看上次解析'}<ChevronRight size={14}/></button>}
    </section> : stage === 1 ? source === 'sample' ? <DemoRecognition selected={selected} onSelect={setSelected} onContinue={() => chooseStage(2)} onBack={home}/> : recognition.loading ? <div className="loading-state"><LoaderCircle size={20} className="spin"/>正在读取已保存的解析记录…</div> : task ? <RecognitionWorkspace task={task} selectedKey={selected} onSelectedKeyChange={setSelected} onContinue={() => chooseStage(2)} onBack={home} onRetry={() => void recognition.retry()} onStop={() => void recognition.stop()} busy={recognition.busy} error={recognition.error}/> : <section className="step-page"><div className="global-alert" role="alert">{recognition.error || '暂无解析记录，请上传图纸开始。'}</div><button className="text-button" onClick={home}><ArrowLeft size={16}/>返回上传</button></section> : null}
    {showFlow && stage >= 2 && <MockStages stage={stage as 2 | 3 | 4} parts={identities} orderQty={orderQty || 1} flow={mockFlow} onNext={() => chooseStage(Math.min(4, stage + 1))} onBack={() => chooseStage(Math.max(1, stage - 1))} onStage={chooseStage}/>}
    <footer className="app-footer">图智工坊 Demo <span>·</span> 上传图纸接真实 AI 解析；用料、价格、BOM 与审核为模拟演示</footer>
  </main>
}
