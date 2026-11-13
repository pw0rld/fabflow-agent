'use client'

import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, Box, Crosshair, ExternalLink, Layers, LoaderCircle, RotateCcw, X } from 'lucide-react'
import { batchMaterialDisplay } from '@/lib/batch-material-display'
import { estimatedWeight, quotationCount } from '@/lib/batch-display-format'
import './batch-part-viewer.css'

const API_PART = /^\/workbench\/api\/quote-batches\/(BATCH-[a-f0-9]{32})\/runs\/(RUN-[a-f0-9]{32})\/(?:customer-)?parts\/(L[0-9]{4,8})$/
const PRIVATE_API_PART = /^\/ortho2cad-test\/(?:quote-workbench|customer-upload-admin)\/api\/quote-batches\/(BATCH-[a-f0-9]{32})\/runs\/(RUN-[a-f0-9]{32})\/(?:customer-)?parts\/(L[0-9]{4,8})$/
const CUSTOMER_API_PART = /^\/workbench\/customer\/api\/orders\/(BATCH-[a-f0-9]{32})\/runs\/(RUN-[a-f0-9]{32})\/customer-parts\/(L[0-9]{4,8})$/
const CASE_PART = /^\/ortho2cad-test\/customer-cases\/([A-Za-z0-9][A-Za-z0-9._-]{0,127})\/(?:customer-)?parts\/(L[0-9]{4,8})\.json$/
const SHA = /^[a-f0-9]{64}$/
type Bounds = { min: number[]; max: number[]; size: number[] }
type Solid = { id: string; name?: string; mesh: { positions: number[]; indices: number[] }; bounds_mm?: Bounds }
type Geometry = { source_sha256: string; output_unit: string; solids: Solid[]; [key: string]: unknown }
type Occurrence = { id: string; name?: string; solid_ids: string[] }
export type BatchPartLine = {
  id?: string; part_name?: string; document?: string; mode?: string; material_assumption?: string | null; material_raw?: string | null;
  material_profile?: string | null; material_rate?: string | null; material_price_source_kind?: string | null; material_price_snapshot?: Record<string, unknown> | null;
  material_estimation_label?: string | null; weight_assumptions?: { adopted_material_label?: string | null } | null;
  quantity_per_set?: number | null; quantity_total?: number | null; estimated_net_weight_kg?: string | null; estimated_total_net_weight_kg?: string | null;
  material_cost?: string | null; process_cost?: string | null; purchase_cost?: string | null; setup_cost?: string | null; tooling_cost?: string | null; other_cost?: string | null; total_cost?: string | null;
  total_price?: string | null; priced?: boolean; weight_basis?: string; confidence?: string; notes?: string | string[];
}
export type BatchPartEntry = {
  line_id: string; file_id: string; status: 'ready' | 'unavailable'; reason?: string | null; thumbnail_url: string | null; geometry_url: string | null; viewer_url?: string | null;
  solid_ids: string[]; representative_solid_id: string | null; representative_solid_ids?: string[]; bounds_mm: Bounds | null;
}
export type BatchPartsManifest = { batch_id: string; run_id: string; lines: BatchPartEntry[] }
type PartPayload = {
  batch_id?: string; run_id?: string; line_id: string; part_name?: string; source_sha256: string; geometry?: Geometry; shared_geometry_url?: string;
  groups: { id: string; name?: string; solid_ids: string[] }[]; representative_solid_id?: string | null; representative_solid_ids?: string[];
  occurrences?: Occurrence[]; selected_id?: string; line_snapshot?: BatchPartLine; order_sets?: number; up_axis?: 'y' | 'z'; source_document?: string;
}
type LoadedPart = PartPayload & { geometry: Geometry }
type Viewer = { dispose(): void; reset(): void; select(id: string | null): void }
const loadedParts = new Map<string, LoadedPart>(), sharedGeometry = new Map<string, Geometry>(), geometryBySource = new Map<string, Geometry>()

