'use client'

import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Box, Download, ExternalLink, LoaderCircle, Pencil, RefreshCw, Save, Search, Sparkles, X } from 'lucide-react'
import { estimatedWeight, quotationCount, quotationNumber } from '@/lib/batch-display-format'
import { PUBLIC_QUOTE_API, type QuoteApiBase } from '@/lib/batch-quote-scope'
import './batch-engineering-panel.css'

export type EngineeringView = 'engineering' | 'materials' | 'processes'
type Scalar = string | number | null
type RecognizedField = { value?: unknown; status?: string; source?: string; label?: string; evidence?: unknown; reason?: string }
type Recognition = { id?: string; status: string; stage?: string; message?: string; started_at?: string | null; updated_at?: string | null; error?: string | { message?: string } | null }
type DocumentRequirement = { text: string; reference: string; page: number; region: string; status: 'pending_review' }
type PricingMaterial = { label: string; source?: string; is_assumption?: boolean }
type EngineeringLine = {
  id: string; line_id: string; row_number: number; part_name: string; part_number: string | null;
  drawing_number: string | null; revision: string | null; specification: string | null;
  material_grade: string | null; cad_material_raw: string | null; thickness_mm: string | null;
  material_description?: string | null; material_description_evidence?: unknown;
  pricing_material?: PricingMaterial | null;
  document_requirements?: DocumentRequirement[];
  surface_treatment: string | null; supply_type: string | null; supply_type_candidate: string | null;
  unit: string; quantity_per_set: number | null; quantity_total: number | null;
  parent_line_id: string | null; level: number | null; structure_status: string;
  document: string; file_id?: string; estimated_net_weight_kg: string | null;
  estimated_total_net_weight_kg: string | null; field_sources: Record<string, string>;
  missing_fields: string[]; status: string;
  recognition?: { fields?: Record<string, RecognizedField>; message?: string; status?: string };
  cad_name_code?: string | null;
  finished_dimensions_mm?: unknown; dimensions_mm?: unknown;
}
type MaterialRow = {
  id: string; line_id: string; part_name: string; member_ids: string[]; source_documents: string[];
  quantity_per_set: number | null; quantity_total: number | null; unit: string;
  material_grade: string | null; cad_material_raw: string | null; adopted_material: string | null;
  material_description?: string | null; material_description_evidence?: unknown;
  material_basis: string | null; estimated_net_weight_kg: string | null; estimated_total_net_weight_kg: string | null;
  purchase_usage_kg_per_piece: string | null; purchase_usage_kg_total: string | null;
  purchase_usage_status: string; supply_type: string | null; supply_type_candidate: string | null;
  field_sources?: Record<string, string | null>;
}
type ProcessRow = { line_id: string; part_name: string; document: string; operations: unknown[]; status: string; document_requirements?: DocumentRequirement[] }
type EngineeringCoverage = { lines: number; specification: number; thickness_mm: number; material_grade: number;
  material_description: number; surface_treatment: number; cad_name_code: number; formal_identity: number;
  parent_link: number; buy_candidates: number; thickness_pending_make: number }
type RequirementReview = { line_id: string; part_name: string; field: string; requirement: string; status: string; reason: string }
type EngineeringPricingEntry = { line_id: string; field: string; requirement: string; method: string; unit: string;
  rate: string | null; minimum: string; source_kind: string; inclusions?: unknown; [key: string]: unknown }
type EngineeringPricingPolicy = { version: string; entries: EngineeringPricingEntry[]; [key: string]: unknown }
type EngineeringPricingPlan = { plan: { policy: EngineeringPricingPolicy; requirements: Array<Record<string, unknown>>; assumptions: unknown[] };
  expected_revision: number; current_run_id: string }
type EngineeringRepricing = { source_run_id?: string; before_totals?: Record<string, unknown>; after_totals?: Record<string, unknown>;
  changes?: Array<{ line_id: string; part_name: string; before: Record<string, unknown>; after: Record<string, unknown> }>;
  assumptions?: unknown[]; applied_requirements?: Array<{ line_id: string; field: string; requirement: string; status: string; method: string; cost_evidence?: Record<string, unknown> }>;
  [key: string]: unknown }
type EngineeringManifest = {
  schema_version: number;
  header: { product_name: string | null; product_number: string | null; bom_revision: string | null;
    base_quantity: Scalar; basis_notes: string | null; order_sets: number | null; review_revision: number; status: string;
    quantity_basis?: string; quantity_effect?: string };
  lines: EngineeringLine[]; materials: MaterialRow[]; processes: ProcessRow[];
  stats: { lines: number; material_groups: number; missing_identity_lines: number; missing_specification_lines: number;
    unlinked_structure_lines: number; process_pending_lines: number };
  warnings: string[];
  coverage?: EngineeringCoverage;
  quote_basis?: { order_sets: number | null; total_price: string | null; adopted_materials: string[]; note: string;
    requirements_review?: RequirementReview[]; unresolved_requirements?: number; engineering_repricing?: EngineeringRepricing | null };
}
type Review = { revision?: number; header?: Record<string, Scalar>; lines?: Record<string, Record<string, Scalar>> }
type EngineeringResponse = { manifest: EngineeringManifest; review: Review; revision: number; recognition?: Recognition | null }
type Draft = Record<string, string>
class EngineeringFailure extends Error {
  constructor(message: string, public status: number, public field = '') { super(message) }
}

const headerFields = [['product_name', '产品名称'], ['product_number', '产品编号']] as const
const lineFields = [['part_number', '正式物料编码'], ['drawing_number', '图号'],
  ['material_grade', '材料牌号'], ['thickness_mm', '板厚 / 壁厚（mm）'], ['surface_treatment', '表面处理'], ['specification', '规格型号']] as const
const supplyLabels: Record<string, string> = { make: '自制', buy: '外购', subcontract: '外协', customer: '客供' }
const unitLabels: Record<string, string> = { piece: '件', set: '套', kg: 'kg', m: 'm' }
const valueText = (value: unknown) => typeof value === 'string' || typeof value === 'number' ? String(value) : ''
const show = (value: unknown) => valueText(value) || '—'
const count = (value: number | string | null | undefined) => quotationCount(value, '—')
const weight = (value: string | null | undefined) => estimatedWeight(value, '—')
const recognitionRunning = (status?: string) => ['queued', 'starting', 'running', 'reading', 'processing'].includes(status || '')
const fieldSourceLabels: Record<string, string> = { manual: '人工补录', native: 'CAD读取', native_geometry: 'CAD量测', geometry: 'CAD量测', cad: 'CAD读取', cad_attribute: 'CAD属性', native_attribute: 'CAD属性', ai: 'AI识别', model: 'AI识别', drawing: '图纸标注', name: 'CAD名称', source: '原始记录' }
const requirementFieldLabels: Record<string, string> = { material_grade: '材料牌号', material_description: '材料要求', surface_treatment: '表面处理', document_note: '图纸工艺要求', operations: '工序要求', specification: '规格要求', thickness_mm: '板厚 / 壁厚' }
const requirementStatus = (status: string) => status === 'priced' ? '已纳入本次核价 · 待审核' : status === 'category_matches' ? '采用类别相符，仍待核对'
  : status === 'not_adopted' || status === 'not_separately_priced' ? '待核价' : '待核对'
