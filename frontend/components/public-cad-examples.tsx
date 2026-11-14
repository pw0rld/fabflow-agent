'use client'

import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Box, Check, ChevronRight, Download, ExternalLink, FileText, Focus, Layers3, LoaderCircle, RotateCcw, ShieldCheck } from 'lucide-react'
import WillFlowBrand from '@/components/willflow-brand'
import './public-cad-examples.css'

const ROOT = '/workbench/public-examples/'
const IDS = ['motor-bracket', 'sheet-metal-enclosure', 'flanged-bushing'] as const
type ExampleId = typeof IDS[number]
type Tab = 'model' | 'bom' | 'quote'
type Summary = { id: ExampleId; title: string; category: string; description: string; filename: string;
  source_label: string; license: string; part_count: number; occurrence_count: number;
  detail_url: string; geometry_url: string; download_url: string }
type Part = { id: string; name: string; quantity: number; solid_ids: string[]; bbox_mm: number[];
  thickness_mm: number | null; volume_mm3: number; material_raw: string | null;
  estimated_net_weight_kg: string | null; material_assumption: string }
type QuoteItem = { part_id: string; quantity_total: number; unit_price: string; amount: string }
type Detail = { schema_version: 1; id: ExampleId; title: string; category: string; description: string;
  filename: string; units: 'mm'; geometry_url: string; download_url: string;
  source: { repository: string; url: string; commit: string; sha256: string; filename: string;
    license: string; license_url: string; attribution: string; changes: string };
  stats: { part_count: number; occurrence_count: number; solid_count: number }; parts: Part[];
  quotation: { order_sets: number; currency: 'CNY'; subtotal: string; markup_percent: string; total: string;
    unit_per_set: string; status: 'illustrative'; items: QuoteItem[]; assumptions: string[] }; prepared_at: string }
type Solid = { id: string; name?: string; mesh: { positions: number[]; indices: number[] } }
type Group = { id: string; name: string; solid_ids: string[] }
type Geometry = { source_sha256: string; output_unit: 'mm'; solids: Solid[]; groups: Group[] }
type Prepared = { detail: Detail; geometry: Geometry }
type NativeViewer = { dispose(): void; reset(): void; select(id: string | null): void; isolate(id: string | null): void }
const idAllowed = (id: unknown): id is ExampleId => typeof id === 'string' && IDS.includes(id as ExampleId)
const asset = (id: ExampleId, file: 'example.json' | 'geometry.json' | 'source.step') => ROOT + id + '/' + file
const count = (value: unknown, maximum = 10000): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0 && value <= maximum
const decimal = (value: unknown): value is string => typeof value === 'string' && /^\d{1,16}(?:\.\d{1,12})?$/.test(value) && Number.isFinite(Number(value))
const finiteMeasure = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1e15
const text = (value: unknown, maximum = 1000): value is string => typeof value === 'string' && value.length <= maximum
const number = (value: number | string) => Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const money = (value: string) => '¥' + number(value)

async function readJson(url: string, signal: AbortSignal, maximum: number): Promise<unknown> {
  const allowed = url === ROOT + 'catalog.json' || IDS.some(id => url === asset(id, 'example.json') || url === asset(id, 'geometry.json'))
  if (!allowed) throw new Error('示例资料地址无效。')
  const response = await fetch(url, { signal, credentials: 'omit', cache: 'no-cache', redirect: 'error' })
  if (!response.ok) throw new Error(response.status === 404 ? '公开示例尚未准备好，请稍后重新载入。' : '示例资料暂未载入，请重试。')
  const length = response.headers.get('Content-Length')
  if (length && (!/^\d+$/.test(length) || Number(length) > maximum)) throw new Error('示例资料超出显示范围。')
  const value = await response.text()
  if (value.length > maximum) throw new Error('示例资料超出显示范围。')
  return JSON.parse(value)
}

