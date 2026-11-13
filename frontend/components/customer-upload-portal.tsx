'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowDownToLine, ArrowRight, Box, Check, ChevronRight, Clock3, FileArchive, FileCheck2, FileText, FolderOpen, LoaderCircle, Plus, RefreshCw, Scale, ShieldCheck, UploadCloud, X } from 'lucide-react'
import CustomerManufacturingPanel from '@/components/customer-manufacturing-panel'
import { estimatedWeight, quotationCount } from '@/lib/batch-display-format'
import { savedWeightDisplay, type CustomerWeightSummary } from '@/lib/customer-weight-summary'
import './customer-upload-portal.css'

const API = '/workbench/customer/api'
const LAST_ORDER = 'hyl-customer-last-order'
const validOrder = (value: unknown): value is string => typeof value === 'string' && /^BATCH-[a-f0-9]{32}$/.test(value)
const busy = (status?: string) => status === 'queued' || status === 'running'
const statuses: Record<string, string> = { uploaded: '等待开始', queued: '等待后台处理', running: '正在拆件与整理工艺', completed: '拆件与工艺已整理', completed_with_issues: '部分工艺待核对', interrupted: '处理已中断', failed: '本轮处理未完成' }
type CustomerFile = { id: string; name: string; kind: string; status: string }
export type CustomerOrder = {
  id: string; name: string; status: string; revision: number; created_at: string; updated_at: string;
  files: CustomerFile[]; progress: { total: number; done: number; message: string }; order_sets: number | null; error: string | null;
  can_start?: boolean; can_retry?: boolean; retry_limit?: number; retry_remaining?: number;
  quote: null | { run_id: string; total_price: string | null; currency: string; status: string; simulated: boolean; unpriced_lines: number; quoted_lines: number; quote_url: string; print_url: string; costing_mode?: string; pricing_status?: 'estimated' | 'partial' | 'blocked' | 'manual_review'; manufacturing_url?: string; estimated_subtotal?: string | null; weight_summary?: CustomerWeightSummary };
}
type UploadQuota = { limit: number; used: number; remaining: number; scope: 'global'; accepted_extensions: string[]; max_files: 1; max_bytes: number;
  retry_limit: number; active: boolean; includes_existing_orders: false; unit: 'new_upload'; blocked: boolean }
function validateQuota(value: UploadQuota): UploadQuota {
  if (!value || value.scope !== 'global' || value.limit !== 10 || !Number.isSafeInteger(value.used) || value.used < 0
      || !Number.isSafeInteger(value.remaining) || value.remaining < 0 || value.remaining > value.limit
      || value.remaining !== Math.max(0, value.limit - value.used) || value.max_files !== 1 || value.max_bytes !== 20 * 1024 * 1024
      || !Array.isArray(value.accepted_extensions) || value.accepted_extensions.length !== 2
      || !value.accepted_extensions.includes('.step') || !value.accepted_extensions.includes('.stp')
      || typeof value.blocked !== 'boolean') throw new Error('上传额度信息暂不可用，请刷新后再上传。')
  return value
}
type UploadAttempt = { file: File; event: string; submitted: boolean; uncertain: boolean }
type StartAttempt = { id: string; action: 'start' | 'retry'; payload: { event_id: string; expected_revision: number; order_sets?: number }; submitted: boolean; uncertain: boolean }
class PortalError extends Error { constructor(message: string, readonly status: number) { super(message) } }
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers); headers.set('X-Workbench-Request', '1')
  const response = await fetch(API + path, { ...init, headers, credentials: 'same-origin', cache: 'no-store' })
  let value: unknown
  try { value = await response.json() } catch { throw new PortalError(response.status === 413 ? '文件超过上传限制，请缩小后再上传。' : '连接暂未完成，已提交的资料会保留。请稍后刷新。', response.status) }
  if (!response.ok) { const failure = value as { error?: { message?: string } | string }; throw new PortalError(typeof failure.error === 'string' ? failure.error : failure.error?.message || '操作未完成，请稍后重试。', response.status) }
  return value as T
}
const eventId = () => 'customer-' + crypto.randomUUID()
const jsonPost = (value: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) })
const money = (value: string | null) => value === null || !Number.isFinite(Number(value)) ? '暂未计价' : `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
const date = (value: string) => { const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? '' : parsed.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) }
const size = (bytes: number) => bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(0)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`
function safeQuoteUrl(value: string | undefined, id: string, filename: string): string | null {
  if (!value || !validOrder(id)) return null
  const prefix = `${API}/orders/${id}/runs/`
  return value.startsWith(prefix) && /^RUN-[a-zA-Z0-9-]+\/(?:customer-quote|customer-print)\.html$/.test(value.slice(prefix.length)) && value.endsWith('/' + filename) ? value : null
}

