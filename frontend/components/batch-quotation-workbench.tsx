'use client'

import { Fragment, useEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, Box, Check, ChevronRight, Eye, ExternalLink, FileArchive, FileText, FolderOpen, LoaderCircle, MessageSquareText, Printer, RefreshCw, Search, Upload } from 'lucide-react'
import BatchCostChart from './batch-cost-chart'
import BatchProcessCostPanel from './batch-process-cost-panel'
import BatchCostSheetPanel from './batch-cost-sheet-panel'
import BatchQuoteAssistant from './batch-quote-assistant'
import BatchEngineeringPanel, { type EngineeringView } from './batch-engineering-panel'
import CustomerManufacturingPanel from './customer-manufacturing-panel'
import BatchPartViewer, { safePartPreviewUrl, safePartThumbnailUrl, type BatchPartEntry, type BatchPartLine, type BatchPartsManifest } from './batch-part-viewer'
import { batchMaterialDisplay } from '@/lib/batch-material-display'
import { estimatedWeight, quotationCount, quotationNumber } from '@/lib/batch-display-format'
import { quoteApiForScope, type QuoteScope } from '@/lib/batch-quote-scope'
import './batch-quotation-workbench.css'

const isBatchId = (value: unknown): value is string => typeof value === 'string' && /^BATCH-[a-f0-9]{32}$/.test(value)
const MARKET_PROFILE = 'foshan-reference-20261106'
type MaterialProfile = 'carbon_steel' | 'spcc' | 'ss304' | 'ss316' | 'al5052'
type PriceSource = 'market_reference' | 'supplier_quote' | 'manual'
type PriceRow = { rate: string | null; tax_included: boolean; source_kind: PriceSource; reference_id: string | null; source_text: string; quote_date: string | null }
type MaterialPricing = { vat_rate_percent: string; confirmed: boolean; rows: Record<MaterialProfile, PriceRow> }
type Settings = { order_sets: number; pricing_method: 'cost_plus' | 'gross_margin'; rate_percent: string; material_profile: MaterialProfile; rate_profile: 'demo-v1' | typeof MARKET_PROFILE; demo_confirmed: boolean; scope_confirmed: boolean; material_pricing?: MaterialPricing; costing_mode?: 'process-v1' }
type PriceReference = { id: string; material_profile: MaterialProfile; grade: string; form: string; surface?: string | null; thickness_mm: string | null; width_mm?: string | null; price_cny_per_kg: string; quote_date: string; source_name: string; source_url: string; tax_included: boolean | null; delivery_basis: string | null }
type PricingCatalog = { version: string; researched_on: string; profiles: { id: MaterialProfile; name: string; density_kg_m3: string; reference_id: string | null }[]; references: PriceReference[]; notes?: string[] }
type SourceFile = { id: string; name: string; relative_path: string; kind: string; size: number; sha256: string; status: string; error?: string | null; solid_count?: number; duplicate_of?: string | null }
type Line = BatchPartLine & { id: string; document: string; file_id: string; part_name: string; mode: string; quantity_per_set: number | null; quantity_total: number | null; estimated_net_weight_kg: string | null; estimated_total_net_weight_kg: string | null; unit_price?: string | null; total_price: string | null; priced: boolean; material_price_snapshot?: Record<string, unknown> | null }
type DisplayGroup = Line & { member_ids: string[]; member_count: number; source_documents: string[]; aggregated: boolean }
type BomFilter = 'all' | 'make' | 'buy' | 'unpriced'
type QuoteResult = { lines: Line[]; display_groups?: DisplayGroup[]; run_id?: string; settings?: Settings; cost_sheet?: unknown; price_basis_label?: string; totals: { material_cost: string | null; process_cost: string | null; purchase_cost: string | null; setup_cost: string | null; total_cost: string | null; total_price: string | null; priced_lines: number; unpriced_lines: number }; weight_summary: { estimated_set_net_weight_kg: string | null; estimated_total_net_weight_kg: string | null; known_total_net_weight_kg?: string | null; missing_estimated_lines: number; low_confidence_lines: number }; assumptions?: unknown[]; limitations?: string[]; simulated: boolean; status: string; links?: { quote_url?: string; bom_url?: string; summary_bom_url?: string; print_url?: string; cost_url?: string; json_url?: string; summary_bom_preview_url?: string; bom_preview_url?: string; cost_preview_url?: string; record_preview_url?: string; internal_cost_url?: string; internal_cost_csv_url?: string } }
type Batch = { id: string; name: string; status: string; revision: number; created_at: string; updated_at: string; files: SourceFile[]; settings: Settings | null; progress: { total: number; done: number; current: string | null; message: string }; result: QuoteResult | null; runs?: unknown[] }
type RecentBatch = Pick<Batch, 'id' | 'name' | 'status' | 'created_at' | 'updated_at'>
const running = (status?: string) => status === 'queued' || status === 'running'
const statuses: Record<string, string> = { uploaded: '资料已上传', queued: '等待后台处理', running: '正在估重和报价', completed: '报价草稿已生成', completed_with_issues: '部分资料需核对', interrupted: '本轮处理已中断', failed: '本轮尚未完成' }
const fileStatuses: Record<string, string> = { waiting: '等待处理', queued: '等待处理', processing: '正在读取', running: '正在读取', ready: '读取完成', failed: '未完成', reference: '参考附件', duplicate: '重复文件', unsupported: '暂未处理', ignored: '暂未处理' }
const materials: Record<MaterialProfile, string> = { carbon_steel: '碳钢 · Q235 类假设', spcc: '冷轧钢 · SPCC 类假设', ss304: '不锈钢 304 类假设', ss316: '不锈钢 316 类假设', al5052: '铝合金 5052 假设' }
const materialKeys = Object.keys(materials) as MaterialProfile[]
const priceSourceLabels: Record<PriceSource, string> = { market_reference: '网上行情参考', supplier_quote: '供应商录入价', manual: '手工采用价' }
const money = (value: string | null | undefined) => value == null ? '未计价' : `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
const weight = estimatedWeight
const count = (value: number | null | undefined) => quotationCount(value, '待核对')
const noteText = (value: string | string[] | undefined) => Array.isArray(value) ? value.join('；') : value || ''
const dateLabel = (value: string) => { const date = new Date(value); return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) }
const sourceLink = (value: unknown) => typeof value === 'string' && /^https?:\/\//i.test(value) ? value : undefined
const referenceSpec = (reference: PriceReference) => [reference.grade, reference.surface, reference.form, reference.thickness_mm ? `${quotationNumber(reference.thickness_mm)} mm` : '', reference.width_mm ? `宽 ${quotationNumber(reference.width_mm)} mm` : ''].filter(Boolean).join(' · ')
function defaultPriceRows(catalog: PricingCatalog | null): Record<MaterialProfile, PriceRow> {
  return Object.fromEntries(materialKeys.map(key => {
    const reference = catalog?.references.find(row => row.material_profile === key)
    return [key, { rate: reference?.price_cny_per_kg ?? null, tax_included: reference?.tax_included ?? true, source_kind: reference ? 'market_reference' : 'manual', reference_id: reference?.id ?? null, source_text: reference?.source_name || '', quote_date: reference?.quote_date || null }]
  })) as Record<MaterialProfile, PriceRow>
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  headers.set('X-Workbench-Request', '1')
  const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...init, headers })
  let data: unknown
  try { data = await response.json() } catch { throw new Error(response.status === 413 ? '资料包超过服务器允许的大小，请缩小压缩包后重试。' : '暂未收到有效结果，请稍后重试。') }
  if (!response.ok) {
    const failure = data as { error?: { message?: string } | string; message?: string }
    throw new Error(typeof failure.error === 'string' ? failure.error : failure.error?.message || failure.message || `请求未完成（${response.status}）`)
  }
  return data as T
}
function assumptionText(item: unknown): string {
  if (typeof item === 'string') return item
  if (!item || typeof item !== 'object') return ''
  const entry = item as Record<string, unknown>
  const clean = (value: unknown) => typeof value === 'string' || typeof value === 'number' ? String(value) : ''
  return [clean(entry.label || entry.name), clean(entry.value), clean(entry.scope || entry.basis || entry.description)].filter(Boolean).join(' · ')
}

function MaterialPriceEditor({ catalog, rows, vat, disabled, processFirst, onVat, onEdit, onSource }: {
  catalog: PricingCatalog; rows: Record<MaterialProfile, PriceRow>; vat: string; disabled: boolean; processFirst: boolean;
  onVat(value: string): void; onEdit(key: MaterialProfile, patch: Partial<PriceRow>): void; onSource(key: MaterialProfile, source: PriceSource): void;
}) {
  return <details className="bq-material-price-details" data-testid="bq-material-prices" open>
    <summary>查看与调整本单材料价<span>行情采样日期保留 · 支持供应商报价覆盖</span></summary>
    <p className="bq-note">下面是同类材料估价基准，板厚、卷料/平板及牌号差异尚未自动匹配。价格与工厂采购成交价分开记录；没有价格可留空，实际涉及的项目会保留为未计价。</p>
    <div className="bq-material-price-table"><table><thead><tr><th>材料与参考规格</th><th>本单采用原价<br />元 / kg</th><th>采用税口径</th><th>价格来源</th></tr></thead><tbody>{materialKeys.map(key => {
      const row = rows[key], reference = catalog.references.find(item => item.material_profile === key)
      const market = row.source_kind === 'market_reference'
      return <tr key={key}>
        <td><strong>{materials[key]}</strong>{reference ? <><small>{referenceSpec(reference)}</small><small>参考原价 ¥{quotationNumber(reference.price_cny_per_kg)}/kg · {reference.quote_date}</small><small>{reference.delivery_basis || '来源未说明交货、运费条件'}</small></> : <small>本次没有可核实的行情数值，可填供应商报价或留空。</small>}</td>
        <td><input type="number" min="0" step="any" inputMode="decimal" aria-label={`${materials[key]}本单采用原价`} data-testid={`bq-price-${key}`} disabled={disabled} value={row.rate ?? ''} placeholder="未提供" onChange={event => onEdit(key, { rate: event.target.value || null, ...(market ? { source_kind: 'manual', source_text: '', quote_date: null } : {}) })} />{!market && reference && <small>已改用本单录入价</small>}</td>
        <td><select aria-label={`${materials[key]}采用税口径`} data-testid={`bq-price-tax-${key}`} disabled={disabled || (market && reference?.tax_included === true)} value={row.tax_included ? 'included' : 'excluded'} onChange={event => onEdit(key, { tax_included: event.target.value === 'included' })}><option value="included">按含税价采用</option><option value="excluded">按未税价采用</option></select><small>{market && reference?.tax_included === true ? '原来源注明含税' : market ? `原文未说明；本单暂按${row.tax_included ? '含税' : '未税'}处理，可核实后改` : '按本单录入的价格条件'}</small></td>
        <td><select aria-label={`${materials[key]}价格来源类型`} data-testid={`bq-price-source-${key}`} disabled={disabled} value={row.source_kind} onChange={event => onSource(key, event.target.value as PriceSource)}>{reference && <option value="market_reference">网上行情参考</option>}<option value="supplier_quote">供应商录入价</option><option value="manual">手工采用价</option></select>{market && reference ? <><a href={sourceLink(reference.source_url)} target="_blank" rel="noopener noreferrer">{reference.source_name}<ExternalLink size={10} /></a><small>{reference.quote_date} · 原口径与报价时点以来源为准</small></> : <><input aria-label={`${materials[key]}价格来源说明`} data-testid={`bq-price-source-text-${key}`} disabled={disabled} value={row.source_text} onChange={event => onEdit(key, { source_text: event.target.value })} placeholder={row.source_kind === 'supplier_quote' ? '供应商 / 报价单号' : '本单手工估价说明'} maxLength={2000} /><label className="bq-price-date">报价日期<input type="date" aria-label={`${materials[key]}报价日期`} disabled={disabled} value={row.quote_date || ''} onChange={event => onEdit(key, { quote_date: event.target.value || null })} /></label></>}</td>
      </tr>
    })}</tbody></table></div>
    <div className="bq-vat-assumption"><label>本次估价税率假设（%）<input type="number" min="0" max="100" step="any" inputMode="decimal" data-testid="bq-vat-assumption" value={vat} disabled={disabled} onChange={event => onVat(event.target.value)} /></label><p>含税采用价由后台按此假设换算为未税材料价。预填 13% 仅为本次估价假设，来源没有确认的税率和税口径仍保留为未知；请按实际报价条件修改。</p></div>
    <p className="bq-note">{processFirst ? '按切割、穿孔及折弯候选工程量分项核价；平板毛坯按二维轮廓包络估算，未排版。材质与重量用于复核，工序费率仍为演示采用值。' : '原料利用率仍暂按 85%；综合加工、准备费与外购标准件仍使用演示费率。本单整体保持参考报价草稿，待工程与商务审核。'}</p>
  </details>
}

function MaterialPriceSnapshot({ value }: { value?: Record<string, unknown> | null }) {
  if (!value) return null
  const scalar = (item: unknown) => typeof item === 'string' || typeof item === 'number' ? String(item) : ''
  const ref = value.reference && typeof value.reference === 'object' ? value.reference as Record<string, unknown> : null
  const adopted = scalar(value.adopted_rate_cny_per_kg), net = scalar(value.normalized_net_rate)
  const kind = scalar(value.source_kind) as PriceSource
  return <div className="bq-price-snapshot" data-testid="bq-price-snapshot">
    <strong>本次保存的材料价依据</strong>
    <p>{priceSourceLabels[kind] || '采用价来源'} · {scalar(value.source_text) || '价格来源待补'}{scalar(value.quote_date) ? ` · ${scalar(value.quote_date)}` : ''}</p>
    <p>采用原价：{adopted ? `¥${quotationNumber(adopted)}/kg` : '未提供'}{typeof value.adopted_tax_included === 'boolean' ? `（${value.adopted_tax_included ? '按含税价采用' : '按未税价采用'}）` : ''}；本单未税价：{net ? `约 ¥${quotationNumber(net)}/kg` : '未计价'}。</p>
    {scalar(value.adopted_vat_rate_percent) && <p>采用税率假设 {quotationNumber(scalar(value.adopted_vat_rate_percent))}%；原来源税率{value.reference_vat_rate_percent == null ? '未说明' : ` ${quotationNumber(scalar(value.reference_vat_rate_percent))}%`}。</p>}
    {ref && <p>原参考规格：{[ref.grade, ref.surface, ref.form, ref.thickness_mm ? `${quotationNumber(scalar(ref.thickness_mm))} mm` : ''].map(scalar).filter(Boolean).join(' · ')}。同类材料基准尚未逐件核对规格。</p>}
    {kind === 'supplier_quote' && value.supplier_price_verified !== true && <p>供应商价格为本单录入，尚未核验。</p>}
    {Array.isArray(value.notes) && value.notes.filter((item): item is string => typeof item === 'string').map((item, index) => <p key={index}>{item}</p>)}
    {sourceLink(value.source_url) && <a href={sourceLink(value.source_url)} target="_blank" rel="noopener noreferrer">{kind === 'market_reference' ? '查看行情来源' : '查看原行情参考'}<ExternalLink size={10} /></a>}
  </div>
}

function PartThumbnail({ line, entry, loading, onOpen }: { line: Line; entry?: BatchPartEntry; loading: boolean; onOpen(url: string, entry: BatchPartEntry): void }) {
  const [imageFailed, setImageFailed] = useState(false)
  const previewUrl = safePartPreviewUrl(entry?.geometry_url), thumbnail = safePartThumbnailUrl(entry?.thumbnail_url, previewUrl)
  useEffect(() => setImageFailed(false), [thumbnail])
  if (entry?.status === 'ready' && previewUrl) return <button type="button" className="bq-part-thumbnail" data-testid={`bq-part-open-${line.id}`} aria-label={`查看 ${line.part_name} 的单件 3D 和总装位置`} title="查看原生单件 3D / 总装定位" onClick={event => { event.currentTarget.focus(); onOpen(previewUrl, entry) }}>
    {thumbnail && !imageFailed ? <img src={thumbnail} alt={`${line.part_name} · 代表实体缩略图`} loading="lazy" decoding="async" data-testid={`bq-part-thumb-${line.id}`} onError={() => setImageFailed(true)} /> : <Box size={23} />}<span>查看 3D</span>
  </button>
  return <div className="bq-part-thumbnail unavailable" data-testid={`bq-part-unavailable-${line.id}`} title={loading ? '正在读取预览目录' : entry?.reason || '本次报价未保留可用的原生零件几何。'} aria-label={loading ? '零件预览正在载入' : `${line.part_name} 的原生预览暂不可用`}><Box size={21} /><span>{loading ? '载入中' : '暂无预览'}</span></div>
}

function QuoteLineIdentity({ line, previewLine, entry, loading, caption, onOpen }: {
  line: Line; previewLine: Line; entry?: BatchPartEntry; loading: boolean; caption?: string;
  onOpen(url: string, entry: BatchPartEntry): void;
}) {
  const material = batchMaterialDisplay(line)
  return <div className="bq-line-identity"><PartThumbnail line={previewLine} entry={entry} loading={loading} onOpen={onOpen} /><div className="bq-line-copy">
    <div className="bq-line-title"><strong>{line.part_name}</strong><span className={`bq-kind-tag ${line.mode}`}>{line.mode === 'buy' ? '外购件' : line.mode === 'make' ? '自制件' : '待核对'}</span>{!line.priced && <span className="bq-unpriced-tag">未计价</span>}</div>
    <div className="bq-line-material" data-testid={`bq-material-display-${line.id}`}><span>{material.adopted_text} · {material.adopted_label}</span><span className="bq-price-kind" title={`价格来源：${material.price_text}`}>{material.price_text}</span></div>
    <details className="bq-line-notes"><summary>查看依据{caption ? <span className="bq-line-source"> · {caption}</span> : null}</summary><dl className="bq-material-facts">{material.cad_text && <div><dt>CAD材料</dt><dd>{material.cad_text}</dd></div>}<div><dt>{material.adopted_label}</dt><dd>{material.adopted_text}</dd></div><div><dt>价格来源</dt><dd>{material.price_text}</dd></div></dl><p>{material.note}</p><p>{line.weight_basis || '估重依据尚未提供。'}</p>{line.confidence === 'low' && <p>本行采用粗略估算，几何条件需进一步核对。</p>}{noteText(line.notes) && <p>{noteText(line.notes)}</p>}<MaterialPriceSnapshot value={line.material_price_snapshot} /></details>
  </div></div>
}

function SourcePreviewButton({ line, entry, loading, onOpen }: {
  line: Line; entry?: BatchPartEntry; loading: boolean; onOpen(url: string, entry: BatchPartEntry): void;
}) {
  const previewUrl = safePartPreviewUrl(entry?.geometry_url)
  if (entry?.status !== 'ready' || !previewUrl) return <span className="bq-source-unavailable">{loading ? '预览载入中' : '暂无预览'}</span>
  return <button type="button" className="bq-source-open" data-testid={`bq-source-open-${line.id}`} aria-label={`查看 ${line.document} 中 ${line.part_name} 的原生 3D`} onClick={() => onOpen(previewUrl, entry)}><Box size={12} />查看 3D</button>
}

function quotedDisplayGroups(result: QuoteResult | null | undefined): DisplayGroup[] {
  if (!result) return []
  const originalIds = new Set(result.lines.map(line => line.id)), seen = new Set<string>()
  const candidates = result.display_groups
  // An incomplete grouping must never hide a saved quotation line. Legacy
  // responses fall back to one row per source; grouping is only done server-side.
  if (Array.isArray(candidates) && candidates.length > 0 && candidates.every(group => {
    if (!group || !Array.isArray(group.member_ids) || !group.member_ids.length || group.id !== group.member_ids[0]
      || group.member_count !== group.member_ids.length || !Array.isArray(group.source_documents)
      || group.aggregated !== (group.member_count > 1)) return false
    for (const id of group.member_ids) {
      if (!originalIds.has(id) || seen.has(id)) return false
      seen.add(id)
    }
    return true
  }) && seen.size === originalIds.size) return candidates
  return result.lines.map(line => ({ ...line, member_ids: [line.id], member_count: 1, source_documents: [line.document], aggregated: false }))
}

type WorkbenchProps = { embedded?: boolean; forcedScope?: QuoteScope; initialBatchId?: string | null;
  onNewOrder?(): void; onBatchSelected?(id: string, scope: QuoteScope): void }
export default function BatchQuotationWorkbench({ embedded = false, forcedScope, initialBatchId, onNewOrder, onBatchSelected }: WorkbenchProps = {}) {
  const Container = embedded ? 'div' : 'main'
  const navigation = useRef({ onNewOrder, onBatchSelected }); navigation.current = { onNewOrder, onBatchSelected }
  const [scope, setScope] = useState<QuoteScope | null>(null)
  const privateCase = scope === 'poland', customerAdmin = scope === 'customer', API = quoteApiForScope(scope || 'public')
  const [batch, setBatch] = useState<Batch | null>(null)
  const [recent, setRecent] = useState<RecentBatch[]>([])
  const [busy, setBusy] = useState(''), [loading, setLoading] = useState(true)
  const [error, setError] = useState(''), [pollError, setPollError] = useState(''), [dragging, setDragging] = useState(false)
  const [sets, setSets] = useState(''), [rate, setRate] = useState('')
  const [method, setMethod] = useState<Settings['pricing_method']>('cost_plus')
  const [material, setMaterial] = useState<Settings['material_profile']>('carbon_steel')
  const [priceProfile, setPriceProfile] = useState<Settings['rate_profile']>(MARKET_PROFILE)
  const [catalog, setCatalog] = useState<PricingCatalog | null>(null), [catalogError, setCatalogError] = useState('')
  const [vatAssumption, setVatAssumption] = useState('13')
  const [priceRows, setPriceRows] = useState<Record<MaterialProfile, PriceRow>>(() => defaultPriceRows(null))
  const [editingQuote, setEditingQuote] = useState(false)
  const [resultView, setResultView] = useState<EngineeringView | 'cost' | 'costsheet' | 'manufacturing'>('manufacturing')
  const [bomView, setBomView] = useState<'grouped' | 'sources'>('grouped')
  const [search, setSearch] = useState(''), [bomFilter, setBomFilter] = useState<BomFilter>('all')
  const [parts, setParts] = useState<{ key: string | null; lines: BatchPartEntry[]; loading: boolean; error: string }>({ key: null, lines: [], loading: false, error: '' })
  const [partsReload, setPartsReload] = useState(0)
  const [partSelection, setPartSelection] = useState<{ url: string; entry: BatchPartEntry; line: Line; orderSets?: number } | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [assistantOpen, setAssistantOpen] = useState(false)
  const assistantRequested = useRef(false), assistantOpened = useRef(false)
  const input = useRef<HTMLInputElement>(null), epoch = useRef(0), liveId = useRef<string | null>(null)
  const tableAnchor = useRef<HTMLDivElement>(null)
  const catalogRef = useRef<PricingCatalog | null>(null), restoredPricing = useRef(false)
  const partsCache = useRef(new Map<string, BatchPartEntry[]>())
  const uploading = useRef(false), mounted = useRef(true)
  const startKey = useRef<{ signature: string; id: string } | null>(null), retryKey = useRef<{ signature: string; id: string } | null>(null)
  const result = batch?.result, active = running(batch?.status)
  const hasCostSheet = !!result?.cost_sheet
  const processFirst = !batch?.settings || batch.settings.costing_mode === 'process-v1'
  const partsKey = batch && result?.run_id && /^RUN-[a-f0-9]{32}$/.test(result.run_id) ? `${API}/${batch.id}/runs/${result.run_id}/parts` : null
  const partEntries = new Map((parts.key === partsKey ? parts.lines : []).map(entry => [entry.line_id, entry]))
  const resultUsesMarket = result?.settings?.rate_profile === MARKET_PROFILE
  const settingsVisible = !privateCase && !!batch && (batch.status === 'uploaded' || editingQuote)
  const failedFiles = batch?.files.filter(file => file.status === 'failed') || []
  const stepFiles = batch?.files.filter(file => file.kind === 'step' && !file.duplicate_of && file.status !== 'duplicate') || []
  const stage = !batch ? 0 : settingsVisible ? 1 : 2
  useEffect(() => { setBomView('grouped'); setSearch(''); setBomFilter('all'); setResultView(privateCase ? 'cost' : 'manufacturing'); setAssistantOpen(false) }, [batch?.id, privateCase])
  useEffect(() => {
    if (customerAdmin || processFirst || !batch?.result?.run_id || !assistantRequested.current || assistantOpened.current) return
    assistantOpened.current = true
    if (batch.result.cost_sheet) setResultView('costsheet'); else setAssistantOpen(true)
  }, [batch?.id, batch?.result?.run_id, customerAdmin, processFirst])
  useEffect(() => { if (hasCostSheet) setAssistantOpen(false) }, [result?.run_id, hasCostSheet])

  function take(next: Batch, form = false) {
    if (!isBatchId(next.id)) throw new Error('订单标识无效，请重新打开订单。')
    liveId.current = next.id; setBatch(next)
    if (form) {
      setEditingQuote(false)
      setSets(next.settings?.order_sets == null ? '' : String(next.settings.order_sets)); setMethod(next.settings?.pricing_method || 'cost_plus'); setRate(next.settings?.rate_percent || ''); setMaterial(next.settings?.material_profile || 'carbon_steel')
      setPriceProfile(next.settings?.rate_profile || MARKET_PROFILE)
      restoredPricing.current = !!next.settings?.material_pricing
      setVatAssumption(next.settings?.material_pricing?.vat_rate_percent || '13')
      setPriceRows(next.settings?.material_pricing?.rows || defaultPriceRows(catalogRef.current)); setConfirmed(false)
    }
    if (embedded && scope) navigation.current.onBatchSelected?.(next.id, scope)
    else { const url = new URL(window.location.href); url.searchParams.set('id', next.id); window.history.replaceState({}, '', url) }
  }
  async function refreshList() {
    if (privateCase || !scope) return
    try { const data = await request<{ batches: RecentBatch[] }>(API); if (mounted.current) setRecent((data.batches || []).filter(row => isBatchId(row.id)).slice(0, 12)) } catch { /* The active order remains usable if its list is temporarily unavailable. */ }
  }
  async function loadPricing() {
    if (privateCase || !scope) return
    setCatalogError('')
    try {
      const next = await request<PricingCatalog>(`${API}/pricing`)
      if (next.version !== MARKET_PROFILE || !Array.isArray(next.references) || !Array.isArray(next.profiles)) throw new Error('材料参考价版本暂不可用。')
      if (mounted.current) { catalogRef.current = next; setCatalog(next); if (!restoredPricing.current) setPriceRows(defaultPriceRows(next)) }
    } catch (failure) { if (mounted.current) setCatalogError(failure instanceof Error ? failure.message : '材料行情暂未载入，请重试。') }
  }
  async function openBatch(id: string) {
    if (!isBatchId(id) || busy) return
    const current = ++epoch.current
    setLoading(true); setError(''); setPollError('')
    try { const next = await request<Batch>(`${API}/${id}`); if (mounted.current && current === epoch.current) take(next, true) }
    catch (failure) { if (mounted.current && current === epoch.current) setError(failure instanceof Error ? failure.message : '订单暂时无法打开。') }
    finally { if (mounted.current && current === epoch.current) setLoading(false) }
  }
  useEffect(() => {
    mounted.current = true
    const params = new URL(window.location.href).searchParams
    assistantRequested.current = params.get('assistant') === '1'
    setScope(forcedScope || (params.get('scope') === 'customer' ? 'customer' : params.get('scope') === 'poland' ? 'poland' : 'public'))
    return () => { mounted.current = false; ++epoch.current }
  }, [forcedScope])
  useEffect(() => {
    if (!scope) return
    const id = initialBatchId !== undefined ? initialBatchId : new URL(window.location.href).searchParams.get('id')
    if (id) {
      if (isBatchId(id)) { if (liveId.current !== id) void openBatch(id) }
      else { setLoading(false); setError('订单链接无效，请从已保存订单重新打开。') }
    } else if (privateCase) {
      const abort = new AbortController()
      void request<{ scope: string; available: boolean; entry_batch_id: string | null }>(`${API}/case`, { signal: abort.signal, redirect: 'error' }).then(entry => {
        if (abort.signal.aborted || !mounted.current) return
        if (entry.scope !== 'poland' || !entry.available || !isBatchId(entry.entry_batch_id)) throw new Error('波兰订单暂未就绪，请稍后重新打开。')
        return openBatch(entry.entry_batch_id)
      }).catch(failure => { if (!abort.signal.aborted && mounted.current) { setLoading(false); setError(failure instanceof Error ? failure.message : '波兰订单暂未载入。') } })
      return () => abort.abort()
    } else { setLoading(false); if (initialBatchId === null) { liveId.current = null; setBatch(null); setPartSelection(null) } }
    if (!privateCase) { void refreshList(); void loadPricing() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, initialBatchId])
  useEffect(() => {
    setPartSelection(null)
    if (!partsKey) { setParts({ key: null, lines: [], loading: false, error: '' }); return }
    const cached = partsCache.current.get(partsKey)
    if (cached) { setParts({ key: partsKey, lines: cached, loading: false, error: '' }); return }
    const abort = new AbortController(), key = partsKey
    setParts({ key, lines: [], loading: true, error: '' })
    void request<BatchPartsManifest>(key, { signal: abort.signal, redirect: 'error' }).then(next => {
      if (abort.signal.aborted) return
      if (`${API}/${next.batch_id}/runs/${next.run_id}/parts` !== key || !Array.isArray(next.lines)) throw new Error('零件预览目录与本次报价不一致。')
      const entries = next.lines.filter(entry => entry && typeof entry.line_id === 'string' && /^L[0-9]{4,8}$/.test(entry.line_id)).map(entry => {
        const expected = `${key}/${entry.line_id}`
        return entry.status === 'ready' && safePartPreviewUrl(entry.geometry_url) === expected ? entry : { ...entry, status: 'unavailable' as const, geometry_url: null, thumbnail_url: null }
      })
      partsCache.current.set(key, entries); setParts({ key, lines: entries, loading: false, error: '' })
    }).catch(failure => { if (!abort.signal.aborted) setParts({ key, lines: [], loading: false, error: failure instanceof Error ? failure.message : '原生零件预览目录暂不可用。' }) })
    return () => abort.abort()
  }, [partsKey, partsReload])
  useEffect(() => {
    if (!batch || !running(batch.status)) return
    const id = batch.id; let cancelled = false; let timer: ReturnType<typeof setTimeout> | undefined
    const poll = async () => {
      try {
        const next = await request<Batch>(`${API}/${id}`)
        if (cancelled || liveId.current !== id) return
        take(next); setPollError('')
        if (!running(next.status)) { void refreshList(); return }
      } catch { if (!cancelled && liveId.current === id) setPollError('暂未取到最新进度。后台任务会继续，正在重新连接。') }
      if (!cancelled) timer = setTimeout(poll, 2000)
    }
    timer = setTimeout(poll, 2000)
    return () => { cancelled = true; if (timer) clearTimeout(timer) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batch?.id, batch?.status])
  function editPrice(key: MaterialProfile, patch: Partial<PriceRow>) {
    setPriceRows(previous => ({ ...previous, [key]: { ...previous[key], ...patch } })); setConfirmed(false)
  }
  function selectPriceSource(key: MaterialProfile, source: PriceSource) {
    const reference = catalog?.references.find(row => row.material_profile === key)
    if (source === 'market_reference' && reference) editPrice(key, { rate: reference.price_cny_per_kg, tax_included: reference.tax_included ?? true, source_kind: source, reference_id: reference.id, source_text: reference.source_name, quote_date: reference.quote_date })
    else editPrice(key, { source_kind: source, source_text: '', quote_date: null })
  }
  async function upload(file?: File) {
    if (privateCase || customerAdmin || !scope) return
    if (!file || uploading.current || busy) return
    const archive = /\.(zip|rar)$/i.test(file.name), step = /\.(step|stp)$/i.test(file.name)
    if (!archive && !step) { setError('请选择 ZIP / RAR 订单资料包，或 STEP / STP 文件。'); return }
    if (!file.size || file.size > (archive ? 50 : 20) * 1024 * 1024) { setError(archive ? '请选择非空、大小不超过 50 MB 的 ZIP / RAR 文件。' : '请选择非空、大小不超过 20 MB 的 STEP / STP 文件。'); return }
    const current = ++epoch.current
    uploading.current = true; setBusy('正在上传并整理文件…'); setError(''); setPollError(''); setLoading(false)
    try {
      const next = await request<Batch>(`${API}?name=${encodeURIComponent(file.name)}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Event-Id': crypto.randomUUID() }, body: file })
      if (mounted.current && current === epoch.current) { take(next, true); startKey.current = null; retryKey.current = null; void refreshList() }
    } catch (failure) { if (mounted.current && current === epoch.current) setError(failure instanceof Error ? failure.message : '上传尚未完成，请重试。') }
    finally { uploading.current = false; if (mounted.current && current === epoch.current) setBusy('') }
  }
  async function start() {
    if (privateCase || !scope || !batch || busy || active) return
    if (!/^\d+$/.test(sets) || !Number.isSafeInteger(Number(sets)) || Number(sets) < 1) { setError('请填写订单套数，使用大于零的整数。'); return }
    if (!/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(rate) || !Number.isFinite(Number(rate))) { setError('请填写有效的定价比例，例如 20 表示 20%。'); return }
    if (method === 'gross_margin' && Number(rate) >= 100) { setError('目标毛利率须小于 100%。'); return }
    if (!confirmed) { setError('请先确认本次成套范围、采用价格及估算假设。'); return }
    if (!stepFiles.length) { setError('资料中没有可处理的 STEP / STP 文件，请上传包含 CAD 的资料包。'); return }
    if (priceProfile === MARKET_PROFILE) {
      if (!catalog || catalogError) { setError('材料参考价尚未载入，请重试加载后再开始。'); return }
      if (!/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(vatAssumption) || !Number.isFinite(Number(vatAssumption)) || Number(vatAssumption) > 100) { setError('请填写 0～100 的本次估价税率假设。'); return }
      for (const key of materialKeys) {
        const row = priceRows[key]
        if (row.rate == null || row.rate === '') continue
        if (!/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(row.rate) || !Number.isFinite(Number(row.rate)) || Number(row.rate) <= 0) { setError(`${materials[key]} 的采用单价无效，请填写大于零的元/kg 数值或留空。`); return }
        if (row.source_kind !== 'market_reference' && !row.source_text.trim()) { setError(`请补充${materials[key]}的价格来源，例如供应商、采购单号或手工估价说明。`); return }
      }
    }
    const settings: Settings = { order_sets: Number(sets), pricing_method: method, rate_percent: rate, material_profile: material, rate_profile: priceProfile, demo_confirmed: true, scope_confirmed: true,
      ...(!batch.settings || batch.settings.costing_mode === 'process-v1' ? { costing_mode: 'process-v1' as const } : {}),
      ...(priceProfile === MARKET_PROFILE ? { material_pricing: { vat_rate_percent: vatAssumption, confirmed: true, rows: priceRows } } : {}) }
    const signature = JSON.stringify([batch.id, batch.revision, settings])
    if (startKey.current?.signature !== signature) startKey.current = { signature, id: crypto.randomUUID() }
    setBusy('正在提交后台报价…'); setError(''); const id = batch.id
    try { const next = await request<Batch>(`${API}/${id}/start`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: batch.revision, request_id: startKey.current.id, settings }) }); if (mounted.current && liveId.current === id) { take(next); setEditingQuote(false); void refreshList() } }
    catch (failure) { if (mounted.current && liveId.current === id) setError(failure instanceof Error ? failure.message : '报价请求尚未完成，请重试。') }
    finally { if (mounted.current) setBusy('') }
  }
  async function retry(ids: string[]) {
    if (privateCase || !scope || !batch || !ids.length || busy || active) return
    const signature = JSON.stringify([batch.id, batch.revision, ids])
    if (retryKey.current?.signature !== signature) retryKey.current = { signature, id: crypto.randomUUID() }
    setBusy('正在重试未完成文件…'); setError(''); const id = batch.id
    try { const next = await request<Batch>(`${API}/${id}/retry`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: batch.revision, request_id: retryKey.current.id, file_ids: ids }) }); if (mounted.current && liveId.current === id) { take(next); setEditingQuote(false) } }
    catch (failure) { if (mounted.current && liveId.current === id) setError(failure instanceof Error ? failure.message : '重试请求尚未完成，请稍后再试。') }
    finally { if (mounted.current) setBusy('') }
  }
  function safeLink(value: string | undefined) { return batch && value?.startsWith(`${API}/${batch.id}/`) ? value : undefined }
  const uniqueDocuments = [...new Set(result?.lines.map(line => line.document) || [])]
  const displayGroups = quotedDisplayGroups(result), originalLines = new Map(result?.lines.map(line => [line.id, line]) || [])
  const groupedUnpriced = displayGroups.filter(line => !line.priced).length
  const filterRows = bomView === 'sources' ? (result?.lines || []) : displayGroups
  const filterCounts = { all: filterRows.length, make: filterRows.filter(line => line.mode === 'make').length, buy: filterRows.filter(line => line.mode === 'buy').length, unpriced: filterRows.filter(line => !line.priced).length }
  const needle = search.trim().toLocaleLowerCase()
  const matchesFilter = (line: Line) => bomFilter === 'all' || (bomFilter === 'unpriced' ? !line.priced : line.mode === bomFilter)
  const matchesSearch = (line: Line, sources: string[] = []) => !needle || [line.part_name, line.document, line.id, ...sources].some(value => value?.toLocaleLowerCase().includes(needle))
  const visibleGroups = displayGroups.filter(group => matchesFilter(group) && matchesSearch(group, [...group.source_documents, ...group.member_ids]))
  const visibleLines = (result?.lines || []).filter(line => matchesFilter(line) && matchesSearch(line))
  const visibleDocuments = uniqueDocuments.filter(document => visibleLines.some(line => line.document === document))
  const orderCountLabel = result?.settings?.order_sets ? `${count(result.settings.order_sets)} 套合计` : '订单合计'
  const hasFilter = !!needle || bomFilter !== 'all'
  const showUnpriced = () => { setSearch(''); setBomFilter('unpriced'); setBomView('grouped'); tableAnchor.current?.scrollIntoView({ block: 'start' }) }
  const clearFilters = () => { setSearch(''); setBomFilter('all') }
  const partsLoading = !!partsKey && (parts.key !== partsKey || parts.loading)
  const openOriginalPart = (line: Line, url: string, entry: BatchPartEntry) => setPartSelection({ url, entry, line, orderSets: result?.settings?.order_sets })
  const total = batch?.progress?.total || stepFiles.length, done = batch?.progress?.done || 0

  return <Container className={`bq-app${assistantOpen ? ' bq-assistant-open' : ''}${embedded ? ' bq-embedded' : ''}`} data-testid="batch-quotation-workbench" data-scope={scope || 'loading'}>
    {!embedded && <header className="bq-topbar"><a href="/workbench/" className="bq-brand"><span>好</span><strong>好运来</strong><small>{customerAdmin ? '客户订单核价' : '整单报价工作台'}</small></a><a className="bq-home" href="/workbench/"><ArrowLeft size={14} />返回工作台</a></header>}
    <div className="bq-shell">
      <div className="bq-heading"><div><p className="bq-eyebrow">{customerAdmin ? 'CUSTOMER ORDERS · INTERNAL REVIEW' : privateCase ? 'POLAND ORDER · QUOTATION' : 'ORDER QUOTATION'}</p><h1>{customerAdmin ? '客户订单核价' : privateCase ? '波兰订单，继续调整报价' : '一包资料，整理成一份报价'}</h1><p>{customerAdmin ? '查看客户提交的 CAD 资料，核对内部成本，并保存新的客户报价。' : privateCase ? '复用已保存的识别结果，提出修改、预览金额，再另存新报价。' : '上传 CAD 资料，按订单套数估重、核算，生成待审核的 BOM 和模拟报价。'}</p></div>{!customerAdmin && (privateCase ? <a className="bq-button" href="/workbench/quotation/" data-testid="bq-new"><Upload size={15} />上传新订单</a> : batch && <button className="bq-button" disabled={!!busy} onClick={() => input.current?.click()} data-testid="bq-new"><Upload size={15} />上传新订单</button>)}</div>
      {privateCase ? <aside className="bq-case-notice" data-testid="bq-private-case"><strong>波兰订单 · 原始报价条件：10 套</strong><p>沿用 CAD 实体、工程标注与已保存的计价依据。修改先生成预览，点击采用后保存新版本；原 10 套报价保留。模拟报价待审核。</p></aside> : <ol className="bq-steps" aria-label="整单报价流程">{['上传订单资料', '确认报价条件', '查看报价草稿'].map((label, index) => <li key={label} className={stage === index ? 'active' : stage > index ? 'done' : ''} aria-current={stage === index ? 'step' : undefined}><span>{stage > index ? <Check size={13} /> : index + 1}</span>{label}{index < 2 && <ChevronRight size={15} />}</li>)}</ol>}
      {embedded && onNewOrder && <div className="bq-new-inquiry-row"><button type="button" className="bq-button" onClick={() => navigation.current.onNewOrder?.()} data-testid="bq-new-inquiry"><Upload size={14} />上传新询价</button></div>}
      {error && <div className="bq-alert error" role="alert" data-testid="bq-error">{error}<button type="button" onClick={() => setError('')}>关闭</button></div>}
      {(loading || busy) && <div className="bq-feedback" role="status" data-testid="bq-busy"><LoaderCircle size={16} className="bq-spin" />{busy || '正在打开已保存订单…'}</div>}
      {customerAdmin && !batch && !loading && <div className="bq-case-notice" data-testid="bq-customer-admin-empty"><strong>从下方订单列表打开客户询价</strong><p>客户通过专用入口上传，内部核价与客户提交记录分别展示。</p></div>}
      {!privateCase && !customerAdmin && scope && !batch && !loading && <section className={`bq-upload${dragging ? ' dragging' : ''}`} data-testid="bq-drop" role="button" tabIndex={0} aria-label="上传 ZIP、RAR 或 STEP 订单资料" aria-disabled={!!busy}
        onClick={() => { if (!busy) input.current?.click() }} onKeyDown={event => { if (event.target === event.currentTarget && !busy && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); input.current?.click() } }}
        onDragOver={event => { event.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); if (event.dataTransfer.files.length !== 1) setError('请选择一个 ZIP / RAR 资料包，将本次订单的文件放在同一个压缩包中。'); else void upload(event.dataTransfer.files[0]) }}>
        <div className="bq-upload-icon"><FileArchive size={30} /></div><h2>选择 ZIP / RAR，或拖放到这里</h2><p>将本次订单的 STEP / STP 与参考附件放在一个压缩包中。</p><span className="bq-button primary"><Upload size={15} />上传订单资料</span><small>ZIP / RAR 最大 50 MB · 也支持单份 STEP / STP，最大 20 MB</small>
      </section>}
      {batch && <section className="bq-order-card">
        <div className="bq-section-heading"><div><h2>{batch.name}</h2><p>{stepFiles.length} 份待计价 CAD · {batch.files.filter(file => file.kind !== 'step').length} 份参考或其他附件</p></div><span className={`bq-badge ${active ? 'processing' : ''}`} data-testid="bq-status">{active && <LoaderCircle size={12} className="bq-spin" />}{statuses[batch.status] || batch.status}</span></div>
        {active && <div className="bq-progress" data-testid="bq-progress"><div><strong>{batch.progress?.message || '后台正在逐份读取 CAD 并核算。'}</strong><span>{done} / {total} 份</span></div><progress max={Math.max(total, 1)} value={done} aria-label="CAD 处理进度" /><p>{batch.progress?.current ? `当前：${batch.progress.current}。` : ''}可以离开页面，稍后从已保存订单继续查看。</p>{pollError && <p className="bq-poll-error">{pollError}</p>}</div>}
        {!!failedFiles.length && !active && <div className="bq-alert warning"><span>{failedFiles.length} 份 CAD 尚未完成，已有结果保留。未计价部分不视为免费。</span>{!privateCase && <button className="bq-button" type="button" data-testid="bq-retry-all" disabled={!!busy} onClick={() => void retry(failedFiles.map(file => file.id))}><RefreshCw size={13} />重试未完成文件</button>}</div>}
        <details className="bq-file-details" open={batch.status === 'uploaded' || (!result && !active)}><summary><FolderOpen size={15} />订单文件清单<span>{batch.files.length} 份</span></summary>
          <div className="bq-file-list" data-testid="bq-files">{batch.files.map(file => <div className={`bq-file ${file.status === 'failed' ? 'failed' : ''}`} key={file.id} data-testid={`bq-file-${file.id}`}><FileText size={16} /><div><strong>{file.relative_path || file.name}</strong><small>{file.duplicate_of ? '重复文件 · 相同内容仅计价一次' : fileStatuses[file.status] || (file.kind === 'step' ? file.status : '参考附件')}{file.solid_count != null ? ` · ${file.solid_count} 个原生实体` : ''}{file.error ? ` · ${file.error}` : ''}</small></div><a href={`${API}/${batch.id}/files/${encodeURIComponent(file.id)}`} target="_blank" rel="noopener noreferrer">打开</a>{!privateCase && file.status === 'failed' && !active && <button disabled={!!busy} data-testid={`bq-retry-${file.id}`} onClick={() => void retry([file.id])}>重试</button>}</div>)}</div>
          <p className="bq-note">图片和 PDF 作为参考附件保存，本轮不会自动据此增加工艺费用。</p>
        </details>
      </section>}
      {batch && settingsVisible && <section className="bq-settings" data-testid="bq-settings">
        <div className="bq-section-heading"><div><h2>{editingQuote ? '调整本次报价条件' : '确认这次怎么报价'}</h2><p>{editingQuote ? '复用已保存的 CAD 结果生成新报价，原报价保留。' : '填写一次订单条件，系统在后台逐份处理。'}</p></div><span className="bq-badge">{priceProfile === MARKET_PROFILE ? '行情参考 · 待审核' : '演示费率 · 待审核'}</span></div>
        <label className="bq-price-profile">材料价格方案<select data-testid="bq-price-profile" value={priceProfile} disabled={!!busy} onChange={event => { setPriceProfile(event.target.value as Settings['rate_profile']); setConfirmed(false) }}><option value={MARKET_PROFILE}>佛山行情参考 · 可录入供应商价格</option><option value="demo-v1">旧版演示材料费率</option></select></label>
        <div className="bq-form-grid">
          <label>订单套数<input type="number" inputMode="numeric" min="1" step="1" value={sets} onChange={event => { setSets(event.target.value); setConfirmed(false) }} placeholder="本次需要多少套" data-testid="bq-order-sets" disabled={!!busy} /></label>
          <label>定价方式<select value={method} onChange={event => { setMethod(event.target.value as Settings['pricing_method']); setConfirmed(false) }} data-testid="bq-method" disabled={!!busy}><option value="cost_plus">成本加成</option><option value="gross_margin">目标毛利率</option></select></label>
          <label>{method === 'cost_plus' ? '加成比例（%）' : '目标毛利率（%）'}<input type="number" inputMode="decimal" min="0" step="any" value={rate} onChange={event => { setRate(event.target.value); setConfirmed(false) }} placeholder="填写本次比例" data-testid="bq-rate" disabled={!!busy} /></label>
          <label>未明确材料时采用<select value={material} onChange={event => { setMaterial(event.target.value as Settings['material_profile']); setConfirmed(false) }} data-testid="bq-material-profile" disabled={!!busy}>{Object.entries(materials).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        </div>
        <p className="bq-note">明确的 CAD 材料优先用于选择材料类别，缺少牌号时按所选材料假设估重。行情的规格、税运与采购成交条件仍须核对。</p>
        {priceProfile === MARKET_PROFILE && (catalog ? <MaterialPriceEditor catalog={catalog} rows={priceRows} vat={vatAssumption} disabled={!!busy} processFirst={processFirst} onVat={value => { setVatAssumption(value); setConfirmed(false) }} onEdit={editPrice} onSource={selectPriceSource} /> : <div className="bq-pricing-loading" role="status">{catalogError || '正在载入有日期和来源的材料参考价…'}<button type="button" onClick={() => void loadPricing()} disabled={!!busy}>重新加载</button></div>)}
        <details className="bq-rate-details"><summary>{priceProfile === MARKET_PROFILE ? '查看加工与用料演示假设' : '查看演示费率与计价范围'}</summary>
          {priceProfile === 'demo-v1' && <div className="bq-rate-table"><table><thead><tr><th>材料假设</th><th>密度 kg/m³</th><th>材料价 元/kg</th></tr></thead><tbody><tr><td>碳钢 / SPCC 类</td><td>7,850.00</td><td>6.00</td></tr><tr><td>不锈钢 304</td><td>7,930.00</td><td>26.00</td></tr><tr><td>不锈钢 316</td><td>7,980.00</td><td>28.00</td></tr><tr><td>铝合金 5052</td><td>2,680.00</td><td>20.00</td></tr></tbody></table></div>}
          {processFirst ? <><p>演示工艺采用值：切割 1.20 元/m，闭合轮廓暂按每次穿孔 0.15 元，折弯候选 1.00 元/折，激光准备 20.00 元/零件分组/单。工厂费率尚未核实；未确定工艺不补成零费用。</p><p>平板材料按二维毛坯包络、板厚和材质核算；折弯件未展开时不能据成品包络计下料。重量用于复核，不作为综合加工费依据。</p></> : <><p>原料利用率暂按 85.00%；综合加工费按每件 5.00 元与“单件净重 × 8.00 元/kg”中的较高值；准备费每个物料分组一次 30.00 元；识别为标准件时，暂按每件 0.80 元外购价。</p><p>这些是演示计算假设，不表示已经识别或核定实际激光、折弯、焊接等工序费用。按每份不重复 STEP 的根装配各一套汇总，装配间是否重叠仍需核对。</p></>}<p>人民币、未税、不含运费；不含包装重量。结果保留工程与商务审核。</p>
        </details>
        <label className="bq-confirm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} data-testid="bq-confirm" disabled={!!busy} /><span>{priceProfile === MARKET_PROFILE ? '按每份不重复 STEP 根模型各 1 套，采用显示的材料价、税率与规格假设，以及演示加工费率，生成待审核的参考报价草稿。' : '按每份不重复 STEP 根模型各 1 套，并使用以上演示材料和加工费率生成模拟报价。'}</span></label>
        <div className="bq-form-actions"><span>原文件和估算依据会随结果保留。</span>{editingQuote && <button className="bq-button" disabled={!!busy} data-testid="bq-cancel-pricing" onClick={() => { take(batch, true); setEditingQuote(false) }}>取消调整</button>}<button className="bq-button primary" data-testid="bq-start" disabled={!!busy || !stepFiles.length} onClick={() => void start()}>{editingQuote ? '按新条件重新报价' : '开始后台报价'}<ArrowRight size={15} /></button></div>
      </section>}
      {batch && result && <section className="bq-results" data-testid="bq-result">
        {processFirst && resultView === 'processes' && <BatchProcessCostPanel lines={result.lines} />}
        <div className="bq-result-toolbar"><nav className="bq-result-tabs" role="tablist" aria-label="订单资料视图">{([['manufacturing', '拆件与工艺'], ['engineering', '工程 BOM'], ['materials', '备料汇总'], ['processes', '工艺明细'], ['costsheet', '内部核价'], ['cost', '成本与报价']] as const).map(([view, label]) => <button key={view} type="button" role="tab" aria-selected={resultView === view} data-testid={`bq-tab-${view}`} onClick={() => setResultView(view)}>{view === 'manufacturing' && <Box size={14} />}{label}</button>)}</nav>{hasCostSheet || customerAdmin || processFirst ? <button type="button" className="bq-button" onClick={() => setResultView('costsheet')} title="在内部核价中修改并另存版本" data-testid="bq-costsheet-open">修改核价明细</button> : <button type="button" className="bq-button bq-assistant-trigger" aria-expanded={assistantOpen} aria-controls="bqa-panel" disabled={!!busy || active || editingQuote || !result.run_id} onClick={() => setAssistantOpen(value => !value)} data-testid="bq-assistant-open"><MessageSquareText size={15} />修改报价</button>}</div>
        <div className="bq-customer-quote-entry" data-testid="bq-customer-quote-entry"><div><strong>客户报价单</strong><span>对外展示报价明细与交易条件，内部核价另行保留。</span></div><div>{safeLink(result.links?.quote_url) && <a className="bq-button primary" href={safeLink(result.links?.quote_url)} target="_blank" rel="noopener noreferrer" data-testid="bq-customer-quote"><ExternalLink size={14} />打开客户报价单</a>}{safeLink(result.links?.print_url) && <a className="bq-button" href={safeLink(result.links?.print_url)} target="_blank" rel="noopener noreferrer" data-testid="bq-customer-print"><Printer size={14} />客户打印版</a>}</div></div>
        {resultView === 'manufacturing' && (customerAdmin && result.run_id
          ? <CustomerManufacturingPanel key={`staff/${batch.id}/${result.run_id}`} source="staff" batchId={batch.id} runId={result.run_id} revision={batch.revision} quoteCalculated={result.totals.total_price != null && result.totals.unpriced_lines === 0} onQuoteApplied={value => {
            const saved = value as Batch
            if (!saved || saved.id !== batch.id || !isBatchId(saved.id) || !saved.result?.run_id || !Array.isArray(saved.files) || !Number.isSafeInteger(saved.revision)) throw new Error('新报价已保存，请刷新当前订单查看。')
            if (liveId.current === saved.id) { take(saved); setAssistantOpen(false); setResultView('manufacturing'); setError(''); setPollError('') }
            void refreshList()
          }} />
          : <section className="bq-native-parts" data-testid="bq-native-parts"><div className="bq-section-heading"><div><h2>总装拆件</h2><p>逐件查看原生几何，再核对物料与加工要求。</p></div><span>{result.lines.length} 项来源零件</span></div><div className="bq-native-parts-grid">{result.lines.map(line => <article key={line.id}><QuoteLineIdentity line={line} previewLine={line} entry={partEntries.get(line.id)} loading={partsLoading} onOpen={(url, entry) => openOriginalPart(line, url, entry)} /><small>每套 {count(line.quantity_per_set)} 件 · 本单 {count(line.quantity_total)} 件</small></article>)}</div></section>)}
        <div hidden={resultView === 'manufacturing' || resultView === 'costsheet' || (processFirst && resultView === 'processes')}><BatchEngineeringPanel key={`${API}/${batch.id}/${result.run_id || ''}`} apiBase={API} savedCase={privateCase || customerAdmin || hasCostSheet} batchId={batch.id} runId={result.run_id} view={resultView === 'manufacturing' || resultView === 'cost' || resultView === 'costsheet' ? null : resultView} onRepriced={next => { const updated = next as Batch; if (!updated || updated.id !== batch.id || !isBatchId(updated.id) || !updated.result?.run_id) throw new Error('新报价已保存，请重新打开当前订单查看。'); take(updated); setResultView('cost'); void refreshList() }} onPreview={lineId => { const line = originalLines.get(lineId), entry = partEntries.get(lineId); const url = safePartPreviewUrl(entry?.geometry_url); if (line && entry?.status === 'ready' && url) openOriginalPart(line, url, entry) }} previewReady={new Set([...partEntries.values()].filter(entry => entry.status === 'ready' && safePartPreviewUrl(entry.geometry_url)).map(entry => entry.line_id))} /></div>
        {result.run_id && <BatchCostSheetPanel key={`cost-sheet/${API}/${batch.id}/${result.run_id}`} apiBase={API} batchId={batch.id} runId={result.run_id} active={resultView === 'costsheet'} disabled={!!busy || active || editingQuote} lines={result.lines} orderSets={result.settings?.order_sets} onApplied={next => { const updated = next as Batch; if (!updated || updated.id !== batch.id || !isBatchId(updated.id) || !updated.result?.run_id) throw new Error('核价已保存，请重新打开当前订单查看。'); take(updated); setAssistantOpen(false); setResultView('costsheet'); void refreshList() }} />}
        <div hidden={resultView !== 'cost'} data-testid="bq-cost-view" role="tabpanel">
        <div className="bq-section-heading"><div><h2>{active || editingQuote ? '上次保存的报价草稿' : resultUsesMarket ? '行情参考报价草稿' : '报价草稿'}</h2><p>{resultUsesMarket ? '材料价采用本次保存的来源；加工及用料仍含演示假设。' : '模拟报价，'}待工程与商务审核。人民币、未税、不含运费。</p></div><span className="bq-badge">待审核</span></div>
        <div className="bq-order-summary" data-testid="bq-order-summary"><strong>{result.settings?.order_sets ? `${count(result.settings.order_sets)} 套` : '套数未提供'}</strong><span>{displayGroups.length} 项物料</span>{groupedUnpriced > 0 ? <button type="button" data-testid="bq-jump-unpriced" onClick={showUnpriced}>{groupedUnpriced} 项未计价<ArrowRight size={14} /></button> : <span className="bq-summary-complete">全部已计价</span>}</div>
        {(active || editingQuote) && <p className="bq-snapshot-note" data-testid="bq-prior-quote-note">{active ? '本轮正在后台处理。' : ''}下方金额、重量及来源仍对应上次已保存报价；本轮报价完成后才会更新。</p>}
        {(groupedUnpriced > 0 || failedFiles.length > 0) && <div className="bq-alert warning"><span>以下金额仅覆盖已计价范围。{groupedUnpriced > 0 ? `另有 ${groupedUnpriced} 项物料未计价。` : ''}{failedFiles.length > 0 ? `另有 ${failedFiles.length} 份文件未完成。` : ''}缺失项未作为零成本纳入。</span></div>}
        <div className="bq-metrics">
          <div className="price"><span>{groupedUnpriced > 0 || failedFiles.length > 0 ? '已计价范围 · 报价金额' : resultUsesMarket ? '行情参考报价总额' : '模拟报价总额'}</span><strong data-testid="bq-total-price">{money(result.totals.total_price)}</strong><small>已计价部分成本 {money(result.totals.total_cost)}</small></div>
          <div><span>每套预估净重</span><strong data-testid="bq-set-weight">{weight(result.weight_summary.estimated_set_net_weight_kg)}</strong><small>按本次成套范围 · 不含包装</small></div>
          <div><span>{result.settings?.order_sets ? `${count(result.settings.order_sets)} 套预估净重` : '整单预估净重'}</span><strong data-testid="bq-total-weight">{weight(result.weight_summary.estimated_total_net_weight_kg)}</strong><small>{result.weight_summary.missing_estimated_lines ? `已知部分 ${weight(result.weight_summary.known_total_net_weight_kg)}；${result.weight_summary.missing_estimated_lines} 行暂无法估算` : result.weight_summary.low_confidence_lines ? `其中 ${result.weight_summary.low_confidence_lines} 行采用粗略估算，依据见物料明细` : '原生体积 × 材料密度假设，未经称重'}</small></div>
        </div>
        <div className="bq-result-actions">
          {safeLink(result.links?.quote_url) && <a href={safeLink(result.links?.quote_url)} className="bq-button primary" data-testid="bq-open-quote" target="_blank" rel="noopener noreferrer"><ExternalLink size={15} />客户报价单</a>}
          {safeLink(result.links?.summary_bom_preview_url) && <a href={safeLink(result.links?.summary_bom_preview_url)} className="bq-button" data-testid="bq-preview-summary-bom" target="_blank" rel="noopener noreferrer"><Eye size={15} />预览汇总 BOM</a>}
          {safeLink(result.links?.bom_preview_url) && <a href={safeLink(result.links?.bom_preview_url)} className="bq-button" data-testid="bq-preview-source" target="_blank" rel="noopener noreferrer"><Eye size={15} />预览来源明细</a>}
          {safeLink(result.links?.print_url) && <a href={safeLink(result.links?.print_url)} className="bq-button" data-testid="bq-preview-print" target="_blank" rel="noopener noreferrer"><Printer size={15} />客户打印版</a>}
          {safeLink(result.links?.internal_cost_url) && <a href={safeLink(result.links?.internal_cost_url)} className="bq-button" data-testid="bq-internal-cost" target="_blank" rel="noopener noreferrer"><Eye size={15} />内部核价明细</a>}
          {safeLink(result.links?.internal_cost_csv_url) && <a href={safeLink(result.links?.internal_cost_csv_url)} className="bq-button" data-testid="bq-internal-cost-csv" download>导出内部成本</a>}
          {safeLink(result.links?.cost_preview_url) && <a href={safeLink(result.links?.cost_preview_url)} className="bq-button" data-testid="bq-preview-cost" target="_blank" rel="noopener noreferrer"><Eye size={15} />预览成本明细</a>}
          {safeLink(result.links?.record_preview_url) && <a href={safeLink(result.links?.record_preview_url)} className="bq-button" data-testid="bq-preview-record" target="_blank" rel="noopener noreferrer"><Eye size={15} />预览报价记录</a>}
          {!hasCostSheet && !privateCase && !active && !editingQuote && <button className="bq-button" data-testid="bq-edit-pricing" disabled={!!busy} onClick={() => { take(batch, true); setEditingQuote(true) }}>调整报价条件<RefreshCw size={13} /></button>}
        </div>
        {resultView !== 'costsheet' && resultView !== 'manufacturing' && <BatchCostChart totals={result.totals} unpricedLines={result.totals.unpriced_lines} />}
        <div className="bq-bom-heading" ref={tableAnchor} tabIndex={-1}><div><h3>物料与报价明细</h3><p data-testid="bq-group-count">{displayGroups.length} 项物料，保留 {result.lines.length} 条来源记录</p></div><div className="bq-bom-view" aria-label="物料明细显示方式"><button type="button" data-testid="bq-view-grouped" aria-pressed={bomView === 'grouped'} onClick={() => setBomView('grouped')}>汇总物料</button><button type="button" data-testid="bq-view-sources" aria-pressed={bomView === 'sources'} onClick={() => setBomView('sources')}>按来源查看</button></div></div>
        <div className="bq-bom-tools"><label className="bq-search"><Search size={16} /><input data-testid="bq-search" type="search" aria-label="搜索物料名称、型号或来源文件" placeholder="搜索名称、型号或来源文件" maxLength={250} value={search} onChange={event => setSearch(event.target.value)} /></label><div className="bq-filter-tabs" role="group" aria-label="物料类型筛选">{([['all', '全部'], ['make', '自制件'], ['buy', '外购件'], ['unpriced', '未计价']] as [BomFilter, string][]).map(([value, label]) => <button key={value} type="button" data-testid={`bq-filter-${value}`} aria-pressed={bomFilter === value} onClick={() => setBomFilter(value)}>{label}<span>{filterCounts[value]}</span></button>)}</div></div>
        <div className="bq-filter-status"><span data-testid="bq-filter-count">显示 {bomView === 'grouped' ? `${visibleGroups.length} / ${displayGroups.length} 项物料` : `${visibleLines.length} / ${result.lines.length} 条来源`}</span>{hasFilter && <button type="button" data-testid="bq-clear-filters" onClick={clearFilters}>清除筛选</button>}</div>
        <div className="bq-parts-status" data-testid="bq-parts-status" role="status">{partsKey && parts.key === partsKey && parts.loading ? <><LoaderCircle size={12} className="bq-spin" />正在读取原生零件预览目录…</> : partsKey && parts.key === partsKey && parts.error ? <><span>原生零件预览暂不可用；报价数据和金额保留。{parts.error}</span><button type="button" data-testid="bq-parts-retry" onClick={() => { partsCache.current.delete(partsKey); setPartsReload(value => value + 1) }}>重新读取</button></> : partsKey ? '点击缩略图查看原生单件 3D，并在来源 STEP 中定位。缩略图展示代表实体。' : '这份历史报价没有原生预览映射，金额和物料记录仍保留。'}</div>
        <div data-testid="bq-part-lines">{bomView === 'grouped' ? <div className="bq-table-wrap bq-summary-table"><table><thead><tr><th>物料与来源</th><th>每套用量</th><th>{orderCountLabel}</th><th>单件预估净重</th><th>订单预估净重</th><th>报价单价</th><th>报价金额</th></tr></thead><tbody>{visibleGroups.map(group => {
          const members = group.member_ids.map(id => originalLines.get(id)).filter((line): line is Line => !!line)
          const representative = members[0]
          if (!representative) return null
          return <Fragment key={group.id}><tr className={!group.priced ? 'bq-unpriced-row' : undefined} data-testid={`bq-group-${group.id}`} data-member-count={group.member_count}>
            <td><QuoteLineIdentity line={group} previewLine={representative} entry={partEntries.get(representative.id)} loading={partsLoading} caption={group.aggregated ? `${group.source_documents.length} 个来源 · 已汇总` : representative.document} onOpen={(url, entry) => openOriginalPart(representative, url, entry)} /></td>
            <td>{count(group.quantity_per_set)}</td><td data-testid={`bq-group-quantity-${group.id}`}>{count(group.quantity_total)}</td><td>{weight(group.estimated_net_weight_kg)}</td><td>{weight(group.estimated_total_net_weight_kg)}</td><td>{group.priced ? group.unit_price == null ? '未提供' : money(group.unit_price) : '未计价'}</td><td className={!group.priced ? 'bq-unpriced' : 'bq-group-amount'} data-testid={`bq-group-amount-${group.id}`}>{group.priced ? money(group.total_price) : '未计价'}</td>
          </tr>{group.aggregated && <tr className="bq-group-detail"><td colSpan={7}><details className="bq-group-sources" data-testid={`bq-group-sources-${group.id}`}><summary>展开 {group.member_count} 条来源明细</summary><div className="bq-source-table-wrap"><table><thead><tr><th>来源装配</th><th>每套数量</th><th>订单数量</th><th>订单预估净重</th><th>原行报价金额</th><th>原生预览</th></tr></thead><tbody>{members.map(line => <tr key={line.id} data-testid={`bq-source-line-${line.id}`}><td><strong>{line.document}</strong><small>{line.id}</small></td><td>{count(line.quantity_per_set)}</td><td>{count(line.quantity_total)}</td><td>{weight(line.estimated_total_net_weight_kg)}</td><td>{line.priced ? money(line.total_price) : '未计价'}</td><td><SourcePreviewButton line={line} entry={partEntries.get(line.id)} loading={partsLoading} onOpen={(url, entry) => openOriginalPart(line, url, entry)} /></td></tr>)}</tbody></table></div><p>预览对应各来源的原始数量、重量和金额；汇总不会重新计算费用。</p></details></td></tr>}</Fragment>
        })}{visibleGroups.length === 0 && <tr><td colSpan={7} className="bq-empty-results">没有符合条件的物料。可更换关键词或清除筛选。</td></tr>}</tbody></table></div> : visibleDocuments.map((document, index) => { const lines = visibleLines.filter(line => line.document === document); return <details className="bq-bom-document" key={document} open={hasFilter || visibleDocuments.length === 1}>
          <summary><span className="bq-document-number">{String(index + 1).padStart(2, '0')}</span><strong>{document}</strong><span>{lines.length} 行物料</span></summary>
          <div className="bq-table-wrap"><table><thead><tr><th>零件</th><th>每套用量</th><th>{orderCountLabel}</th><th>单件预估净重</th><th>订单预估净重</th><th>报价金额</th></tr></thead><tbody>{lines.map(line => <tr key={line.id} className={!line.priced ? 'bq-unpriced-row' : undefined}>
            <td><QuoteLineIdentity line={line} previewLine={line} entry={partEntries.get(line.id)} loading={partsLoading} onOpen={(url, entry) => openOriginalPart(line, url, entry)} /></td><td>{count(line.quantity_per_set)}</td><td>{count(line.quantity_total)}</td><td>{weight(line.estimated_net_weight_kg)}</td><td>{weight(line.estimated_total_net_weight_kg)}</td><td className={!line.priced ? 'bq-unpriced' : ''}>{line.priced ? money(line.total_price) : '未计价'}</td>
          </tr>)}</tbody></table></div>
        </details> })}{bomView === 'sources' && visibleDocuments.length === 0 && <div className="bq-empty-results">没有符合条件的来源明细。可更换关键词或清除筛选。</div>}</div>
        <details className="bq-result-details"><summary>{hasCostSheet ? '原始估算条件（内部追溯）' : '查看本次价格来源与估算假设'}</summary><p className="bq-common-basis">{hasCostSheet ? '本版费用以内部核价单为准。以下保留原报价的估算条件，已替换的费用不再采用或叠加。' : '重量按已保存的 CAD 体积及本单采用密度估算，未经称重。材料采用类别与 CAD 原文分别保留；共同假设仅在这里列出，逐件依据可在物料行内展开。汇总只相加原记录，不重新计价。'}</p>{!!result.assumptions?.length && <ul>{result.assumptions.map(assumptionText).filter(Boolean).map((item, index) => <li key={index}>{item}</li>)}</ul>}</details>
        {!!result.limitations?.length && <details className="bq-result-details"><summary>查看尚待核对的范围</summary><ul>{result.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}
        </div>
      </section>}
      {!privateCase && recent.length > 0 && <section className="bq-recent"><div className="bq-section-heading"><h2><FolderOpen size={17} />已保存订单</h2><span>继续查看，无需重新上传</span></div>{recent.map(row => <button key={row.id} className={`bq-recent-row${row.id === batch?.id ? ' selected' : ''}`} disabled={!!busy || loading} data-testid={`bq-recent-${row.id}`} onClick={() => void openBatch(row.id)}><FileArchive size={17} /><strong>{row.name}</strong><span>{statuses[row.status] || row.status}</span><small>{dateLabel(row.updated_at || row.created_at)}</small><ChevronRight size={15} /></button>)}</section>}
      <footer className="bq-footer">资料和处理结果保存在服务器，可稍后继续查看。所有报价均为待审核草稿。</footer>
    </div>
    {!privateCase && !customerAdmin && scope && <input ref={input} type="file" accept=".zip,.rar,.step,.stp" hidden data-testid="bq-upload" onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; void upload(file) }} />}
    {partSelection && <BatchPartViewer key={partSelection.url} previewUrl={partSelection.url} entry={partSelection.entry} lineSnapshot={partSelection.line} orderSets={partSelection.orderSets} onClose={() => setPartSelection(null)} />}
    {!customerAdmin && !processFirst && batch && result?.run_id && !hasCostSheet && <BatchQuoteAssistant key={`${API}/${batch.id}`} apiBase={API} batchId={batch.id} runId={result.run_id} open={assistantOpen} disabled={!!busy || active || editingQuote} orderSets={result.settings?.order_sets} totalPrice={result.totals.total_price} lines={result.lines} onClose={() => setAssistantOpen(false)} onApplied={next => { const updated = next as Batch; if (!updated || updated.id !== batch.id || !isBatchId(updated.id) || !updated.result?.run_id) throw new Error('新报价已保存，请重新打开当前订单查看。'); take(updated); setResultView('cost'); void refreshList() }} />}
  </Container>
}