function catalogPayload(value: unknown): Summary[] {
  const catalog = value as { schema_version?: unknown; examples?: Summary[] }
  if (!catalog || catalog.schema_version !== 1 || !Array.isArray(catalog.examples) || !catalog.examples.length || catalog.examples.length > IDS.length) throw new Error('公开示例目录暂不可读。')
  const seen = new Set<string>()
  for (const entry of catalog.examples) {
    if (!entry || !idAllowed(entry.id) || seen.has(entry.id) || !text(entry.title, 120) || !text(entry.category, 120)
        || !text(entry.description) || !text(entry.filename, 240) || !text(entry.source_label, 200) || !text(entry.license, 100)
        || !count(entry.part_count) || !count(entry.occurrence_count) || entry.detail_url !== asset(entry.id, 'example.json')
        || entry.geometry_url !== asset(entry.id, 'geometry.json') || entry.download_url !== asset(entry.id, 'source.step')) throw new Error('公开示例目录校验未完成，请重试。')
    seen.add(entry.id)
  }
  return [...catalog.examples].sort((a, b) => IDS.indexOf(a.id) - IDS.indexOf(b.id))
}

function detailPayload(value: unknown, id: ExampleId): Detail {
  const d = value as Detail
  if (!d || d.schema_version !== 1 || d.id !== id || d.units !== 'mm' || !text(d.title, 120) || !text(d.filename, 240)
      || !text(d.description) || d.geometry_url !== asset(id, 'geometry.json') || d.download_url !== asset(id, 'source.step')
      || !d.source || !text(d.source.repository, 200) || !text(d.source.attribution, 2000) || !text(d.source.changes, 2000)
      || !text(d.source.url, 2048) || !text(d.source.license_url, 2048) || !text(d.source.license, 100) || !text(d.source.filename, 240)
      || !/^[a-f0-9]{40}$/.test(d.source.commit) || !/^[a-f0-9]{64}$/.test(d.source.sha256)
      || !d.stats || !count(d.stats.part_count) || !count(d.stats.occurrence_count) || !count(d.stats.solid_count)
      || !Array.isArray(d.parts) || d.parts.length !== d.stats.part_count) throw new Error('示例参数与当前模型不一致，请重新载入。')
  const partIds = new Set<string>(), solids = new Set<string>()
  for (const part of d.parts) {
    if (!part || !/^P\d{4,8}$/.test(part.id) || partIds.has(part.id) || !text(part.name, 512) || !count(part.quantity)
        || !Array.isArray(part.solid_ids) || !part.solid_ids.length || part.solid_ids.length > 10000
        || !Array.isArray(part.bbox_mm) || part.bbox_mm.length !== 3 || !part.bbox_mm.every(finiteMeasure)
        || !finiteMeasure(part.volume_mm3) || part.thickness_mm !== null && !finiteMeasure(part.thickness_mm)
        || part.material_raw !== null && !text(part.material_raw, 1000) || !text(part.material_assumption, 1000)
        || part.estimated_net_weight_kg !== null && !decimal(part.estimated_net_weight_kg)) throw new Error('示例零件参数尚未完整载入。')
    for (const solidId of part.solid_ids) {
      if (typeof solidId !== 'string' || !/^S\d{4,8}$/.test(solidId) || solids.has(solidId)) throw new Error('示例实体归属无法核对。')
      solids.add(solidId)
    }
    partIds.add(part.id)
  }
  if (solids.size !== d.stats.solid_count) throw new Error('示例实体数量无法核对。')
  const q = d.quotation
  if (!q || q.status !== 'illustrative' || q.currency !== 'CNY' || !count(q.order_sets, 100000)
      || !decimal(q.subtotal) || !decimal(q.markup_percent) || !decimal(q.total) || !decimal(q.unit_per_set)
      || !Array.isArray(q.items) || q.items.length !== d.parts.length
      || !Array.isArray(q.assumptions) || q.assumptions.length > 30 || !q.assumptions.every(item => text(item, 2000))) throw new Error('示例报价尚未完整载入。')
  const priced = new Set<string>()
  for (const item of q.items) {
    if (!item || !partIds.has(item.part_id) || priced.has(item.part_id) || !count(item.quantity_total, 10000000)
        || !decimal(item.unit_price) || !decimal(item.amount)) throw new Error('示例报价与零件不一致。')
    priced.add(item.part_id)
  }
  return d
}