type PortalProps = { embedded?: boolean; initialOrderId?: string | null; onManageOrder?(id: string): void; onOrderSelected?(id: string | null): void }
export default function CustomerUploadPortal({ embedded = false, initialOrderId, onManageOrder, onOrderSelected }: PortalProps = {}) {
  const Container = embedded ? 'div' : 'main'
  const callbacks = useRef({ onManageOrder, onOrderSelected }), initialOrder = useRef(initialOrderId)
  callbacks.current = { onManageOrder, onOrderSelected }; initialOrder.current = initialOrderId
  const [ready, setReady] = useState(false), [loading, setLoading] = useState(false)
  const [quota, setQuota] = useState<UploadQuota | null>(null), [quotaLoading, setQuotaLoading] = useState(false), [quotaError, setQuotaError] = useState('')
  const [orders, setOrders] = useState<CustomerOrder[]>([]), [selected, setSelected] = useState<string | null>(null)
  const [file, setFile] = useState<File | null>(null), [quantity, setQuantity] = useState('1')
  const [error, setError] = useState(''), [connectionError, setConnectionError] = useState('')
  const [submitting, setSubmitting] = useState(false), [dragging, setDragging] = useState(false), [preview, setPreview] = useState(false), [pollTick, setPollTick] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null), selectedRef = useRef<string | null>(null)
  const uploadAttempt = useRef<UploadAttempt | null>(null), startAttempt = useRef<StartAttempt | null>(null)
  const submissionLock = useRef(false), mounted = useRef(true)
  const quotaEpoch = useRef(0)
  const current = orders.find(order => order.id === selected) || null
  const canUpload = ready && !!quota && !quota.blocked && quota.remaining > 0 && !quotaLoading
  const pendingUpload = uploadAttempt.current, pendingProcessing = startAttempt.current
  const recoverUpload = !current && !!file && !!pendingUpload && pendingUpload.file === file && pendingUpload.submitted && pendingUpload.uncertain
  const recoverProcessing = !!current && !!pendingProcessing && pendingProcessing.id === current.id && pendingProcessing.submitted && pendingProcessing.uncertain
  const refreshQuota = useCallback(async (): Promise<UploadQuota | null> => {
    const epoch = ++quotaEpoch.current
    setQuotaLoading(true); setQuotaError('')
    try {
      const next = validateQuota(await request<UploadQuota>('/quota'))
      if (mounted.current && epoch === quotaEpoch.current) setQuota(next)
      return next
    } catch (failure) {
      if (mounted.current && epoch === quotaEpoch.current) { setQuota(null); setQuotaError(failure instanceof Error ? failure.message : '暂未读取到上传额度，请重试。') }
      return null
    } finally { if (mounted.current && epoch === quotaEpoch.current) setQuotaLoading(false) }
  }, [])
  const adopt = useCallback((order: CustomerOrder) => { setOrders(previous => [order, ...previous.filter(item => item.id !== order.id)]) }, [])
  const choose = useCallback((order: CustomerOrder | null) => {
    const id = order?.id || null; selectedRef.current = id; setSelected(id); setPreview(false); setError(''); setConnectionError(''); setQuantity(String(order?.order_sets || 1))
    if (!order) { setFile(null); uploadAttempt.current = null; startAttempt.current = null }
    try { if (id) localStorage.setItem(LAST_ORDER, id); else localStorage.removeItem(LAST_ORDER) } catch { /* Browser storage is optional. */ }
    callbacks.current.onOrderSelected?.(id)
  }, [])
  const initialize = useCallback(async () => {
    setLoading(true); setConnectionError('')
    try {
      await request('/session', jsonPost({})); void refreshQuota(); const value = await request<{ orders: CustomerOrder[] }>('/orders')
      if (!mounted.current) return
      setReady(true); setOrders(value.orders)
      let previous: string | null = initialOrder.current !== undefined ? initialOrder.current : selectedRef.current
      if (initialOrder.current === undefined && !previous) { try { previous = localStorage.getItem(LAST_ORDER) } catch { /* Not required for authentication. */ } }
      if (previous && validOrder(previous)) { const saved = value.orders.find(order => order.id === previous); if (saved) choose(saved) }
    } catch (failure) { if (mounted.current) setConnectionError(failure instanceof Error ? failure.message : '暂时无法连接，请重试。') }
    finally { if (mounted.current) setLoading(false) }
  }, [choose, refreshQuota])
  useEffect(() => { mounted.current = true; void initialize(); return () => { mounted.current = false } }, [initialize])
  useEffect(() => {
    if (!ready) return
    const refresh = () => { if (document.visibilityState === 'visible' && !submissionLock.current) void refreshQuota() }
    window.addEventListener('focus', refresh); document.addEventListener('visibilitychange', refresh)
    return () => { window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', refresh) }
  }, [ready, refreshQuota])
  useEffect(() => {
    if (!ready || initialOrderId === undefined || initialOrderId === selectedRef.current) return
    if (initialOrderId === null) { choose(null); return }
    if (!validOrder(initialOrderId)) { setError('询价链接无效，请从我的询价重新打开。'); return }
    const saved = orders.find(order => order.id === initialOrderId)
    if (saved) choose(saved)
    else setError('本浏览器没有这笔询价，请从我的询价重新选择。')
  }, [ready, initialOrderId, orders, choose])
  useEffect(() => {
    if (!ready || !selected || !busy(current?.status)) return
    let cancelled = false
    const timer = setTimeout(async () => {
      try { const order = await request<CustomerOrder>(`/orders/${selected}`); if (!cancelled) { adopt(order); setConnectionError('') } }
      catch (failure) { if (!cancelled) setConnectionError(failure instanceof Error ? failure.message : '连接暂时中断，后台会继续处理。') }
      finally { if (!cancelled) setPollTick(value => value + 1) }
    }, 3000)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [ready, selected, current?.status, adopt, pollTick])

  function pickFiles(files: FileList | null) {
    setDragging(false); setError(''); setFile(null); uploadAttempt.current = null; startAttempt.current = null
    if (!files || files.length !== 1) { setError('每次只能上传一份 STEP / STP 文件，请重新选择。'); return }
    if (!ready || !quota || quota.blocked || quota.remaining < 1) { setError(quota?.remaining === 0 ? '所有客户共享的 10 次新上传额度已用完，已有订单仍可查看。' : '请先刷新上传额度，再选择文件。'); return }
    const chosen = files[0]
    if (!/\.(step|stp)$/i.test(chosen.name)) { setError('本入口仅接收一份 STEP / STP 文件，不接收 ZIP、RAR 或其他格式。'); return }
    if (chosen.size === 0 || chosen.size > 20 * 1024 * 1024) { setError('请选择非空 STEP / STP 文件，大小不能超过 20 MB。'); return }
    setFile(chosen); uploadAttempt.current = { file: chosen, event: eventId(), submitted: false, uncertain: false }; startAttempt.current = null
  }
  async function submit(action: 'start' | 'retry' = 'start', recovery?: 'upload' | 'processing') {
    if (submissionLock.current || !ready) return
    if (recovery !== 'processing' && (!/^[1-9][0-9]*$/.test(quantity) || Number(quantity) > 100000)) { setError('订单数量请输入 1 至 100000 的整数。'); return }
    submissionLock.current = true; setSubmitting(true); setError('')
    try {
      let order = current
      if (!order) {
        if (!file) throw new Error('请先选择要上传的文件。')
        const saved = uploadAttempt.current
        const replay = recovery === 'upload' && !!saved && saved.file === file && saved.submitted && saved.uncertain
        if (recovery === 'upload' && !replay) throw new Error('没有可恢复的上传请求，请重新选择文件。')
        const latest = await refreshQuota()
        if (!replay && !latest) throw new Error('暂未读取到上传额度，请刷新后重试；已有订单不受影响。')
        if (!replay && latest && (latest.blocked || latest.remaining < 1)) throw new Error('所有客户共享的 10 次新上传额度已用完，已有订单仍可继续处理和查看。')
        const attempt: UploadAttempt = saved?.file === file ? saved : { file, event: eventId(), submitted: false, uncertain: false }
        uploadAttempt.current = attempt; attempt.submitted = true; attempt.uncertain = false
        try {
          order = await request<CustomerOrder>(`/orders?name=${encodeURIComponent(attempt.file.name)}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Event-Id': attempt.event }, body: attempt.file })
          if (!validOrder(order.id)) throw new Error('上传结果暂未确认，请恢复上次上传结果。')
        } catch (failure) {
          if (failure instanceof PortalError && failure.status >= 400 && failure.status < 500) uploadAttempt.current = null
          else attempt.uncertain = true
          throw failure
        }
        uploadAttempt.current = null; adopt(order); choose(order); setQuantity(quantity); setFile(null); void refreshQuota()
      }
      let attempt = startAttempt.current
      const replay = recovery === 'processing' && !!attempt && attempt.id === order.id && attempt.submitted && attempt.uncertain
      if (recovery === 'processing' && !replay) throw new Error('没有可恢复的处理请求，请刷新当前订单。')
      if (!replay && attempt?.id === order.id && attempt.submitted && attempt.uncertain) throw new Error('上次处理结果尚未确认，请先恢复上次处理结果。')
      if (!replay && action === 'retry' && (order.can_retry === false || order.retry_remaining === 0)) throw new Error('本订单的重试次数已用完，请联系工厂核对原文件。')
      if (!replay && action === 'start' && order.can_start === false) throw new Error('当前订单不能再次开始处理，请查看已有进度或结果。')
      const sets = replay ? attempt!.payload.order_sets || order.order_sets || 1 : Number(quantity)
      if (!replay && (!attempt || attempt.id !== order.id || (action === 'start' && attempt.payload.order_sets !== sets) || attempt.action !== action)) {
        attempt = { id: order.id, action, payload: { event_id: eventId(), expected_revision: order.revision, ...(action === 'start' ? { order_sets: sets } : {}) }, submitted: false, uncertain: false }; startAttempt.current = attempt
      }
      if (!attempt) throw new Error('处理请求暂未就绪，请刷新当前订单。')
      attempt.submitted = true; attempt.uncertain = false
      try {
        const started = await request<CustomerOrder>(`/orders/${order.id}/${attempt.action}`, jsonPost(attempt.payload))
        adopt(started); setQuantity(String(started.order_sets || sets)); setFile(null); startAttempt.current = null; setConnectionError('')
      } catch (failure) {
        if (failure instanceof PortalError && failure.status >= 400 && failure.status < 500) startAttempt.current = null
        else attempt.uncertain = true
        if (failure instanceof PortalError && failure.status === 429) void refreshQuota()
        // A lost response can follow an accepted job. Read durable state before
        // offering another submission; retries retain the same event key.
        try { const saved = await request<CustomerOrder>(`/orders/${order.id}`); adopt(saved); setQuantity(String(saved.order_sets || sets)) } catch { /* Retain the exact uncertain request identity for recovery. */ }
        throw failure
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '提交未完成，请重试。'); void refreshQuota()
      if (!current) {
        try { const saved = await request<{ orders: CustomerOrder[] }>('/orders'); if (mounted.current) { setOrders(saved.orders); if (saved.orders.length > 0) setError(previous => `${previous} 已上传的资料可从“我的询价”中打开继续处理。`) } } catch { /* A possibly accepted upload can be recovered from the existing order list. */ }
      }
    }
    finally { submissionLock.current = false; setSubmitting(false) }
  }
  async function refreshCurrent() {
    if (!current || loading || submitting) return
    setLoading(true)
    try { adopt(await request<CustomerOrder>(`/orders/${current.id}`)); setConnectionError(''); setError('') }
    catch (failure) { setConnectionError(failure instanceof Error ? failure.message : '暂时无法刷新。') }
    finally { setLoading(false) }
  }
  const quoteUrl = current?.quote && safeQuoteUrl(current.quote.quote_url, current.id, 'customer-quote.html')
  const weightSummary = current?.quote?.weight_summary, weights = savedWeightDisplay(weightSummary)
  const printUrl = current?.quote && safeQuoteUrl(current.quote.print_url, current.id, 'customer-print.html')
  const processFirst = current?.quote?.costing_mode === 'process-v1'
  const quoteComplete = !!current?.quote && (!processFirst || current.quote.pricing_status === 'estimated' || current.quote.pricing_status === 'manual_review') && current.quote.total_price !== null
  const partialAmount = processFirst && !quoteComplete && current?.quote?.estimated_subtotal != null && Number.isFinite(Number(current.quote.estimated_subtotal))
  const running = busy(current?.status), stage = quoteComplete ? 3 : current?.quote || running || current?.status === 'failed' || current?.status === 'interrupted' ? 2 : 1
  const total = Math.max(0, Number(current?.progress.total) || 0), completed = Math.max(0, Math.min(total, Number(current?.progress.done) || 0)), progress = total ? Math.round(completed / total * 100) : 0

  return <Container className={`cup-app${embedded ? ' cup-embedded' : ''}`} data-testid="customer-portal">
    {!embedded && <header className="cup-header"><div className="cup-header-inner"><div className="cup-brand"><span><Box size={23} strokeWidth={1.7} /></span><div><strong>好运来</strong><small>客户询价</small></div></div><span className="cup-header-note"><ShieldCheck size={16} />专属订单空间</span></div></header>}
    <div className="cup-shell">
      <section className="cup-hero"><div><div className="cup-eyebrow">CAD · PARTS &amp; PROCESS</div><h1>从总装拆件，逐项核算工艺。</h1><p>每次上传一份 STEP / STP，拆出制造零件，整理工艺图与加工工程量。</p></div><div className="cup-hero-note"><FileCheck2 size={23} strokeWidth={1.5} /><span>拆分零件<ArrowRight size={13} />工艺拆图<ArrowRight size={13} />分项报价</span></div></section>
      <section className={`cup-upload-quota${quota?.remaining === 0 ? ' exhausted' : ''}`} data-testid="customer-upload-quota" data-remaining={quota?.remaining} aria-label="共享上传额度"><div className="cup-quota-heading"><div><ShieldCheck size={17} /><strong>所有客户共享 {quota?.limit || 10} 次新上传</strong></div><div className="cup-quota-count">剩余 <strong data-testid="customer-quota-remaining">{quota ? quota.remaining : '读取中'}</strong> 次</div><button type="button" onClick={() => void refreshQuota()} disabled={!ready || quotaLoading || submitting} data-testid="customer-quota-refresh" aria-label="刷新上传额度"><RefreshCw size={14} className={quotaLoading ? 'cup-spin' : ''} />刷新额度</button></div><p>每次仅一份 STEP / STP，最大 20 MB。历史订单不占本轮新上传次数。</p>{quota?.remaining === 0 && <p className="cup-quota-status" role="status" data-testid="customer-quota-exhausted">新上传额度已用完；已上传订单仍可开始处理、查看拆件和报价。</p>}{quota?.blocked && quota.remaining > 0 && <p className="cup-quota-status" role="status">新上传暂不可用，请刷新额度后重试。</p>}{quotaError && <p className="cup-quota-status" role="status" data-testid="customer-quota-error">{quotaError} 旧订单仍可查看。</p>}</section>
      {connectionError && <div className="cup-alert cup-connection" role="status" data-testid="customer-connection-error"><span>{connectionError}{running && ' 后台任务会继续运行。'}</span><button type="button" onClick={() => void (ready && current ? refreshCurrent() : initialize())} disabled={loading}><RefreshCw size={14} className={loading ? 'cup-spin' : ''} />重新连接</button></div>}
      <div className="cup-layout">
        <section className="cup-main-panel">
          <ol className="cup-steps" aria-label="询价流程">{['上传总装', '拆件与工艺', '报价草稿'].map((label, index) => <li key={label} className={stage === index + 1 ? 'active' : stage > index + 1 ? 'done' : ''} aria-current={stage === index + 1 ? 'step' : undefined}><span>{stage > index + 1 ? <Check size={14} /> : `0${index + 1}`}</span>{label}{index < 2 && <ChevronRight size={16} />}</li>)}</ol>
          {!current && <div className="cup-upload-panel">
            <div className="cup-section-label">提交新的询价</div><h2>上传一份 STEP 总装</h2><p className="cup-description">每次仅接收一个 STEP / STP 文件。</p>
            <input ref={inputRef} type="file" accept=".step,.stp" aria-label="上传一份 STEP 或 STP" data-testid="customer-file-input" disabled={!canUpload || submitting} onChange={event => { pickFiles(event.target.files); event.target.value = '' }} />
            <button type="button" className={`cup-dropzone ${dragging ? 'dragging' : ''} ${file ? 'has-file' : ''}`} disabled={!canUpload || submitting} data-testid="customer-dropzone" onClick={() => inputRef.current?.click()} onDragOver={event => { event.preventDefault(); if (canUpload && !submitting) setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); if (ready && !submitting) pickFiles(event.dataTransfer.files) }}><span className="cup-upload-icon">{file ? <Box size={31} strokeWidth={1.5} /> : <UploadCloud size={32} strokeWidth={1.5} />}</span><strong>{file ? file.name : quota?.remaining === 0 ? '新上传额度已用完' : '点击选择一份文件，或拖放到这里'}</strong><span>{file ? `${size(file.size)} · 点击更换` : 'STEP / STP'}</span><small>每次 1 份 · 最大 20 MB</small></button>
            <div className="cup-order-quantity"><label htmlFor="cup-quantity">订单数量<span>套</span></label><input id="cup-quantity" data-testid="customer-quantity" type="number" min="1" max="100000" step="1" inputMode="numeric" value={quantity} onChange={event => { setQuantity(event.target.value); startAttempt.current = null }} disabled={submitting || !ready} /><p>以本份 STEP 根装配为一套。</p></div>
            <div className="cup-submit-row"><span>当前为演示报价，结果待工厂审核。</span><button type="button" className="cup-primary" data-testid="customer-submit" disabled={!canUpload || !file || submitting || recoverUpload} onClick={() => void submit()}>{submitting ? <><LoaderCircle size={17} className="cup-spin" />正在上传</> : <>提交询价<ArrowRight size={16} /></>}</button></div>
            {recoverUpload && <div className="cup-recovery" data-testid="customer-upload-recovery"><p>上次上传结果尚未确认。复用同一份文件，核对是否已保存。</p><button type="button" className="cup-secondary" disabled={!ready || submitting} onClick={() => void submit('start', 'upload')} data-testid="customer-upload-recover"><RefreshCw size={14} className={submitting ? 'cup-spin' : ''} />恢复上次上传结果</button></div>}
          </div>}
          {current && <div className="cup-order-panel" data-testid="customer-current-order">
            <div className="cup-order-heading"><div><span className="cup-section-label">当前询价</span><h2>{current.name}</h2><p>{date(current.created_at)}<span>·</span>{current.order_sets || quantity} 套<span>·</span>{current.files?.length || 0} 份资料</p></div><div className="cup-order-tools">{onManageOrder && <button type="button" className="cup-secondary" disabled={submitting} onClick={() => callbacks.current.onManageOrder?.(current.id)} data-testid="workbench-manage-order">内部核价<ArrowRight size={13} /></button>}<button type="button" className="cup-icon-button" aria-label="刷新订单" title="刷新订单" onClick={() => void refreshCurrent()} disabled={loading || submitting}><RefreshCw size={17} className={loading ? 'cup-spin' : ''} /></button></div></div>
            {recoverProcessing && <div className="cup-recovery" data-testid="customer-processing-recovery"><p>上次处理结果尚未确认。仅恢复已提交的请求，不发起新的重试。</p><button type="button" className="cup-secondary" disabled={!ready || submitting} onClick={() => void submit(startAttempt.current?.action || 'start', 'processing')} data-testid="customer-processing-recover"><RefreshCw size={14} className={submitting ? 'cup-spin' : ''} />恢复上次处理结果</button></div>}
            {current.status === 'uploaded' && <div className="cup-ready-card"><FileCheck2 size={33} strokeWidth={1.5} /><div><h3>资料已保存</h3><p>确认数量后开始读取，无需再次上传文件。</p></div><label>订单数量（套）<input type="number" min="1" max="100000" step="1" value={quantity} onChange={event => { setQuantity(event.target.value); startAttempt.current = null }} disabled={submitting || recoverProcessing} aria-label="已上传订单数量" /></label><button className="cup-primary" data-testid="customer-start" disabled={submitting || recoverProcessing || current.can_start === false} onClick={() => void submit()}>{submitting ? <LoaderCircle size={16} className="cup-spin" /> : <ArrowRight size={16} />}开始处理</button></div>}
            {running && <div className="cup-progress-card" data-testid="customer-progress"><span className="cup-progress-icon"><LoaderCircle size={26} className="cup-spin" /></span><h3>{statuses[current.status] || '正在处理'}</h3><p>{current.progress.message || '正在读取模型，整理零件与报价。'}</p><div className="cup-progress-label"><span>STEP 文件处理进度</span><strong>{completed} / {total}</strong></div><div className="cup-progress-track" role="progressbar" aria-label="文件处理进度" aria-valuemin={0} aria-valuemax={total || 1} aria-valuenow={completed}><span style={{ width: `${progress}%` }} /></div><small><Clock3 size={13} />可以关闭页面，稍后用同一浏览器回来查看。</small></div>}
            {!running && (current.status === 'failed' || current.status === 'interrupted' || current.status === 'completed_with_issues' && current.files.some(item => item.kind === 'step' && item.status === 'failed')) && <div className="cup-incomplete-card" data-testid="customer-failed"><FileText size={30} strokeWidth={1.5} /><h3>{statuses[current.status]}</h3><p>{current.error || '资料已保留，请联系工厂核对文件。'}</p>{current.retry_remaining != null && <small className="cup-retry-quota" data-testid="customer-retry-remaining">本订单最多重试 {current.retry_limit || 1} 次，剩余 {current.retry_remaining} 次。</small>}{!recoverProcessing && (current.can_retry === false || current.retry_remaining === 0) && <p className="cup-retry-limit" role="status" data-testid="customer-retry-limit">{current.retry_remaining === 0 ? '重试次数已用完，请联系工厂核对原文件。' : '当前订单不能再次自动处理，请联系工厂核对原文件。'}</p>}<button className="cup-primary" data-testid="customer-retry" disabled={submitting || recoverProcessing || current.can_retry === false || current.retry_remaining === 0} onClick={() => void submit('retry')}><RefreshCw size={16} className={submitting ? 'cup-spin' : ''} />{recoverProcessing ? '处理结果待恢复' : current.retry_remaining === 0 ? '重试次数已用完' : '重试处理'}</button></div>}
            {processFirst && current.quote && <CustomerManufacturingPanel key={`${current.id}/${current.quote.run_id}`} batchId={current.id} runId={current.quote.run_id} revision={current.revision} quoteCalculated={quoteComplete} onQuoteApplied={value => {
              const saved = value as CustomerOrder
              if (!saved || saved.id !== current.id || !validOrder(saved.id) || !saved.quote?.run_id || !Array.isArray(saved.files) || !Number.isSafeInteger(saved.revision)) throw new Error('新报价已保存，请刷新当前订单查看。')
              adopt(saved)
              if (selectedRef.current === saved.id) { setPreview(false); setError(''); setConnectionError('') }
            }} />}
            {current.quote && <div className={`cup-quote-area${processFirst && !quoteComplete ? ' cup-process-pending' : ''}`} data-testid="customer-quote-result" data-pricing-status={current.quote.pricing_status || 'legacy'}>
              <div className="cup-quote-summary"><div><span className="cup-quote-tag"><Check size={13} />{processFirst ? quoteComplete ? '已形成工艺估算报价草稿' : '拆件与工艺草稿已整理' : '报价草稿已生成'}</span><h3>{partialAmount ? '已识别分项估算小计' : processFirst && !quoteComplete ? '全单报价待核算' : '订单报价'}</h3>
                {quoteComplete ? <strong className="cup-total" data-testid="customer-total">{money(current.quote.total_price)}</strong> : partialAmount ? <strong className="cup-pending-total" data-testid="customer-total" data-amount-scope="known_subtotal">{money(current.quote.estimated_subtotal ?? null)}</strong> : <strong className="cup-pending-total" data-testid="customer-total">报价待计算</strong>}
                <p>{current.order_sets || 1} 套 · {current.quote.quoted_lines} 项已形成估价{current.quote.unpriced_lines > 0 ? ` · ${current.quote.unpriced_lines} 项待核对` : ''}</p>
                {partialAmount && <small className="cup-known-subtotal" data-testid="customer-known-subtotal">尚有路线或工程条件待核价，此小计不是整单报价。</small>}
              </div><div className="cup-quote-actions">{quoteUrl && <a className="cup-primary" href={quoteUrl} target="_blank" rel="noopener noreferrer" data-testid="customer-open-quote"><FileText size={16} />{processFirst && !quoteComplete ? '查看工艺核价草稿' : '查看客户报价单'}<ArrowRight size={15} /></a>}{printUrl && <a className="cup-secondary" href={printUrl} target="_blank" rel="noopener noreferrer" data-testid="customer-open-print"><ArrowDownToLine size={16} />打印 / 另存 PDF</a>}</div></div>
              <section className={`cup-weight-summary ${weights.status}`} data-testid="customer-quote-weight-summary" data-weight-status={weights.status} aria-label="报价重量汇总"><div className="cup-weight-heading"><span><Scale size={15} />预估净重</span><small>{weights.status === 'complete' ? '订单重量已汇总' : weights.status === 'partial' ? '已知零件小计' : '未汇总'}</small></div><div className="cup-weight-grid"><div><span>{weights.setLabel}</span><strong data-testid="customer-quote-set-weight">{estimatedWeight(weights.set, '未汇总')}</strong></div><div><span>{weights.totalLabel}</span><strong data-testid="customer-quote-order-weight">{estimatedWeight(weights.total, '未汇总')}</strong>{weightSummary?.order_sets != null && <small>{quotationCount(weightSummary.order_sets)} 套合计</small>}</div></div>{weights.status === 'partial' && <p className="cup-weight-partial" data-testid="customer-quote-weight-scope">另有 {quotationCount(weightSummary?.missing_estimated_lines)} 项尚未估重，小计不代表完整订单总重。</p>}{weights.status === 'unavailable' && <p className="cup-weight-partial" data-testid="customer-quote-weight-scope">当前记录尚未保存完整重量汇总。</p>}<details className="cup-weight-evidence" data-testid="customer-quote-weight-evidence"><summary>查看估重说明<ChevronRight size={12} /></summary><p>按 CAD 体积与所用材料密度估算，不含包装。重量用于与材料、拆件及工艺报价交叉核对。</p>{weightSummary && <p>已保存 {quotationCount(weightSummary.known_weight_lines)} 项零件重量；各零件的采用材料和密度可在拆件详情中展开查看。{weightSummary.low_confidence_lines > 0 && <> 含 {quotationCount(weightSummary.low_confidence_lines)} 项粗估，待工程核对。</>}</p>}</details></section>
              <div className="cup-quote-note"><span>{current.quote.simulated ? '模拟报价 · 待审核' : '报价草稿 · 待审核'}</span>{processFirst && !quoteComplete ? ' · 在上方查看工程量与具体待补项，再生成估算报价；工程确认与价格计算分别记录。' : current.quote.unpriced_lines > 0 ? ` · ${current.quote.unpriced_lines} 项暂未计价，当前金额仅覆盖已计价范围。` : ' · 以工厂最终确认的报价为准。'}</div>
              {quoteUrl && <><button type="button" className="cup-inline-preview" aria-expanded={preview} onClick={() => setPreview(value => !value)} data-testid="customer-preview-toggle">{preview ? <X size={15} /> : <FileText size={15} />}{preview ? '收起报价预览' : '在本页预览报价草稿'}<ChevronRight size={15} className={preview ? 'cup-rotated' : ''} /></button>{preview && <iframe className="cup-quote-frame" src={quoteUrl} title="客户报价草稿预览" data-testid="customer-quote-frame" sandbox="allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-modals" />}</>}
            </div>}
            <details className="cup-files"><summary>已保存资料<span>{current.files?.length || 0} 份</span></summary><ul>{current.files?.map(item => <li key={item.id}><span>{item.kind === 'step' ? <Box size={16} /> : <FileText size={16} />}{item.name}</span><small>{({ ready: '读取完成', failed: '待核对', reference: '参考资料', duplicate: '重复文件', ignored: '暂未处理', unsupported: '暂未处理', processing: '读取中', running: '读取中', waiting: '已保存', queued: '等待处理' } as Record<string, string>)[item.status] || '已保存'}</small></li>)}</ul></details>
          </div>}
          {error && <div className="cup-alert" role="alert" data-testid="customer-error">{error}</div>}
        </section>
        <aside className="cup-orders-panel"><div className="cup-orders-heading"><div><FolderOpen size={18} /><h2>我的询价</h2></div><button type="button" onClick={() => choose(null)} aria-label="新建询价" data-testid="customer-new-order" disabled={submitting}><Plus size={17} /></button></div><p className="cup-orders-description">本浏览器提交的订单</p><div className="cup-order-list" data-testid="customer-order-list">{orders.length === 0 ? <div className="cup-empty"><FolderOpen size={29} strokeWidth={1.3} /><strong>{loading ? '正在载入' : '还没有提交记录'}</strong><p>提交后，处理进度和报价<br />会保存在这里。</p></div> : orders.map(order => <button type="button" key={order.id} className={`cup-order-item ${selected === order.id ? 'selected' : ''}`} onClick={() => choose(order)} disabled={submitting} data-testid="customer-order-item" data-order-id={order.id}><span className="cup-order-file-icon">{/\.(zip|rar)$/i.test(order.name) ? <FileArchive size={20} /> : <Box size={20} />}</span><span className="cup-order-item-copy"><strong>{order.name}</strong><small>{date(order.created_at)}{order.order_sets ? ` · ${order.order_sets} 套` : ''}</small><span className={busy(order.status) ? 'processing' : order.quote ? 'complete' : ''}>{busy(order.status) && <LoaderCircle size={11} className="cup-spin" />}{statuses[order.status] || '资料已保存'}</span></span><ChevronRight size={14} /></button>)}</div><div className="cup-browser-note"><ShieldCheck size={16} /><p>本浏览器仅能查看自己的提交记录。<br />请保留浏览器数据，以便下次查看。</p></div></aside>
      </div><footer className="cup-footer"><span>好运来 · 客户询价服务</span><span>STEP 原生读取 · 后台持续处理</span></footer>
    </div>
  </Container>
}
