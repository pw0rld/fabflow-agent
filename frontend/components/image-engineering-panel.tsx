'use client'

import { useEffect, useMemo, useState } from 'react'
import { ArrowLeft, ArrowRight, Check, Download, ExternalLink, FileImage, Info, Layers, Package, Ruler } from 'lucide-react'
import { buildImageEngineering, engineeringCsv, type EngineeringFeature, type EngineeringPart } from '../lib/image-engineering'
import { type BomEvidence, type ImageBomSource } from '../lib/image-bom-demo'
import './image-engineering-panel.css'

type Props = { source: ImageBomSource | null; filename: string; sample: boolean; disabled: boolean; onViewSource: () => void; onNext: () => void }
const IMAGE_PATH = '/ortho2cad-test/drawing-split/'
const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const categoryLabels: Record<string, string> = { identity: '身份与图号', material: '材料', thickness: '板厚', dimension: '尺寸与基准', hole: '孔', slot: '槽与窗口', bend: '折弯标注', weld: '焊接要求', surface: '表面处理', tolerance: '公差', other: '其他原始标注' }
const quantityUnit: Record<string, string> = { bend: '条折弯表记录', spot: '焊点', hole: '孔', piece: '次/件', m: 'm', m2: 'm²', hour: '小时' }

function safeAsset(value: unknown, source: ImageBomSource, sample: boolean): string | null {
  if (typeof value !== 'string' || !value || value.length > 700) return null
  try {
    const url = new URL(value, new URL(IMAGE_PATH, window.location.origin))
    if (url.origin !== location.origin || url.username || url.password || url.search || url.hash) return null
    const prefix = source.jobId ? `${IMAGE_PATH}api/jobs/${source.jobId}/files/` : sample ? `${IMAGE_PATH}assets/` : ''
    if (!prefix || !url.pathname.startsWith(prefix)) return null
    const name = decodeURIComponent(url.pathname.slice(prefix.length))
    return /^[a-zA-Z0-9._-]+$/.test(name) && name !== '.' && name !== '..' ? url.href : null
  } catch { return null }
}

