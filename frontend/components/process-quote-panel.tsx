'use client'

import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Calculator, Check, ChevronDown, CircleAlert, ExternalLink, LoaderCircle, RefreshCw } from 'lucide-react'
import { quotationCount, quotationNumber } from '@/lib/batch-display-format'
import './process-quote-panel.css'

export type QuoteQuantityStatus = 'measured' | 'estimated' | 'unresolved'
export type QuoteQuantities = {
  material_area_mm2?: string | number | null; material_gross_area_mm2?: string | number | null;
  cut_length_mm?: string | number | null; pierce_count?: string | number | null;
  bend_count?: string | number | null; bend_length_mm?: string | number | null; thickness_mm?: string | number | null;
}
export type QuoteQuantityEvidence = {
  status: QuoteQuantityStatus; method?: string; quantities: QuoteQuantities;
  quantity_statuses?: Partial<Record<keyof QuoteQuantities, QuoteQuantityStatus>>;
  measurements?: { native_skin_area_proxy_mm2?: string | number | null; volume_equivalent_area_mm2?: string | number | null; area_volume_thickness_ratio?: string | number | null };
  basis: string[]; manufacturing_release: false;
}
type PlanLine = QuoteQuantityEvidence & { id: string; part_name: string; material_ready?: boolean; blockers: { code: string; message: string }[] }
const ROUTE_ASSUMPTION = 'sheet_form_weld_demo'
type RouteProposal = { id: typeof ROUTE_ASSUMPTION; version: string; label: string; requires_confirmation: boolean; applies_to: string[]; description: string }
type Plan = {
  plan_digest: string; expected_revision: number; current_run_id: string; can_estimate: boolean;
  status: 'ready' | 'partial' | 'blocked'; lines: PlanLine[];
  material_estimate: { label: string; is_assumption: boolean }; scope_label: string; simulated: true;
  route_proposal?: RouteProposal | null; estimated_subtotal?: string | null; total_price?: string | null;
  pending_parts?: number; already_applied?: boolean;
}
type Source = 'customer' | 'staff'
const baseFor = (source: Source) => source === 'staff' ? '/ortho2cad-test/customer-upload-admin/api/quote-batches' : '/workbench/customer/api/orders'
const validBatch = (value: unknown): value is string => typeof value === 'string' && /^BATCH-[a-f0-9]{32}$/.test(value)
const validRun = (value: unknown): value is string => typeof value === 'string' && /^RUN-[a-f0-9]{32}$/.test(value)
const validLine = (value: unknown): value is string => typeof value === 'string' && /^L[0-9]{4,8}$/.test(value)
const number = (value: unknown): number | null => {
  if (typeof value !== 'string' && typeof value !== 'number' || value === '') return null
  const result = Number(value)
  return Number.isFinite(result) && result >= 0 ? result : null
}
export const quoteQuantityFields: { key: keyof QuoteQuantities; label: string; unit: string; count?: boolean }[] = [
  { key: 'thickness_mm', label: '计价采用板厚', unit: 'mm' },
  { key: 'material_area_mm2', label: '净料面积', unit: 'mm²' },
  { key: 'material_gross_area_mm2', label: '计价用料面积', unit: 'mm²' },
  { key: 'cut_length_mm', label: '切割长度', unit: 'mm' },
  { key: 'pierce_count', label: '穿孔数量', unit: '次', count: true },
  { key: 'bend_count', label: '折弯数量', unit: '折', count: true },
  { key: 'bend_length_mm', label: '折弯总长', unit: 'mm' },
]
export function quoteQuantityLabel(status?: QuoteQuantityStatus) {
  return status === 'measured' ? 'CAD 量测' : status === 'estimated' ? '报价暂估' : '待补充'
}
export function quoteQuantityFieldLabel(evidence: QuoteQuantityEvidence, key: keyof QuoteQuantities, fallback: string) {
  if (evidence.method === 'native_brep_inventory') {
    if (key === 'material_area_mm2') return '表面积折算代理'
    if (key === 'cut_length_mm') return '切割行程代理'
  }
  return fallback
}
export function quoteQuantityValue(value: unknown, unit: string, count = false) {
  if ((typeof value !== 'string' && typeof value !== 'number') || number(value) == null) return '待补充'
  return `${count ? quotationCount(value) : quotationNumber(value)} ${unit}`
}
export function quoteQuantityState(evidence: QuoteQuantityEvidence, key: keyof QuoteQuantities): QuoteQuantityStatus {
  if (number(evidence.quantities?.[key]) == null) return 'unresolved'
  const status = evidence.quantity_statuses?.[key] || evidence.status
  return status === 'measured' ? 'measured' : status === 'estimated' ? 'estimated' : 'unresolved'
}
export function QuoteAreaCrosscheck({ evidence }: { evidence: QuoteQuantityEvidence }) {
  const measured = evidence.measurements
  if (number(measured?.native_skin_area_proxy_mm2) == null && number(measured?.volume_equivalent_area_mm2) == null) return null
  return <aside className="pqp-crosscheck" data-testid="process-quote-area-crosscheck"><strong>用料面积对照 · 报价暂估</strong><dl><div><dt>表面积折算代理</dt><dd>{quoteQuantityValue(measured?.native_skin_area_proxy_mm2, 'mm²')}</dd></div><div><dt>体积／采用厚度等效面积</dt><dd>{quoteQuantityValue(measured?.volume_equivalent_area_mm2, 'mm²')}</dd></div><div><dt>前者／后者</dt><dd>{quoteQuantityValue(measured?.area_volume_thickness_ratio, '倍')}</dd></div></dl><p>保留原生计算差异供工程核对；两种面积均不代表生产展开，体积等效面积也不构成对体积估重的独立验证。</p></aside>
}
class PricingError extends Error { constructor(message: string, readonly status: number) { super(message) } }
async function readResponse(response: Response): Promise<unknown> {
  let value: unknown
  try { value = await response.json() } catch { throw new PricingError('报价结果尚未确认，请恢复原请求或刷新订单查看。', response.status) }
  if (!response.ok) {
    const failure = value as { error?: { message?: string } | string }
    throw new PricingError(typeof failure.error === 'string' ? failure.error : failure.error?.message || '本次报价计算未完成，请重试。', response.status)
  }
  return value
}
function validatedPlan(value: unknown): Plan {
  if (!value || typeof value !== 'object') throw new Error('报价准备资料暂不可用。')
  const plan = value as Plan
  if (!/^[a-f0-9]{64}$/.test(plan.plan_digest) || !Number.isSafeInteger(plan.expected_revision) || plan.expected_revision < 0
      || !validRun(plan.current_run_id) || typeof plan.can_estimate !== 'boolean' || !['ready', 'partial', 'blocked'].includes(plan.status)
      || !Array.isArray(plan.lines) || plan.lines.length > 10000 || !plan.lines.every(line => validLine(line?.id)
        && typeof line.part_name === 'string' && ['measured', 'estimated', 'unresolved'].includes(line.status)
        && line.quantities && typeof line.quantities === 'object' && Array.isArray(line.basis) && line.basis.every(item => typeof item === 'string')
        && Array.isArray(line.blockers) && line.blockers.every(item => item && typeof item.code === 'string' && typeof item.message === 'string'))
      || typeof plan.scope_label !== 'string' || !plan.material_estimate || typeof plan.material_estimate.label !== 'string'
      || typeof plan.material_estimate.is_assumption !== 'boolean' || plan.simulated !== true) throw new Error('报价准备资料与当前订单不一致，请刷新订单。')
  if (plan.route_proposal != null) {
    const route = plan.route_proposal
    if (route.id !== ROUTE_ASSUMPTION || typeof route.version !== 'string' || typeof route.label !== 'string'
        || typeof route.description !== 'string' || typeof route.requires_confirmation !== 'boolean'
        || !Array.isArray(route.applies_to) || !route.applies_to.every(id => validLine(id) && plan.lines.some(line => line.id === id))) throw new Error('工程假设方案与当前拆件不一致，请刷新订单。')
  }
  return plan
}
type Attempt = { event_id: string; expected_revision: number; plan_digest: string; route_assumption?: typeof ROUTE_ASSUMPTION }

