'use client'

import { useEffect, useRef, useState } from 'react'
import { Box, ChevronRight, Download, FileText, Layers3, LoaderCircle, RefreshCw, Ruler, Scale, ScanLine } from 'lucide-react'
import BatchPartViewer, { safePartPreviewUrl } from '@/components/batch-part-viewer'
import SourceAssemblyPreview from '@/components/source-assembly-preview'
import ProcessQuotePanel, { QuoteAreaCrosscheck, quoteQuantityFieldLabel, quoteQuantityFields, quoteQuantityLabel, quoteQuantityState, quoteQuantityValue, type QuoteQuantityEvidence } from '@/components/process-quote-panel'
import { estimatedWeight, quotationCount, quotationNumber } from '@/lib/batch-display-format'
import { savedWeightDisplay, type CustomerWeightAssumptions, type CustomerWeightSummary, type MaterialRequirementHint } from '@/lib/customer-weight-summary'
import './customer-manufacturing-panel.css'

type ManufacturingSource = 'customer' | 'staff'
const apiFor = (source: ManufacturingSource) => source === 'staff' ? '/ortho2cad-test/customer-upload-admin/api/quote-batches' : '/workbench/customer/api/orders'
type TechnicalProcess = {
  id: string; name: string; kind: string; status: string;
  quantity_per_piece: string | number | null; quantity_total: string | number | null;
  unit: string; basis: string; details?: Record<string, unknown>;
}
type WeightEvidence = {
  native_volume_mm3: string | null; volume_scope: 'per_piece'; volume_source: 'saved_native_volume' | 'unavailable';
  volume_status?: 'native' | 'low_confidence' | 'proxy' | 'unavailable';
  dimensions_mm: [string, string, string] | null; dimensions_source: 'saved_native_evidence' | 'cached_native_geometry' | 'unavailable';
  thickness_mm: string | null; thickness_status: 'candidate' | 'conflict' | 'unresolved' | 'not_applicable';
  thickness_source: 'saved_native_evidence' | 'cached_native_geometry' | 'unavailable'; density_kg_m3: string | null;
  adopted_material_label: string | null; cad_material_text: string | null; material_source: CustomerWeightAssumptions['source'];
  material_description: string; includes_packaging: false; role: 'cross_check';
  material_requirement_hint?: MaterialRequirementHint | null;
  measured_thickness_candidates_mm?: string[];
}
type TechnicalPart = {
  id: string; part_name: string; source: string; quantity_per_set: number | null; quantity_total: number | null;
  material: { text: string; source: 'cad' | 'estimate' | 'unknown'; adopted: boolean };
  thickness_mm: string | null; dimensions_mm: number[] | null;
  estimated_net_weight_kg: string | null; estimated_total_net_weight_kg: string | null;
  estimated_set_net_weight_kg?: string | null; weight_assumptions?: CustomerWeightAssumptions;
  native_volume_mm3?: string | null; weight_evidence?: WeightEvidence;
  geometry_url: string | null; thumbnail_url: string | null;
  step_url?: string | null;
  quotation_quantities?: QuoteQuantityEvidence;
  processes: TechnicalProcess[]; blockers: string[];
  laser: { status: 'ready' | 'unavailable'; svg_url: string | null; dxf_url: string | null };
  bends: { id: string; angle_deg: number; radius_mm: number; length_mm: number; status: 'pending_review' }[];
  weight_role: 'cross_check';
}
type ManufacturingManifest = {
  batch_id: string; run_id: string; status: string; parts: TechnicalPart[];
  summary: { parts_count: number; quantity_total: number; estimated_parts: number; blocked_parts: number };
  weight_summary?: CustomerWeightSummary;
}
const processStatuses: Record<string, string> = {
  estimated: '已按工程量估算', measured: '已量测', ready: '工程量已整理', candidate: '工艺候选',
  pending_review: '待工艺核对', pending: '待工艺核对', blocked: '待补工程依据',
  unresolved: '待工艺核对', not_applicable: '不适用', not_required: '无此工序',
  missing_quantity: '工程量待确认', missing_rate: '待工厂核价', unsupported: '待工程补图',
}
const processLabel = (status: string) => processStatuses[status] || '待工艺核对'
const quantity = (value: string | number | null, unit: string) => {
  if (value == null) return unit === '单' ? '按单计量' : '待确认'
  const integral = ['次', '件', '个', '处', '孔', '折'].includes(unit)
  return `${integral ? quotationCount(value, '待确认') : quotationNumber(value, '待确认')}${unit ? ` ${unit}` : ''}`
}
function safeAsset(value: string | null | undefined, batch: string, run: string, part: string, kind: 'thumbnail' | 'laser.svg' | 'laser.dxf' | 'step', source: ManufacturingSource): string | null {
  if (!value || !/^BATCH-[a-f0-9]{32}$/.test(batch) || !/^RUN-[a-f0-9]{32}$/.test(run) || !/^L[0-9]{4,8}$/.test(part)) return null
  const base = `${apiFor(source)}/${batch}/runs/${run}/`
  const expected = kind === 'step' ? `${base}customer-parts/${part}/step` : kind === 'thumbnail' ? `${base}customer-parts/${part}/thumbnail` : `${base}manufacturing/${part}/${kind}`
  return value === expected ? value : null
}