export default function ImageEngineeringPanel({ source, filename, sample, disabled, onViewSource, onNext }: Props) {
  const profile = useMemo(() => source ? buildImageEngineering(source) : null, [source])
  const [partId, setPartId] = useState('')
  const [regionId, setRegionId] = useState('')
  const [focusEvidence, setFocusEvidence] = useState<BomEvidence | null>(null)
  const part = profile?.parts.find(item => item.partId === partId) || profile?.parts[0]
  const manifest = record(source?.manifest)
  const regions = (Array.isArray(manifest.regions) ? manifest.regions : []).map(record)
  const partRegions = regions.filter(region => region.part_id === part?.partId)
  const region = partRegions.find(item => item.id === regionId) || partRegions.find(item => item.type === 'flat_pattern') || partRegions.find(item => item.type === 'isometric') || partRegions[0]
  const preview = source ? safeAsset(region?.file, source, sample) : null

  useEffect(() => { setPartId(''); setRegionId(''); setFocusEvidence(null) }, [profile?.sourceFingerprint])
  useEffect(() => {
    const getState = () => ({ sourceKey: profile?.sourceFingerprint || null, profile, selectedPart: part?.partId || null, selectedRegion: region?.id || null, status: disabled ? 'blocked' : 'ready' })
    const target = window as typeof window & { HylImageEngineering?: { getState: typeof getState } }
    target.HylImageEngineering = { getState }
    return () => { delete target.HylImageEngineering }
  }, [profile, part, region, disabled])

  if (!source || !profile) return <div className="ieng-empty"><Layers size={28} /><strong>加工特征与工艺清单</strong><p>读取当前图纸的分区与参数后，这里会按零件整理加工信息。</p></div>
  function locate(evidence: BomEvidence) {
    if (!profile) return
    const owner = regions.find(item => item.id === evidence.regionId)
    if (typeof owner?.part_id === 'string' && profile.parts.some(item => item.partId === owner.part_id)) setPartId(owner.part_id)
    if (evidence.regionId) setRegionId(evidence.regionId)
    setFocusEvidence(evidence)
  }
  function evidenceItems(items: BomEvidence[]) {
    return items.length ? <div className="ieng-evidence">{items.map((item, index) => {
      const r = regions.find(region => region.id === item.regionId), href = safeAsset(r?.file, source!, sample)
      return <div key={`${item.regionId}-${index}`}><button type="button" onClick={() => locate(item)} disabled={disabled}><span>{item.rawText || '来源原文未提供'}</span><small>{typeof r?.title === 'string' ? r.title : '来源待定位'}<ArrowRight size={11} /></small></button>{href && <a href={href} target="_blank" rel="noopener noreferrer" aria-label="打开来源分图"><ExternalLink size={12} /></a>}</div>
    })}</div> : <p className="ieng-muted">本项没有明确的原图位置，须回图核对。</p>
  }
  function featureRows(features: EngineeringFeature[]) {
    return <div className="ieng-feature-list">{features.map(feature => <article key={feature.id} className={!feature.usable ? 'unusable' : ''} data-testid={`engineering-feature-${feature.id}`}><div><span className="ieng-category">{categoryLabels[feature.category] || '其他'}</span><strong>{feature.label}</strong><span className="ieng-candidate">{feature.sourceStatus === 'rejected' ? '已标记排除 · 保留原文' : !feature.usable ? '未归属或存疑 · 不参与计量' : '待核对'}</span></div><p>{feature.displayValue || '未提取到值'}{feature.unit ? ` ${feature.unit}` : ''}</p>{(feature.direction && !['unknown', 'not_applicable'].includes(feature.direction) || feature.datum || feature.feature) && <div className="ieng-feature-meta">{feature.direction && !['unknown', 'not_applicable'].includes(feature.direction) && <span>方向：{({ horizontal: '横向', vertical: '纵向', diameter: '直径', radial: '半径', angle: '角度' } as Record<string, string>)[feature.direction] || feature.direction}</span>}{feature.datum && <span>原文基准：{feature.datum}</span>}{feature.feature && <span>对应对象：{feature.feature}</span>}</div>}{evidenceItems(feature.evidence)}</article>)}</div>
  }
  function exportCsv() {
    if (disabled || !profile) return
    const url = URL.createObjectURL(new Blob([engineeringCsv(profile)], { type: 'text/csv;charset=utf-8' })), link = document.createElement('a')
    link.href = url; link.download = '加工特征与工艺清单-待核对.csv'; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  const visibleGroups = part ? (['hole', 'slot', 'dimension', 'tolerance', 'bend', 'weld', 'surface', 'material', 'thickness', 'identity', 'other'] as const).filter(category => part.groups[category].length) : []
  const unassignedCount = profile.unassigned.features.length + profile.unassigned.bends.length + profile.unassigned.processNotes.length

  return <section className={`ieng-panel${disabled ? ' ieng-disabled' : ''}`} data-testid="image-engineering-panel" aria-busy={disabled}>
    <div className="ieng-heading"><div><span>DRAWING TO PROCESS</span><h2>把图纸信息整理成加工依据</h2><p>查看零件的孔槽、折弯、焊接与后处理要求，每项可回到来源分图。</p></div><button type="button" className="unified-button" data-testid="image-export-engineering" disabled={disabled} onClick={exportCsv}><Download size={14} />下载特征与工艺</button></div>
    <div className="ieng-notice"><Info size={16} /><span>{sample ? '本例使用预设分区和整理记录。' : '本页整理当前任务已保存的识别结果。'} 图纸要求、推导数量和工艺建议均待工程核对，不代表已经形成机床程序或确定工时。</span></div>
    <div className="ieng-source-bar"><FileImage size={15} /><strong>{filename}</strong><span>{profile.parts.length} 个零件身份 · {profile.counts.parameters} 项参数 · {profile.counts.processNotes} 条工艺说明 · {profile.counts.bendRows} 行折弯表</span></div>
    {!!profile.parts.length && <nav className="ieng-part-tabs" aria-label="加工零件">{profile.parts.map((item, index) => <button type="button" key={item.partId} className={part?.partId === item.partId ? 'active' : ''} data-testid={`engineering-part-${item.partId}`} onClick={() => { setPartId(item.partId); setRegionId(''); setFocusEvidence(null) }}><Package size={14} /><span>{String(index + 1).padStart(2, '0')}</span>{item.name || '名称待核对'}</button>)}</nav>}
    {part ? <>
      <div className="ieng-overview"><div className="ieng-preview-card"><div className="ieng-card-top"><strong>{part.name || '当前零件'} · 来源视图</strong><span>原图裁片</span></div><div className="ieng-preview">{preview ? <img src={preview} alt={`${part.name} ${String(region?.title || '来源分图')}`} data-testid="engineering-source-preview" /> : <div><FileImage size={28} /><p>来源图片暂未定位</p></div>}</div><div className="ieng-region-tabs">{partRegions.map(r => <button type="button" key={String(r.id)} className={r.id === region?.id ? 'active' : ''} onClick={() => { setRegionId(String(r.id)); setFocusEvidence(null) }}>{String(r.title || r.type)}</button>)}</div>{focusEvidence && <div className="ieng-focus"><span>当前依据</span>{focusEvidence.rawText}</div>}{preview && <a className="ieng-original-link" href={preview} target="_blank" rel="noopener noreferrer">打开当前来源分图<ExternalLink size={12} /></a>}</div>
        <aside className="ieng-material-card"><div className="ieng-card-top"><strong>物料与用料依据</strong><Ruler size={16} /></div><MaterialSummary part={part} />{!!part.materialBasis.missing.length && <div className="ieng-missing"><strong>还缺哪些条件</strong>{part.materialBasis.missing.map((item, index) => <p key={index}><span />{item}</p>)}</div>}<p className="ieng-mass-note">展开图存在，不代表展开净面积已知。成型外形尺寸、图片像素和视图比例不用于直接计算采购重量。</p></aside></div>
      <section className="ieng-section"><div className="ieng-section-heading"><div><h3>加工特征与原始标注</h3><p>孔槽、尺寸、公差与工艺标注分别保存；同一零件的不同视图共同提供依据。</p></div><span>{part.features.length} 项记录</span></div>{visibleGroups.length ? <div className="ieng-groups">{visibleGroups.map(category => <details key={category} open={['hole', 'slot', 'dimension', 'weld'].includes(category)} data-testid={`engineering-group-${category}`}><summary><span>{categoryLabels[category]}</span><small>{part.groups[category].length} 项</small></summary>{featureRows(part.groups[category])}</details>)}</div> : <div className="ieng-empty small"><p>当前没有可归属的孔槽或尺寸标注，其他原文与缺项仍保留。</p></div>}</section>
      <section className="ieng-section" data-testid="engineering-bends"><div className="ieng-section-heading"><div><h3>折弯表逐行对应</h3><p>保留标签、方向、角度与内半径。表格条目数需要工程师核对后采用为计价数量。</p></div><span data-testid="engineering-bend-count">{part.bends.length} 行</span></div>{part.bends.length ? <div className="ieng-table-scroll"><table><thead><tr><th>标签</th><th>方向</th><th>角度</th><th>内半径</th><th>来源与状态</th></tr></thead><tbody>{part.bends.map(bend => <tr key={bend.id}><td><strong>{bend.tag || '未提供'}</strong></td><td>{bend.direction || '未提供'}</td><td>{bend.angleDeg === null ? '未提供' : `${bend.angleDeg}°`}</td><td>{bend.innerRadiusMm === null ? '未提供' : `${bend.innerRadiusMm} mm`}</td><td><span className="ieng-candidate">{bend.usable ? '候选 · 待核对' : bend.sourceStatus === 'rejected' || bend.sourceStatus === 'excluded' ? '已排除 · 不采用' : '信息不完整或存疑 · 不计量'}</span>{evidenceItems(bend.evidence)}</td></tr>)}</tbody></table></div> : <div className="ieng-empty small"><p>本轮没有取得可归属的折弯表记录；未按箱体外形推测折弯次数。</p></div>}</section>
      <section className="ieng-section" data-testid="engineering-processes"><div className="ieng-section-heading"><div><h3>工艺候选清单</h3><p>由已保存的图纸要求整理。清单顺序为展示分组，不代表生产工序顺序。</p></div><span>{part.processes.length} 项</span></div><div className="ieng-process-list">{part.processes.map(process => <article key={process.id} data-testid={`engineering-process-${process.kind}`}><div className="ieng-process-name"><span><Check size={14} /></span><div><h4>{process.title}</h4><small>{process.origin === 'drawing_requirement' ? '图纸要求 · 待采用' : '工艺建议 · 待采用'}</small></div></div><div className="ieng-process-detail"><p>{process.description}</p>{process.quantity ? <div className="ieng-quantity"><strong>{process.quantity.value} {quantityUnit[process.quantity.unit] || process.quantity.unit}</strong><span>{process.quantity.basis === 'table_rows' ? '按表格记录整理，尚未核定制造次数' : process.quantity.basis === 'derived' ? '依据原文推导 · 待核对' : '原文计量候选'}</span><p>{process.quantity.explanation}</p></div> : <p className="ieng-missing-quantity">计价量待补充，不填默认长度、次数或工时。</p>}{evidenceItems(process.evidence)}</div></article>)}</div>{!part.processes.length && <div className="ieng-empty small"><p>没有可归属的工艺要求，须回图核对。</p></div>}</section>
    </> : <div className="ieng-empty"><Package size={30} /><strong>尚未确认零件身份</strong><p>原始标注保留在下方未归属资料中，分图数量不会直接变成制造零件数量。</p></div>}
    {!!unassignedCount && <details className="ieng-unassigned" data-testid="engineering-unassigned"><summary>尚未明确属于哪个零件（{unassignedCount} 项）</summary><p>这些资料未复制到任一零件或成本行，须先核对来源归属。</p>{featureRows([...profile.unassigned.features, ...profile.unassigned.processNotes])}{profile.unassigned.bends.map(bend => <div className="ieng-unassigned-bend" key={bend.id}>折弯标签 {bend.tag || '未提供'} · {bend.direction || '方向未提供'} · {bend.angleDeg === null ? '角度未提供' : `${bend.angleDeg}°`} · {bend.innerRadiusMm === null ? '内半径未提供' : `内半径 ${bend.innerRadiusMm} mm`}<span className="ieng-candidate">{bend.sourceStatus === 'rejected' || bend.sourceStatus === 'excluded' ? '已排除 · 不采用' : '归属未定 · 不计量'}</span>{evidenceItems(bend.evidence)}</div>)}</details>}
    {!!profile.warnings.length && <details className="ieng-warnings"><summary>整理说明与待核对事项（{profile.warnings.length} 项）</summary>{profile.warnings.map((warning, index) => <p key={index}>{warning}</p>)}</details>}
    <div className="ieng-actions"><button type="button" className="unified-button" onClick={onViewSource}><ArrowLeft size={14} />图纸与参数</button><button type="button" className="unified-button primary" data-testid="engineering-next-bom" disabled={disabled} onClick={onNext}>下一步 · 物料 BOM<ArrowRight size={14} /></button></div>
  </section>
}

function MaterialSummary({ part }: { part: EngineeringPart }) {
  const basis = part.materialBasis
  const materials = basis.materialFeatures.map(item => `${item.displayValue}${item.unit ? ` ${item.unit}` : ''}`)
  return <div className="ieng-material-summary"><div><span>材料原文候选</span><strong>{materials.length ? materials.join(' / ') : '本轮未取得可归属的材料'}</strong></div><div><span>板厚原文候选</span><strong>{basis.thicknessFeatures.map(item => `${item.displayValue}${item.unit ? ` ${item.unit}` : ''}`).join(' / ') || '未提供'}</strong></div><div><span>展开图</span><strong>{basis.flatRegionIds.length ? `有 ${basis.flatRegionIds.length} 幅来源分图` : '未取得可归属的展开图'}</strong></div><div><span>展开净面积</span><strong>{basis.netAreaMm2 ? `${basis.netAreaMm2.value} mm² · 待核对` : '尚无完整数值依据'}</strong></div><div><span>切割路径长度</span><strong>{basis.cutLengthMm ? `${basis.cutLengthMm.value} mm · 待核对` : '尚未建立可计量的闭合轮廓'}</strong></div><div><span>材料密度</span><strong>{basis.densityKgM3 ? `${basis.densityKgM3.value} kg/m³ · 待核对` : '未提供采用标准，未套默认值'}</strong></div>{basis.theoreticalMassKg && <div><span>依据图纸数值推导的理论净重</span><strong>{basis.theoreticalMassKg.value} kg/件 · 待核对</strong></div>}</div>
}