function recognitionField(row: EngineeringLine, key: string): RecognizedField | undefined { return row.recognition?.fields?.[key] }
function fieldValue(row: EngineeringLine, key: string): unknown {
  const current = row[key as keyof EngineeringLine]
  if (row.field_sources?.[key] === 'manual') return current
  return current != null && current !== '' ? current : recognitionField(row, key)?.value
}
function evidenceText(value: unknown): string {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map(evidenceText).filter(Boolean).join('；')
  if (!value || typeof value !== 'object') return ''
  const entry = value as Record<string, unknown>
  const summary = ['document', 'file_name', 'reference', 'page', 'region', 'quote', 'raw_text', 'text', 'reason'].map(key => valueText(entry[key])).filter(Boolean).join(' · ')
  if (summary) return summary
  try { return JSON.stringify(value).slice(0, 1800) } catch { return '' }
}
function fieldLabel(row: EngineeringLine, key: string): string {
  if (row.field_sources?.[key] === 'manual') return '人工补录'
  const field = recognitionField(row, key), source = field?.source || row.field_sources?.[key]
  return field?.label || (source ? fieldSourceLabels[source] || '已提取 · 待核对' : '')
}
function EngineeringField({ row, name, unit }: { row: EngineeringLine; name: string; unit?: string }) {
  const value = fieldValue(row, name), field = recognitionField(row, name), shown = valueText(value)
  if (!shown && row.field_sources?.[name] === 'manual') return <span className="beb-field-empty" title="人工已清空该字段，识别候选不会自动填回。">人工清空</span>
  if (!shown && name === 'thickness_mm') {
    if (row.supply_type_candidate === 'buy') return <span className="beb-field-empty" title="外购候选按零件型号和采购规格核对，不将紧固件外形尺寸作为板厚。">外购按型号核对</span>
    if (field?.reason && field.label) return <span className="beb-field-empty" title={field.reason}>{field.label}</span>
  }
  return <><span title={evidenceText(field?.evidence) || field?.reason || undefined}>{shown ? shown + (unit ? ` ${unit}` : '') : '—'}</span>{(shown || ['conflict', 'not_stated', 'unreadable', 'not_applicable'].includes(field?.status || '')) && fieldLabel(row, name) && <small className="beb-field-source">{fieldLabel(row, name)}</small>}</>
}
function EngineeringIdentity({ row }: { row: EngineeringLine }) {
  const part = valueText(fieldValue(row, 'part_number')), drawing = valueText(fieldValue(row, 'drawing_number'))
  if (part || drawing) return <>{part && <EngineeringField row={row} name="part_number" />}{drawing && <small>图号：{drawing}</small>}</>
  if (row.cad_name_code && !['part_number', 'drawing_number'].some(key => row.field_sources?.[key] === 'manual')) return <><span>{row.cad_name_code}</span><small className="beb-field-source">CAD名称编码</small></>
  return <span>—</span>
}
function EngineeringMaterialRequirement({ row }: { row: EngineeringLine }) {
  if (row.field_sources?.material_grade === 'manual' || valueText(fieldValue(row, 'material_grade')) || recognitionField(row, 'material_grade')?.status === 'conflict') return <EngineeringField row={row} name="material_grade" />
  if (row.material_description) return <><span title={evidenceText(row.material_description_evidence) || undefined}>{row.material_description}</span><small className="beb-field-source">图纸材料原文</small></>
  return <><span>{row.cad_material_raw || '未标'}</span>{row.cad_material_raw && <small className="beb-field-source">CAD属性原文</small>}</>
}
function EngineeringMaterial({ row }: { row: EngineeringLine }) {
  if (!row.pricing_material?.label) return <EngineeringMaterialRequirement row={row} />
  return <div className="beb-material-basis"><div><small>图纸要求</small><EngineeringMaterialRequirement row={row} /></div><div><small>本次核价材料</small><span>{row.pricing_material.label}{row.pricing_material.is_assumption ? '（估价假设）' : ''}</span>{row.pricing_material.source && <small className="beb-field-source">{row.pricing_material.source}</small>}</div></div>
}
function MaterialInformation({ row }: { row: MaterialRow }) {
  if (row.field_sources?.material_grade === 'manual' && !row.material_grade) return <span className="beb-field-empty">人工清空</span>
  return <><span title={evidenceText(row.material_description_evidence) || undefined}>{show(row.material_grade || row.material_description || row.cad_material_raw)}</span>{!row.material_grade && row.material_description && <small className="beb-field-source">图纸材料原文</small>}</>
}
function DocumentRequirements({ requirements, batchId, lineId, apiBase }: { requirements: DocumentRequirement[]; batchId: string; lineId: string; apiBase: QuoteApiBase }) {
  if (!requirements.length) return <><span className="beb-pending">尚无已保存的图纸工艺标注</span><small>工时待核对</small></>
  return <div className="beb-document-requirements" data-testid={`bq-process-requirements-${lineId}`}><span className="beb-document-requirements-label">图纸工艺标注原文</span><ul>{requirements.map((requirement, index) => {
    const source = /^BATCH-[a-f0-9]{32}$/.test(batchId) && /^F[0-9]{4}$/.test(requirement.reference)
      ? `${apiBase}/${batchId}/files/${requirement.reference}${Number.isSafeInteger(requirement.page) && requirement.page > 0 ? `#page=${requirement.page}` : ''}` : null
    return <li key={`${requirement.reference}/${requirement.page}/${index}`}><span className="beb-document-requirement-text">{requirement.text}</span><small>{source ? <a href={source} target="_blank" rel="noopener noreferrer">查看标注来源 · 第 {requirement.page} 页</a> : <span>{requirement.reference} · 第 {requirement.page} 页</span>}{requirement.region && <span> · {requirement.region}</span>}</small></li>
  })}</ul><small className="beb-process-time">工序路线与工时待工程核对</small></div>
}
function coverageOf(manifest: EngineeringManifest): EngineeringCoverage {
  if (manifest.coverage) return manifest.coverage
  const rows = manifest.lines, has = (value: unknown) => value != null && value !== ''
  return { lines: rows.length, specification: rows.filter(row => has(row.specification)).length,
    thickness_mm: rows.filter(row => has(row.thickness_mm)).length,
    material_grade: rows.filter(row => has(row.material_grade)).length,
    material_description: rows.filter(row => has(row.material_description)).length,
    surface_treatment: rows.filter(row => has(row.surface_treatment)).length,
    cad_name_code: rows.filter(row => has(row.cad_name_code)).length,
    formal_identity: rows.filter(row => has(row.part_number) || has(row.drawing_number)).length,
    parent_link: rows.filter(row => has(row.parent_line_id)).length,
    buy_candidates: rows.filter(row => row.supply_type_candidate === 'buy').length,
    thickness_pending_make: rows.filter(row => !has(row.thickness_mm) && row.supply_type_candidate !== 'buy').length }
}
function rowRecognitionSummary(row: EngineeringLine): string {
  const fields = Object.values(row.recognition?.fields || {})
  if (!fields.length) return ''
  const found = fields.filter(field => field.value != null && field.value !== '').length
  const absent = fields.filter(field => ['not_stated', 'not_marked', 'unmarked'].includes(field.status || '')).length
  return found ? `已提取 ${found} 项${absent ? ` · ${absent} 项未标注` : ''}` : absent ? '原文件未标注工程参数' : '尚未识别到工程参数'
}
const supply = (row: { supply_type: string | null; supply_type_candidate: string | null }) => row.supply_type
  ? supplyLabels[row.supply_type] || row.supply_type
  : row.supply_type_candidate && supplyLabels[row.supply_type_candidate] ? `${supplyLabels[row.supply_type_candidate]}（候选）` : '—'
const operationText = (value: unknown): string => {
  if (typeof value === 'string') return value
  if (!value || typeof value !== 'object') return ''
  const item = value as Record<string, unknown>
  return valueText(item.operation || item.name || item.label)
}
function editorDefaults(data: EngineeringResponse) {
  const keys = [...lineFields.map(([key]) => key), 'supply_type', 'parent_line_id']
  return {
    header: Object.fromEntries([...headerFields.map(([key]) => key), 'basis_notes'].map(key => [key, valueText(data.review?.header?.[key])])),
    lines: Object.fromEntries(data.manifest.lines.map(row => [row.id, Object.fromEntries(keys.map(key => [key, valueText(data.review?.lines?.[row.id]?.[key] ?? row[key as keyof EngineeringLine])]))])),
  }
}

