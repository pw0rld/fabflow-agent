'use client'

import { useEffect, useRef, useState } from 'react'
import { ChevronRight, LoaderCircle, Plus, Save, Trash2 } from 'lucide-react'
import BatchCostChart, { type BatchCostTotals } from './batch-cost-chart'
import BatchFactoryReferencePanel from './batch-factory-reference-panel'
import { quotationCount, quotationNumber } from '@/lib/batch-display-format'
import { PUBLIC_QUOTE_API, type QuoteApiBase } from '@/lib/batch-quote-scope'
import type { CostCategory, CostEntry, CostMethod, CostSheetLine, CostSheetPolicy, CostSheetPricing, CostSheetResponse, FactoryReferenceItem, FactoryReferenceRate, ProcessCatalogItem } from '@/lib/batch-cost-sheet-types'
import './batch-cost-sheet-panel.css'

type Props = { batchId: string; runId: string; apiBase?: QuoteApiBase; active: boolean; disabled?: boolean;
  lines: CostSheetLine[]; orderSets?: number; onApplied(batch: unknown): void }
const categories: [CostCategory, string][] = [['material', '材料'], ['purchase', '辅料与外购'], ['process', '分工序加工'], ['setup', '准备费'], ['tooling', '模具'], ['other', '其他费用']]
const categoryName = Object.fromEntries(categories) as Record<CostCategory, string>
const ORDER = '__order__'
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
const cash = (value: string | null | undefined) => value == null ? '未计价' : `¥${quotationNumber(value)}`
const sourceName = (entry: CostEntry) => entry.source_kind === 'legacy_estimate' ? '已保存估算' : entry.source_kind === 'engineering_adopted' ? '已保存工程核价' : '本单录入'
const decimal = (value: unknown) => typeof value === 'string' && value.length <= 32 && /^(?:0|[1-9]\d*)(?:\.\d{1,8})?$/.test(value) && Number(value) <= 1e12
const methods: Record<CostCategory, CostMethod[]> = { material: ['material', 'fixed', 'quantity_rate'], purchase: ['quantity_rate', 'fixed'], process: ['quantity_rate', 'fixed'], setup: ['fixed', 'quantity_rate'], tooling: ['fixed', 'tooling_allocation'], other: ['fixed', 'quantity_rate'] }
const units: Record<string, string> = { piece: '件', set: '套', order: '单', minute: '分钟', hour: '小时', kg: 'kg', m: '米', m2: '平方米', dm2: '平方分米', hole: '孔', bend: '折', point: '焊点 / 点' }
const methodName: Record<CostMethod, string> = { retained: '沿用已保存费用', fixed: '本单固定金额', quantity_rate: '本单工程量 × 费率', material: '净料 + 损耗 − 废料回收', tooling_allocation: '按数量分摊' }
const requiredNumbers: Record<CostMethod, [keyof CostEntry, string][]> = { retained: [], fixed: [['amount', '固定金额']], quantity_rate: [['quantity', '本单工程量'], ['rate', '采用费率']], material: [['net_kg', '本单净料'], ['loss_kg', '本单损耗'], ['unit_price', '材料采用价'], ['scrap_price', '废料回收价']], tooling_allocation: [['tooling_amount', '模具总额'], ['amortization_quantity', '总分摊数量']] }
class SheetFailure extends Error { constructor(message: string, public status: number) { super(message) } }
async function ask<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers); headers.set('X-Workbench-Request', '1')
  const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', redirect: 'error', ...init, headers })
  const data = await response.json().catch(() => { throw new Error('暂未收到有效响应，所填内容保留，可重试。') })
  if (!response.ok) throw new SheetFailure(typeof data?.error === 'string' ? data.error : data?.error?.message || `核价请求未完成（${response.status}）`, response.status)
  return data as T
}
function validate(value: CostSheetResponse): CostSheetResponse {
  if (!value?.sheet || value.sheet.version !== 'cost-sheet-v1' || !Array.isArray(value.sheet.entries) || !Array.isArray(value.sheet.catalog)
      || typeof value.sheet.base_digest !== 'string' || typeof value.source_digest !== 'string' || !Number.isSafeInteger(value.expected_revision)
      || typeof value.can_reprice !== 'boolean') throw new Error('本版核价信息暂不可读，请重新读取。')
  return value
}
function originalPolicy(value: CostSheetResponse): CostSheetPolicy {
  return clone(value.sheet.policy || { base_digest: value.sheet.base_digest, entries: value.sheet.entries,
    customer_terms: value.sheet.customer_terms, ...(value.source_binding ? { source_binding: value.source_binding } : {}) })
}
function makeEntry(category: CostCategory, line: CostSheetLine | null, method = methods[category][0]): CostEntry {
  const entry: CostEntry = { id: 'CS-' + crypto.randomUUID().replaceAll('-', ''), line_id: line?.id || null,
    category, label: categoryName[category], method, source_kind: 'manual_adopted' }
  if (method === 'fixed') entry.amount = ''
  if (method === 'quantity_rate') Object.assign(entry, { quantity: category === 'purchase' && line?.quantity_total != null ? String(line.quantity_total) : '', rate: '', unit: 'piece' })
  if (method === 'material') Object.assign(entry, { net_kg: quotationNumber(line?.estimated_total_net_weight_kg, '').replaceAll(',', ''), loss_kg: '', unit_price: '', scrap_price: '' })
  if (category === 'tooling') entry.tooling_id = ''
  if (method === 'tooling_allocation') Object.assign(entry, { tooling_amount: '', amortization_quantity: '' })
  return entry
}
function EntryEditor({ entry, catalog, disabled, onChange, onRemove }: { entry: CostEntry; catalog: ProcessCatalogItem[]; disabled: boolean; onChange(entry: CostEntry): void; onRemove(): void }) {
  const change = (field: keyof CostEntry, value: string) => onChange({ ...entry, [field]: value })
  const number = (field: keyof CostEntry, label: string, hint?: string) => <label className="bcs-field" key={field}>{label}<input type="text" inputMode={field === 'amortization_quantity' ? 'numeric' : 'decimal'} value={String(entry[field] ?? '')} disabled={disabled} onChange={event => change(field, event.target.value)} maxLength={32} placeholder={['quantity', 'net_kg', 'amortization_quantity'].includes(field) ? '请输入大于 0 的数量' : '请填写；不收费时明确填 0'} data-testid={`bcs-entry-${entry.id}-${field}`} />{hint && <small>{hint}</small>}</label>
  const chooseMethod = (method: CostMethod) => {
    const next = makeEntry(entry.category, null, method)
    if (method === 'quantity_rate' && entry.process_id) next.unit = catalog.find(item => item.id === entry.process_id)?.default_unit || 'piece'
    onChange({ ...next, id: entry.id, line_id: entry.line_id, label: entry.label,
      ...(entry.note ? { note: entry.note } : {}), ...(entry.tooling_id ? { tooling_id: entry.tooling_id } : {}), ...(entry.process_id ? { process_id: entry.process_id } : {}) })
  }
  return <div className="bcs-item" data-testid={`bcs-entry-${entry.id}`}>
    <div className="bcs-item-heading"><strong>{categoryName[entry.category]}明细</strong><button type="button" className="bcs-remove" onClick={onRemove} disabled={disabled} data-testid={`bcs-remove-${entry.id}`}><Trash2 size={12} />移除本项</button></div>
    <div className="bcs-edit-grid">
      {entry.category === 'process' && <label className="bcs-field bcs-wide">工序目录<select value={entry.process_id || ''} disabled={disabled} onChange={event => {
        const found = catalog.find(item => item.id === event.target.value)
        if (found) onChange({ ...entry, process_id: found.id, label: found.label, ...(entry.method === 'quantity_rate' ? { unit: found.default_unit } : {}) })
        else { const next = { ...entry }; delete next.process_id; onChange(next) }
      }} data-testid={`bcs-process-${entry.id}`}><option value="">自定义工序 / 请选择目录</option>{[...new Set(catalog.map(item => item.group))].map(group => <optgroup label={group} key={group}>{catalog.filter(item => item.group === group).map(item => <option value={item.id} key={item.id}>{item.label}</option>)}</optgroup>)}</select><small>目录仅提供工序名称与单位，费率由本单填写。</small></label>}
      <label className="bcs-field">费用名称<input value={entry.label} disabled={disabled} onChange={event => change('label', event.target.value)} maxLength={160} data-testid={`bcs-entry-${entry.id}-label`} /></label>
      <label className="bcs-field">计费方式<select value={entry.method} disabled={disabled} onChange={event => chooseMethod(event.target.value as CostMethod)} data-testid={`bcs-method-${entry.id}`}>{methods[entry.category].map(method => <option value={method} key={method}>{entry.category === 'tooling' && method === 'fixed' ? '模具全额计入本单' : methodName[method]}</option>)}</select></label>
      {entry.method === 'fixed' && number('amount', '本单固定金额（元）')}
      {entry.method === 'quantity_rate' && <>{number('quantity', '本单总工程量 / 数量')}{number('rate', `采用费率（元 / ${units[entry.unit || ''] || '单位'}）`)}<label className="bcs-field">计量单位<select value={entry.unit || 'piece'} onChange={event => change('unit', event.target.value)} disabled={disabled} data-testid={`bcs-entry-${entry.id}-unit`}>{Object.entries(units).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select><small>按本单总量填写，不会再次乘零件数量。</small></label></>}
      {entry.method === 'material' && <>{number('net_kg', '本单净料（kg）', '可沿用本行订单预估净重，并按实际备料核对。')}{number('loss_kg', '本单损耗（kg）')}{number('unit_price', '材料采用价（元/kg）')}{number('scrap_price', '废料回收价（元/kg）')}<p className="bcs-target-note">材料成本按（净料 + 损耗）× 采用价 − 损耗 × 回收价核算。</p></>}
      {entry.category === 'tooling' && <label className="bcs-field">模具编号<input value={entry.tooling_id || ''} disabled={disabled} onChange={event => change('tooling_id', event.target.value)} maxLength={80} placeholder="例如 M-001" data-testid={`bcs-entry-${entry.id}-tooling_id`} /><small>使用字母、数字、- 或 _；同一编号只选全额或分摊一种。</small></label>}
      {entry.method === 'tooling_allocation' && <>{number('tooling_amount', '模具总额（元）')}{number('amortization_quantity', '模具总分摊数量', '按本行订单件数计入；整单模具按订单套数计入。')}</>}
      <label className="bcs-field bcs-wide">采用依据 / 备注<input value={entry.note || ''} disabled={disabled} onChange={event => change('note', event.target.value)} maxLength={1000} placeholder="供应商报价、工时确认或本单采用说明（选填）" data-testid={`bcs-entry-${entry.id}-note`} /></label>
    </div>
  </div>
}

function signature(policy: CostSheetPolicy) {
  return JSON.stringify({ ...policy, entries: [...policy.entries].sort((a, b) => a.id.localeCompare(b.id)), adopt_unpriced_line_ids: [...(policy.adopt_unpriced_line_ids || [])].sort() })
}
export default function BatchCostSheetPanel({ batchId, runId, apiBase = PUBLIC_QUOTE_API, active, disabled = false, lines, orderSets, onApplied }: Props) {
  const valid = /^BATCH-[a-f0-9]{32}$/.test(batchId) && /^RUN-[a-f0-9]{32}$/.test(runId)
  const endpoint = valid ? `${apiBase}/${batchId}/runs/${runId}/cost-sheet` : null
  const [data, setData] = useState<CostSheetResponse | null>(null), [policy, setPolicy] = useState<CostSheetPolicy | null>(null)
  const [loading, setLoading] = useState(false), [saving, setSaving] = useState(false), [error, setError] = useState('')
  const [reload, setReload] = useState(0), [conflict, setConflict] = useState(false)
  const [selected, setSelected] = useState(lines[0]?.id || ORDER)
  const [invalid, setInvalid] = useState<{ entryId: string; field: string; lineId: string | null } | null>(null)
  const initial = useRef<CostSheetPolicy | null>(null), mounted = useRef(true), host = useRef<HTMLDivElement>(null), errorBox = useRef<HTMLDivElement>(null)
  const attempt = useRef<{ signature: string; eventId: string } | null>(null), posting = useRef<AbortController | null>(null)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; posting.current?.abort() } }, [])
  useEffect(() => {
    if (!endpoint || !active || data) return
    const abort = new AbortController(); setLoading(true); setError('')
    void ask<CostSheetResponse>(endpoint, { signal: abort.signal }).then(reply => {
      if (abort.signal.aborted) return
      const next = validate(reply), nextPolicy = originalPolicy(next)
      setData(next); setPolicy(nextPolicy); initial.current = clone(nextPolicy); setConflict(false)
    }).catch(failure => { if (!abort.signal.aborted) setError(failure instanceof Error ? failure.message : '核价资料暂未载入。') })
      .finally(() => { if (!abort.signal.aborted) setLoading(false) })
    return () => abort.abort()
    // The per-RUN component keeps drafts when another result tab is selected.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [endpoint, active, reload])
  useEffect(() => {
    if (!invalid) return
    const target = host.current?.querySelector<HTMLInputElement | HTMLSelectElement>(invalid.entryId === '__pricing__'
      ? `[data-testid="bcs-pricing-${CSS.escape(invalid.field)}"]`
      : `[data-testid="bcs-entry-${CSS.escape(invalid.entryId)}-${CSS.escape(invalid.field)}"]`)
    const details = target?.closest('details') || (!invalid.entryId ? host.current?.querySelector<HTMLDetailsElement>(`details[data-bcs-category="${CSS.escape(invalid.field)}"]`) : null)
    if (details) details.open = true
    if (target) target.focus(); else errorBox.current?.focus()
  }, [invalid, selected])
  const line = selected === ORDER ? null : lines.find(item => item.id === selected) || null
  const lineId = line?.id || null
  const editable = !!data?.can_reprice && !disabled && !saving && !conflict
  const dirty = !!policy && !!initial.current && signature(policy) !== signature(initial.current)
  const selectedEntries = policy?.entries.filter(entry => entry.line_id === lineId) || []
  const selectedOriginal = initial.current?.entries.filter(entry => entry.line_id === lineId) || []
  const shownCategories = selected === ORDER ? categories.filter(([category]) => category === 'tooling' || category === 'other') : categories
  const categoryDirty = (category: CostCategory) => JSON.stringify(selectedEntries.filter(entry => entry.category === category).sort((a, b) => a.id.localeCompare(b.id))) !== JSON.stringify(selectedOriginal.filter(entry => entry.category === category).sort((a, b) => a.id.localeCompare(b.id)))
  const changedLines = new Set<string>()
  const chartTotals: BatchCostTotals = {}
  if (data) for (const key of ['material_cost', 'purchase_cost', 'process_cost', 'setup_cost', 'tooling_cost', 'other_cost', 'total_cost'] as const) {
    if (Object.prototype.hasOwnProperty.call(data.sheet.totals, key)) { const value = data.sheet.totals[key]; chartTotals[key] = typeof value === 'string' ? value : null }
  }
  if (policy && initial.current) {
    const before = new Map(initial.current.entries.map(entry => [entry.id, entry])), after = new Map(policy.entries.map(entry => [entry.id, entry]))
    for (const entry of [...policy.entries, ...initial.current.entries]) if (JSON.stringify(before.get(entry.id)) !== JSON.stringify(after.get(entry.id))) changedLines.add(entry.line_id || ORDER)
    if (JSON.stringify(policy.customer_terms) !== JSON.stringify(initial.current.customer_terms)) changedLines.add(ORDER)
    if (JSON.stringify(policy.pricing) !== JSON.stringify(initial.current.pricing)) changedLines.add(ORDER)
    for (const id of [...(policy.adopt_unpriced_line_ids || []), ...(initial.current.adopt_unpriced_line_ids || [])]) if ((policy.adopt_unpriced_line_ids || []).includes(id) !== (initial.current.adopt_unpriced_line_ids || []).includes(id)) changedLines.add(id)
  }
  function editEntry(entry: CostEntry) { setPolicy(previous => previous && ({ ...previous, entries: previous.entries.map(item => item.id === entry.id ? entry : item) })); setError(''); setInvalid(null) }
  function add(category: CostCategory, replace = false) {
    if (!editable || !policy) return
    const next = makeEntry(category, line)
    setPolicy(previous => previous && ({ ...previous, entries: [...previous.entries.filter(entry => {
      if (entry.line_id !== lineId) return true
      if (replace && entry.category === category) return false
      return !(category === 'process' && entry.category === 'setup' && entry.method === 'retained')
    }), next] }))
    setError(''); setInvalid(null)
  }
  function addFactoryItem(item: FactoryReferenceItem, sampleRate?: Pick<FactoryReferenceRate, 'label' | 'rate' | 'unit'>) {
    if (!editable || !policy || !data?.sheet.factory_reference || (item.scope === 'line' && !line)) return
    const target = item.scope === 'order' ? null : line
    let next = makeEntry(item.category, target, item.method)
    next.label = sampleRate && item.sample_rates?.length ? `${item.label} · ${sampleRate.label}` : item.label
    next.note = `客户核价参考：${data.sheet.factory_reference.source.name}；${sampleRate ? `样品费率 ${sampleRate.rate} 元/${units[sampleRate.unit] || sampleRate.unit}，已明确采用；本单适用范围与计量另行核对。` : '采用费用结构；用量、金额与本单适用范围另行核对。'}`
    if (next.method === 'quantity_rate') Object.assign(next, { quantity: '', rate: sampleRate?.rate || '', unit: sampleRate?.unit || item.unit })
    if (next.method === 'material') Object.assign(next, { net_kg: '', loss_kg: '', unit_price: sampleRate?.rate || '', scrap_price: '' })
    if (sampleRate) {
      const existing = policy.entries.filter(entry => entry.line_id === next.line_id && entry.category === next.category
        && entry.label === next.label && entry.method === next.method && entry.source_kind === 'manual_adopted'
        && entry.note?.startsWith('客户核价参考：')).slice(-1)[0]
      if (existing) next = { ...existing, note: next.note, ...(next.method === 'material'
        ? { unit_price: sampleRate.rate } : { rate: sampleRate.rate, unit: sampleRate.unit }) }
    }
    setPolicy(previous => previous && ({ ...previous, entries: [...previous.entries.filter(entry => {
      if (entry.id === next.id) return false
      if (entry.line_id !== next.line_id) return true
      if (entry.category === item.category && entry.method === 'retained') return false
      return !(item.category === 'process' && entry.category === 'setup' && entry.method === 'retained')
    }), next] }))
    setSelected(next.line_id || ORDER); setError('')
    setInvalid({ entryId: next.id, field: next.method === 'material' ? 'net_kg' : next.method === 'quantity_rate' ? 'quantity' : 'amount', lineId: next.line_id })
  }
  function changePricing(pricing: CostSheetPricing) {
    if (!editable) return
    setPolicy(previous => previous && ({ ...previous, pricing: { ...pricing } })); setError(''); setInvalid(null)
  }
  function restorePricing() {
    if (!editable || !initial.current) return
    const original = initial.current.pricing
    setPolicy(previous => {
      if (!previous) return previous
      const next = { ...previous }
      if (original) next.pricing = { ...original }; else delete next.pricing
      return next
    })
    setError(''); setInvalid(null)
  }
  function restore(category: CostCategory) {
    if (!editable || !initial.current) return
    const saved = clone(initial.current.entries.filter(entry => entry.line_id === lineId && entry.category === category))
    setPolicy(previous => {
      if (!previous) return previous
      const entries = previous.entries.filter(entry => entry.line_id !== lineId || entry.category !== category)
      if (category === 'process' && !entries.some(entry => entry.line_id === lineId && entry.category === 'setup')) entries.push(...clone(initial.current!.entries.filter(entry => entry.line_id === lineId && entry.category === 'setup' && entry.method === 'retained')))
      return { ...previous, entries: [...entries, ...saved] }
    })
    setError(''); setInvalid(null)
  }
  async function refreshKeepingDraft() {
    if (!endpoint || !data || saving) return
    setLoading(true)
    try {
      const next = validate(await ask<CostSheetResponse>(endpoint))
      if (!mounted.current) return
      if (next.source_digest !== data.source_digest || (next.current_run_id && next.current_run_id !== runId)) { setConflict(true); setError('已有新的报价版本。当前草稿保留，请从订单重新打开最新报价后核对修改。'); return }
      setData(next); setConflict(false); setError('已读取本版最新核价条件，所填草稿保留，请核对后保存。')
    } catch (failure) { if (mounted.current) setError(failure instanceof Error ? failure.message : '最新条件暂未读到，草稿仍保留。') }
    finally { if (mounted.current) setLoading(false) }
  }
  async function save() {
    if (!endpoint || !policy || !data || !editable || !dirty) return
    if (policy.pricing) {
      const pricing = policy.pricing
      let problem: [string, string] | undefined
      if (!decimal(pricing.management_rate_percent) || Number(pricing.management_rate_percent) > 1000) problem = ['management', '请填写 0 至 1000 的管理费率。']
      else if (!['cost_plus', 'gross_margin'].includes(pricing.pricing_method)) problem = ['method', '请选择有效的报价方式。']
      else if (!decimal(pricing.rate_percent) || Number(pricing.rate_percent) > 1000 || (pricing.pricing_method === 'gross_margin' && Number(pricing.rate_percent) >= 100)) problem = ['rate', pricing.pricing_method === 'gross_margin' ? '目标毛利率须大于等于 0 且小于 100。' : '请填写 0 至 1000 的成本加成比例。']
      else if (!['cent', 'whole_yuan'].includes(pricing.rounding)) problem = ['rounding', '请选择有效的管理费取整方式。']
      if (problem) { setInvalid({ entryId: '__pricing__', field: problem[0], lineId: null }); setError(problem[1]); return }
    }
    for (const previous of initial.current?.entries || []) {
      if (policy.entries.some(entry => entry.line_id === previous.line_id && entry.category === previous.category)) continue
      if (previous.category === 'setup' && previous.method === 'retained' && policy.entries.some(entry => entry.line_id === previous.line_id && entry.category === 'process' && entry.method !== 'retained')) continue
      setSelected(previous.line_id || ORDER); setInvalid({ entryId: '', field: previous.category, lineId: previous.line_id })
      setError(`${previous.line_id || '整单'} · ${categoryName[previous.category]}：原费用已移除，请录入明确金额（不收费时填0），或恢复已保存费用。`); return
    }
    for (const entry of policy.entries) {
      if (entry.method === 'retained') continue
      let problem: [string, string] | undefined
      if (!entry.label.trim()) problem = ['label', '费用名称']
      for (const [key, label] of requiredNumbers[entry.method] || []) if (!decimal(entry[key])) { problem = [key, label]; break }
      for (const key of ['quantity', 'net_kg', 'amortization_quantity'] as const) if (key in entry && decimal(entry[key]) && !/[1-9]/.test(entry[key]!)) problem = [key, '大于 0 的数量']
      if (entry.method === 'tooling_allocation' && !/^[1-9]\d*$/.test(entry.amortization_quantity || '')) problem = ['amortization_quantity', '大于 0 的整数分摊数量']
      if (entry.method === 'quantity_rate' && !entry.unit?.trim()) problem = ['unit', '计量单位']
      if (entry.category === 'tooling' && !/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(entry.tooling_id || '')) problem = ['tooling_id', '模具编号（字母、数字、- 或 _）']
      if (problem) {
        setSelected(entry.line_id || ORDER); setInvalid({ entryId: entry.id, field: problem[0], lineId: entry.line_id })
        setError(`${entry.line_id || '整单'} · ${entry.label}：请填写有效的${problem[1]}。空白不作为零费用。`); return
      }
    }
    const body = { expected_revision: data.expected_revision, policy }, key = JSON.stringify(body)
    if (attempt.current?.signature !== key) attempt.current = { signature: key, eventId: crypto.randomUUID() }
    const abort = new AbortController(); posting.current = abort; setSaving(true); setError(''); setInvalid(null)
    try {
      const reply = await ask<{ batch: unknown; run_id: string; source_run_id: string; replayed: boolean }>(endpoint, {
        method: 'POST', signal: abort.signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ event_id: attempt.current.eventId, ...body }) })
      if (!mounted.current || abort.signal.aborted) return
      if (!reply.batch || !/^RUN-[a-f0-9]{32}$/.test(reply.run_id) || reply.source_run_id !== runId) throw new Error('保存结果暂未核对完成，请保留当前草稿重试。')
      onApplied(reply.batch)
    } catch (failure) {
      if (!abort.signal.aborted && mounted.current) { setError(failure instanceof Error ? failure.message : '核价保存未完成，草稿已保留。'); setConflict(failure instanceof SheetFailure && failure.status === 409) }
    } finally { if (!abort.signal.aborted && mounted.current) setSaving(false) }
  }
  if (!active) return null
  return <div ref={host} className="bcs" data-testid="bcs-panel">
    <div className="bcs-heading"><div><h2>内部核价</h2><p>选择需要修改的零件，按实际费用补录。保存后另存新报价，原报价保留。</p></div><span className="bcs-tag">内部资料 · 待审核</span></div>
    {loading && <div className="bcs-feedback" role="status"><LoaderCircle size={16} className="bq-spin" />正在读取已保存的核价条件…</div>}
    {error && <div ref={errorBox} tabIndex={-1} className="bcs-error" role="alert" data-testid="bcs-error">{error}{!data && !loading && <button type="button" onClick={() => setReload(value => value + 1)}>重新读取</button>}{data && conflict && <button type="button" disabled={loading || saving} onClick={() => void refreshKeepingDraft()} data-testid="bcs-refresh-version">读取最新条件，保留草稿</button>}</div>}
    {data && policy && <>
      {!data.can_reprice && <div className="bcs-readonly" data-testid="bcs-readonly">此版本在当前入口仅供查看，已保存金额与明细保留。</div>}
      <BatchCostChart totals={chartTotals} unpricedLines={lines.filter(item => !item.priced).length} />
      <div className="bcs-target"><label>选择本次核价对象<select value={selected} onChange={event => { setSelected(event.target.value); setInvalid(null) }} disabled={saving} data-testid="bcs-line-select">{lines.map(item => <option value={item.id} key={item.id}>{item.id} · {item.part_name} · {item.document}</option>)}<option value={ORDER}>整单 · 模具与其他费用</option></select></label>
        <div className="bcs-target-facts"><div><span>{line ? '本行订单用量' : '订单套数'}</span><strong>{quotationCount(line ? line.quantity_total : orderSets)} {line ? '件' : '套'}</strong></div><div><span>{line ? '本行已保存成本' : '已保存整单成本'}</span><strong data-testid="bcs-saved-cost">{cash(line ? line.total_cost : typeof data.sheet.totals.total_cost === 'string' ? data.sheet.totals.total_cost : null)}</strong></div></div>
        <p className="bcs-target-note">{line ? `${line.document} · ${line.id}。以下只修改这个来源零件；同名其他来源保持原条件。` : '整单项目只计一次；零件费用从上方选择具体零件补录。'}{dirty ? ' 草稿尚未核算，不改变当前报价金额。' : ''}</p>
      </div>
      {data.sheet.factory_reference && data.sheet.factory_reference.items.length > 0 && <BatchFactoryReferencePanel reference={data.sheet.factory_reference} pricing={policy.pricing} accounting={data.sheet.accounting_summary}
        editable={editable} lineName={line?.part_name || null} draftChanged={dirty} pricingChanged={JSON.stringify(policy.pricing) !== JSON.stringify(initial.current?.pricing)}
        replacesRetained={item => policy.entries.some(entry => entry.line_id === (item.scope === 'order' ? null : lineId) && entry.category === item.category && entry.method === 'retained')}
        onAdd={addFactoryItem} onPricing={changePricing} onRestorePricing={restorePricing} />}
      <div className="bcs-categories" key={selected}>
        {shownCategories.map(([category, label], index) => {
          const entries = selectedEntries.filter(entry => entry.category === category), retained = entries.filter(entry => entry.method === 'retained'), manual = entries.filter(entry => entry.method !== 'retained')
          const modified = categoryDirty(category), originalEntries = selectedOriginal.filter(entry => entry.category === category)
          const singleAmount = entries.length === 1 ? data.sheet.entry_amounts?.[entries[0].id] ?? (entries[0].method === 'retained' ? entries[0].amount : undefined) : undefined
          return <details className="bcs-category" data-bcs-category={category} key={category} data-testid={`bcs-category-${category}`}>
            <summary><div><span className="bcs-category-index">{String(index + 1).padStart(2, '0')}</span>{label}</div><div className="bcs-category-summary"><small>{modified ? '已修改' : retained.length ? '已保存估算' : entries.length ? '已保存明细' : '未列费用'}</small><strong>{modified ? '待保存核算' : entries.length === 1 ? cash(singleAmount) : entries.length ? `${entries.length} 项费用` : '—'}</strong><ChevronRight size={14} /></div></summary>
            <div className="bcs-category-body">
              {!!retained.length && <><div className="bcs-preserved"><div><strong>沿用本行已保存费用</strong><p>保留原估算或工程核价；需要改为实际明细时，先替换本类费用。</p></div><button type="button" disabled={!editable} onClick={() => add(category, true)} data-testid={`bcs-replace-${category}`}>替换本类费用</button></div><div className="bcs-saved-items">{retained.map(entry => <div className="bcs-saved-row" key={entry.id}><div>{entry.label}<small>{sourceName(entry)}{entry.note ? ` · ${entry.note}` : ''}</small></div><strong>{cash(data.sheet.entry_amounts?.[entry.id] ?? entry.amount)}</strong></div>)}</div></>}
              {!retained.length && <>
                <div className="bcs-replace-note"><span>{modified && originalEntries.length ? category === 'process' ? '本行原综合加工与旧准备费已替换。下方按工序录入，实际准备费可在准备费中另列。' : '下方明细将替换本行原费用，保存时不会与旧费用叠加。' : '工程量、数量和固定金额均按本行整单范围填写。'}</span>{modified && <button type="button" disabled={!editable} onClick={() => restore(category)} data-testid={`bcs-restore-${category}`}>撤销本类修改</button>}</div>
                <div className="bcs-items">{manual.map(entry => <EntryEditor key={entry.id} entry={entry} catalog={data.sheet.catalog} disabled={!editable} onChange={editEntry} onRemove={() => setPolicy(previous => previous && ({ ...previous, entries: previous.entries.filter(item => item.id !== entry.id) }))} />)}</div>
                {!entries.length && <p className="bcs-empty">本类尚未列费用。仅补充实际发生的项目；空白不会自动填为零。</p>}
                <button type="button" className="bcs-add" disabled={!editable} onClick={() => add(category)} data-testid={`bcs-add-${category}`}><Plus size={12} />{category === 'process' ? '添加一道工序' : `添加${label}`}</button>
              </>}
            </div>
          </details>
        })}
      </div>
      {line && !line.priced && <label className="bq-confirm"><input type="checkbox" checked={(policy.adopt_unpriced_line_ids || []).includes(line.id)} disabled={!editable} onChange={event => setPolicy(previous => previous && ({ ...previous, adopt_unpriced_line_ids: event.target.checked ? [...new Set([...(previous.adopt_unpriced_line_ids || []), line.id])] : (previous.adopt_unpriced_line_ids || []).filter(id => id !== line.id) }))} data-testid="bcs-adopt-unpriced" /><span>本行费用补齐后纳入计价。需要明确材料与加工或外购费用，并保留原订单数量。</span></label>}
      <details className="bcs-terms"><summary>客户报价条件（选填）</summary><div className="bcs-edit-grid">{([['delivery', '交期'], ['payment', '付款条件'], ['validity', '报价有效期'], ['tax_shipping', '税费与运输口径']] as const).map(([key, label]) => <label className="bcs-field" key={key}>{label}<input value={policy.customer_terms?.[key] || ''} disabled={!editable} maxLength={1000} onChange={event => setPolicy(previous => previous && ({ ...previous, customer_terms: { delivery: '', payment: '', validity: '', tax_shipping: '', ...previous.customer_terms, [key]: event.target.value } }))} data-testid={`bcs-term-${key}`} /></label>)}</div></details>
      {dirty && <div className="bcs-changes" data-testid="bcs-changes"><strong>尚未保存：</strong>{[...changedLines].map(id => id === ORDER ? '整单条件' : `${id} · ${lines.find(item => item.id === id)?.part_name || '零件'}`).join('；')}。未修改的来源、几何与费用沿用已保存记录。</div>}
      <div className="bcs-footer"><p>费用明细用于内部核价。客户报价单单独生成，金额由报价程序核算。<br />保存前现有报价不变；所有采用价均保持待审核。</p><button type="button" className="bq-button primary" disabled={!editable || !dirty || loading} onClick={() => void save()} data-testid="bcs-save">{saving ? <LoaderCircle size={14} className="bq-spin" /> : <Save size={14} />}{saving ? '正在另存新报价…' : '保存核价并生成新报价'}</button></div>
    </>}
  </div>
}