export default function CustomerManufacturingPanel({ batchId, runId, revision, initialPartId, source = 'customer', quoteCalculated = false, onQuoteApplied }: {
  batchId: string; runId: string; revision: number; initialPartId?: string; source?: ManufacturingSource;
  quoteCalculated?: boolean; onQuoteApplied?(value: unknown): void;
}) {
  const [data, setData] = useState<ManufacturingManifest | null>(null)
  const [selected, setSelected] = useState<string | null>(null), [tab, setTab] = useState<'part' | 'laser' | 'bends'>('part')
  const [loading, setLoading] = useState(true), [error, setError] = useState(''), [reload, setReload] = useState(0)
  const [viewer, setViewer] = useState<string | null>(null)
  const partDetails = useRef<HTMLElement>(null), focusPart = useRef(false)
  useEffect(() => {
    if (focusPart.current && partDetails.current) { focusPart.current = false; partDetails.current.scrollIntoView({ block: 'start' }) }
  }, [selected])
  const selectForPricing = (id: string) => {
    if (!data?.parts.some(item => item.id === id)) return
    setTab('part')
    if (selected === id) partDetails.current?.scrollIntoView({ block: 'start' })
    else { focusPart.current = true; setSelected(id) }
  }
  useEffect(() => {
    const abort = new AbortController()
    setLoading(true); setError(''); setViewer(null)
    void (async () => {
      try {
        if (!/^BATCH-[a-f0-9]{32}$/.test(batchId) || !/^RUN-[a-f0-9]{32}$/.test(runId) || (initialPartId !== undefined && !/^L[0-9]{4,8}$/.test(initialPartId))) throw new Error('当前拆件记录暂不可用，请刷新订单。')
        const response = await fetch(`${apiFor(source)}/${batchId}/runs/${runId}/manufacturing`, { signal: abort.signal, credentials: 'same-origin', cache: 'no-store', headers: { 'X-Workbench-Request': '1' } })
        if (!response.ok) throw new Error('本次工艺资料暂未载入，请重试。')
        const manifest = await response.json() as ManufacturingManifest
        if (manifest.batch_id !== batchId || manifest.run_id !== runId || !Array.isArray(manifest.parts)) throw new Error('工艺资料与当前订单不一致，请刷新订单。')
        if (initialPartId && !manifest.parts.some(part => part.id === initialPartId)) throw new Error('当前报价中没有这个零件，请返回报价单重新选择。')
        if (abort.signal.aborted) return
        setData(manifest); setSelected(previous => manifest.parts.some(part => part.id === previous) ? previous : initialPartId || manifest.parts[0]?.id || null)
      } catch (failure) { if (!abort.signal.aborted) setError(failure instanceof Error ? failure.message : '工艺资料暂未载入。') }
      finally { if (!abort.signal.aborted) setLoading(false) }
    })()
    return () => abort.abort()
  }, [batchId, runId, revision, reload, initialPartId, source])

  const part = data?.parts.find(item => item.id === selected) || null
  const weightSummary = data?.weight_summary, weights = savedWeightDisplay(weightSummary)
  const weightAssumptions = part?.weight_assumptions
  const weightEvidence = part?.weight_evidence
  const dimensions = weightEvidence ? weightEvidence.dimensions_mm : part?.dimensions_mm
  const thickness = weightEvidence ? weightEvidence.thickness_mm : part?.thickness_mm
  const thicknessCandidates = weightEvidence?.measured_thickness_candidates_mm || []
  const volume = weightEvidence ? weightEvidence.native_volume_mm3 : part?.native_volume_mm3
  const density = weightEvidence ? weightEvidence.density_kg_m3 : weightAssumptions?.density_kg_m3
  const adoptedMaterial = (weightEvidence ? weightEvidence.adopted_material_label : weightAssumptions?.adopted_material_label)
    || (part?.material?.source === 'estimate' ? part.material.text : null)
  const materialSource = weightEvidence?.material_source || weightAssumptions?.source
  const materialHint = weightEvidence?.material_requirement_hint || weightAssumptions?.requirement_hint
  const materialNote = materialSource === 'order_assumption' ? '本单材料假设'
    : materialSource === 'cad_candidate' ? 'CAD 材料候选，待核对'
    : materialSource === 'engineering_candidate' ? '工程材料候选，待核对'
    : materialSource === 'proxy_estimate' || materialSource === 'low_confidence' ? '粗估采用' : '采用依据待核对'
  const roughWeight = materialSource === 'proxy_estimate' || materialSource === 'low_confidence'
  const volumeStatus = weightEvidence?.volume_status || (materialSource === 'proxy_estimate' ? 'proxy' : roughWeight ? 'low_confidence' : volume != null ? 'native' : 'unavailable')
  const thumbnail = part ? safeAsset(part.thumbnail_url, batchId, runId, part.id, 'thumbnail', source) : null
  const laserSvg = part ? safeAsset(part.laser?.svg_url, batchId, runId, part.id, 'laser.svg', source) : null
  const laserDxf = part ? safeAsset(part.laser?.dxf_url, batchId, runId, part.id, 'laser.dxf', source) : null
  const nativeStep = part ? safeAsset(part.step_url, batchId, runId, part.id, 'step', source) : null
  const geometry = part && part.geometry_url === `${apiFor(source)}/${batchId}/runs/${runId}/customer-parts/${part.id}` ? safePartPreviewUrl(part.geometry_url) : null
  const hasLaser = part?.laser?.status === 'ready' && !!laserSvg
  const activeTab = tab === 'laser' && !hasLaser || tab === 'bends' && !part?.bends?.length ? 'part' : tab
  const quoteEvidence = part?.quotation_quantities
  const body = loading && !data ? <div className="cmp-loading" role="status"><LoaderCircle size={20} className="cup-spin" />正在整理零件与工艺依据</div> : error ? <div className="cmp-loading cmp-failure" role="status"><FileText size={24} /><p>{error}</p><button type="button" onClick={() => setReload(value => value + 1)}><RefreshCw size={13} />重试载入</button></div> : !part ? <div className="cmp-loading"><Layers3 size={25} /><p>暂未形成可核对的制造零件，请联系工厂检查原始装配。</p></div> : <>
    <SourceAssemblyPreview previewUrl={geometry} sourceName={part.source} active={!viewer} refreshKey={`${revision}:${reload}`} />
    <ProcessQuotePanel batchId={batchId} runId={runId} revision={revision} source={source} quoteCalculated={quoteCalculated} onApplied={onQuoteApplied} onSelectPart={selectForPricing} />
    <div className="cmp-overview" data-testid="customer-manufacturing-summary"><span><strong>{quotationCount(data?.summary.parts_count)}</strong>项零件</span><span><strong>{quotationCount(data?.summary.quantity_total)}</strong>件订单用量</span><span className={data?.summary.blocked_parts ? 'pending' : ''}>{data?.summary.blocked_parts ? `${quotationCount(data.summary.blocked_parts)} 项需工艺核对` : '逐件工程量已整理'}</span></div>
    <section className={`cmp-weight-summary ${weights.status}`} data-testid="customer-manufacturing-weight-summary" data-weight-status={weights.status} aria-label="全部零件预估重量汇总"><div className="cmp-weight-title"><span><Scale size={15} />全部零件重量汇总</span><small>{weights.status === 'complete' ? '已汇总' : weights.status === 'partial' ? '已知零件小计' : '未汇总'}</small></div><div className="cmp-weight-summary-grid"><div><span>{weights.setLabel}</span><strong data-testid="customer-manufacturing-set-weight">{estimatedWeight(weights.set, '未汇总')}</strong></div><div><span>{weights.totalLabel}</span><strong data-testid="customer-manufacturing-order-weight">{estimatedWeight(weights.total, '未汇总')}</strong>{weightSummary?.order_sets != null && <small>{quotationCount(weightSummary.order_sets)} 套</small>}</div></div><p data-testid="customer-manufacturing-weight-scope">{weights.status === 'partial' ? `另有 ${quotationCount(weightSummary?.missing_estimated_lines)} 项尚未估重，小计不代表完整订单总重。` : weights.status === 'unavailable' ? '当前记录尚未保存完整重量汇总。' : '按 CAD 体积与所用材料密度估算，不含包装。'}{(weightSummary?.low_confidence_lines || 0) > 0 && <span> 含 {quotationCount(weightSummary?.low_confidence_lines)} 项粗估，待工程核对。</span>}</p></section>
    <div className="cmp-workspace">
      <div className="cmp-parts" role="list" aria-label="拆件结果" data-testid="customer-parts-list">{data?.parts.map(item => {
        const thumb = safeAsset(item.thumbnail_url, batchId, runId, item.id, 'thumbnail', source)
        return <button type="button" key={item.id} className={`cmp-part-row${part.id === item.id ? ' selected' : ''}`} aria-pressed={part.id === item.id} onClick={() => { setSelected(item.id); setTab(item.laser?.status === 'ready' ? 'laser' : 'part') }} data-testid="customer-part-item" data-part-id={item.id}><span className="cmp-part-thumb">{thumb ? <img src={thumb} alt="" loading="lazy" /> : <Box size={24} strokeWidth={1.4} />}</span><span className="cmp-part-copy"><strong>{item.part_name || item.id}</strong><small>{item.id} · {quotationCount(item.quantity_total)} 件</small><span className={item.blockers?.length ? 'pending' : ''}>{item.blockers?.length ? '待工艺核对' : '工程量已整理'}</span></span><ChevronRight size={13} /></button>
      })}</div>
      <section ref={partDetails} className="cmp-part-detail" aria-label="当前零件与工艺" data-testid="customer-part-details" data-part-id={part.id}>
        <div className="cmp-part-heading"><div><span>{part.id} · 制造零件</span><h3 data-testid="customer-part-name">{part.part_name}</h3><p>{part.source}</p></div><strong>{quotationCount(part.quantity_total)}<small>件</small></strong></div>
        <div className={`cmp-part-weight${roughWeight ? ' rough' : ''}`} data-testid="customer-part-weight"><div><span><Scale size={12} />单件预估净重</span><strong data-testid="customer-part-unit-weight">{estimatedWeight(part.estimated_net_weight_kg)}</strong></div><div><span>该零件订单预估净重</span><strong data-testid="customer-part-order-weight">{estimatedWeight(part.estimated_total_net_weight_kg)}</strong><small>{quotationCount(part.quantity_total)} 件合计</small></div>{roughWeight && <small className="cmp-weight-caveat" data-testid="customer-part-weight-assumption">粗估，待工程核对</small>}{weightAssumptions?.source === 'order_assumption' && <small className="cmp-weight-caveat" data-testid="customer-part-weight-assumption">按本单材料假设估重</small>}</div>
        <section className="cmp-weight-facts" data-testid="customer-part-weight-facts" data-part-id={part.id} aria-label="单件重量的几何与材料依据">
          <div className="cmp-weight-fact dimensions" data-testid="customer-part-weight-dimensions"><span>成品包络尺寸</span><strong>{dimensions?.length === 3 ? `${dimensions.map(value => quotationNumber(value)).join(' × ')} mm` : '尚未量测'}</strong><small>装配坐标 · 非展开尺寸</small></div>
          <div className="cmp-weight-fact" data-testid="customer-part-weight-thickness"><span>板／壁厚</span><strong>{thickness != null ? `${quotationNumber(thickness)} mm` : thicknessCandidates.length ? `${thicknessCandidates.map(value => quotationNumber(value)).join(' / ')} mm` : weightEvidence?.thickness_status === 'not_applicable' ? '不适用' : '尚未量测'}</strong><small>{thickness != null ? 'CAD 量测候选' : thicknessCandidates.length ? '局部量测候选，未采用为统一板厚' : weightEvidence?.thickness_status === 'conflict' ? 'CAD 量测候选存在冲突' : '尚无 CAD 量测候选'}</small></div>
          <div className="cmp-weight-fact material" data-testid="customer-part-weight-adopted-material"><span>估重采用材料</span><strong>{adoptedMaterial || '采用材料未记录'}</strong><small>估算采用 · {materialNote}</small><small data-testid="customer-part-weight-density">{density != null ? `密度 ${quotationNumber(density)} kg/m³` : '密度未记录'}</small></div>
          <div className="cmp-weight-fact" data-testid="customer-part-weight-volume"><span>{volumeStatus === 'proxy' ? '单件体积依据 · 代理模型' : volumeStatus === 'low_confidence' ? '单件体积依据 · 粗估' : '单件原生体积'}</span><strong>{volume != null ? `${quotationNumber(volume)} mm³` : '未记录'}</strong><small>{volumeStatus === 'proxy' ? '已保存的代理模型体积，待核对' : volumeStatus === 'low_confidence' ? '已保存的粗估体积，待核对' : '单个零件体积，未乘订单件数'}</small></div>
        </section>
        {materialHint && <div className={`cmp-material-hint${materialHint.conflicts_with_adopted_material ? ' conflict' : ''}`} data-testid="customer-material-requirement"><strong>资料材料线索：{materialHint.label}</strong>{materialHint.conflicts_with_adopted_material && <p>与当前估重采用材料不一致，需核对牌号和密度；现有重量仍按所示采用条件估算。</p>}</div>}
        <div className="cmp-tabs" role="group" aria-label="查看工艺图"><button type="button" aria-pressed={activeTab === 'part'} onClick={() => setTab('part')} data-testid="customer-part-tab"><Box size={13} />原生零件</button><button type="button" aria-pressed={activeTab === 'laser'} disabled={!hasLaser} onClick={() => setTab('laser')} data-testid="customer-laser-tab"><ScanLine size={13} />切割轮廓</button><button type="button" aria-pressed={activeTab === 'bends'} disabled={!part.bends?.length} onClick={() => setTab('bends')} data-testid="customer-bend-tab"><Ruler size={13} />折弯候选{part.bends?.length ? ` ${part.bends.length}` : ''}</button></div>
        {activeTab === 'laser' && hasLaser ? <div className="cmp-drawing" data-testid="customer-laser-preview"><img src={laserSvg!} alt={`${part.part_name} 原生切割轮廓`} /><div><span>原生平板轮廓 · mm · 待工程复核</span>{laserDxf && <a href={laserDxf} download data-testid="customer-laser-download"><Download size={12} />下载 DXF</a>}</div></div> : activeTab === 'bends' ? <div className="cmp-bends" data-testid="customer-bend-table"><p>来自原生曲面的折弯候选；折弯方向、顺序及展开仍需工程确认。</p><div className="cmp-bend-scroll"><table><thead><tr><th>编号</th><th>角度</th><th>内半径</th><th>折弯长度</th></tr></thead><tbody>{part.bends.map(bend => <tr key={bend.id}><td>{bend.id}</td><td>{quotationNumber(bend.angle_deg)}°</td><td>{quotationNumber(bend.radius_mm)} mm</td><td>{quotationNumber(bend.length_mm)} mm</td></tr>)}</tbody></table></div></div> : <div className="cmp-native"><div className="cmp-native-visual">{thumbnail ? <img src={thumbnail} alt={`${part.part_name} 原生 CAD 零件`} /> : <Box size={48} strokeWidth={1.1} />}</div><div><span>从总装中的原生实体拆出</span>{geometry && <button type="button" onClick={() => setViewer(geometry)} data-testid="customer-open-part"><Box size={13} />查看单件 3D</button>}</div></div>}
        {nativeStep && <a className="cmp-step-download" href={nativeStep} download data-testid="customer-step-download"><Download size={13} />下载单件 STEP</a>}
        {quoteEvidence && <section className="cmp-quote-quantities" data-testid="customer-quote-quantities" data-line-id={part.id} data-quantity-status={quoteEvidence.status}><div className="cmp-quote-quantity-heading"><h4>报价工程量</h4><span className={quoteEvidence.status}>{quoteQuantityLabel(quoteEvidence.status)}</span></div><p>本次计价采用的工程量；暂估项目仍需工程核对，不作为生产展开或真实刀路。</p><dl>{quoteQuantityFields.map(field => { const state = quoteQuantityState(quoteEvidence, field.key); return <div key={field.key} data-quantity={field.key} data-status={state}><dt>{quoteQuantityFieldLabel(quoteEvidence, field.key, field.label)}</dt><dd>{quoteQuantityValue(quoteEvidence.quantities?.[field.key], field.unit, field.count)}<small className={state}>{quoteQuantityLabel(state)}</small></dd></div> })}</dl><QuoteAreaCrosscheck evidence={quoteEvidence} />{quoteEvidence.basis?.length > 0 && <details><summary>查看工程量依据</summary><ul>{quoteEvidence.basis.map((basis, index) => <li key={index}>{basis}</li>)}</ul></details>}</section>}
        <div className="cmp-process-heading"><h4>工艺与工程量</h4><span>作为分项核价依据</span></div>
        <div className="cmp-process-list" data-testid="customer-process-list">{part.processes?.length ? part.processes.map((process, index) => <article className="cmp-process" key={`${process.id}-${index}`} data-testid="customer-process-item" data-process-kind={process.kind}><div className="cmp-process-top"><h5><span>{String(index + 1).padStart(2, '0')}</span>{process.name}</h5><span className={['estimated', 'ready', 'measured'].includes(process.status) ? 'measured' : 'pending'}>{processLabel(process.status)}</span></div><div className="cmp-process-quantity"><span>单件<strong>{quantity(process.quantity_per_piece, process.unit)}</strong></span><span>订单合计<strong>{quantity(process.quantity_total, process.unit)}</strong></span></div><p>{process.basis || '具体加工要求待工程师确认。'}</p></article>) : <p className="cmp-no-process">尚未确定加工工序，等待工程核对。</p>}</div>
        {!!part.blockers?.length && <div className="cmp-blockers" data-testid="customer-engineering-blockers"><h4>需要工程师核对</h4><ul>{part.blockers.map((text, index) => <li key={index}>{text}</li>)}</ul></div>}
        <details className="cmp-material" data-testid="customer-material-weight"><summary><Scale size={14} />更多估重依据<ChevronRight size={13} /></summary>{(weightEvidence?.cad_material_text || weightAssumptions?.cad_material_text) && <div className="cmp-material-grid"><div><span>CAD 材料原文</span><strong>{weightEvidence?.cad_material_text || weightAssumptions?.cad_material_text}</strong><small>原资料记录，与估重采用材料分开保留。</small></div></div>}{(weightEvidence?.material_description || weightAssumptions?.description) && <p data-testid="customer-part-weight-basis">{weightEvidence?.material_description || weightAssumptions?.description}</p>}<p>体积换算为 m³ 后乘采用密度，得到单件预估净重；该零件订单重量按订单件数汇总。全部零件重量另行汇总，不含包装。</p></details>
      </section>
    </div>
  </>
  return <section className="cmp-panel" data-testid="customer-manufacturing" data-source={source}><div className="cmp-heading"><div><span>PARTS &amp; PROCESS</span><h2><Layers3 size={19} />拆件与工艺</h2><p>逐件查看制造零件、工艺图及加工工程量。</p></div>{!loading && <button type="button" onClick={() => setReload(value => value + 1)} aria-label="刷新拆件与工艺"><RefreshCw size={14} /></button>}</div>{body}{viewer && <BatchPartViewer key={viewer} previewUrl={viewer} customer={source === 'customer'} onClose={() => setViewer(null)} />}</section>
}
