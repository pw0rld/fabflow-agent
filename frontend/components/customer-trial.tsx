'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowDownToLine, ArrowRight, Box, Check, ChevronRight, CircleHelp, Clock3, FileCheck2, FileText, FolderOpen, History, Layers3, LoaderCircle, Plus, RefreshCw, RotateCcw, Scale, ShieldCheck, UploadCloud } from 'lucide-react'
import BatchPartViewer, { safePartPreviewUrl } from '@/components/batch-part-viewer'
import SourceAssemblyPreview from '@/components/source-assembly-preview'
import ProcessQuotePanel from '@/components/process-quote-panel'
import type { CustomerOrder } from '@/components/customer-upload-portal'
import { estimatedWeight, quotationCount, quotationNumber } from '@/lib/batch-display-format'
import { savedWeightDisplay } from '@/lib/customer-weight-summary'
import './customer-trial.css'

const API = '/workbench/customer/api'
const LAST = 'fabflow-trial-last-order'
const validOrder = (value: unknown): value is string => typeof value === 'string' && /^BATCH-[a-f0-9]{32}$/.test(value)
const validRun = (value: unknown): value is string => typeof value === 'string' && /^RUN-[a-f0-9]{32}$/.test(value)
const validPart = (value: unknown): value is string => typeof value === 'string' && /^L[0-9]{4,8}$/.test(value)
const busy = (value?: string) => value === 'queued' || value === 'running'
const statuses: Record<string, string> = { uploaded: '资料已保存', queued: '等待后台处理', running: '正在读取模型与拆件', completed: '已生成工程与报价草稿', completed_with_issues: '已整理，部分项目待核对', failed: '本轮处理未完成', interrupted: '处理已中断' }
type Quota = { limit: number; used: number; remaining: number; scope: string; max_files: number; max_bytes: number; blocked: boolean; accepted_extensions: string[] }
type Part = { id: string; part_name: string; source: string; quantity_total: number | null; quantity_per_set: number | null; thickness_mm: string | null; dimensions_mm: number[] | null; estimated_net_weight_kg: string | null; material: { text: string; source: string }; geometry_url: string | null; thumbnail_url: string | null; blockers: string[]; processes: { id: string; name: string; status: string }[] }
type Manifest = { batch_id: string; run_id: string; parts: Part[]; summary: { parts_count: number; quantity_total: number } }
type UploadAttempt = { file: File; event: string; submitted: boolean; uncertain: boolean }
type StartAttempt = { id: string; action: 'start' | 'retry'; payload: { event_id: string; expected_revision: number; order_sets?: number }; submitted: boolean; uncertain: boolean }
class RequestError extends Error { constructor(message: string, readonly status: number) { super(message) } }
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers); headers.set('X-Workbench-Request', '1')
  const response = await fetch(API + path, { ...init, headers, cache: 'no-store', credentials: 'same-origin', redirect: 'error' })
  let value: unknown
  try { value = await response.json() } catch { throw new RequestError(response.status === 413 ? '文件超过 20 MB，请缩小后上传。' : '连接暂未完成，请恢复本次请求或稍后刷新。', response.status) }
  if (!response.ok) { const failure = value as { error?: string | { message?: string } }; throw new RequestError(typeof failure.error === 'string' ? failure.error : failure.error?.message || '操作未完成，请稍后重试。', response.status) }
  return value as T
}
const jsonPost = (value: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) })
const eventId = () => 'customer-' + crypto.randomUUID()
const money = (value: string | null | undefined) => value == null || !Number.isFinite(Number(value)) ? '待核算' : `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
const date = (value: string) => new Date(value).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
function validateQuota(value: Quota) {
  if (!value || value.scope !== 'global' || value.limit !== 10 || !Number.isSafeInteger(value.used) || value.used < 0 || !Number.isSafeInteger(value.remaining) || value.remaining < 0 || value.remaining !== Math.max(0, value.limit - value.used) || value.max_files !== 1 || value.max_bytes !== 20 * 1024 * 1024 || typeof value.blocked !== 'boolean' || !Array.isArray(value.accepted_extensions) || value.accepted_extensions.length !== 2 || !value.accepted_extensions.includes('.step') || !value.accepted_extensions.includes('.stp')) throw new Error('暂未读取到体验额度，请刷新后再上传。')
  return value
}
function quoteUrl(value: string | undefined, order: CustomerOrder, print = false) {
  const run = order.quote?.run_id, filename = print ? 'customer-print.html' : 'customer-quote.html'
  const expected = validOrder(order.id) && validRun(run) ? `${API}/orders/${order.id}/runs/${run}/${filename}` : null
  return value === expected ? expected : null
}
function partAsset(part: Part, order: CustomerOrder, type: 'geometry' | 'thumbnail') {
  const run = order.quote?.run_id
  if (!validOrder(order.id) || !validRun(run) || !validPart(part.id)) return null
  const expected = `${API}/orders/${order.id}/runs/${run}/customer-parts/${part.id}${type === 'thumbnail' ? '/thumbnail' : ''}`
  return (type === 'geometry' ? part.geometry_url : part.thumbnail_url) === expected ? expected : null
}

export default function CustomerTrial() {
  const [ready, setReady] = useState(false), [orders, setOrders] = useState<CustomerOrder[]>([]), [selected, setSelected] = useState<string | null>(null)
  const [quota, setQuota] = useState<Quota | null>(null), [quotaLoading, setQuotaLoading] = useState(false), [quotaError, setQuotaError] = useState('')
  const [file, setFile] = useState<File | null>(null), [quantity, setQuantity] = useState('1'), [submitting, setSubmitting] = useState(false), [loading, setLoading] = useState(false)
  const [error, setError] = useState(''), [connectionError, setConnectionError] = useState(''), [dragging, setDragging] = useState(false), [poll, setPoll] = useState(0)
  const [tab, setTab] = useState<'model' | 'parts' | 'quote'>('model'), [manifest, setManifest] = useState<Manifest | null>(null), [partId, setPartId] = useState<string | null>(null)
  const [partsLoading, setPartsLoading] = useState(false), [partsError, setPartsError] = useState(''), [partsTick, setPartsTick] = useState(0), [viewer, setViewer] = useState<string | null>(null)
  const [quotePreview, setQuotePreview] = useState(false)
  const mounted = useRef(true), selectedRef = useRef<string | null>(null), input = useRef<HTMLInputElement>(null), lock = useRef(false), quotaEpoch = useRef(0)
  const uploadAttempt = useRef<UploadAttempt | null>(null), startAttempt = useRef<StartAttempt | null>(null)
  const current = orders.find(order => order.id === selected) || null
  const running = busy(current?.status), hasQuote = !!current?.quote
  const canUpload = ready && !!quota && !quota.blocked && quota.remaining > 0 && !quotaLoading
  const recoverUpload = !current && !!file && uploadAttempt.current?.file === file && uploadAttempt.current.submitted && uploadAttempt.current.uncertain
  const recoverProcessing = !!current && startAttempt.current?.id === current.id && startAttempt.current.submitted && startAttempt.current.uncertain
  const selectedPart = manifest?.parts.find(part => part.id === partId) || manifest?.parts[0] || null
  const geometry = current && selectedPart ? partAsset(selectedPart, current, 'geometry') : null
  const processFirst = current?.quote?.costing_mode === 'process-v1'
  const completeQuote = !!current?.quote && current.quote.total_price != null && current.quote.unpriced_lines === 0 && (!processFirst || ['estimated', 'manual_review'].includes(current.quote.pricing_status || ''))
  const knownSubtotal = completeQuote ? null : current?.quote?.estimated_subtotal ?? (processFirst ? null : current?.quote?.total_price ?? null)
  const needsAttention = !!current && ['failed', 'interrupted', 'completed_with_issues'].includes(current.status)
  const weights = savedWeightDisplay(current?.quote?.weight_summary)
  const quoted = current && current.quote ? quoteUrl(current.quote.quote_url, current) : null
  const printable = current && current.quote ? quoteUrl(current.quote.print_url, current, true) : null
  const phase = !current ? 1 : !hasQuote ? 2 : 3
  const adopt = useCallback((value: CustomerOrder) => { setOrders(previous => [value, ...previous.filter(order => order.id !== value.id)]) }, [])
  const choose = useCallback((order: CustomerOrder | null) => {
    selectedRef.current = order?.id || null; setSelected(order?.id || null); setQuantity(String(order?.order_sets || 1)); setTab('model'); setViewer(null); setQuotePreview(false); setError(''); setConnectionError('')
    if (!order) { setFile(null); uploadAttempt.current = null; startAttempt.current = null }
    try { if (order) localStorage.setItem(LAST, order.id); else localStorage.removeItem(LAST) } catch { /* Storage is optional; server cookie owns orders. */ }
  }, [])
  const refreshQuota = useCallback(async () => {
    const epoch = ++quotaEpoch.current; setQuotaLoading(true); setQuotaError('')
    try { const value = validateQuota(await request<Quota>('/quota')); if (mounted.current && epoch === quotaEpoch.current) setQuota(value); return value }
    catch (failure) { if (mounted.current && epoch === quotaEpoch.current) { setQuota(null); setQuotaError(failure instanceof Error ? failure.message : '体验额度暂不可用。') }; return null }
    finally { if (mounted.current && epoch === quotaEpoch.current) setQuotaLoading(false) }
  }, [])
  const initialize = useCallback(async () => {
    setLoading(true); setConnectionError('')
    try {
      await request('/session', jsonPost({})); const result = await request<{ orders: CustomerOrder[] }>('/orders')
      if (!mounted.current) return
      setReady(true); setOrders(result.orders); void refreshQuota()
      let last = selectedRef.current; try { last = last || localStorage.getItem(LAST) } catch { /* Optional. */ }
      const saved = result.orders.find(order => validOrder(last) && order.id === last); if (saved) choose(saved)
    } catch (failure) { if (mounted.current) setConnectionError(failure instanceof Error ? failure.message : '暂时无法连接。') }
    finally { if (mounted.current) setLoading(false) }
  }, [choose, refreshQuota])
  useEffect(() => { mounted.current = true; void initialize(); return () => { mounted.current = false } }, [initialize])
  useEffect(() => {
    if (!ready) return
    const refresh = () => { if (document.visibilityState === 'visible' && !lock.current) void refreshQuota() }
    window.addEventListener('focus', refresh); document.addEventListener('visibilitychange', refresh)
    return () => { window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', refresh) }
  }, [ready, refreshQuota])
  useEffect(() => {
    if (!ready || !selected || !running) return
    let cancelled = false
    const timer = setTimeout(async () => {
      try { const value = await request<CustomerOrder>(`/orders/${selected}`); if (!cancelled) { adopt(value); setConnectionError('') } }
      catch (failure) { if (!cancelled) setConnectionError(failure instanceof Error ? failure.message : '连接暂时中断，后台仍会继续。') }
      finally { if (!cancelled) setPoll(value => value + 1) }
    }, 3000)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [ready, selected, running, adopt, poll])
  useEffect(() => {
    setManifest(null); setPartsError(''); setPartId(null); setViewer(null)
    if (!current?.quote || !validRun(current.quote.run_id)) { setPartsLoading(false); return }
    const abort = new AbortController(); setPartsLoading(true)
    const order = current.id, run = current.quote.run_id
    void request<Manifest>(`/orders/${order}/runs/${run}/manufacturing`, { signal: abort.signal }).then(value => {
      if (value.batch_id !== order || value.run_id !== run || !Array.isArray(value.parts) || value.parts.some(part => !validPart(part.id))) throw new Error('零件资料与当前询价不一致，请重新载入。')
      if (!abort.signal.aborted) { setManifest(value); setPartId(value.parts[0]?.id || null) }
    }).catch(failure => { if (!abort.signal.aborted) setPartsError(failure instanceof Error ? failure.message : '拆件资料暂不可用。') }).finally(() => { if (!abort.signal.aborted) setPartsLoading(false) })
    return () => abort.abort()
  }, [current?.id, current?.quote?.run_id, current?.revision, partsTick])

  function pick(files: FileList | null) {
    setDragging(false); setError('')
    if (uploadAttempt.current?.submitted && uploadAttempt.current.uncertain) { setError('请先恢复上次上传结果，或从我的询价打开已保存的资料。'); return }
    setFile(null); uploadAttempt.current = null; startAttempt.current = null
    if (!files || files.length !== 1) { setError('每次请选择一份 STEP / STP 文件。'); return }
    if (!canUpload) { setError(quota?.remaining === 0 ? '本轮新上传额度已用完，已有询价仍可查看。' : '请先刷新体验额度。'); return }
    const chosen = files[0]
    if (!/\.(step|stp)$/i.test(chosen.name)) { setError('目前仅接收 STEP / STP，不接收压缩包或其他格式。'); return }
    if (!chosen.size || chosen.size > 20 * 1024 * 1024) { setError('请选择非空文件，大小不超过 20 MB。'); return }
    setFile(chosen); uploadAttempt.current = { file: chosen, event: eventId(), submitted: false, uncertain: false }; startAttempt.current = null
  }
  async function submit(action: 'start' | 'retry' = 'start', recovery?: 'upload' | 'processing') {
    if (lock.current || !ready) return
    if (recovery !== 'processing' && (!/^[1-9][0-9]*$/.test(quantity) || Number(quantity) > 100000)) { setError('数量请输入 1 至 100000 的整数。'); return }
    lock.current = true; setSubmitting(true); setError('')
    try {
      let order = current
      if (!order) {
        if (!file) throw new Error('请先选择一份 STEP / STP 文件。')
        const saved = uploadAttempt.current, replay = recovery === 'upload' && saved?.file === file && saved.submitted && saved.uncertain
        if (recovery === 'upload' && !replay) throw new Error('当前没有可恢复的上传请求。')
        const latest = await refreshQuota()
        if (!replay && (!latest || latest.blocked || latest.remaining < 1)) throw new Error(latest?.remaining === 0 ? '本轮新上传额度已用完，已有询价仍可继续处理。' : '体验额度暂不可用，请稍后刷新。')
        const attempt = saved?.file === file ? saved : { file, event: eventId(), submitted: false, uncertain: false }
        uploadAttempt.current = attempt; attempt.submitted = true; attempt.uncertain = false
        try { order = await request<CustomerOrder>(`/orders?name=${encodeURIComponent(attempt.file.name)}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Event-Id': attempt.event }, body: attempt.file }); if (!validOrder(order.id)) throw new Error('上传结果未确认，请恢复本次请求。') }
        catch (failure) { if (failure instanceof RequestError && failure.status >= 400 && failure.status < 500) uploadAttempt.current = null; else attempt.uncertain = true; throw failure }
        uploadAttempt.current = null; adopt(order); choose(order); setQuantity(quantity); setFile(null); void refreshQuota()
      }
      let attempt = startAttempt.current
      const replay = recovery === 'processing' && attempt?.id === order.id && attempt.submitted && attempt.uncertain
      if (recovery === 'processing' && !replay) throw new Error('当前没有可恢复的处理请求。')
      if (!replay && attempt?.id === order.id && attempt.submitted && attempt.uncertain) throw new Error('请先恢复上次处理请求。')
      if (!replay && (action === 'retry' ? order.can_retry === false || order.retry_remaining === 0 : order.can_start === false)) throw new Error('当前询价不能重复处理，请查看已有结果。')
      const sets = replay ? attempt!.payload.order_sets || order.order_sets || 1 : Number(quantity)
      if (!replay && (!attempt || attempt.id !== order.id || attempt.action !== action || action === 'start' && attempt.payload.order_sets !== sets)) { attempt = { id: order.id, action, payload: { event_id: eventId(), expected_revision: order.revision, ...(action === 'start' ? { order_sets: sets } : {}) }, submitted: false, uncertain: false }; startAttempt.current = attempt }
      if (!attempt) throw new Error('处理请求未就绪，请刷新询价。')
      attempt.submitted = true; attempt.uncertain = false
      try { const value = await request<CustomerOrder>(`/orders/${order.id}/${attempt.action}`, jsonPost(attempt.payload)); adopt(value); setQuantity(String(value.order_sets || sets)); startAttempt.current = null; setConnectionError('') }
      catch (failure) {
        if (failure instanceof RequestError && failure.status >= 400 && failure.status < 500) startAttempt.current = null; else attempt.uncertain = true
        try { adopt(await request<CustomerOrder>(`/orders/${order.id}`)) } catch { /* Retain the exact uncertain request for explicit recovery. */ }
        throw failure
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '提交未完成，请稍后恢复。'); void refreshQuota()
      try { const value = await request<{ orders: CustomerOrder[] }>('/orders'); if (mounted.current) setOrders(value.orders) } catch { /* A possibly accepted upload remains discoverable in history. */ }
    } finally { lock.current = false; setSubmitting(false) }
  }
  async function refresh() {
    if (!current || loading || submitting) return
    setLoading(true)
    try { adopt(await request<CustomerOrder>(`/orders/${current.id}`)); setConnectionError(''); setPartsTick(value => value + 1) }
    catch (failure) { setConnectionError(failure instanceof Error ? failure.message : '暂时无法刷新。') }
    finally { setLoading(false) }
  }
  const total = Math.max(0, Number(current?.progress?.total) || 0), done = Math.max(0, Math.min(total, Number(current?.progress?.done) || 0))
  return <main className="fft" data-testid="trial-page">
    <header className="fft-header"><a className="fft-brand" href="/try/" aria-label="FabFlow 首页"><span><Box size={24} strokeWidth={1.7}/></span><strong>FabFlow<span>制造报价体验</span></strong></a><div className="fft-header-right"><span className="fft-private"><ShieldCheck size={15}/>专属询价空间</span><a href="#trial-history"><History size={15}/>我的询价{orders.length > 0 && <i>{orders.length}</i>}</a></div></header>
    <div className="fft-shell"><section className="fft-intro"><div><span className="fft-eyebrow">FROM CAD TO QUOTE</span><h1>上传模型，<em>让报价开始。</em></h1><p>看清装配与零件，查看材料、工艺和报价草稿。</p></div><div className="fft-hero-note"><span><ShieldCheck size={17}/>仅本浏览器可查看自己的询价</span><small>STEP 原生读取 · 后台持续处理</small></div></section>
      <nav className="fft-steps" aria-label="询价流程">{[{ label: '上传模型', note: 'STEP / STP' }, { label: '模型与零件', note: '原生拆件与参数' }, { label: '查看报价', note: '估价草稿，待审核' }].map((step, index) => <button type="button" key={step.label} className={phase === index + 1 ? 'active' : phase > index + 1 ? 'done' : ''} aria-current={phase === index + 1 ? 'step' : undefined} disabled={index > 0 && !current || index === 2 && !hasQuote} onClick={() => { if (index === 0 && !submitting && !recoverUpload && !recoverProcessing) choose(null); else if (index === 1) setTab('model'); else if (index === 2) setTab('quote') }}><b>{phase > index + 1 ? <Check size={17}/> : `0${index + 1}`}</b><span><strong>{step.label}</strong><small>{step.note}</small></span>{index < 2 && <ChevronRight size={17}/>}</button>)}</nav>
      {connectionError && <div className="fft-alert" role="status"><span>{connectionError}{running && ' 后台仍会继续处理。'}</span><button type="button" onClick={() => void (ready && current ? refresh() : initialize())} disabled={loading}><RefreshCw size={14}/>重新连接</button></div>}
      <div className="fft-layout"><aside className="fft-config"><div className="fft-card-top"><span>询价配置</span>{current && <button type="button" onClick={() => choose(null)} disabled={submitting || !!recoverProcessing}><Plus size={14}/>新建</button>}</div>
        {current ? <div className="fft-file-selected"><span><Box size={24}/></span><div><strong>{current.name}</strong><small>{date(current.created_at)}</small></div><FileCheck2 size={17}/></div> : <><input ref={input} hidden type="file" accept=".step,.stp" aria-label="上传 STEP 模型" data-testid="trial-file-input" disabled={!canUpload || submitting || !!recoverUpload} onChange={event => { pick(event.target.files); event.target.value = '' }}/><button type="button" className={`fft-drop${dragging ? ' dragging' : ''}${file ? ' selected' : ''}`} data-testid="trial-dropzone" disabled={!canUpload || submitting || !!recoverUpload} onClick={() => input.current?.click()} onDragOver={event => { event.preventDefault(); if (canUpload && !submitting && !recoverUpload) setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); if (!submitting && !recoverUpload) pick(event.dataTransfer.files) }}><span><UploadCloud size={29} strokeWidth={1.5}/></span><strong>{file ? file.name : '拖放 CAD 文件到这里'}</strong><small>{file ? `${(file.size / 1024 / 1024).toFixed(2)} MB · 点击更换` : '或点击选择文件'}</small><i>STEP / STP · 单文件 ≤ 20 MB</i></button></>}
        <div className="fft-quantity"><label htmlFor="trial-quantity">订单数量<span>套</span></label><input id="trial-quantity" data-testid="trial-quantity" type="number" min="1" max="100000" step="1" inputMode="numeric" value={quantity} disabled={!ready || submitting || !!recoverProcessing || !!current && current.status !== 'uploaded'} onChange={event => { setQuantity(event.target.value); startAttempt.current = null }}/><small>以本份 STEP 根装配为一套</small></div>
        <div className="fft-config-readonly"><div><span>材料与规格</span><strong>从 CAD 资料读取</strong></div><div><span>加工工艺</span><strong>按零件工程条件整理</strong></div><p>未标明或需工程判断的条件，会在结果中单独列出。</p></div>
        {!current && <button type="button" className="fft-primary fft-full" data-testid="trial-submit" disabled={!canUpload || !file || submitting || !!recoverUpload} onClick={() => void submit()}>{submitting ? <LoaderCircle size={17} className="fft-spin"/> : <ArrowRight size={17}/>}开始分析与询价</button>}
        {current?.status === 'uploaded' && <button type="button" className="fft-primary fft-full" data-testid="trial-start" disabled={submitting || !!recoverProcessing || current.can_start === false} onClick={() => void submit()}>{submitting ? <LoaderCircle size={17} className="fft-spin"/> : <ArrowRight size={17}/>}开始处理已保存模型</button>}
        {(recoverUpload || recoverProcessing) && <div className="fft-recovery"><strong>上次请求的结果待确认</strong><p>恢复同一次请求，不会新建重复询价。</p><button type="button" className="fft-secondary" data-testid="trial-recover" disabled={submitting} onClick={() => void submit(startAttempt.current?.action || 'start', recoverUpload ? 'upload' : 'processing')}><RotateCcw size={14}/>恢复本次请求</button></div>}
        <div className="fft-quota" data-testid="trial-quota"><div><span>共享体验额度</span><button type="button" onClick={() => void refreshQuota()} disabled={!ready || quotaLoading || submitting} aria-label="刷新体验额度"><RefreshCw size={13} className={quotaLoading ? 'fft-spin' : ''}/></button></div><p>剩余 <strong>{quota ? quota.remaining : '—'}</strong> / {quota?.limit || 10} 次</p><small>所有客户合计；已保存询价不重复占用上传次数。</small>{quota?.remaining === 0 && <em>新上传额度已用完，已有询价仍可查看。</em>}{quotaError && <em role="status">{quotaError}</em>}</div>
        <div className="fft-review-note"><CircleHelp size={15}/><p>体验结果为工程与报价草稿，以工厂审核确认为准。</p></div>
      </aside><section className="fft-workspace" aria-label="模型与报价结果" data-testid={current ? 'trial-current-order' : 'trial-workspace'}>
        {!current ? <div className="fft-empty" data-testid="trial-empty"><div className="fft-empty-art" aria-hidden="true"><span className="fft-orbit"/><span className="fft-outline-box"><Box size={74} strokeWidth={.8}/></span><i><Layers3 size={19}/></i><b><FileText size={18}/></b></div><span className="fft-eyebrow">YOUR NEXT PART STARTS HERE</span><h2>从一份 CAD，看清每个零件。</h2><p>上传后，在这里旋转查看总装、浏览拆件结果，<br/>再查看属于这笔询价的报价草稿。</p><div className="fft-empty-features"><span><Box size={17}/>原生 3D 预览</span><span><Layers3 size={17}/>零件与工程参数</span><span><FileText size={17}/>客户报价单</span></div></div> : <><div className="fft-workspace-heading"><div><span className={`fft-status${running ? ' running' : needsAttention ? ' attention' : ''}`}>{running ? <LoaderCircle size={13} className="fft-spin"/> : needsAttention ? <CircleHelp size={13}/> : <Check size={13}/>} {statuses[current.status] || '资料已保存'}</span><h2 title={current.name}>{current.name}</h2></div><button type="button" className="fft-icon" onClick={() => void refresh()} disabled={loading || submitting} aria-label="刷新当前询价"><RefreshCw size={17} className={loading ? 'fft-spin' : ''}/></button></div>
          {running && <div className="fft-progress" data-testid="trial-progress"><LoaderCircle size={24} className="fft-spin"/><div><strong>{current.progress.message || '正在后台处理 STEP 模型'}</strong><p>可以离开页面，稍后用同一浏览器回来查看。</p>{total > 0 && <><div className="fft-progress-bar" role="progressbar" aria-label="文件处理进度" aria-valuemin={0} aria-valuemax={total} aria-valuenow={done}><span style={{ width: `${done / total * 100}%` }}/></div><small>{done} / {total} 份文件已处理；随后继续整理工程与报价。</small></>}</div><Clock3 size={17}/></div>}
          {!running && (current.status === 'failed' || current.status === 'interrupted' || current.status === 'completed_with_issues' && current.files.some(item => item.kind === 'step' && item.status === 'failed')) && <div className="fft-failure" role="status"><strong>资料已保留，本轮处理未完成</strong><p>{current.error || '请核对原模型后，再选择是否重试。'}</p><button type="button" className="fft-secondary" data-testid="trial-retry" disabled={submitting || !!recoverProcessing || current.can_retry === false || current.retry_remaining === 0} onClick={() => void submit('retry')}><RefreshCw size={14}/>重试处理{current.retry_remaining != null && `（剩余 ${current.retry_remaining} 次）`}</button></div>}
          {hasQuote && <><div className="fft-tabs" role="group" aria-label="查看询价结果">{([{ id: 'model', label: '原始模型', icon: Box }, { id: 'parts', label: `零件清单${manifest ? ` · ${manifest.parts.length}` : ''}`, icon: Layers3 }, { id: 'quote', label: '报价草稿', icon: FileText }] as const).map(item => <button type="button" key={item.id} aria-pressed={tab === item.id} className={tab === item.id ? 'active' : ''} onClick={() => { setTab(item.id); setViewer(null) }}><item.icon size={15}/>{item.label}</button>)}</div>
            {tab !== 'quote' && (partsLoading ? <div className="fft-loading" role="status"><LoaderCircle size={24} className="fft-spin"/>正在载入模型与零件</div> : partsError ? <div className="fft-loading"><p>{partsError}</p><button type="button" className="fft-secondary" onClick={() => setPartsTick(value => value + 1)}>重新载入</button></div> : !selectedPart ? <div className="fft-loading"><Box size={29}/><p>暂未形成可预览的零件，等待工程核对。</p></div> : tab === 'model' ? <div className="fft-model" data-testid="trial-model"><SourceAssemblyPreview previewUrl={geometry} sourceName={selectedPart.source} active={!viewer} refreshKey={`${current.revision}:${partsTick}`}/><div className="fft-model-summary"><div><Layers3 size={18}/><strong>{manifest?.parts.length}</strong><span>项零件</span></div><div><Scale size={18}/><strong>{estimatedWeight(weights.total, '未汇总')}</strong><span>{weights.totalLabel}</span></div><button type="button" onClick={() => setTab('parts')}>浏览拆件<ArrowRight size={15}/></button></div></div> : <div className="fft-parts" data-testid="trial-parts"><div className="fft-parts-list">{manifest?.parts.map(part => { const thumb = partAsset(part, current, 'thumbnail'); return <button type="button" key={part.id} className={part.id === selectedPart.id ? 'selected' : ''} aria-pressed={part.id === selectedPart.id} onClick={() => setPartId(part.id)}><span>{thumb ? <img src={thumb} alt="" loading="lazy"/> : <Box size={25}/>}</span><div><strong>{part.part_name || part.id}</strong><small>{quotationCount(part.quantity_total)} 件 · {part.id}</small></div><ChevronRight size={13}/></button> })}</div><div className="fft-part-detail"><div className="fft-part-visual">{partAsset(selectedPart, current, 'thumbnail') ? <img src={partAsset(selectedPart, current, 'thumbnail')!} alt={`${selectedPart.part_name} 原生 CAD 零件`}/> : <Box size={65} strokeWidth={1}/>}<button type="button" className="fft-secondary" disabled={!geometry} onClick={() => geometry && setViewer(geometry)}>旋转查看单件 3D<ArrowRight size={14}/></button></div><div className="fft-part-title"><span>{selectedPart.id}</span><h3>{selectedPart.part_name}</h3><strong>{quotationCount(selectedPart.quantity_total)} 件</strong></div><dl className="fft-part-facts"><div><dt>材料</dt><dd>{selectedPart.material?.text || '资料未标明'}{selectedPart.material?.source === 'estimate' && <small>本次估价采用</small>}</dd></div><div><dt>板／壁厚</dt><dd>{selectedPart.thickness_mm != null ? `${quotationNumber(selectedPart.thickness_mm)} mm` : '待工程核对'}</dd></div><div><dt>成品包络尺寸</dt><dd>{selectedPart.dimensions_mm?.length === 3 ? selectedPart.dimensions_mm.map(value => quotationNumber(value)).join(' × ') + ' mm' : '待工程核对'}</dd></div><div><dt>单件预估净重</dt><dd>{estimatedWeight(selectedPart.estimated_net_weight_kg)}</dd></div></dl>{!!selectedPart.processes?.length && <div className="fft-process-tags">{selectedPart.processes.map((process, index) => <span key={`${process.id}-${index}`}>{process.name}</span>)}</div>}{!!selectedPart.blockers?.length && <details className="fft-part-notes"><summary>工程待核对 · {selectedPart.blockers.length} 项</summary><ul>{selectedPart.blockers.map((note, index) => <li key={index}>{note}</li>)}</ul></details>}</div></div>)}
            {tab === 'quote' && <div className="fft-quote" data-testid="trial-quote" data-amount-scope={completeQuote ? 'order_total' : knownSubtotal != null ? 'known_subtotal' : 'unavailable'}><div className="fft-quote-hero"><span><FileCheck2 size={17}/> {completeQuote ? '订单估价草稿' : knownSubtotal != null ? '已知分项估价草稿' : '工程核价草稿'}</span><strong>{completeQuote ? money(current.quote!.total_price) : knownSubtotal != null ? money(knownSubtotal) : '等待工程核价'}</strong><p>{completeQuote ? `${current.order_sets || 1} 套 · ${current.quote!.quoted_lines} 项已形成估价` : knownSubtotal != null ? `已知分项小计，${current.quote!.unpriced_lines > 0 ? `${current.quote!.unpriced_lines} 项尚未计价，` : ''}尚不代表整单报价。` : '可先查看零件和待核对条件，再确认估算方案。'}</p><small>{current.quote!.simulated ? '模拟报价 · 待审核' : '报价草稿 · 待审核'} · 人民币 · 未税 · 不含运费</small><div>{quoted && <a className="fft-primary" href={quoted} target="_blank" rel="noopener noreferrer">查看客户报价单<ArrowRight size={15}/></a>}{printable && <a className="fft-secondary" href={printable} target="_blank" rel="noopener noreferrer"><ArrowDownToLine size={15}/>打印 / 另存 PDF</a>}</div></div><div className="fft-quote-weight"><Scale size={17}/><span>{weights.totalLabel}</span><strong>{estimatedWeight(weights.total, '未汇总')}</strong><small>用于与材料及工艺核价交叉核对</small></div>{current.quote!.costing_mode === 'process-v1' && <details className="fft-plan" open={!completeQuote}><summary>确认工程估算方案<ChevronRight size={15}/></summary><ProcessQuotePanel batchId={current.id} runId={current.quote!.run_id} revision={current.revision} source="customer" quoteCalculated={completeQuote} onSelectPart={id => { setPartId(id); setTab('parts') }} onApplied={value => { const saved = value as CustomerOrder; if (!validOrder(saved?.id) || saved.id !== current.id || !validRun(saved.quote?.run_id) || !Array.isArray(saved.files)) throw new Error('报价结果已保存，请刷新询价。'); adopt(saved); setQuotePreview(false) }}/></details>}{quoted && <><button type="button" className="fft-preview-toggle" onClick={() => setQuotePreview(value => !value)} aria-expanded={quotePreview}><FileText size={15}/>{quotePreview ? '收起报价预览' : '在本页预览客户报价'}<ChevronRight size={14}/></button>{quotePreview && <iframe title="客户报价草稿预览" src={quoted} className="fft-quote-frame" sandbox="allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-modals"/>}</>}</div>}
          </>}
          {!hasQuote && !running && current.status === 'uploaded' && <div className="fft-saved"><FileCheck2 size={45} strokeWidth={1.2}/><h3>模型已保存</h3><p>确认左侧数量，开始读取装配、拆分零件并整理报价。</p></div>}
        </>}
      </section></div>{error && <div className="fft-alert fft-error" role="alert" data-testid="trial-error">{error}</div>}
      <details className="fft-history" id="trial-history"><summary><span><History size={18}/>我的询价<strong>{orders.length}</strong></span><small>本浏览器的提交记录</small><ChevronRight size={16}/></summary><div>{orders.length ? orders.map(order => <button type="button" key={order.id} data-testid="trial-history-order" data-order-id={order.id} className={selected === order.id ? 'selected' : ''} onClick={() => choose(order)} disabled={submitting || !!recoverUpload || !!recoverProcessing}><Box size={21}/><span><strong>{order.name}</strong><small>{date(order.created_at)} · {order.order_sets || 1} 套</small></span><em>{statuses[order.status] || '资料已保存'}</em><ArrowRight size={15}/></button>) : <p><FolderOpen size={20}/>{loading ? '正在载入询价记录' : '提交后，模型、处理进度与报价会保存在这里。'}</p>}</div></details>
      <footer className="fft-footer"><span><Box size={16}/>FabFlow · 从 CAD 到制造报价</span><span>原生几何量测 · 工程候选待审核</span></footer>
    </div>{viewer && safePartPreviewUrl(viewer) && <BatchPartViewer key={viewer} previewUrl={viewer} customer onClose={() => setViewer(null)}/>}
  </main>
}