function geometryPayload(value: unknown, detail: Detail): Geometry {
  const g = value as Geometry
  if (!g || g.source_sha256 !== detail.source.sha256 || g.output_unit !== 'mm' || !Array.isArray(g.solids)
      || g.solids.length !== detail.stats.solid_count || !Array.isArray(g.groups) || g.groups.length !== detail.parts.length) throw new Error('示例显示几何与原件不一致。')
  const solids = new Set<string>(), groups = new Set<string>()
  for (const solid of g.solids) {
    if (!solid || !/^S\d{4,8}$/.test(solid.id) || solids.has(solid.id) || !Array.isArray(solid.mesh?.positions) || !Array.isArray(solid.mesh?.indices)) throw new Error('示例显示几何不完整。')
    solids.add(solid.id)
  }
  for (const group of g.groups) {
    if (!group) throw new Error('示例显示分组不完整。')
    const part = detail.parts.find(part => part.id === group.id)
    if (!part || groups.has(group.id) || !Array.isArray(group.solid_ids) || group.solid_ids.length !== part.solid_ids.length
        || group.solid_ids.some((solidId, index) => solidId !== part.solid_ids[index] || !solids.has(solidId))) throw new Error('示例显示分组与 BOM 不一致。')
    groups.add(group.id)
  }
  return g
}

function externalUrl(value: string): string | null {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && !url.username && !url.password
      && ['github.com', 'raw.githubusercontent.com', 'creativecommons.org', 'ohwr.org', 'cern-ohl.web.cern.ch', 'opensource.org'].includes(url.hostname) ? url.href : null
  } catch { return null }
}

function ModelView({ data, selected, isolated, onSelect, onIsolation }: {
  data: Prepared; selected: string | null; isolated: boolean; onSelect(id: string | null): void; onIsolation(value: boolean): void;
}) {
  const host = useRef<HTMLDivElement>(null), instance = useRef<NativeViewer | null>(null), selectedRef = useRef(selected), isolatedRef = useRef(isolated)
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading'), [error, setError] = useState(''), [attempt, setAttempt] = useState(0)
  selectedRef.current = selected; isolatedRef.current = isolated
  const selectRef = useRef(onSelect), isolationRef = useRef(onIsolation)
  selectRef.current = onSelect; isolationRef.current = onIsolation
  useEffect(() => {
    setState('loading'); setError('')
    const abort = new AbortController(); let viewer: NativeViewer | null = null, canvas: HTMLCanvasElement | null = null
    const onLost = () => { if (!abort.signal.aborted) { viewer?.dispose(); instance.current = null; setState('error'); setError('三维显示已中断，请重新载入。') } }
    void (async () => {
      try {
        const moduleUrl = '/workbench/cad-preview/step.js'
        const module = await import(/* webpackIgnore: true */ moduleUrl)
        if (abort.signal.aborted || !host.current) return
        viewer = await module.mountGeometry({ element: host.current, geometry: data.geometry, groups: data.geometry.groups,
          upAxis: 'z', componentColors: true, signal: abort.signal,
          onSelectionChange: (id: string | null) => {
            if (!abort.signal.aborted) selectRef.current(data.detail.parts.some(part => part.id === id) ? id : null)
          } })
        if (abort.signal.aborted) { viewer?.dispose(); return }
        canvas = host.current?.querySelector('canvas') || null
        if (!viewer || !canvas) throw new Error('Viewer is not ready.')
        instance.current = viewer
        viewer?.select(selectedRef.current)
        if (isolatedRef.current && selectedRef.current) viewer?.isolate(selectedRef.current)
        canvas?.addEventListener('webglcontextlost', onLost)
        setState('ready')
      } catch { viewer?.dispose(); if (instance.current === viewer) instance.current = null; if (!abort.signal.aborted) { setState('error'); setError('模型显示未完成，请重新载入；原始 STEP 仍可下载。') } }
    })()
    return () => { abort.abort(); canvas?.removeEventListener('webglcontextlost', onLost); viewer?.dispose(); if (instance.current === viewer) instance.current = null }
  }, [data, attempt])
  useEffect(() => {
    if (!instance.current || state !== 'ready') return
    instance.current.select(selected)
    instance.current.isolate(isolated && selected ? selected : null)
  }, [selected, isolated, state])
  return <div className="pce-model">
    <div className="pce-model-toolbar"><span><i/>{isolated ? '单件视图' : '原始装配位置'}</span><div><button type="button" data-testid="public-example-isolate" disabled={!selected || state !== 'ready'} className={isolated ? 'active' : ''} onClick={() => isolationRef.current(!isolated)}><Focus size={15}/>{isolated ? '返回总装' : '单独查看'}</button><button type="button" data-testid="public-example-reset" disabled={state !== 'ready'} onClick={() => instance.current?.reset()}><RotateCcw size={14}/>复位视角</button></div></div>
    <div className="pce-canvas" ref={host} data-testid="public-example-viewer" data-state={state}>
      {state === 'loading' && <div className="pce-stage-message" role="status"><LoaderCircle className="pce-spin" size={25}/><strong>正在载入原生模型</strong><span>公开 STEP 已预先解析，正在准备三维显示。</span></div>}
      {state === 'error' && <div className="pce-stage-message pce-error" role="alert"><Box size={28}/><strong>三维显示暂未完成</strong><span>{error}</span><button type="button" className="pce-secondary" onClick={() => setAttempt(value => value + 1)}>重新载入模型</button></div>}
    </div><div className="pce-model-note"><span>拖动旋转 · 滚轮缩放 · 点击部件查看参数</span><span>毫米 · 原生几何</span></div>
  </div>
}