async function engineeringRequest(url: string, init?: RequestInit): Promise<EngineeringResponse> {
  const headers = new Headers(init?.headers); headers.set('X-Workbench-Request', '1')
  const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...init, headers })
  let data: unknown
  try { data = await response.json() } catch { throw new Error('工程资料暂时无法读取，请稍后重试。') }
  if (!response.ok) {
    const body = data as { error?: { message?: string; details?: unknown } | string }
    const message = typeof body.error === 'string' ? body.error : body.error?.message || `请求未完成（${response.status}）`
    const details = typeof body.error === 'object' && body.error?.details && typeof body.error.details === 'object' ? body.error.details as Record<string, unknown> : null
    throw new EngineeringFailure(message, response.status, valueText(details?.field))
  }
  const result = data as EngineeringResponse
  if (!result || !Number.isSafeInteger(result.revision) || !result.manifest || !Array.isArray(result.manifest.lines)
      || !Array.isArray(result.manifest.materials) || !Array.isArray(result.manifest.processes)) throw new Error('工程资料格式尚未就绪，请重新载入。')
  return result
}

async function engineeringPricingRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers); headers.set('X-Workbench-Request', '1')
  const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', redirect: 'error', ...init, headers })
  const reply = await response.json().catch(() => { throw new Error('核价服务尚未返回有效响应，已填写的费率仍保留，可重试。') })
  if (!response.ok) throw new EngineeringFailure(typeof reply?.error === 'string' ? reply.error : reply?.error?.message || `核价未完成（${response.status}）`, response.status)
  return reply as T
}
const pricingMethodLabels: Record<string, string> = { adopt_material: '按图纸材料重新核价', purchase_mesh: '纱网采购', pipe_weld_seal: '圆管焊接与防漏加工', mesh_weld: '纱网焊接', hotdip: '热浸锌处理' }
function pricingSourceText(value: unknown): string {
  if (value == null) return ''
  if (typeof value === 'string' || typeof value === 'number') return String(value)
  if (Array.isArray(value)) return value.map(pricingSourceText).filter(Boolean).join('；')
  if (typeof value !== 'object') return ''
  const source = value as Record<string, unknown>
  return ['name', 'label', 'text', 'value', 'scope', 'description', 'note', 'reason', 'reference', 'quote', 'source', 'inclusions'].map(key => pricingSourceText(source[key])).filter(Boolean).join(' · ')
}
function pricingCostText(evidence: Record<string, unknown>): string {
  const unit = evidence.unit === 'piece' ? '件' : evidence.unit === 'kg' || evidence.unit === 'kg_order' ? 'kg' : ''
  return [valueText(evidence.basis), evidence.amount != null ? `本项成本 ¥${quotationNumber(valueText(evidence.amount))}` : '',
    evidence.rate != null ? `费率 ¥${quotationNumber(valueText(evidence.rate))}${unit ? ` / ${unit}` : ''}` : '',
    evidence.source_kind === 'demo' ? '演示假设' : '沿用本单材料采用价', pricingSourceText(evidence.inclusions)].filter(Boolean).join('；')
}
function validatePricingPlan(reply: EngineeringPricingPlan) {
  if (!reply?.plan?.policy || !Array.isArray(reply.plan.policy.entries) || !Array.isArray(reply.plan.requirements) || !Array.isArray(reply.plan.assumptions)
    || !Number.isSafeInteger(reply.expected_revision) || !/^RUN-[a-f0-9]{32}$/.test(reply.current_run_id)) throw new Error('核价条件尚未就绪，请重新读取。')
  return reply
}
function EngineeringPricingAction({ prefix, disabled, onRepriced }: { prefix: string | null; disabled?: boolean; onRepriced?(batch: unknown): void }) {
  const [open, setOpen] = useState(false), [loading, setLoading] = useState(false), [saving, setSaving] = useState(false)
  const [plan, setPlan] = useState<EngineeringPricingPlan | null>(null), [policy, setPolicy] = useState<EngineeringPricingPolicy | null>(null)
  const [error, setError] = useState(''), [stale, setStale] = useState(false)
  const dialog = useRef<HTMLDialogElement>(null), errorElement = useRef<HTMLParagraphElement>(null)
  const controller = useRef<AbortController | null>(null), attempt = useRef<{ signature: string; eventId: string } | null>(null)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; controller.current?.abort() } }, [])
  useEffect(() => {
    if (!open || !dialog.current) return
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null, current = dialog.current
    if (!current.open) current.showModal()
    return () => { if (current.open) current.close(); if (previous?.isConnected) previous.focus() }
  }, [open])
  useEffect(() => { if (error) errorElement.current?.focus() }, [error])
  async function loadPlan(preserve = false) {
    if (!prefix || loading || saving) return
    const abort = new AbortController(); controller.current?.abort(); controller.current = abort
    setOpen(true); setLoading(true); setError('')
    try {
      const next = validatePricingPlan(await engineeringPricingRequest<EngineeringPricingPlan>(prefix + '/engineering-pricing', { signal: abort.signal }))
      if (abort.signal.aborted || !mounted.current) return
      setPlan(next); setPolicy(previous => ({ ...next.plan.policy, entries: next.plan.policy.entries.map(entry => {
        const prior = preserve && previous?.entries.find(row => row.line_id === entry.line_id && row.field === entry.field && row.method === entry.method && row.requirement === entry.requirement)
        return prior && entry.source_kind === 'demo' ? { ...entry, rate: prior.rate, minimum: prior.minimum } : entry
      }) })); setStale(false)
    } catch (failure) { if (!abort.signal.aborted && mounted.current) setError(failure instanceof Error ? failure.message : '核价条件读取失败，请重试。') }
    finally { if (!abort.signal.aborted && mounted.current) setLoading(false) }
  }
  function changeEntry(index: number, key: 'rate' | 'minimum', value: string) {
    setPolicy(previous => previous && ({ ...previous, entries: previous.entries.map((entry, position) => position === index ? { ...entry, [key]: value } : entry) }))
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!prefix || !plan || !policy || saving || loading || stale) return
    const signature = JSON.stringify({ expected_revision: plan.expected_revision, policy })
    if (attempt.current?.signature !== signature) attempt.current = { signature, eventId: crypto.randomUUID() }
    const abort = new AbortController(); controller.current = abort; setSaving(true); setError('')
    try {
      const reply = await engineeringPricingRequest<{ batch: unknown; run_id: string; source_run_id: string; replayed: boolean }>(prefix + '/engineering-pricing', {
        method: 'POST', signal: abort.signal, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ event_id: attempt.current.eventId, expected_revision: plan.expected_revision, policy }),
      })
      if (abort.signal.aborted || !mounted.current) return
      if (!reply.batch || !/^RUN-[a-f0-9]{32}$/.test(reply.run_id)) throw new Error('新报价返回格式有误，请保持当前条件重试。')
      if (!onRepriced) throw new Error('新报价已保存，请重新打开当前订单查看。')
      onRepriced(reply.batch); setOpen(false)
    } catch (failure) {
      if (!abort.signal.aborted && mounted.current) { setError(failure instanceof Error ? failure.message : '核价未完成，费率仍保留，可重试。'); setStale(failure instanceof EngineeringFailure && failure.status === 409) }
    } finally { if (!abort.signal.aborted && mounted.current) setSaving(false) }
  }
  return <><button type="button" className="bq-button primary beb-reprice-button" data-testid="bq-engineering-reprice" disabled={disabled || loading || saving || !prefix} onClick={() => { if (plan && policy) setOpen(true); else void loadPlan() }}><RefreshCw size={14} />按工程要求生成新报价</button>
    {open && <dialog ref={dialog} className="beb-editor-dialog beb-pricing-dialog" aria-labelledby="beb-pricing-title" onCancel={event => { event.preventDefault(); if (!saving && !loading) setOpen(false) }}><section className="beb-editor" data-testid="bq-engineering-pricing-editor"><header><div><p className="beb-eyebrow">ENGINEERING PRICING</p><h3 id="beb-pricing-title">按工程要求生成新报价</h3></div><button type="button" aria-label="关闭工程核价" disabled={saving || loading} onClick={() => setOpen(false)}><X size={19} /></button></header><form onSubmit={event => void submit(event)}><div className="beb-editor-body">
      <p className="beb-editor-note">沿用本单套数与定价方式，新建待审核的模拟报价，原报价和来源记录保留。人民币、未税、不含运费。</p>
      {loading && <p className="beb-pricing-loading" role="status"><LoaderCircle size={16} className="bq-spin" />正在读取图纸要求及核价条件…</p>}
      {policy && <div className="beb-pricing-entries">{policy.entries.map((entry, index) => {
        const requirement = plan?.plan.requirements.find(row => row.line_id === entry.line_id && row.field === entry.field && row.requirement === entry.requirement)
        const demo = entry.source_kind === 'demo', evidence = requirement ? pricingSourceText(requirement.evidence || requirement.source_evidence) : ''
        const material = requirement?.material_rate != null ? `${entry.requirement} · 采用价 ¥${quotationNumber(valueText(requirement.material_rate))} / kg（未税）` : ''
        return <section className="beb-pricing-entry" key={`${entry.line_id}/${entry.field}/${entry.method}`} data-testid={`bq-pricing-entry-${entry.line_id}-${entry.method}`}><div className="beb-pricing-entry-heading"><div><span>{entry.line_id} · {pricingMethodLabels[entry.method] || '工程要求核价'}</span><strong>{entry.requirement}</strong></div><span className={demo ? 'beb-price-source demo' : 'beb-price-source'}>{demo ? '演示费率' : '沿用材料参考价'}</span></div>
          {entry.method === 'adopt_material' ? <p className="beb-pricing-source">{material || '按已保存的材料要求，匹配本单材料价格与密度；不新增图纸属性。'}</p> : <div className="beb-pricing-rate-grid"><label>采用费率（{entry.unit === 'kg_order' ? '元 / kg' : '元 / 件'}）<input type="number" min="0" step="0.01" inputMode="decimal" required value={entry.rate ?? ''} disabled={saving || loading || !demo} onChange={event => changeEntry(index, 'rate', event.target.value)} data-testid={`bq-pricing-rate-${entry.line_id}-${entry.method}`} /></label>{entry.method === 'hotdip' && <label>整单该处理最低收费（元）<input type="number" min="0" step="0.01" inputMode="decimal" required value={entry.minimum} disabled={saving || loading || !demo} onChange={event => changeEntry(index, 'minimum', event.target.value)} data-testid={`bq-pricing-minimum-${entry.line_id}-${entry.method}`} /></label>}</div>}
          {requirement?.quantity_total != null && <p className="beb-pricing-quantity">已保存订单用量：{count(valueText(requirement.quantity_total))} 件{entry.method === 'hotdip' ? '；按本单该处理总净重计费，最低收费仅计一次。' : ''}</p>}
          {pricingSourceText(entry.inclusions) && <p className="beb-pricing-inclusions">{pricingSourceText(entry.inclusions)}</p>}{evidence && <details><summary>查看要求来源</summary><p>{evidence}</p></details>}
        </section>
      })}</div>}
      {plan && <details className="beb-pricing-assumptions" open><summary>本次价格依据与假设</summary><ul>{plan.plan.assumptions.filter(assumption => {
        const name = assumption && typeof assumption === 'object' ? valueText((assumption as Record<string, unknown>).name) : ''
        return !policy?.entries.some(entry => entry.source_kind === 'demo' && name === `${entry.line_id} ${entry.requirement}`)
      }).map((assumption, index) => <li key={index}>{pricingSourceText(assumption) || '本项采用演示假设，待人工核对。'}</li>)}</ul><p>材料参考价仍需供应商询价核实；上列加工和纱网费率为演示假设。纱网不推断为 304 或 316，不将防漏加工等同于已通过试压验收。</p></details>}
      {error && <p ref={errorElement} tabIndex={-1} className="beb-error" role="alert" data-testid="bq-engineering-pricing-error">{error}{stale && <button type="button" disabled={saving || loading} onClick={() => void loadPlan(true)}>读取最新条件，保留所填费率</button>}{!policy && !loading && <button type="button" onClick={() => void loadPlan()}>重新读取</button>}</p>}
    </div><footer><button type="button" className="bq-button" disabled={saving || loading} onClick={() => setOpen(false)}>稍后再核价</button><button type="submit" className="bq-button primary" disabled={!plan || !policy?.entries.length || saving || loading || stale} data-testid="bq-engineering-pricing-submit">{saving ? <LoaderCircle size={14} className="bq-spin" /> : <Save size={14} />}{saving ? '正在生成新报价' : '生成新的模拟报价'}</button></footer></form></section></dialog>}
  </>
}
function EngineeringPricingSummary({ repricing, batchId, apiBase }: { repricing?: EngineeringRepricing | null; batchId: string; apiBase: QuoteApiBase }) {
  if (!repricing?.before_totals || !repricing.after_totals) return null
  const money = (value: unknown) => value == null || value === '' ? '—' : `¥${quotationNumber(valueText(value))}`
  const before = repricing.before_totals, after = repricing.after_totals
  const changes = Array.isArray(repricing.changes) ? repricing.changes : []
  const assumptions = Array.isArray(repricing.assumptions) ? repricing.assumptions : []
  const requirements = Array.isArray(repricing.applied_requirements) ? repricing.applied_requirements : []
  const oldRun = typeof repricing.source_run_id === 'string' && /^RUN-[a-f0-9]{32}$/.test(repricing.source_run_id) ? repricing.source_run_id : null
  return <section className="beb-repricing-summary" data-testid="bq-engineering-repricing-summary" aria-label="工程要求重新核价结果"><div className="beb-repricing-heading"><div><strong>工程要求已纳入新报价</strong><p>{count(requirements.length)} 项要求已计入，新报价保持待审核。</p></div>{oldRun && <a className="bq-button" href={`${apiBase}/${batchId}/runs/${oldRun}/quote.html`} target="_blank" rel="noopener noreferrer" data-testid="bq-prior-engineering-quote"><ExternalLink size={13} />查看原报价</a>}</div>
    <div className="beb-repricing-totals"><div><span>原报价金额</span><strong>{money(before.total_price)}</strong></div><div><span>新报价金额</span><strong data-testid="bq-engineering-repriced-total">{money(after.total_price)}</strong></div><div><span>成本变化</span><strong>{money(before.total_cost)} → {money(after.total_cost)}</strong></div></div>
    <details className="beb-repricing-details"><summary>查看核价变化与价格依据</summary><div className="beb-requirements-table-wrap"><table><thead><tr><th>零件</th><th>原成本</th><th>新成本</th><th>原报价金额</th><th>新报价金额</th></tr></thead><tbody>{changes.map(row => <tr key={row.line_id}><td><small>{row.line_id}</small><strong>{row.part_name}</strong></td><td>{money(row.before.total_cost)}</td><td>{money(row.after.total_cost)}</td><td>{money(row.before.total_price)}</td><td>{money(row.after.total_price)}</td></tr>)}</tbody></table></div><ul>{requirements.map((requirement, index) => <li key={`${requirement.line_id}/${requirement.field}/${index}`}><strong>{requirement.line_id} · {requirement.requirement}</strong><span>已纳入核价 · {pricingMethodLabels[requirement.method] || '工程要求'}</span>{requirement.cost_evidence && <small>{pricingCostText(requirement.cost_evidence)}</small>}</li>)}</ul>{!!assumptions.length && <div className="beb-repricing-assumptions"><strong>本次采用条件</strong><ul>{assumptions.map((assumption, index) => <li key={index}>{pricingSourceText(assumption)}</li>)}</ul></div>}</details>
  </section>
}