export default function ProcessQuotePanel({ batchId, runId, revision, source, quoteCalculated = false, onApplied, onSelectPart }: {
  batchId: string; runId: string; revision: number; source: Source; quoteCalculated?: boolean;
  onApplied?(value: unknown): void; onSelectPart?(id: string): void;
}) {
  const [plan, setPlan] = useState<Plan | null>(null), [loading, setLoading] = useState(true), [error, setError] = useState('')
  const [reload, setReload] = useState(0), [submitting, setSubmitting] = useState(false), [uncertain, setUncertain] = useState(false)
  const [adoptRoute, setAdoptRoute] = useState(false)
  const mounted = useRef(true), locked = useRef(false), attempt = useRef<Attempt | null>(null), attemptEndpoint = useRef<string | null>(null), applied = useRef(onApplied)
  applied.current = onApplied
  const endpoint = validBatch(batchId) && validRun(runId) ? `${baseFor(source)}/${batchId}/runs/${runId}/process-pricing` : null
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => {
    const abort = new AbortController()
    setPlan(null); setError(''); setLoading(true); setAdoptRoute(false)
    if (attemptEndpoint.current !== endpoint) { attempt.current = null; setUncertain(false); attemptEndpoint.current = endpoint }
    void (async () => {
      try {
        if (!endpoint) throw new Error('当前报价记录暂不可用，请刷新订单。')
        const response = await fetch(endpoint, { signal: abort.signal, credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: { 'X-Workbench-Request': '1' } })
        const next = validatedPlan(await readResponse(response))
        if (!abort.signal.aborted) setPlan(next)
      } catch (failure) { if (!abort.signal.aborted) setError(failure instanceof Error ? failure.message : '暂未读取到报价准备资料。') }
      finally { if (!abort.signal.aborted) setLoading(false) }
    })()
    return () => abort.abort()
  }, [endpoint, revision, reload])

  async function calculate(recovery = false) {
    if (locked.current || !endpoint || !applied.current) return
    if (!recovery && (!plan || !plan.can_estimate || plan.current_run_id !== runId || uncertain || plan.route_proposal?.requires_confirmation && !adoptRoute)) return
    const payload: Attempt | null = recovery ? attempt.current : plan ? { event_id: 'process-quote-' + crypto.randomUUID(), expected_revision: plan.expected_revision, plan_digest: plan.plan_digest } : null
    if (!payload) return
    if (!recovery && plan?.route_proposal?.requires_confirmation && adoptRoute) payload.route_assumption = ROUTE_ASSUMPTION
    attempt.current = payload; locked.current = true; setSubmitting(true); setError('')
    try {
      const response = await fetch(endpoint, { method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        headers: { 'Content-Type': 'application/json', 'X-Workbench-Request': '1' }, body: JSON.stringify(payload) })
      const result = await readResponse(response) as { run_id?: unknown; order?: { id?: unknown; quote?: { run_id?: unknown } }; batch?: { id?: unknown; result?: { run_id?: unknown } } }
      const saved = source === 'customer' ? result.order : result.batch
      const savedRun = source === 'customer' ? result.order?.quote?.run_id : result.batch?.result?.run_id
      if (!saved || saved.id !== batchId || !validRun(result.run_id) || savedRun !== result.run_id) throw new Error('报价保存结果尚未确认，请恢复原请求或刷新订单。')
      if (mounted.current) { applied.current?.(saved); setUncertain(false) }
      attempt.current = null
    } catch (failure) {
      const retrySame = !(failure instanceof PricingError && failure.status >= 400 && failure.status < 500)
      if (!retrySame) attempt.current = null
      if (mounted.current) {
        setUncertain(retrySame); setError(failure instanceof Error ? failure.message : '本次计算结果尚未确认。')
        if (failure instanceof PricingError && failure.status === 409) { setPlan(null); setAdoptRoute(false) }
      }
    } finally { locked.current = false; if (mounted.current) setSubmitting(false) }
  }

  const lines = plan?.lines || [], gaps = lines.flatMap(line => line.blockers.map(blocker => ({ ...blocker, id: line.id, part_name: line.part_name })))
  const route = plan?.route_proposal, requiresRoute = route?.requires_confirmation === true
  const materialReady = (line: PlanLine) => line.material_ready === true || (number(line.quantities.material_gross_area_mm2) || 0) > 0 && (number(line.quantities.thickness_mm) || 0) > 0
  const processReady = lines.filter(line => materialReady(line) && number(line.quantities.cut_length_mm) != null && number(line.quantities.pierce_count) != null
    && (number(line.quantities.bend_count) != null || route?.requires_confirmation === false && route.applies_to.includes(line.id))).length
  const stale = !!plan && plan.current_run_id !== runId
  const canCalculate = !!plan?.can_estimate && !stale && !!onApplied && !submitting && !uncertain && (!requiresRoute || adoptRoute)
  const hasSubtotal = number(plan?.estimated_subtotal) != null, hasTotal = number(plan?.total_price) != null
  const savedQuoteUrl = plan && validBatch(batchId) && validRun(plan.current_run_id) && (plan.already_applied || stale)
    ? `${baseFor(source)}/${batchId}/runs/${plan.current_run_id}/customer-quote.html` : null
  const stages = [
    { name: '总装拆件', state: lines.length ? 'done' : 'pending', detail: lines.length ? `${quotationCount(lines.length)} 项零件` : '等待资料' },
    { name: '用料与工序', state: lines.length && processReady === lines.length ? 'review' : 'pending', detail: lines.length ? `${quotationCount(processReady)} / ${quotationCount(lines.length)} 项具估价依据 · 工程待审` : '等待工程量' },
    { name: '成本核算', state: quoteCalculated ? 'done' : 'pending', detail: quoteCalculated ? '已计算' : !plan ? '等待读取' : plan.can_estimate ? '可以计算' : '查看待补项' },
    { name: '报价草稿', state: quoteCalculated ? 'done' : 'pending', detail: quoteCalculated ? '已生成 · 待审核' : '计算后生成' },
  ]
  return <section className="pqp" data-testid="process-quote-panel" data-plan-status={plan?.status || 'loading'} data-source={source}>
    <div className="pqp-heading"><div><p>PROCESS QUOTATION</p><h3>从拆件到估算报价</h3></div><span className={`pqp-state ${quoteCalculated ? 'complete' : plan?.status || ''}`}>{quoteCalculated ? '草稿已生成' : loading ? '正在准备' : !plan ? '准备资料未载入' : plan.can_estimate ? '可计算估算报价' : '工程量待补充'}</span></div>
    <ol className="pqp-stages" aria-label="报价准备进度" data-testid="process-quote-stages">{stages.map((stage, index) => <li className={stage.state} key={stage.name} data-stage={index + 1}><span className="pqp-step-icon">{stage.state === 'done' ? <Check size={12} /> : index + 1}</span><div><strong>{stage.name}</strong><small>{stage.detail}</small></div></li>)}</ol>
    {loading ? <div className="pqp-loading" role="status"><LoaderCircle size={16} className="pqp-spin" />正在整理已保存的工程量</div> : plan && <>
      <div className="pqp-adoption"><div><span>估价采用材料</span><strong data-testid="process-quote-material">{plan.material_estimate?.label || '采用条件待核对'}</strong>{plan.material_estimate?.is_assumption && <small>本单估算条件，具体牌号及工艺仍待审核。</small>}</div><button type="button" className="pqp-primary" data-testid="process-quote-calculate" disabled={!canCalculate} onClick={() => void calculate()}>{submitting ? <LoaderCircle size={15} className="pqp-spin" /> : <Calculator size={15} />}{submitting ? '正在计算报价' : requiresRoute ? adoptRoute ? '生成本方案估算价' : '采用方案后生成报价' : quoteCalculated ? '更新估算报价' : '生成估算报价'}<ArrowRight size={13} /></button></div>
      {route && <section className="pqp-route" data-testid="process-quote-route" data-route-id={route.id} data-route-adopted={!route.requires_confirmation}><div><strong>{route.requires_confirmation ? '可采用的工程假设' : '本单已采用的工程假设'}</strong><span>估算方案价 · 待审核</span></div>{route.requires_confirmation ? <label><input type="checkbox" checked={adoptRoute} disabled={submitting || uncertain || stale || !plan.can_estimate} onChange={event => setAdoptRoute(event.target.checked)} data-testid="process-quote-route-adopt" /><span>采用{route.label}</span></label> : <h4>{route.label}</h4>}<p>{route.description}</p><p>成形与拼焊按每件预算暂估；折弯次数、焊长和工时没有因此被识别或确认，生产展开仍待审核。</p><div className="pqp-route-parts">{route.applies_to.map(id => <button key={id} type="button" onClick={() => onSelectPart?.(id)} disabled={!onSelectPart}>{lines.find(line => line.id === id)?.part_name || id}</button>)}</div></section>}
      {(hasSubtotal || hasTotal) && <section className={`pqp-amount${hasTotal ? ' complete' : ''}`} data-testid="process-quote-estimate-amount" data-amount-scope={hasTotal ? 'proposed_total' : 'known_subtotal'}><span>{hasTotal ? '估算方案价 · 待审核' : '已识别分项估算小计'}</span><strong>¥{quotationNumber(hasTotal ? plan.total_price : plan.estimated_subtotal)}</strong><p>{hasTotal ? '价格按本方案采用条件核算，工程量及生产要求仍待审核。' : '尚有路线或工程条件待核价，此小计不是整单报价。'}{requiresRoute && adoptRoute && <span> 所选工程假设尚未计算，请点击上方生成本方案估算价。</span>}</p></section>}
      {savedQuoteUrl && <a className="pqp-saved" href={savedQuoteUrl} target="_blank" rel="noopener noreferrer" data-testid="process-quote-open-saved"><ExternalLink size={13} />查看已保存报价</a>}
      <p className="pqp-scope" data-testid="process-quote-scope">{stale ? '当前订单已有新报价，请刷新订单查看。' : plan.scope_label || '按已整理工程量核算；报价草稿待工程及商务审核。'}</p>
      {!!gaps.length && <div className="pqp-gaps" data-testid="process-quote-gaps"><strong><CircleAlert size={13} />当前待补</strong>{gaps.slice(0, 3).map((gap, index) => <div key={`${gap.id}-${gap.code}-${index}`}><span>{gap.part_name}：{gap.message}</span>{onSelectPart && <button type="button" onClick={() => onSelectPart(gap.id)}>查看零件</button>}</div>)}{gaps.length > 3 && <small>其余 {quotationCount(gaps.length - 3)} 项见下方逐件明细。</small>}</div>}
      <details className="pqp-details" data-testid="process-quote-details"><summary>查看 {quotationCount(lines.length)} 项工程量与计价依据<ChevronDown size={13} /></summary><div>{lines.map(line => <article key={line.id} data-testid="process-quote-line" data-line-id={line.id}><header><button type="button" disabled={!onSelectPart} onClick={() => onSelectPart?.(line.id)}>{line.part_name}<small>{line.id}</small></button><span className={`pqp-evidence ${line.status}`}>{quoteQuantityLabel(line.status)}</span></header><dl>{quoteQuantityFields.map(field => { const state = quoteQuantityState(line, field.key); return <div key={field.key} data-quantity={field.key} data-status={state}><dt>{quoteQuantityFieldLabel(line, field.key, field.label)}</dt><dd>{quoteQuantityValue(line.quantities[field.key], field.unit, field.count)}<small className={state}>{quoteQuantityLabel(state)}</small></dd></div> })}</dl><QuoteAreaCrosscheck evidence={line} />{line.basis.length > 0 && <ul>{line.basis.map((basis, index) => typeof basis === 'string' && <li key={index}>{basis}</li>)}</ul>}{line.blockers.length > 0 && <ul className="pqp-line-gaps">{line.blockers.map((blocker, index) => <li key={`${blocker.code}-${index}`}>{blocker.message}</li>)}</ul>}</article>)}</div></details>
    </>}
    {error && <div className="pqp-error" role="alert" data-testid="process-quote-error"><span>{error}</span>{uncertain ? <button type="button" data-testid="process-quote-recover" disabled={submitting} onClick={() => void calculate(true)}><RefreshCw size={13} />恢复上次核价结果</button> : <button type="button" data-testid="process-quote-retry" disabled={submitting} onClick={() => setReload(value => value + 1)}><RefreshCw size={13} />重新读取准备资料</button>}</div>}
    <p className="pqp-note">工程量区分 CAD 量测、报价暂估和待补项；估算工程量不作为生产展开图。计算后另存新报价，原报价保留。</p>
  </section>
}