function PartFacts({ part }: { part: Part }) {
  return <div className="pce-facts" data-testid="public-example-part-facts"><div><span>成品包络尺寸</span><strong>{part.bbox_mm.map(number).join(' × ')}<small>mm</small></strong></div>
    {part.thickness_mm !== null && <div><span>几何识别板／壁厚</span><strong>{number(part.thickness_mm)}<small>mm</small></strong></div>}
    <div><span>原生实体体积</span><strong>{number(part.volume_mm3)}<small>mm³ / 件</small></strong></div>
    {part.material_raw && <div><span>CAD 材料原文</span><strong>{part.material_raw}</strong></div>}
    {part.estimated_net_weight_kg !== null && <div><span>单件预估净重</span><strong>{number(part.estimated_net_weight_kg)}<small>kg</small></strong><p>按“{part.material_assumption}”估重</p></div>}
    <p className="pce-facts-note">尺寸、体积来自原生 CAD；估重与报价采用条件单独列明。</p>
  </div>
}

export default function PublicCadExamples() {
  const [catalog, setCatalog] = useState<Summary[]>([]), [selected, setSelected] = useState<ExampleId>('motor-bracket')
  const [catalogLoading, setCatalogLoading] = useState(true), [catalogError, setCatalogError] = useState(''), [catalogTick, setCatalogTick] = useState(0)
  const [prepared, setPrepared] = useState<Prepared | null>(null), [loading, setLoading] = useState(false), [error, setError] = useState(''), [tick, setTick] = useState(0)
  const [tab, setTab] = useState<Tab>('model'), [partId, setPartId] = useState<string | null>(null), [isolated, setIsolated] = useState(false)
  const workspace = useRef<HTMLElement>(null)
  useEffect(() => {
    const abort = new AbortController(); setCatalogLoading(true); setCatalogError('')
    void readJson(ROOT + 'catalog.json', abort.signal, 128 * 1024).then(value => {
      const entries = catalogPayload(value)
      if (!abort.signal.aborted) { setCatalog(entries); setSelected(previous => entries.some(item => item.id === previous) ? previous : entries[0].id) }
    }).catch(failure => { if (!abort.signal.aborted) setCatalogError(failure instanceof Error ? failure.message : '示例目录暂未载入。') })
      .finally(() => { if (!abort.signal.aborted) setCatalogLoading(false) })
    return () => abort.abort()
  }, [catalogTick])
  useEffect(() => {
    if (!catalog.some(item => item.id === selected)) return
    const abort = new AbortController(); setPrepared(null); setLoading(true); setError(''); setPartId(null); setIsolated(false)
    void (async () => {
      try {
        const detail = detailPayload(await readJson(asset(selected, 'example.json'), abort.signal, 512 * 1024), selected)
        const geometry = geometryPayload(await readJson(asset(selected, 'geometry.json'), abort.signal, 24 * 1024 * 1024), detail)
        if (!abort.signal.aborted) setPrepared({ detail, geometry })
      } catch (failure) { if (!abort.signal.aborted) setError(failure instanceof Error ? failure.message : '示例暂未载入，请重试。') }
      finally { if (!abort.signal.aborted) setLoading(false) }
    })()
    return () => abort.abort()
  }, [selected, catalog, tick])
  const current = prepared?.detail.id === selected ? prepared : null, detail = current?.detail
  const selectedPart = detail?.parts.find(part => part.id === partId) || null
  const sourceUrl = detail ? externalUrl(detail.source.url) : null, licenseUrl = detail ? externalUrl(detail.source.license_url) : null
  const quoteItems = new Map<string, QuoteItem>(detail?.quotation.items.map(item => [item.part_id, item] as const) || [])
  function chooseExample(id: ExampleId) { setSelected(id); setTab('model'); setPartId(null); setIsolated(false) }
  function selectPart(id: string | null) { setPartId(id); if (!id) setIsolated(false) }
  return <main className="pce" data-testid="public-examples-page" data-example-id={selected}>
    <header className="pce-header"><WillFlowBrand className="pce-willflow-brand"/><div className="pce-header-actions"><span className="pce-public"><ShieldCheck size={15}/>CAD 体验空间</span><a className="pce-home-link" href="https://willflowai.com/"><ExternalLink size={14}/><span>返回官网</span></a><a className="pce-primary" href="/try/?mode=upload" data-testid="public-example-upload-own">用我的 STEP 体验<ArrowRight size={16}/></a></div></header>
    <div className="pce-shell"><section className="pce-hero"><div><span className="pce-eyebrow"><i/>WILLFLOW.AI · CAD WORKSPACE</span><h1>从一个模型，<em>看见拆件与报价。</em></h1><p>选择一份常见公开模型，查看原生几何、零件 BOM 与模拟报价。</p></div><div className="pce-hero-note"><Check size={15}/><span>无需上传 · 不占体验次数<small>公开样例预先解析，随时查看</small></span></div></section>
      <section className="pce-catalog" aria-label="选择公开 CAD 示例" data-testid="public-example-catalog">
        {catalogLoading ? <div className="pce-catalog-message" role="status"><LoaderCircle size={20} className="pce-spin"/>正在载入公开示例</div> : catalogError ? <div className="pce-catalog-message pce-error" role="alert"><span>{catalogError}</span><button type="button" className="pce-secondary" onClick={() => setCatalogTick(value => value + 1)}>重新载入目录</button></div> : catalog.map((item, index) => <button type="button" className={`pce-sample${item.id === selected ? ' selected' : ''}`} key={item.id} data-testid={`public-example-card-${item.id}`} aria-pressed={item.id === selected} onClick={() => chooseExample(item.id)}><span className="pce-sample-icon">{item.id === 'sheet-metal-enclosure' ? <Layers3 size={29}/> : item.id === 'motor-bracket' ? <Focus size={29}/> : <Box size={29}/>}</span><div><small>0{index + 1} / {item.category}</small><h2>{item.title}</h2><p>{item.description}</p><span>{item.part_count} 种零件<i/> {item.occurrence_count} 件 / 套</span></div><span className="pce-sample-check">{item.id === selected ? <Check size={16}/> : <ChevronRight size={16}/>}</span></button>)}
      </section>
      <section className="pce-workspace" ref={workspace} aria-label="公开样例结果">
        <div className="pce-workspace-top"><div><span className="pce-tag">公开示例 · 真实 CAD</span><h2>{detail?.title || catalog.find(item => item.id === selected)?.title || '公开模型'}</h2><p>{detail?.filename || catalog.find(item => item.id === selected)?.filename || '正在读取示例'}</p></div><a className="pce-secondary" data-testid="public-example-source-download" href={asset(selected, 'source.step')} download><Download size={15}/>原始 STEP</a></div>
        <nav className="pce-tabs" aria-label="示例流程">{([{ id: 'model', label: '原生模型', icon: Box }, { id: 'bom', label: '拆件与 BOM', icon: Layers3 }, { id: 'quote', label: '模拟报价', icon: FileText }] as const).map((item, index) => <button type="button" key={item.id} className={tab === item.id ? 'active' : ''} aria-current={tab === item.id ? 'step' : undefined} data-testid={`public-example-tab-${item.id}`} onClick={() => setTab(item.id)}><b>0{index + 1}</b><item.icon size={17}/><span>{item.label}</span>{item.id === 'bom' && detail && <i>{detail.stats.part_count}</i>}</button>)}</nav>
        {loading || catalogLoading ? <div className="pce-loading" role="status"><LoaderCircle size={27} className="pce-spin"/><strong>正在读取公开示例</strong><p>载入已保存的原生几何与参数。</p></div> : error ? <div className="pce-loading pce-error" role="alert"><Box size={30}/><strong>示例资料暂未载入</strong><p>{error}</p><button type="button" className="pce-secondary" onClick={() => setTick(value => value + 1)}>重新载入示例</button></div> : current && detail ? <>
          {tab === 'model' && <div className="pce-model-layout"><aside className="pce-parts-list"><div><Layers3 size={16}/><strong>原生零件</strong><span>{detail.parts.length}</span></div><button type="button" className={!partId ? 'selected pce-all-parts' : 'pce-all-parts'} onClick={() => selectPart(null)} data-testid="public-example-all-parts"><Box size={18}/><span>查看整体模型<small>{detail.stats.solid_count} 个原生实体</small></span></button>{detail.parts.map(part => <button type="button" key={part.id} className={partId === part.id ? 'selected' : ''} onClick={() => selectPart(part.id)} data-testid={`public-example-part-${part.id}`}><span className="pce-part-dot"/><span><strong>{part.name}</strong><small>{part.id} · {part.quantity} 件 / 套</small></span><ChevronRight size={13}/></button>)}</aside><div className="pce-model-center"><ModelView data={current} selected={partId} isolated={isolated} onSelect={selectPart} onIsolation={setIsolated}/></div><aside className="pce-parameters"><span className="pce-eyebrow">CAD PARAMETERS</span><h3>{selectedPart ? selectedPart.name : '模型概览'}</h3><span className="pce-native-badge">原生 CAD 量测</span>{selectedPart ? <PartFacts part={selectedPart}/> : <><dl className="pce-overview"><div><dt>零件种类</dt><dd>{detail.stats.part_count}<small>种</small></dd></div><div><dt>每套零件</dt><dd>{detail.stats.occurrence_count}<small>件</small></dd></div><div><dt>原生实体</dt><dd>{detail.stats.solid_count}<small>个</small></dd></div><div><dt>模型单位</dt><dd>mm</dd></div></dl><div className="pce-select-hint"><Focus size={23}/><p>点击模型或左侧零件，查看尺寸、体积与预估重量。</p></div></>}<button type="button" className="pce-text-button" onClick={() => setTab('bom')}>查看完整 BOM<ArrowRight size={14}/></button></aside></div>}
          {tab === 'bom' && <div className="pce-bom" data-testid="public-example-bom"><div className="pce-section-heading"><div><h3>从原始 CAD 整理零件清单</h3><p>按原生零件定义整理，重复实例保留数量；几何与计价采用条件分开展示。</p></div><span>{detail.stats.part_count} 种 · {detail.stats.occurrence_count} 件 / 套</span></div><div className="pce-table-scroll"><table><thead><tr><th>零件 / 来源</th><th>每套数量</th><th>成品包络尺寸（mm）</th><th>板／壁厚（mm）</th><th>单件估重（kg）</th><th>报价采用材料</th><th/></tr></thead><tbody>{detail.parts.map(part => <tr key={part.id}><td><strong>{part.name}</strong><small>{part.id}</small></td><td>{part.quantity}</td><td>{part.bbox_mm.map(number).join(' × ')}</td><td>{part.thickness_mm === null ? <span className="pce-muted">未列入</span> : number(part.thickness_mm)}</td><td>{part.estimated_net_weight_kg === null ? <span className="pce-muted">未列入</span> : number(part.estimated_net_weight_kg)}</td><td>{part.material_assumption}<small>示例假设</small></td><td><button type="button" className="pce-text-button" onClick={() => { selectPart(part.id); setTab('model') }}>查看 3D<ArrowRight size={13}/></button></td></tr>)}</tbody></table></div><p className="pce-table-note">包络尺寸为成品几何尺寸；估重采用列明的材料密度，材料与工艺仍需工程核对。</p></div>}
          {tab === 'quote' && <div className="pce-quote" data-testid="public-example-quote"><div className="pce-quote-hero"><div><span>模拟报价 · {detail.quotation.order_sets} 套</span><strong data-testid="public-example-quote-total">{money(detail.quotation.total)}</strong><p>每套 {money(detail.quotation.unit_per_set)}<i/>人民币 · 示例计价</p></div><div className="pce-quote-label"><FileText size={32}/><strong>从零件，到一份报价草稿。</strong><p>展示 CAD 参数如何进入计价流程；<br/>采用价为示例假设，非采购成交价。</p></div></div><div className="pce-table-scroll"><table><thead><tr><th>序号</th><th>零件名称</th><th>{detail.quotation.order_sets} 套数量</th><th>报价单价（元）</th><th>金额（元）</th></tr></thead><tbody>{detail.parts.map((part, index) => { const item = quoteItems.get(part.id)!; return <tr key={part.id}><td>{String(index + 1).padStart(3, '0')}</td><td><strong>{part.name}</strong><small>{part.id}</small></td><td>{item.quantity_total}</td><td>{number(item.unit_price)}</td><td className="pce-amount">{number(item.amount)}</td></tr> })}</tbody></table></div><details className="pce-assumptions"><summary>查看示例计价条件<span>{detail.quotation.assumptions.length} 项</span></summary><ul>{detail.quotation.assumptions.map((assumption, index) => <li key={index}>{assumption}</li>)}</ul></details></div>}
          <details className="pce-source" data-testid="public-example-source"><summary><ShieldCheck size={14}/><span>{detail.source.repository} · {detail.source.license}</span><span>查看公开来源与许可</span></summary><div><p>{detail.source.attribution}</p><p>{detail.source.changes}</p><p>原件：{detail.source.filename} · 固定版本 {detail.source.commit.slice(0, 12)}</p><small>SHA-256：{detail.source.sha256}</small><div>{sourceUrl && <a href={sourceUrl} target="_blank" rel="noreferrer">原作者文件<ExternalLink size={13}/></a>}{licenseUrl && <a href={licenseUrl} target="_blank" rel="noreferrer">许可条款<ExternalLink size={13}/></a>}</div></div></details>
        </> : null}
      </section>
      <section className="pce-own-model"><div><span className="pce-eyebrow">TRY IT WITH YOUR PARTS</span><h2>换成你的模型，看看结果。</h2><p>填写企业资料后上传一份 STEP，体验原生拆件、BOM 整理与报价草稿。</p></div><a href="/try/?mode=upload" className="pce-primary">上传我的 STEP<ArrowRight size={17}/></a></section>
      <footer className="pce-footer"><span>公开模型与原作者许可随样例列明。</span><span>意志流动 WILLFLOW.AI · CAD 拆图与报价体验</span></footer>
    </div>
  </main>
}