export default function BatchEngineeringPanel({ batchId, runId, apiBase = PUBLIC_QUOTE_API, savedCase = false, view, onPreview, previewReady, onRepriced }: {
  batchId: string; runId?: string; apiBase?: QuoteApiBase; savedCase?: boolean; view: EngineeringView | null; onPreview(lineId: string): void; previewReady: Set<string>; onRepriced?(batch: unknown): void;
}) {
  const valid = /^BATCH-[a-f0-9]{32}$/.test(batchId) && typeof runId === 'string' && /^RUN-[a-f0-9]{32}$/.test(runId)
  const prefix = valid ? `${apiBase}/${batchId}/runs/${runId}` : null
  const endpoint = prefix ? prefix + '/engineering' : null
  const [data, setData] = useState<EngineeringResponse | null>(null), [loading, setLoading] = useState(true)
  const [error, setError] = useState(''), [reload, setReload] = useState(0), [search, setSearch] = useState('')
  const [recognitionStarting, setRecognitionStarting] = useState(false), [recognitionError, setRecognitionError] = useState('')
  const [documentFilter, setDocumentFilter] = useState(''), [editing, setEditing] = useState(false), [saving, setSaving] = useState(false)
  const [headerDraft, setHeaderDraft] = useState<Draft>({}), [lineDrafts, setLineDrafts] = useState<Record<string, Draft>>({})
  const [selected, setSelected] = useState(''), [formError, setFormError] = useState(''), [savedMessage, setSavedMessage] = useState('')
  const [conflict, setConflict] = useState(false), [invalidField, setInvalidField] = useState('')
  const initialHeader = useRef<Draft>({}), initialLines = useRef<Record<string, Draft>>({}), sequence = useRef(0)
  const saveAbort = useRef<AbortController | null>(null)
  const recognitionAbort = useRef<AbortController | null>(null)
  const editor = useRef<HTMLDialogElement>(null), errorElement = useRef<HTMLParagraphElement>(null)
  useEffect(() => {
    const token = ++sequence.current, abort = new AbortController()
    saveAbort.current?.abort(); recognitionAbort.current?.abort(); setData(null); setError(''); setLoading(true); setEditing(false); setSaving(false); setRecognitionStarting(false); setRecognitionError('')
    if (!endpoint) { setLoading(false); return () => abort.abort() }
    void engineeringRequest(endpoint, { signal: abort.signal, redirect: 'error' }).then(next => {
      if (!abort.signal.aborted && sequence.current === token) setData(next)
    }).catch(failure => { if (!abort.signal.aborted && sequence.current === token) setError(failure instanceof Error ? failure.message : '工程资料暂未载入。') })
      .finally(() => { if (!abort.signal.aborted && sequence.current === token) setLoading(false) })
    return () => { abort.abort(); saveAbort.current?.abort(); recognitionAbort.current?.abort(); ++sequence.current }
  }, [endpoint, reload])
  const recognitionActive = recognitionRunning(data?.recognition?.status)
  useEffect(() => {
    if (!endpoint || !recognitionActive || editing) return
    const abort = new AbortController(), token = sequence.current
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const next = await engineeringRequest(endpoint!, { signal: abort.signal, redirect: 'error' })
        if (!abort.signal.aborted && sequence.current === token) { setData(next); setRecognitionError('') }
      } catch (failure) {
        if (!abort.signal.aborted && sequence.current === token) setRecognitionError(failure instanceof Error ? failure.message : '进度暂时无法读取，识别仍在后台执行。')
      } finally {
        if (!abort.signal.aborted && sequence.current === token) timer = setTimeout(() => void poll(), 2500)
      }
    }
    timer = setTimeout(() => void poll(), 1800)
    return () => { abort.abort(); clearTimeout(timer) }
  }, [endpoint, recognitionActive, data?.recognition?.id, editing])
  useEffect(() => {
    if (!editing || !editor.current) return
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const dialog = editor.current
    if (!dialog.open) dialog.showModal()
    return () => { if (dialog.open) dialog.close(); if (previous?.isConnected) previous.focus() }
  }, [editing])
  useEffect(() => {
    if (!formError) return
    const input = invalidField ? editor.current?.querySelector<HTMLElement>(`[data-testid="${CSS.escape(invalidField)}"]`) : null
    if (input) input.focus(); else errorElement.current?.focus()
  }, [formError, invalidField])

  function openEditor(id?: string) {
    if (!data) return
    const defaults = editorDefaults(data)
    initialHeader.current = defaults.header; initialLines.current = defaults.lines
    setHeaderDraft({ ...initialHeader.current }); setLineDrafts({}); setSelected(id || data.manifest.lines[0]?.id || '')
    setFormError(''); setSavedMessage(''); setConflict(false); setInvalidField(''); setEditing(true)
  }
  function editLine(key: string, value: string) {
    if (!selected) return
    setLineDrafts(previous => ({ ...previous, [selected]: { ...(previous[selected] || initialLines.current[selected] || {}), [key]: value } }))
    setSavedMessage('')
  }
  async function startRecognition() {
    if (savedCase || !prefix || !endpoint || recognitionStarting || recognitionActive || saving) return
    const abort = new AbortController(), token = sequence.current
    recognitionAbort.current = abort; setRecognitionStarting(true); setRecognitionError('')
    try {
      const response = await fetch(prefix + '/engineering-recognize', { method: 'POST', credentials: 'same-origin', cache: 'no-store', signal: abort.signal, redirect: 'error',
        headers: { 'Content-Type': 'application/json', 'X-Workbench-Request': '1' }, body: JSON.stringify({ event_id: crypto.randomUUID() }) })
      const reply = await response.json().catch(() => { throw new Error('识别服务暂未返回有效响应，请稍后查看后台进度。') })
      if (!response.ok) throw new Error(typeof reply?.error === 'string' ? reply.error : reply?.error?.message || '工程识别暂未开始，请重试。')
      const next = reply?.manifest && Number.isSafeInteger(reply.revision) ? reply as EngineeringResponse
        : await engineeringRequest(endpoint, { signal: abort.signal, redirect: 'error' })
      if (!abort.signal.aborted && sequence.current === token) setData(next)
    } catch (failure) {
      if (!abort.signal.aborted && sequence.current === token) setRecognitionError(failure instanceof Error ? failure.message : '工程识别暂未开始，请重试。')
    } finally { if (!abort.signal.aborted && sequence.current === token) setRecognitionStarting(false) }
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!data || !endpoint || saving) return
    const header = Object.fromEntries(Object.entries(headerDraft).filter(([key, value]) => value !== initialHeader.current[key]))
    const lines: Record<string, Draft> = {}
    for (const [id, values] of Object.entries(lineDrafts)) {
      const changed = Object.fromEntries(Object.entries(values).filter(([key, value]) => value !== initialLines.current[id]?.[key]))
      if (Object.keys(changed).length) lines[id] = changed
    }
    if (!Object.keys(header).length && !Object.keys(lines).length) { setFormError('尚未修改工程信息。'); return }
    const abort = new AbortController(), token = sequence.current
    saveAbort.current = abort; setSaving(true); setFormError(''); setInvalidField(''); setConflict(false)
    try {
      const next = await engineeringRequest(endpoint, { method: 'POST', signal: abort.signal, redirect: 'error',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ event_id: crypto.randomUUID(), expected_revision: data.revision, review: { header, lines } }) })
      if (!abort.signal.aborted && sequence.current === token) { setData(next); setEditing(false); setSavedMessage('工程信息已保存为待审核资料。') }
    } catch (failure) {
      if (!abort.signal.aborted && sequence.current === token) {
        setFormError(failure instanceof Error ? failure.message : '保存未完成，草稿已保留。')
        setConflict(failure instanceof EngineeringFailure && failure.status === 409)
        if (failure instanceof EngineeringFailure && failure.field) {
          const lineField = /(?:^|\.)lines\.(L\d{4,8})\.([a-z_]+)$/.exec(failure.field)
          const headerField = /(?:^|\.)header\.([a-z_]+)$/.exec(failure.field)
          if (lineField) { setSelected(lineField[1]); setInvalidField(`bq-engineering-line-${lineField[2]}`) }
          else if (headerField) setInvalidField(`bq-engineering-header-${headerField[1]}`)
        }
      }
    } finally { if (!abort.signal.aborted && sequence.current === token) setSaving(false) }
  }
  async function refreshVersion() {
    if (!endpoint || saving) return
    const abort = new AbortController(), token = sequence.current
    saveAbort.current = abort; setSaving(true)
    try {
      const next = await engineeringRequest(endpoint, { signal: abort.signal, redirect: 'error' })
      if (!abort.signal.aborted && sequence.current === token) {
        const defaults = editorDefaults(next), previousHeader = initialHeader.current, previousLines = initialLines.current
        setHeaderDraft(previous => Object.fromEntries(Object.entries(defaults.header).map(([key, value]) => [key, previous[key] !== previousHeader[key] ? previous[key] : value])))
        setLineDrafts(previous => Object.fromEntries(Object.entries(previous).filter(([id]) => defaults.lines[id]).map(([id, values]) => [id,
          Object.fromEntries(Object.entries(defaults.lines[id]).map(([key, value]) => [key, values[key] !== previousLines[id]?.[key] ? values[key] : value]))])))
        initialHeader.current = defaults.header; initialLines.current = defaults.lines
        setData(next); setConflict(false); setInvalidField(''); setFormError('已读取最新版本，你修改的字段仍保留在表单中，请核对后保存。')
      }
    } catch (failure) { if (!abort.signal.aborted && sequence.current === token) setFormError(failure instanceof Error ? failure.message : '最新版本尚未读取，草稿仍保留。') }
    finally { if (!abort.signal.aborted && sequence.current === token) setSaving(false) }
  }

  if (view === null) {
    const basis = data?.manifest.quote_basis
    const pending = (basis?.requirements_review || []).filter(row => !['category_matches', 'priced'].includes(row.status))
    const unresolved = basis?.unresolved_requirements ?? pending.length
    if (!basis) return null
    return <><EngineeringPricingSummary repricing={basis.engineering_repricing} batchId={batchId} apiBase={apiBase} />{unresolved > 0 && <section className="beb-cost-requirements" role="status" data-testid="bq-cost-engineering-requirements"><div><strong>{count(unresolved)} 项工程要求待核价</strong><p>原模拟金额尚待按这些工程要求重新核价。</p></div>{!savedCase && <EngineeringPricingAction prefix={prefix} disabled={recognitionActive || recognitionStarting || saving} onRepriced={onRepriced} />}<details><summary>查看待核价要求</summary><ul>{pending.map((requirement, index) => <li key={`${requirement.line_id}/${requirement.field}/${index}`}><span>{requirement.line_id} · {requirement.part_name}</span><strong>{requirement.requirement}</strong><small>待核价 · {requirement.reason}</small></li>)}</ul></details></section>}</>
  }
  if (!endpoint) return <div className="beb-empty">这份历史报价没有可关联的运行记录，原报价仍可查看。</div>
  if (loading) return <div className="beb-loading" role="status"><LoaderCircle className="bq-spin" size={18} />正在整理工程资料…</div>
  if (!data || error) return <div className="beb-error" role="alert">{error || '工程资料暂未就绪。'}<button type="button" onClick={() => setReload(value => value + 1)}>重新载入<RefreshCw size={14} /></button></div>
  const manifest = data.manifest, header = manifest.header
  const needle = search.trim().toLocaleLowerCase()
  const matches = (values: unknown[]) => !needle || values.map(valueText).join(' ').toLocaleLowerCase().includes(needle)
  const visible = manifest.lines.filter(row => (!documentFilter || row.document === documentFilter)
    && matches([row.id, row.part_name, row.part_number, row.drawing_number, row.material_grade, row.material_description, row.document]))
  const documents = [...new Set(manifest.lines.map(row => row.document))]
  const linesById = new Map(manifest.lines.map(row => [row.id, row]))
  const selectedDraft = lineDrafts[selected] || initialLines.current[selected] || {}
  const title = view === 'engineering' ? '工程 BOM' : view === 'materials' ? '备料汇总' : '工艺明细'
  const file = view === 'engineering' ? 'engineering-bom.html' : view === 'materials' ? 'materials.html' : 'processes.html'
  const recognition = data.recognition
  const recognitionFailure = recognition?.status === 'failed'
  const recognitionCompleted = recognition?.status === 'completed' || recognition?.status === 'completed_with_issues'
  const jobError = typeof recognition?.error === 'string' ? recognition.error : recognition?.error?.message
  const coverage = coverageOf(manifest), quoteBasis = manifest.quote_basis
  const requirements = quoteBasis?.requirements_review || []
  const unresolvedRequirements = quoteBasis?.unresolved_requirements
    ?? requirements.filter(row => ['not_adopted', 'not_separately_priced'].includes(row.status)).length
  const showIdentity = manifest.lines.some(row => row.part_number || row.drawing_number)
  const showParent = manifest.lines.some(row => row.parent_line_id)
  const materialKnown = manifest.lines.filter(row => row.field_sources?.material_grade === 'manual'
    ? row.material_grade : row.material_grade || row.material_description || row.cad_material_raw).length
  const materialMissing = Math.max(0, coverage.lines - materialKnown)
  const finishMissing = Math.max(0, coverage.lines - coverage.surface_treatment)
  return <div className="beb" data-testid="bq-engineering-panel" data-view={view} role="tabpanel">
    <div className="beb-heading"><div><p className="beb-eyebrow">{view === 'engineering' ? 'ENGINEERING BILL OF MATERIALS' : view === 'materials' ? 'MATERIAL REQUIREMENTS' : 'PROCESS INFORMATION'}</p><h2>{title}</h2><p>{view === 'engineering' ? '核对零件身份、工程规格和用量。' : view === 'materials' ? '按本次已保存的订单范围汇总备料需求。' : '查看已记录工序，未提供的项目留待工程补充。'}</p></div><span className="beb-status">待工程审核</span></div>
    <div className={`beb-identity${header.product_number ? '' : ' beb-identity-compact'}`}><div><span>产品名称</span><strong>{show(header.product_name)}</strong></div>{header.product_number && <div data-testid="bq-engineering-product-number"><span>产品编号</span><strong>{header.product_number}</strong></div>}<div><span>用量基准</span><strong>{count(header.base_quantity)} 套</strong></div><div><span>订单数量</span><strong>{count(header.order_sets)} 套</strong></div></div>
    {view === 'engineering' && <section className="beb-coverage" aria-labelledby="beb-coverage-title" data-testid="bq-engineering-coverage">
      <div className="beb-coverage-heading"><h3 id="beb-coverage-title">工程信息覆盖</h3><span>{count(coverage.lines)} 条来源物料 · 提取值待工程核对</span></div>
      <div className="beb-coverage-grid">
        <div><span>成品尺寸 / 规格</span><strong data-testid="bq-coverage-specification">{count(coverage.specification)} <small>/ {count(coverage.lines)} 行</small></strong><p>成品包络与图纸规格</p></div>
        <div><span>板厚 / 壁厚</span><strong data-testid="bq-coverage-thickness">{count(coverage.thickness_mm)} <small>行有量测候选</small></strong><p>{count(coverage.buy_candidates)} 行外购候选按型号核对</p></div>
        <div><span>材料信息</span><strong data-testid="bq-coverage-material">{count(materialKnown)} <small>/ {count(coverage.lines)} 行</small></strong><p>牌号 {count(coverage.material_grade)} 行 · 原文描述 {count(coverage.material_description)} 行</p></div>
        <div><span>表面处理</span><strong data-testid="bq-coverage-finish">{count(coverage.surface_treatment)} <small>/ {count(coverage.lines)} 行</small></strong><p>采用已有属性与图纸标注</p></div>
      </div>
      <div className="beb-coverage-gaps" data-testid="bq-engineering-gaps"><strong>尚需取得依据</strong><span>材料信息 {count(materialMissing)} 行 · 表面处理 {count(finishMissing)} 行 · 可靠板 / 壁厚 {count(coverage.thickness_pending_make)} 条自制候选。</span>{(!showIdentity || !showParent) && <small>{!showIdentity ? '当前按 CAD 名称定位，正式物料编码及图号尚未提供。' : ''}{!showParent ? '装配父项尚未整理，沿用本次已保存的数量范围。' : ''}</small>}</div>
    </section>}
    {quoteBasis && view !== 'processes' && <section className={`beb-quote-basis${requirements.length ? ' has-requirements' : ''}`} aria-label="本次报价采用条件" data-testid="bq-engineering-quote-basis">
      <div><span>本次报价采用条件</span><strong>{quoteBasis.total_price != null ? `${unresolvedRequirements > 0 ? '原模拟报价' : '已保存报价'} ¥${quotationNumber(quoteBasis.total_price)}` : '报价金额尚未完整'}{quoteBasis.order_sets != null ? ` · ${count(quoteBasis.order_sets)} 套` : ''}</strong></div>
      <p>核价采用材料：{quoteBasis.adopted_materials.length ? quoteBasis.adopted_materials.join('、') : '尚未保存采用类别'}<small>{quoteBasis.note || '仅作本次核价条件；工程材料以原始资料和人工核对结果为准。'}</small></p>
      {unresolvedRequirements > 0 && <div className="beb-requirements-alert" role="status" data-testid="bq-engineering-reprice-required"><strong>{count(unresolvedRequirements)} 项已识别工程要求待核价</strong><p>原模拟金额尚待按这些工程要求重新核价。</p>{!savedCase && <EngineeringPricingAction prefix={prefix} disabled={recognitionActive || recognitionStarting || saving || editing} onRepriced={onRepriced} />}</div>}
      {!!requirements.length && <details className="beb-requirements"><summary className="beb-requirements-heading"><strong>查看工程要求与原报价核对（{requirements.length} 项）</strong><span>材料及工艺要求逐项保留</span></summary><div className="beb-requirements-table-wrap"><table data-testid="bq-engineering-requirements"><thead><tr><th>零件</th><th>工程要求</th><th>核价状态</th><th>说明</th></tr></thead><tbody>{requirements.map((requirement, index) => <tr key={`${requirement.line_id}/${requirement.field}/${index}`} data-testid={`bq-engineering-requirement-${requirement.line_id}-${requirement.field}`}><td><span>{requirement.line_id}</span><strong>{requirement.part_name}</strong></td><td><span>{requirementFieldLabels[requirement.field] || requirement.field}</span><strong>{requirement.requirement}</strong></td><td><span className={`beb-requirement-status${requirement.status === 'category_matches' ? ' matches' : ''}`}>{requirementStatus(requirement.status)}</span></td><td>{requirement.reason}</td></tr>)}</tbody></table></div></details>}
    </section>}
    {quoteBasis?.engineering_repricing && view !== 'processes' && <EngineeringPricingSummary repricing={quoteBasis.engineering_repricing} batchId={batchId} apiBase={apiBase} />}
    <div className="beb-actions"><div className="beb-actions-main">{view === 'engineering' && <>{!savedCase && <button className="bq-button primary" type="button" data-testid="bq-engineering-recognize" disabled={recognitionStarting || recognitionActive || saving} onClick={() => void startRecognition()}>{recognitionStarting || recognitionActive ? <LoaderCircle size={15} className="bq-spin" /> : <Sparkles size={15} />}{recognitionStarting ? '正在启动识别' : recognitionActive ? '后台识别中' : recognitionFailure ? '重试自动识别' : recognitionCompleted ? '重新识别工程信息' : '自动识别工程信息'}</button>}<button className="bq-button" type="button" data-testid="bq-engineering-edit" onClick={() => openEditor()}><Pencil size={15} />人工核对与修正</button></>}<a className="bq-button" href={`${prefix}/${file}`} target="_blank" rel="noopener noreferrer" data-testid={`bq-engineering-open-${view}`}><ExternalLink size={15} />打开打印视图</a>{view === 'engineering' && <a className="bq-button" href={`${prefix}/engineering-bom.csv`} download data-testid="bq-engineering-download"><Download size={15} />下载工程 BOM</a>}</div><span>{view === 'engineering' ? `${manifest.lines.length} 条来源物料` : view === 'materials' ? `${manifest.materials.length} 项汇总需求` : `${manifest.processes.length} 项零件工艺`}</span></div>
    {view === 'engineering' && <div className={`beb-recognition${recognitionFailure ? ' failed' : ''}`} data-testid="bq-engineering-recognition" role="status">
      <div className="beb-recognition-symbol">{recognitionActive || recognitionStarting ? <LoaderCircle size={18} className="bq-spin" /> : <Sparkles size={18} />}</div>
      <div><strong>{savedCase ? '沿用已保存的工程识别结果' : recognitionActive || recognitionStarting ? '正在识别工程信息' : recognitionFailure ? '本轮识别未完成' : recognitionCompleted ? '工程识别结果已保存' : 'CAD 参数读取与 AI 图纸识别'}</strong><p data-testid="bq-engineering-recognition-message">{savedCase ? '本次修改复用原订单的 CAD 读取与图纸标注；可核对工程资料，再通过修改助手调整计价条件。' : recognition?.message || jobError || (recognitionActive ? '正在处理资料，已保存结果会保留。' : '自动读取 CAD 的成品尺寸、板厚和材料，再核对附件中的工程标注与表面处理。')}</p><small>{recognitionActive ? '可切换页面或关闭浏览器，后台继续；再次打开即可查看结果。' : recognitionCompleted ? '提取值附来源并保持待核对；未标注的信息显示为 —，表面处理不会依据模型颜色猜测。' : '没有标注的材料牌号和表面处理会明确保留为空，不使用行情规格代替。'}</small></div>
    </div>}
    {recognitionError && <p className="beb-error" role="alert" data-testid="bq-engineering-recognition-error">{recognitionError}</p>}
    {savedMessage && <p className="beb-success" role="status">{savedMessage}</p>}
    <p className="beb-basis" data-testid="bq-engineering-basis">{header.quantity_basis || '数量沿用已保存报价中“每份 STEP 根模型各一套”的范围，资料间重叠和装配关系仍需核对。'}{header.quantity_effect || '人工填写的归属仅整理结构，不重新计算数量。'}{header.basis_notes && <small>补充范围说明：{header.basis_notes}</small>}</p>
    {view === 'engineering' && <>
      <div className="beb-tools"><label className="beb-search"><Search size={16} /><input type="search" maxLength={200} value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索零件、编码、图号或材料" aria-label="搜索工程物料" data-testid="bq-engineering-search" /></label><label className="beb-source-filter">来源文件<select value={documentFilter} onChange={event => setDocumentFilter(event.target.value)} data-testid="bq-engineering-source"><option value="">全部来源</option>{documents.map(document => <option key={document} value={document}>{document}</option>)}</select></label></div>
      <div className="beb-table-wrap"><table className={!showIdentity && !showParent ? 'beb-table-compact' : undefined} data-testid="bq-engineering-table"><thead><tr><th>行号 / 零件名称 / 来源</th>{showIdentity && <th>物料编码 / 图号</th>}<th>材料牌号 / 原文</th><th>成品尺寸 / 规格</th><th>板厚 / 壁厚</th><th>表面处理</th><th>供应方式</th><th>单位</th><th>每套用量</th><th>订单用量</th><th>预估单重</th>{showParent && <th>父项归属</th>}<th>操作</th></tr></thead><tbody>{visible.map(row => <tr key={row.id} data-testid={`bq-engineering-row-${row.id}`}>
        <td><small className="beb-row-id">{row.id}</small><div data-testid={!showIdentity ? `bq-engineering-identity-${row.id}` : undefined}><strong className="beb-part-name">{row.part_name}</strong>{!showIdentity && row.cad_name_code && <small className="beb-field-source">{row.cad_name_code === row.part_name ? 'CAD名称编码' : `CAD名称编码：${row.cad_name_code}`}</small>}</div><small>{row.document}</small>{rowRecognitionSummary(row) && <small className="beb-recognition-row-status">{rowRecognitionSummary(row)}</small>}</td>
        {showIdentity && <td data-testid={`bq-engineering-identity-${row.id}`}><EngineeringIdentity row={row} /></td>}
        <td data-testid={`bq-engineering-material-${row.id}`}><EngineeringMaterial row={row} /></td>
        <td data-testid={`bq-engineering-specification-${row.id}`}><EngineeringField row={row} name="specification" /></td>
        <td className="beb-number" data-testid={`bq-engineering-thickness-${row.id}`}><EngineeringField row={row} name="thickness_mm" unit="mm" /></td>
        <td data-testid={`bq-engineering-finish-${row.id}`}><EngineeringField row={row} name="surface_treatment" /></td>
        <td>{supply(row)}</td><td>{unitLabels[row.unit] || row.unit || '—'}</td>
        <td className="beb-number">{count(row.quantity_per_set)}</td><td className="beb-number">{count(row.quantity_total)}</td><td className="beb-number">{weight(row.estimated_net_weight_kg)}</td>
        {showParent && <td>{row.parent_line_id ? <span>{linesById.get(row.parent_line_id)?.part_name || row.parent_line_id}</span> : <span className="beb-pending">未关联</span>}</td>}
        <td><div className="beb-row-actions">{previewReady.has(row.id) && <button type="button" onClick={() => onPreview(row.id)} data-testid={`bq-engineering-preview-${row.id}`}><Box size={14} />查看零件</button>}<button type="button" onClick={() => openEditor(row.id)} data-testid={`bq-engineering-edit-${row.id}`}><Pencil size={13} />修正</button></div></td>
      </tr>)}{!visible.length && <tr><td colSpan={11 + Number(showIdentity) + Number(showParent)} className="beb-empty">没有符合条件的物料。</td></tr>}</tbody></table></div>
      <p className="beb-footnote">尺寸为成品外形尺寸，展开用料另行核对。识别值可查看来源并人工修正；— 表示本轮没有可采用的标注或测量依据。</p>
    </>}
    {view === 'materials' && <>
      <p className="beb-explain">净重表示零件本身的预估重量。采购计价量沿用已保存估算，包含原有用料假设，尚需排样或采购核对。</p>
      <div className="beb-table-wrap"><table data-testid="bq-materials-table"><thead><tr><th>物料名称 / 来源</th><th>供应方式</th><th>工程材料 / 原文</th><th>估重采用类别</th><th>单位</th><th>订单用量</th><th>预估净重</th><th>原料采购计价量</th></tr></thead><tbody>{manifest.materials.map(row => <tr key={row.id} data-testid={`bq-materials-row-${row.id}`}><td><strong>{row.part_name}</strong><small>{row.source_documents.join('、')}</small>{row.member_ids.length > 1 && <small>{row.member_ids.length} 条来源已汇总</small>}</td><td>{supply(row)}</td><td><MaterialInformation row={row} /></td><td>{show(row.adopted_material)}<small>{show(row.material_basis)}</small></td><td>{unitLabels[row.unit] || row.unit || '—'}</td><td className="beb-number">{count(row.quantity_total)}</td><td className="beb-number">{weight(row.estimated_total_net_weight_kg)}</td><td className="beb-number">{weight(row.purchase_usage_kg_total)}<small>{row.purchase_usage_kg_total == null ? '待补采购用量' : '沿用估算，待核对'}</small></td></tr>)}</tbody></table></div>
    </>}
    {view === 'processes' && <>
      <p className="beb-explain">展示已保存工序与图纸工艺标注原文。工序路线、工时和计费仍待工程核对。</p>
      <div className="beb-table-wrap"><table data-testid="bq-processes-table"><thead><tr><th>行号 / 零件名称</th><th>来源文件</th><th>已记录工序</th><th>图纸工艺要求 / 工时</th></tr></thead><tbody>{manifest.processes.map(row => { const operations = row.operations.map(operationText).filter(Boolean); return <tr key={row.line_id} data-testid={`bq-processes-row-${row.line_id}`}><td><small className="beb-row-id">{row.line_id}</small><strong>{row.part_name}</strong></td><td>{row.document}</td><td>{operations.length ? operations.join('、') : <span className="beb-pending">工序路线待确认</span>}</td><td><DocumentRequirements requirements={row.document_requirements || []} batchId={batchId} lineId={row.line_id} apiBase={apiBase} /></td></tr> })}</tbody></table></div>
    </>}
    {!!manifest.warnings.length && <details className="beb-warnings"><summary>查看数量范围与工程待核对事项</summary><ul>{manifest.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></details>}
    {editing && <dialog ref={editor} className="beb-editor-dialog" aria-labelledby="beb-editor-title" onCancel={event => { event.preventDefault(); if (!saving) setEditing(false) }}><section className="beb-editor" data-testid="bq-engineering-editor"><header><div><p className="beb-eyebrow">ENGINEERING INFORMATION</p><h3 id="beb-editor-title">补充工程信息</h3></div><button type="button" onClick={() => setEditing(false)} aria-label="关闭工程信息补录" disabled={saving}><X size={19} /></button></header><form onSubmit={event => void save(event)}><div className="beb-editor-body"><p className="beb-editor-note">工程资料与已保存报价分别保存；修改工程条件后需重新核价。本次补录保持待审核，不改变数量或金额。</p><fieldset disabled={saving}><legend>产品与数量口径</legend><div className="beb-form-grid">{headerFields.map(([key, label]) => <label key={key}>{label}<input value={headerDraft[key] || ''} onChange={event => setHeaderDraft(previous => ({ ...previous, [key]: event.target.value }))} maxLength={200} data-testid={`bq-engineering-header-${key}`} /></label>)}<label className="beb-wide">数量范围说明<textarea value={headerDraft.basis_notes || ''} onChange={event => setHeaderDraft(previous => ({ ...previous, basis_notes: event.target.value }))} maxLength={1000} rows={2} data-testid="bq-engineering-header-basis_notes" placeholder="描述本次资料的成套范围及需要核对的重复装配" /></label></div><p className="beb-fixed-basis">基准固定为 1 套；零件数量沿用本次保存范围，补录归属不重算数量。</p></fieldset><fieldset disabled={saving}><legend>零件工程信息</legend><label className="beb-line-selector">选择零件<select value={selected} onChange={event => setSelected(event.target.value)} data-testid="bq-engineering-line-select">{manifest.lines.map(row => <option key={row.id} value={row.id}>{row.id} · {row.part_name} · {row.document}</option>)}</select></label><div className="beb-form-grid">{lineFields.map(([key, label]) => <label key={key}>{label}<input value={selectedDraft[key] || ''} onChange={event => editLine(key, event.target.value)} inputMode={key === 'thickness_mm' ? 'decimal' : 'text'} maxLength={key === 'thickness_mm' ? 50 : 200} data-testid={`bq-engineering-line-${key}`} /></label>)}<label>供应方式<select value={selectedDraft.supply_type || ''} onChange={event => editLine('supply_type', event.target.value)} data-testid="bq-engineering-line-supply_type"><option value="">尚未指定</option>{Object.entries(supplyLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label className="beb-wide">人工归属<select value={selectedDraft.parent_line_id || ''} onChange={event => editLine('parent_line_id', event.target.value)} data-testid="bq-engineering-line-parent_line_id"><option value="">归属待关联</option>{manifest.lines.filter(row => row.id !== selected).map(row => <option key={row.id} value={row.id}>{row.id} · {row.part_name}</option>)}</select><small>用于整理归属关系，不代表装配结构已经审核。</small></label></div></fieldset>{formError && <p ref={errorElement} tabIndex={-1} className="beb-error" role="alert" data-testid="bq-engineering-save-error">{formError} 当前草稿已保留。{conflict && <button type="button" disabled={saving} data-testid="bq-engineering-refresh-version" onClick={() => void refreshVersion()}>读取最新版本，保留草稿</button>}</p>}</div><footer><button className="bq-button" type="button" onClick={() => setEditing(false)} disabled={saving}>取消</button><button className="bq-button primary" type="submit" disabled={saving} data-testid="bq-engineering-save">{saving ? <LoaderCircle size={15} className="bq-spin" /> : <Save size={15} />}{saving ? '正在保存' : '保存工程资料'}</button></footer></form></section></dialog>}
  </div>
}