function sameOriginPath(value: unknown): string | null {
  if (typeof value !== 'string' || !value || value.length > 800 || value.includes('\\')) return null
  try {
    const url = new URL(value, window.location.origin)
    if (url.origin !== window.location.origin || url.username || url.password || url.search || url.hash) return null
    return url.pathname
  } catch { return null }
}
export function safePartPreviewUrl(value: unknown): string | null {
  const path = sameOriginPath(value)
  return path && (API_PART.test(path) || PRIVATE_API_PART.test(path) || CUSTOMER_API_PART.test(path) || CASE_PART.test(path)) ? path : null
}
export function safePartThumbnailUrl(value: unknown, previewUrl: unknown): string | null {
  const source = safePartPreviewUrl(previewUrl), path = sameOriginPath(value)
  if (!source || !path) return null
  return path === (API_PART.test(source) || PRIVATE_API_PART.test(source) || CUSTOMER_API_PART.test(source) ? `${source}/thumbnail` : source.replace(/\.json$/, '/thumbnail.png')) ? path : null
}
function safeSharedUrl(value: unknown, previewUrl: string): string | null {
  const match = CASE_PART.exec(previewUrl), path = sameOriginPath(value)
  if (!match || !path) return null
  const prefix = `/ortho2cad-test/customer-cases/${match[1]}/geometry/`
  return path.startsWith(prefix) && /^F[0-9]{4,8}\.json$/.test(path.slice(prefix.length)) ? path : null
}
async function getJson(url: string, signal: AbortSignal): Promise<unknown> {
  const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal, headers: { 'X-Workbench-Request': '1' } })
  if (!response.ok) throw new Error(response.status === 404 ? '此报价尚没有可用的原生零件预览。' : `零件预览暂未读取完成（${response.status}）。`)
  try { return await response.json() } catch { throw new Error('零件预览数据暂不可读，请重试。') }
}
function nativeGeometry(value: unknown, sourceSha: string, scope: string): Geometry {
  if (!value || typeof value !== 'object') throw new Error('没有可显示的原生几何。')
  const geometry = value as Geometry
  if (geometry.source_sha256 !== sourceSha || geometry.output_unit !== 'mm' || !Array.isArray(geometry.solids) || !geometry.solids.length || geometry.solids.length > 10000) throw new Error('预览几何与原 STEP 来源不一致。')
  const ids = new Set<string>()
  for (const solid of geometry.solids) {
    if (!solid || typeof solid.id !== 'string' || ids.has(solid.id) || !solid.mesh || !Array.isArray(solid.mesh.positions) || !Array.isArray(solid.mesh.indices)) throw new Error('原生实体的显示数据不完整。')
    ids.add(solid.id)
  }
  const cacheKey = `${scope}/${sourceSha}`
  const existing = geometryBySource.get(cacheKey)
  if (existing) return existing
  geometryBySource.set(cacheKey, geometry)
  return geometry
}
async function loadPart(url: string, signal: AbortSignal, refresh: boolean): Promise<LoadedPart> {
  if (!refresh && loadedParts.has(url)) return loadedParts.get(url) as LoadedPart
  const raw = await getJson(url, signal)
  if (!raw || typeof raw !== 'object') throw new Error('零件预览数据无效。')
  const data = raw as PartPayload, api = API_PART.exec(url) || PRIVATE_API_PART.exec(url) || CUSTOMER_API_PART.exec(url), sample = CASE_PART.exec(url)
  const expectedId = api?.[3] || sample?.[2]
  if (data.line_id !== expectedId || (api && (data.batch_id !== api[1] || data.run_id !== api[2])) || !SHA.test(data.source_sha256) || !Array.isArray(data.groups)) throw new Error('零件预览与本次保存的报价行不一致。')
  if (refresh) geometryBySource.delete(`${url.split('/parts/')[0]}/${data.source_sha256}`)
  let geometry = data.geometry
  if (!geometry) {
    const shared = safeSharedUrl(data.shared_geometry_url, url)
    if (!shared) throw new Error('此零件没有可用的原生几何来源。')
    geometry = !refresh ? sharedGeometry.get(shared) : undefined
    if (!geometry) {
      const sharedRaw = await getJson(shared, signal)
      const candidate = sharedRaw && typeof sharedRaw === 'object' && 'geometry' in sharedRaw ? (sharedRaw as { geometry: unknown }).geometry : sharedRaw
      geometry = nativeGeometry(candidate, data.source_sha256, url.split('/parts/')[0])
      sharedGeometry.set(shared, geometry)
    }
  }
  geometry = nativeGeometry(geometry, data.source_sha256, url.split('/parts/')[0])
  const available = new Set(geometry.solids.map(solid => solid.id))
  if (data.groups.some(group => !group || typeof group.id !== 'string' || !Array.isArray(group.solid_ids) || group.solid_ids.some(id => typeof id !== 'string'))) throw new Error('原生零件的分组引用无效。')
  const row = data.groups.find(group => group.id === data.line_id) || data.groups.find(group => group.id === data.selected_id)
  const representatives = data.representative_solid_ids?.length ? data.representative_solid_ids : data.representative_solid_id ? [data.representative_solid_id] : []
  if (!row?.solid_ids.length || row.solid_ids.some(id => !available.has(id)) || !representatives.length || representatives.some(id => !row.solid_ids.includes(id))) throw new Error('报价行未保留完整的原生实例引用，暂不可预览。')
  if (data.occurrences != null && (!Array.isArray(data.occurrences) || data.occurrences.some(item => !item || typeof item.id !== 'string' || !Array.isArray(item.solid_ids) || !item.solid_ids.length || item.solid_ids.some(id => !row.solid_ids.includes(id))))) throw new Error('零件实例映射不完整，暂不可预览。')
  const result = { ...data, geometry }
  loadedParts.set(url, result)
  return result
}
const count = (value: number | null | undefined) => quotationCount(value)
const money = (value: string | null | undefined) => value == null ? '未计价' : `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
const weight = (value: string | null | undefined) => estimatedWeight(value, '未提供')

function PartDetails({ data, line, orderSets, customer = false }: { data: LoadedPart | null; line?: BatchPartLine; orderSets?: number; customer?: boolean }) {
  const saved = line || data?.line_snapshot, sets = orderSets ?? data?.order_sets
  const material = batchMaterialDisplay(saved || {})
  const costs = [['材料费', saved?.material_cost], ['加工费', saved?.process_cost], ['外购费', saved?.purchase_cost], ['准备费', saved?.setup_cost], ...(saved?.tooling_cost != null ? [['模具费', saved.tooling_cost]] as const : []), ...(saved?.other_cost != null ? [['其他费用', saved.other_cost]] as const : [])] as const
  const hasCosts = costs.some(([, value]) => value != null) || saved?.total_cost != null || saved?.total_price != null
  return <aside className="bpv-details" data-testid="bq-part-details">
    <p className="bpv-eyebrow">{customer ? 'PART DETAILS' : 'SAVED QUOTATION'}</p><h3>{customer ? '零件信息' : '本行报价快照'}</h3>
    {saved ? <><p className="bpv-details-note">{customer ? '数量与预估重量沿用本次报价。' : '以下数量、重量与费用来自已保存报价，预览操作不会重新核价。'}</p><dl className="bpv-material-summary" data-testid="bq-part-material-display">
      {material.cad_text && <div className="bpv-material-fact"><dt>CAD材料</dt><dd>{material.cad_text}</dd></div>}
      <div className="bpv-material-fact"><dt>{customer ? '报价采用材料' : material.adopted_label}</dt><dd>{material.adopted_text}</dd></div>
      {!customer && <div className="bpv-material-fact"><dt>价格来源</dt><dd>{material.price_text}</dd></div>}
    </dl><dl className="bpv-facts"><div><dt>物料类别</dt><dd>{saved.mode === 'buy' ? '外购标准件' : saved.mode === 'make' ? '自制件' : '以报价记录为准'}</dd></div>
      <div><dt>每套数量</dt><dd>{count(saved.quantity_per_set)}{saved.quantity_per_set == null ? '' : ' 件'}</dd></div>
      <div><dt>订单数量{sets ? `（${count(sets)} 套）` : ''}</dt><dd>{count(saved.quantity_total)}{saved.quantity_total == null ? '' : ' 件'}</dd></div>
      <div><dt>单件预估净重</dt><dd>{weight(saved.estimated_net_weight_kg)}</dd></div>
      <div><dt>订单预估净重</dt><dd>{weight(saved.estimated_total_net_weight_kg)}</dd></div>
    </dl>{!customer && hasCosts && <div className="bpv-costs"><h4>已保存的费用</h4><dl>{costs.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{money(value)}</dd></div>)}<div className="bpv-cost-total"><dt>本行成本</dt><dd>{money(saved.total_cost)}</dd></div><div className="bpv-price-total"><dt>本行报价</dt><dd>{saved.priced === false ? '未计价' : money(saved.total_price)}</dd></div></dl><p>人民币、未税、不含运费，待工程与商务审核。</p></div>}
    {!customer && <details className="bpv-source-detail"><summary>查看材料与估重依据</summary><p>{material.note}</p>{saved.material_assumption && <p>{saved.material_assumption}</p>}{saved.weight_basis && <p>{saved.weight_basis}</p>}{saved.confidence === 'low' && <p>本行含粗略估重，尚待核对。</p>}</details>}</> : <p className="bpv-details-note">此预览没有附带报价行快照，仅展示已保存的原生几何与来源；未补填数量、重量或金额。</p>}
    <div className="bpv-source"><h4>原生几何来源</h4><p>{saved?.document || data?.source_document || '来源文件随原生几何记录保存'}</p>{data && !customer && <><p>原件 SHA-256</p><code>{data.source_sha256}</code><p>报价行 {data.line_id}{data.run_id ? ` · ${data.run_id}` : ''}</p></>}<p>三维图用于核对零件与装配位置，材料、工艺及报价仍待审核。</p></div>
  </aside>
}

export default function BatchPartViewer({ previewUrl, lineSnapshot, orderSets, entry, onClose, standalone = false, customer = false }: {
  previewUrl: string; lineSnapshot?: BatchPartLine; orderSets?: number; entry?: BatchPartEntry; onClose?(): void; standalone?: boolean; customer?: boolean;
}) {
  const [data, setData] = useState<LoadedPart | null>(null), [error, setError] = useState(''), [status, setStatus] = useState('正在读取已保存的原生零件…')
  const [loading, setLoading] = useState(true), [ready, setReady] = useState(false), [reload, setReload] = useState(0)
  const [mode, setMode] = useState<'single' | 'assembly'>('single'), [instance, setInstance] = useState(''), [locateAll, setLocateAll] = useState(true)
  const host = useRef<HTMLDivElement>(null), dialog = useRef<HTMLDialogElement>(null), closeButton = useRef<HTMLButtonElement>(null), viewer = useRef<Viewer | null>(null)
  const dismiss = useRef(onClose); dismiss.current = onClose
  useEffect(() => {
    if (standalone || !dialog.current) return
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const modal = dialog.current
    modal.showModal(); closeButton.current?.focus()
    return () => { modal.close(); document.body.style.overflow = overflow; if (previous?.isConnected) previous.focus() }
  }, [standalone])
  useEffect(() => {
    const abort = new AbortController(), url = safePartPreviewUrl(previewUrl)
    setLoading(true); setReady(false); setError(''); setData(null); setStatus('正在读取已保存的原生零件…')
    if (!url) { setError('预览链接无效，请从已保存的报价行重新打开。'); setLoading(false); return () => abort.abort() }
    void loadPart(url, abort.signal, reload > 0).then(next => {
      if (abort.signal.aborted) return
      setData(next); setMode('single'); setInstance(''); setLocateAll(true); setLoading(false)
    }).catch(failure => { if (!abort.signal.aborted) { setLoading(false); setError(failure instanceof Error ? failure.message : '原生零件暂未能显示。') } })
    return () => abort.abort()
  }, [previewUrl, reload])

  const solids = data?.geometry.solids || [], available = new Set(solids.map(solid => solid.id))
  const rowGroup = data?.groups.find(group => group.id === data.line_id) || data?.groups.find(group => group.id === data.selected_id)
  const members = [...new Set(rowGroup?.solid_ids || entry?.solid_ids || [])].filter(id => available.has(id))
  const representatives = data?.representative_solid_ids?.length ? data.representative_solid_ids : entry?.representative_solid_ids?.length ? entry.representative_solid_ids : [data?.representative_solid_id || entry?.representative_solid_id || '']
  const representativeIds = [...new Set(representatives)].filter(id => members.includes(id))
  const occurrences = (data?.occurrences || []).filter(item => item && typeof item.id === 'string' && Array.isArray(item.solid_ids) && item.solid_ids.length && item.solid_ids.every(id => members.includes(id)))
  const selectedOccurrence = occurrences.find(item => item.id === instance)
  const instanceIds = selectedOccurrence?.solid_ids || representativeIds
  const shownIds = mode === 'single' ? instanceIds : solids.map(solid => solid.id)
  const highlightIds = mode === 'single' || !locateAll ? instanceIds : members
  const displayKey = JSON.stringify([mode, shownIds, highlightIds])

  useEffect(() => {
    if (!data || !host.current) return
    const abort = new AbortController(); let current: Viewer | null = null
    const targetId = data.line_id, shown = new Set(shownIds), highlighted = new Set(highlightIds)
    setReady(false); setError(''); setStatus('正在准备原生三维视图…')
    void (async () => {
      try {
        if (!shown.size || !highlighted.size) throw new Error('此报价行未保留可核对的代表实例，暂不能显示单件。')
        const displaySolids = data.geometry.solids.filter(solid => shown.has(solid.id))
        const groups = [{ id: targetId, name: data.part_name || lineSnapshot?.part_name || targetId, solid_ids: [...highlighted] }, ...displaySolids.filter(solid => !highlighted.has(solid.id)).map((solid, index) => ({ id: `quote-context-${index}`, name: solid.name || solid.id, solid_ids: [solid.id] }))]
        const moduleUrl = '/workbench/cad-preview/step.js'
        const module = await import(/* webpackIgnore: true */ moduleUrl)
        if (abort.signal.aborted || !host.current) return
        current = await module.mountGeometry({ element: host.current, geometry: { ...data.geometry, solids: displaySolids }, groups, upAxis: data.up_axis || 'z', signal: abort.signal,
          status: (message: string) => { if (!abort.signal.aborted) setStatus(message) },
          onSelectionChange: (id: string | null) => { if (id !== targetId) current?.select(targetId) },
        })
        if (abort.signal.aborted) { current?.dispose(); return }
        current?.select(targetId); viewer.current = current; setReady(true)
        setStatus(mode === 'single' ? '显示一个原生零件实例，可拖动旋转、滚轮缩放。' : locateAll ? '橙色为本行全部实例，其余原装配实体淡显。' : '橙色为当前实例，其余原装配实体淡显。')
      } catch (failure) { if (!abort.signal.aborted) { setReady(false); setError(failure instanceof Error ? failure.message : '三维视图暂未能显示。') } }
    })()
    return () => { abort.abort(); current?.dispose(); viewer.current = null }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, displayKey])

  const name = lineSnapshot?.part_name || data?.line_snapshot?.part_name || data?.part_name || '原生零件预览'
  const safeUrl = safePartPreviewUrl(previewUrl)
  const separateUrl = safeUrl ? `/workbench/quotation/part/?${customer ? 'customer=1&' : ''}${PRIVATE_API_PART.test(safeUrl) ? 'scope=poland&' : ''}preview=${encodeURIComponent(safeUrl)}` : undefined
  const content = <div className="bpv-panel">
    <header className="bpv-heading"><div><p className="bpv-eyebrow">NATIVE STEP · PART PREVIEW</p><h2 id="bq-part-title">{name}</h2><p>从已保存 STEP 实体查看单件及其装配位置</p></div>{!standalone && <button ref={closeButton} type="button" className="bpv-close" data-testid="bq-part-close" aria-label="关闭零件预览" onClick={() => dismiss.current?.()}><X size={20} /></button>}</header>
    <div className="bpv-body"><section className="bpv-visual" aria-label="原生零件三维图">
      <div className="bpv-toolbar"><div className="bpv-modes" role="group" aria-label="零件查看方式"><button type="button" data-testid="bq-part-single" aria-pressed={mode === 'single'} disabled={!data} onClick={() => setMode('single')}><Box size={15} />单件 3D</button><button type="button" data-testid="bq-part-assembly" aria-pressed={mode === 'assembly'} disabled={!data} onClick={() => setMode('assembly')}><Layers size={15} />总装定位</button></div><button type="button" className="bpv-reset" data-testid="bq-part-reset" disabled={!ready} onClick={() => viewer.current?.reset()}><RotateCcw size={13} />复位</button></div>
      {data && <div className="bpv-instance-bar"><label>查看实例<select data-testid="bq-part-instance" value={instance} onChange={event => setInstance(event.target.value)}><option value="">代表实例{representativeIds.length > 1 ? `（含 ${representativeIds.length} 个实体）` : ''}</option>{occurrences.map((item, index) => <option key={item.id} value={item.id}>{item.name || `实例 ${index + 1}`}{item.solid_ids.length > 1 ? `（${item.solid_ids.length} 个实体）` : ''}</option>)}</select></label>{mode === 'assembly' && <div className="bpv-locate" role="group" aria-label="总装高亮范围"><button type="button" data-testid="bq-part-locate-all" aria-pressed={locateAll} onClick={() => setLocateAll(true)}><Crosshair size={12} />本行全部</button><button type="button" data-testid="bq-part-locate-current" aria-pressed={!locateAll} onClick={() => setLocateAll(false)}>当前实例</button></div>}</div>}
      <div className="bpv-stage"><div ref={host} className="bpv-canvas" data-testid="bq-part-canvas" data-mode={mode} data-ready={ready ? 'true' : 'false'} data-displayed-solid-ids={JSON.stringify(shownIds)} data-highlighted-solid-ids={JSON.stringify(highlightIds)} data-visible-solid-count={shownIds.length} data-highlighted-solid-count={highlightIds.length} />{loading && <div className="bpv-overlay" role="status"><LoaderCircle size={26} className="bpv-spin" /><span>正在读取原生零件</span></div>}{error && <div className="bpv-overlay bpv-failure" role="alert" data-testid="bq-part-error"><Box size={30} /><strong>本次预览未完成</strong><p>{error}</p><button type="button" data-testid="bq-part-retry" onClick={() => setReload(value => value + 1)}>重新读取</button></div>}</div>
      <div className="bpv-view-status" role="status" data-testid="bq-part-status"><span>{error ? '报价记录和金额仍保留，可关闭预览后继续查看。' : status}</span>{!standalone && separateUrl && <a href={separateUrl} target="_blank" rel="noopener noreferrer">独立打开<ExternalLink size={11} /></a>}</div>
      <p className="bpv-geometry-note">{mode === 'single' ? '单件视图展示一个实例，复合实例保留其全部实体；不将重复装配数量叠成一件。' : '总装定位仅使用本行来源 STEP 的原有装配坐标，不拼接其他文件。'}显示网格来自原生 CAD，未重新绘制造型。</p>
    </section><PartDetails data={data} line={lineSnapshot} orderSets={orderSets} customer={customer} /></div>
  </div>
  return standalone ? <section className="bpv bpv-standalone" data-testid="bq-part-standalone">{content}</section> : <dialog ref={dialog} className="bpv bpv-dialog" data-testid="bq-part-dialog" aria-labelledby="bq-part-title" onCancel={event => { event.preventDefault(); dismiss.current?.() }} onClick={event => { if (event.target === event.currentTarget) dismiss.current?.() }}>{content}</dialog>
}

export function BatchPartPreviewPage() {
  const [url, setUrl] = useState<string | null>(null), [error, setError] = useState(''), [loaded, setLoaded] = useState(false)
  const [customer, setCustomer] = useState(false)
  useEffect(() => {
    const preview = new URL(window.location.href).searchParams.get('preview'), safe = safePartPreviewUrl(preview)
    setCustomer(new URL(window.location.href).searchParams.get('customer') === '1')
    if (safe) setUrl(safe)
    else setError('预览链接无效，请从报价单中的零件预览入口打开。')
    setLoaded(true)
  }, [])
  const privatePart = url ? PRIVATE_API_PART.exec(url) : null
  const back = privatePart ? `/workbench/quotation/?scope=${url?.includes('/customer-upload-admin/') ? 'customer' : 'poland'}&id=${privatePart[1]}` : '/workbench/quotation/'
  return <main className="bpv-page"><header className="bpv-page-heading">{customer ? <span>零件三维预览</span> : <a href={back}><ArrowLeft size={15} />返回整单报价</a>}<span>好运来 · 原生零件预览</span></header>{url ? <BatchPartViewer previewUrl={url} standalone customer={customer} /> : <div className="bpv-page-empty" role="status">{loaded ? error : '正在打开零件预览…'}</div>}</main>
}
