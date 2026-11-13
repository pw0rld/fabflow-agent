'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, Calculator, Check, ClipboardCheck, Download, ExternalLink, FileImage, Info, Layers, Package, Sparkles } from 'lucide-react'
import { adoptProcessCandidates, applyDemoAssumptions, applyMaterialBasis, applyProcessDemoRates, bomCsv, calculateDemoQuote, calculateMaterialBasis, createImageBomDraft, editBomDraft, editBomRow, editMaterialBasis, editProcessCharge, formatCents, processUnitLabel, quoteHtml, readImageBomDraft, setProcessMode, sourceFingerprint, type BomField, type ImageBomDraft, type ImageBomDraftField, type ImageBomRowField, type ImageBomSource, type ImageDemoQuote, type ImageProcessUnit } from '../lib/image-bom-demo'
import { buildImageEngineering } from '../lib/image-engineering'
import './image-bom-panel.css'

export type ImageBomStage = 'bom' | 'cost' | 'quote'
type Props = { source: ImageBomSource | null; filename: string; sample: boolean; stage: ImageBomStage; disabled: boolean; onStageChange: (stage: ImageBomStage) => void; onViewSource: () => void; onViewEngineering: () => void }
const STORAGE_KEY = 'hyl-image-bom-draft-v1'
const IMAGE_PATH = '/ortho2cad-test/drawing-split/'
const originLabel = { candidate: '图纸候选 · 待核对', manual: '手动补充 · 待核对', demo: '演示假设', unknown: '待补充' }
const fieldLabels: Record<string, string> = { name: '零件名称', material: '材料', thickness: '板厚', quantityPerSet: '每套数量', materialUsageKg: '单件用料', materialRateCnyKg: '材料单价', operationFeeCnyPiece: '单件综合工序费', setupFeeCny: '本行准备费', orderSets: '订单套数', policyMethod: '定价方式', policyPercent: '定价比例', rows: '物料归属' }
const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}

function displayField(field: BomField): string { return field.value === null ? '待补充' : `${field.value}${field.unit ? ` ${field.unit}` : ''}` }
function FieldBadge({ field }: { field: BomField }) { return <small className={`ibom-origin ${field.origin}`}>{originLabel[field.origin]}</small> }

/** Source links can only open this job's single-file assets or fixed sample assets. */
function sourceAsset(value: unknown, source: ImageBomSource, sample: boolean): string | null {
  if (typeof value !== 'string' || value.length > 700 || !value) return null
  try {
    const url = new URL(value, new URL(IMAGE_PATH, window.location.origin))
    if (url.origin !== window.location.origin || url.username || url.password || url.search || url.hash) return null
    const prefix = source.jobId ? `${IMAGE_PATH}api/jobs/${source.jobId}/files/` : sample ? `${IMAGE_PATH}assets/` : ''
    if (!prefix || !url.pathname.startsWith(prefix)) return null
    const name = decodeURIComponent(url.pathname.slice(prefix.length))
    return /^[a-zA-Z0-9._-]+$/.test(name) && name !== '.' && name !== '..' ? url.href : null
  } catch { return null }
}

function download(content: string, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type })), link = document.createElement('a')
  link.href = url; link.download = filename; document.body.append(link); link.click(); link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export default function ImageBomPanel({ source, filename, sample, stage, disabled, onStageChange, onViewSource, onViewEngineering }: Props) {
  const [draft, setDraft] = useState<ImageBomDraft | null>(null)
  const [calculation, setCalculation] = useState<ImageDemoQuote | null>(null)
  const [notice, setNotice] = useState('')
  const [saved, setSaved] = useState(false)
  const [saveError, setSaveError] = useState(false)
  const previousKey = useRef<string | null>(null)
  const appliedKey = useRef<string | null>(null)
  const sourceKey = source ? sourceFingerprint(source) : null
  const engineering = useMemo(() => source ? buildImageEngineering(source) : null, [source])

  useEffect(() => {
    if (!source || !sourceKey) { appliedKey.current = null; setDraft(null); setCalculation(null); setSaved(false); return }
    if (appliedKey.current === sourceKey) return
    const changed = previousKey.current !== null && previousKey.current !== sourceKey
    let restored: ImageBomDraft | null = null
    try { restored = readImageBomDraft(localStorage.getItem(STORAGE_KEY), source) } catch { /* Private browsing can disable storage. */ }
    setDraft(restored || createImageBomDraft(source)); setCalculation(null); setSaved(!!restored); setSaveError(false)
    setNotice(changed ? '识别结果或校正版本已更新，上一版草稿与计算结果已失效。请核对当前物料并重新计算。' : restored ? '已恢复当前图纸的演示草稿；报价金额需重新计算。' : '')
    previousKey.current = sourceKey
    appliedKey.current = sourceKey
  }, [source, sourceKey])

  useEffect(() => {
    const getState = () => ({ sourceKey, lineCount: draft?.rows.length || 0, draft, calculation, status: disabled ? 'blocked' : 'ready', saved, stage })
    const target = window as typeof window & { HylImageBomDemo?: { getState: typeof getState } }
    target.HylImageBomDemo = { getState }
    return () => { delete target.HylImageBomDemo }
  }, [sourceKey, draft, calculation, disabled, saved, stage])

  function commit(next: ImageBomDraft, message?: string) {
    if (disabled || !source || next.sourceFingerprint !== sourceKey) return
    setDraft(next); setCalculation(null); setNotice(message || '输入已更新，请重新计算报价。')
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(next)); setSaved(true); setSaveError(false) } catch { setSaved(false); setSaveError(true) }
  }
  function rowEdit(id: string, field: ImageBomRowField, value: string) { if (draft) commit(editBomRow(draft, id, { [field]: value.trim() ? value : null })) }
  function globalEdit(field: ImageBomDraftField, value: string) { if (draft) commit(editBomDraft(draft, { [field]: value.trim() ? value : null })) }
  function calculate() {
    if (!draft || disabled || draft.sourceFingerprint !== sourceKey) return
    const next = calculateDemoQuote(draft); setCalculation(next)
    setNotice(next.ok ? '模拟报价已生成，金额按当前草稿计算，待工程与商务审核。' : '还有待补充项，请按下方提示补齐后重新计算。')
    if (next.ok) onStageChange('quote')
  }

  if (!draft || !source || draft.sourceFingerprint !== sourceKey) return <div className="ibom-empty" data-testid="image-bom-empty"><Layers size={27} /><strong>物料与报价草稿</strong><p>完成图纸分图与参数提取后，在这里归并零件、整理物料并演示报价。</p></div>

  const manifest = record(source.manifest)
  const regions = (Array.isArray(manifest.regions) ? manifest.regions : []).map(record)
  const parameters = Array.isArray(manifest.parameters) ? manifest.parameters : []
  const views = regions.filter(region => !['process_table', 'technical_notes', 'title_block', 'table'].includes(String(region.type))).length
  const quote = calculation?.ok && calculation.sourceFingerprint === sourceKey ? calculation : null
  const title = stage === 'bom' ? '从图纸整理物料 BOM' : stage === 'cost' ? '补充用料与成本口径' : '模拟报价 · 待人工审核'
  const subtitle = stage === 'bom' ? '多个视图按所属零件归并；订单数量与加工用量分别补充。' : stage === 'cost' ? '识别参数保留来源，用量与费率由演示假设或人工补充。' : '按当前物料、数量与费用计算。可下载草稿交由人工核对。'
  const evidenceFor = (field: BomField) => field.evidence.map((item, index) => {
    const region = regions.find(region => region.id === item.regionId), href = sourceAsset(region?.file, source, sample)
    return <div className="ibom-evidence-item" key={`${item.regionId}-${index}`}><span>{item.rawText || '图纸候选来源'}</span>{href ? <a href={href} target="_blank" rel="noopener noreferrer">{typeof region?.title === 'string' ? region.title : '来源分图'}<ExternalLink size={11} /></a> : <button type="button" onClick={onViewSource}>查看图纸与参数</button>}</div>
  })

  function fieldInput(rowId: string, field: ImageBomRowField, value: BomField, label: string, test: string, numeric = false, placeholder = '待补充') {
    return <label className="ibom-field"><span>{label}<FieldBadge field={value} /></span><input aria-label={label} data-testid={`image-bom-${test}-${rowId}`} value={value.value || ''} inputMode={numeric ? 'decimal' : 'text'} maxLength={4000} placeholder={placeholder} disabled={disabled} onChange={event => rowEdit(rowId, field, event.target.value)} /></label>
  }

  return <section className={`ibom-panel${disabled ? ' ibom-disabled' : ''}`} data-testid="image-bom-panel" aria-busy={disabled}>
    <div className="ibom-heading"><div><span className="ibom-eyebrow">{stage === 'bom' ? 'MATERIAL BOM' : stage === 'cost' ? 'COST SIMULATION' : 'QUOTATION REVIEW'}</span><h2>{title}</h2><p>{subtitle}</p></div><span className="ibom-draft-tag"><i />演示草稿 · {saved ? '当前浏览器保存' : '尚未保存'}</span></div>
    <div className="ibom-scope"><FileImage size={14} /><span>{filename || draft.sourceName}</span><small>{sample ? '预设整理示例' : `识别轮次 ${source.attempt ?? '—'} · 校正版本 ${source.revision ?? '—'}`}</small><button type="button" onClick={onViewSource}>查看图纸<ArrowRight size={12} /></button></div>
    {notice && <div className="ibom-notice" role="status" data-testid="image-bom-notice"><Info size={14} />{notice}</div>}
    {saveError && <div className="ibom-notice warning" role="status">浏览器未允许保存草稿；本次编辑仍可继续，关闭页面前请下载。</div>}
    <div className="ibom-demo-notice"><Info size={15} /><span><strong>待工程核对</strong>：孔槽、折弯、焊接及后处理要求来自已保存的图纸资料。采用的用量、计价量与价格分别记录来源，所有输出均为模拟草稿。</span></div>

    {stage === 'bom' && <>
      <div className="ibom-stats"><div><Package size={18} /><span>按零件归并<strong data-testid="image-bom-count">{draft.rows.length}<small>种物料</small></strong></span></div><div><Layers size={18} /><span>图纸分区<strong>{views}<small>个视图 / {regions.length} 个分区</small></strong></span></div><div><ClipboardCheck size={18} /><span>识别资料<strong>{parameters.length}<small>项参数 · 待工程核对</small></strong></span></div></div>
      <div className="ibom-section-heading"><div><h3>物料明细</h3><p>同一零件的俯视、侧视、展开等视图合并为一行；视图数量不作为采购数量。</p></div><button type="button" className="unified-button" data-testid="image-export-bom" disabled={disabled} onClick={() => download(bomCsv(draft), '物料BOM-演示草稿.csv', 'text/csv;charset=utf-8')}><Download size={14} />下载 BOM</button></div>
      <div className="ibom-row-list">
        {draft.rows.map((row, index) => {
          const region = regions.find(region => region.part_id === row.partId && !['technical_notes', 'title_block', 'process_table'].includes(String(region.type)))
          const thumbnail = sourceAsset(region?.file, source, sample)
          const partEngineering = engineering?.parts.find(item => item.partId === row.partId)
          return <article className="ibom-row" key={row.id} data-testid={`image-bom-row-${row.id}`}><div className="ibom-row-heading"><div className="ibom-thumbnail">{thumbnail ? <img src={thumbnail} alt="来源图纸分区" /> : <Package size={22} />}</div><div><span>物料 {String(index + 1).padStart(2, '0')} · {row.partId}</span><h3>{row.name.value || '名称待补充'}</h3><small>按图纸零件归属整理 · 尚未工程批准</small></div><span className="ibom-pending">待核对</span></div><div className="ibom-row-fields">
            {fieldInput(row.id, 'name', row.name, '零件名称', 'name')}
            {fieldInput(row.id, 'material', row.material, '材料牌号', 'material', false, '图纸未识别到，请补充')}
            {fieldInput(row.id, 'thickness', row.thickness, `板厚${row.thickness.unit ? `（${row.thickness.unit}）` : ''}`, 'thickness', true)}
            {fieldInput(row.id, 'quantityPerSet', row.quantityPerSet, '每套数量（件/套）', 'quantity', true, '图纸未给出订单数量')}
          </div><div className="ibom-secondary-fields"><div><span>标注外形尺寸</span><strong>{displayField(row.outerSize)}</strong><FieldBadge field={row.outerSize} /></div><div><span>表面处理</span><strong>{displayField(row.surfaceFinish)}</strong><FieldBadge field={row.surfaceFinish} /></div></div>
            <div className="ibom-engineering-summary"><div><strong>{partEngineering?.features.length || 0} 项加工资料</strong><span>孔槽 {partEngineering ? partEngineering.groups.hole.length + partEngineering.groups.slot.length : 0} 项 · 折弯表 {partEngineering?.bends.length || 0} 行 · 工艺候选 {partEngineering?.processes.length || 0} 项</span></div><button type="button" data-testid={`image-view-engineering-${row.id}`} onClick={onViewEngineering}>查看加工特征与原图依据<ArrowRight size={12} /></button></div>
            <details className="ibom-evidence"><summary>查看材料、板厚与尺寸的图纸依据</summary>{(['material', 'thickness', 'outerSize', 'surfaceFinish'] as const).map(key => <div className="ibom-evidence-field" key={key}><strong>{({ material: '材料', thickness: '板厚', outerSize: '尺寸', surfaceFinish: '表面处理' })[key]}</strong>{row[key].evidence.length ? evidenceFor(row[key]) : <p>本轮未提取到可归属的依据，请回原图核对。</p>}</div>)}</details>
          </article>
        })}
        {!draft.rows.length && <div className="ibom-empty"><Package size={25} /><strong>尚未取得可归属的零件</strong><p>请回到图纸与参数核对零件归属，分图区不会自动当作独立物料。</p><button type="button" className="unified-button" onClick={onViewSource}>返回图纸与参数</button></div>}
      </div>
      <div className="ibom-order-bar"><label className="ibom-field"><span>订单套数<FieldBadge field={draft.orderSets} /></span><input aria-label="订单套数" data-testid="image-order-sets" inputMode="numeric" value={draft.orderSets.value || ''} placeholder="待补充" disabled={disabled} onChange={event => globalEdit('orderSets', event.target.value)} /></label><p>每行订单量 = 每套数量 × 订单套数。原图没有的数量保持为空。</p></div>
      {(!!draft.warnings.length || !!draft.unassignedParameters.length) && <details className="ibom-unassigned" data-testid="image-bom-unassigned"><summary>归属与核对提示（{draft.warnings.length + draft.unassignedParameters.length} 项）</summary>{draft.warnings.map((warning, index) => <p key={index}>{warning}</p>)}{draft.unassignedParameters.map((parameter, index) => <p key={`${parameter.id}-${index}`}><strong>{parameter.label}：</strong>{parameter.value || '未提取到值'}{parameter.unit ? ` ${parameter.unit}` : ''} · 尚未明确归属，未自动分配到物料行</p>)}</details>}
      <div className="ibom-actions"><button type="button" className="unified-button" onClick={onViewEngineering}><ArrowLeft size={14} />加工特征</button><button type="button" className="unified-button primary" disabled={disabled || !draft.rows.length} data-testid="image-next-cost" onClick={() => onStageChange('cost')}>下一步 · 用料与成本<ArrowRight size={14} /></button></div>
    </>}

    {stage === 'cost' && <>
      <div className="ibom-assumptions"><div><Sparkles size={20} /><div><h3>依据图纸工艺，分别采用计量与费率</h3><p>先查看来源候选，再采用到本次测算。演示按钮只载入工序费率，缺少的切割长度、工时、重量与订单数量保持为空。</p></div></div><button type="button" className="unified-button primary" data-testid="image-demo-process-rates" disabled={disabled || !draft.rows.length} onClick={() => commit(applyProcessDemoRates(draft), '已采用工艺候选并载入缺少的演示工序费率。原文计量仍待核对；缺少的长度、工时和用料未补造。')}><Sparkles size={14} />采用候选并载入演示费率</button></div>
      <div className="ibom-order-bar cost"><label className="ibom-field"><span>订单套数<FieldBadge field={draft.orderSets} /></span><input aria-label="订单套数" data-testid="image-order-sets" inputMode="numeric" value={draft.orderSets.value || ''} placeholder="待补充" disabled={disabled} onChange={event => globalEdit('orderSets', event.target.value)} /></label><p>材料、板厚与每套数量在物料 BOM 中补充。<button type="button" onClick={() => onStageChange('bom')}>返回修改<ArrowRight size={12} /></button></p></div>
      <div className="ibom-row-list">{draft.rows.map((row, index) => {
        const partEngineering = engineering?.parts.find(item => item.partId === row.partId)
        const materialResult = calculateMaterialBasis(row)
        return <article className="ibom-cost-row" key={row.id}><div className="ibom-cost-row-heading"><span>{String(index + 1).padStart(2, '0')}</span><div><h3>{row.name.value || '名称待补充'}</h3><p>{displayField(row.material)} · 板厚 {displayField(row.thickness)} · 每套 {row.quantityPerSet.value || '待补充'} 件</p></div><button type="button" className="ibom-text-button" onClick={onViewEngineering}>查看图纸工艺<ArrowRight size={12} /></button></div>
          <section className="ibom-material-basis" data-testid={`image-material-basis-${row.id}`}><div className="ibom-cost-subheading"><h4>用料依据</h4><span>{partEngineering?.materialBasis.flatRegionIds.length ? '已有展开分图，净面积待核对' : '暂无可归属展开图'}</span></div><p>先用经核对的展开净面积、板厚和材料密度计算理论净重，再按采用的材料利用率换算用料。成型长宽高和图片像素未参与计算。</p><div className="ibom-basis-fields">{([['netAreaMm2', '展开净面积（mm²）', 'area'], ['densityKgM3', '采用密度（kg/m³）', 'density'], ['utilizationPercent', '材料利用率（%）', 'utilization']] as const).map(([field, label, id]) => <label className="ibom-field" key={field}><span>{label}<FieldBadge field={row.materialBasis[field]} /></span><input aria-label={label} data-testid={`image-material-${id}-${row.id}`} inputMode="decimal" maxLength={40} value={row.materialBasis[field].value || ''} placeholder="待提供，不使用默认值" disabled={disabled} onChange={event => commit(editMaterialBasis(draft, row.id, { [field]: event.target.value.trim() ? event.target.value : null }))} /></label>)}</div>
            {materialResult.ok ? <div className="ibom-material-result" data-testid={`image-material-result-${row.id}`}><div><strong>理论净重 {materialResult.netMassKg} kg/件</strong><span>含利用率口径的用料 {materialResult.materialUsageKg} kg/件 · 待核对</span><p>{materialResult.formula}</p></div><button type="button" className="unified-button" data-testid={`image-material-apply-${row.id}`} disabled={disabled} onClick={() => commit(applyMaterialBasis(draft, row.id), '已采用当前计算用料，仍待工程核对。面积、密度、板厚或利用率变化后需要重新采用。')}>采用计算用料<Check size={13} /></button></div> : <div className="ibom-material-missing" data-testid={`image-material-missing-${row.id}`}><strong>当前不能得出材料用量</strong><span>{materialResult.issues.join('；')}</span><small>可补充以上依据，也可在下方直接填写工厂核定的 kg/件。</small></div>}
          </section>
          <div className="ibom-row-fields ibom-material-fees">
            {fieldInput(row.id, 'materialUsageKg', row.materialUsageKg, '采用单件用料（kg/件）', 'usage', true)}
            {fieldInput(row.id, 'materialRateCnyKg', row.materialRateCnyKg, '材料单价（元/kg）', 'rate', true)}
            {fieldInput(row.id, 'setupFeeCny', row.setupFeeCny, '本行准备费（元/整行订单）', 'setup', true)}
          </div>
          <section className="ibom-process-costs"><div className="ibom-cost-subheading"><h4>加工工序计价</h4><select aria-label="工序计价方式" data-testid={`image-process-mode-${row.id}`} disabled={disabled} value={row.processMode} onChange={event => commit(setProcessMode(draft, row.id, event.target.value === 'comprehensive' ? 'comprehensive' : 'processes'))}><option value="processes">按工序逐项计算</option><option value="comprehensive">按单件综合加工费</option></select></div>
            {row.processMode === 'processes' ? <><div className="ibom-process-adoption"><span>勾选参与测算的工序；数量为图纸候选或人工计量，费率另行采用。</span><button type="button" className="ibom-text-button" data-testid={`image-process-adopt-${row.id}`} disabled={disabled} onClick={() => commit(adoptProcessCandidates(draft, row.id), '已将本零件的工艺候选纳入测算；图纸计量待核对，未提供的计价量与费率仍需补充。')}>采用本件工艺候选<ArrowRight size={12} /></button></div>
              <div className="ibom-process-charge-list">{row.processes.map(process => <article className={process.adopted ? 'adopted' : ''} key={process.id} data-testid={`image-process-row-${row.id}-${process.id}`}><div className="ibom-process-charge-heading"><label><input type="checkbox" data-testid={`image-process-selected-${row.id}-${process.id}`} checked={process.adopted} disabled={disabled} onChange={event => commit(editProcessCharge(draft, row.id, process.id, { adopted: event.target.checked }))} /><strong>{process.title}</strong></label><span>{process.basis === 'drawing_requirement' ? '图纸要求候选' : '工艺建议候选'} · {process.adopted ? '已纳入本次模拟' : '未纳入'}</span></div><div className="ibom-process-charge-fields"><label className="ibom-field"><span>单件计价量<FieldBadge field={process.measure} /></span><div className="ibom-inline-input"><input aria-label={`${process.title}单件计价量`} data-testid={`image-process-measure-${row.id}-${process.id}`} inputMode="decimal" maxLength={40} value={process.measure.value || ''} placeholder="待提供" disabled={disabled || !process.adopted} onChange={event => commit(editProcessCharge(draft, row.id, process.id, { measure: event.target.value.trim() ? event.target.value : null }))} />{process.unit === 'piece' && !process.measure.value && <button type="button" data-testid={`image-process-once-${row.id}-${process.id}`} disabled={disabled || !process.adopted} onClick={() => commit(editProcessCharge(draft, row.id, process.id, { measure: '1' }), '已明确本工序按每件 1 次计费，费率及包含范围仍需核对。')}>每件 1 次</button>}</div></label><label className="ibom-field"><span>计量单位</span><select aria-label={`${process.title}计量单位`} data-testid={`image-process-unit-${row.id}-${process.id}`} value={process.unit} disabled={disabled || !process.adopted} onChange={event => commit(editProcessCharge(draft, row.id, process.id, { unit: event.target.value as ImageProcessUnit }))}>{(['unknown', 'piece', 'bend', 'spot', 'hole', 'm', 'm2', 'hour'] as const).map(unit => <option key={unit} value={unit}>{processUnitLabel(unit)}</option>)}</select></label><label className="ibom-field"><span>费率（元/{processUnitLabel(process.unit)}）<FieldBadge field={process.rate} /></span><input aria-label={`${process.title}费率`} data-testid={`image-process-rate-${row.id}-${process.id}`} inputMode="decimal" maxLength={40} value={process.rate.value || ''} placeholder="待提供" disabled={disabled || !process.adopted} onChange={event => commit(editProcessCharge(draft, row.id, process.id, { rate: event.target.value.trim() ? event.target.value : null }))} /></label></div>{process.explanation && <p className="ibom-process-quantity-basis">{process.explanation}</p>}{process.evidence.length > 0 && <details className="ibom-process-evidence"><summary>查看本工序图纸依据</summary>{evidenceFor({ ...process.measure, evidence: process.evidence })}</details>}</article>)}</div>
              {!row.processes.length && <div className="ibom-material-missing"><strong>本轮没有可归属的工艺候选</strong><span>请返回加工特征核对原图要求，或明确采用工厂提供的单件综合加工费。</span></div>}
              <p className="ibom-exclusivity">当前只汇总勾选工序的计价量 × 费率，不叠加单件综合加工费。未采用工序不代表已经覆盖整套加工要求。</p>
            </> : <div className="ibom-comprehensive"><p>当前仅采用工厂提供的单件综合加工费，逐项工序费不另计。需核对该费用已包含哪些图纸工序。</p>{fieldInput(row.id, 'operationFeeCnyPiece', row.operationFeeCnyPiece, '单件综合加工费（元/件）', 'operation', true)}</div>}
          </section>
        </article>
      })}</div>
      <section className="ibom-policy"><div><h3>定价方式</h3><p>明确区分成本加成与目标毛利率。</p></div><label className="ibom-field"><span>计算方式<FieldBadge field={draft.policyMethod} /></span><select data-testid="image-pricing-policy" aria-label="定价方式" value={draft.policyMethod.value || 'none'} disabled={disabled} onChange={event => globalEdit('policyMethod', event.target.value === 'none' ? '' : event.target.value)}><option value="none">请选择</option><option value="cost_plus">成本加成</option><option value="gross_margin">目标毛利率</option></select></label><label className="ibom-field"><span>比例（%）<FieldBadge field={draft.policyPercent} /></span><input data-testid="image-pricing-percent" aria-label="定价比例" inputMode="decimal" value={draft.policyPercent.value || ''} placeholder="待补充" disabled={disabled} onChange={event => globalEdit('policyPercent', event.target.value)} /></label></section>
      <details className="ibom-assumption-details"><summary>计费口径与来源说明</summary><p>材料费 = 采用单件用料 × 材料单价 × 每套数量 × 订单套数。展开净面积 × 板厚 × 密度计算理论净重，再除以明确采用的材料利用率；它与采购用量口径分别核对。</p><p>逐项工序费 = 各采用工序的单件计价量 × 对应单位费率 × 订单件数。综合工序费与逐项工序费二选一，准备费对每行整单只计一次。未知量不补 0。</p><p>成本加成：成本 ×（1 + 比例）；目标毛利率：成本 ÷（1 − 比例）。人民币、未税、不含运费。演示费率并非工厂价格库匹配结果。</p></details>
      <details className="ibom-legacy-demo" data-testid="image-legacy-demo"><summary>完整流程模拟：使用固定用量和综合加工费</summary><p>仅供页面流程演示：0.25 kg/件、20 元/kg、8 元/件综合加工费、30 元准备费、每套 1 件、10 套、成本加成 20%。这些数值没有根据当前图纸计算；点击后切换为综合加工费计价，已有输入保留。</p><button type="button" className="unified-button" data-testid="image-demo-rates" disabled={disabled || !draft.rows.length} onClick={() => commit(applyDemoAssumptions(draft), '已采用固定用量与综合加工费进行完整流程模拟；这些数值并非从图纸计算，逐项工序费没有叠加。')}>载入完整流程模拟数据</button></details>
      <Blockers calculation={calculation} draft={draft} />
      {quote && <div className="ibom-cost-summary"><span>订单成本<strong data-testid="image-cost-total">¥{formatCents(quote.totals.totalCostCents)}</strong></span><span>模拟报价总额<strong>¥{formatCents(quote.totals.lineQuoteCents)}</strong></span></div>}
      <div className="ibom-actions"><button type="button" className="unified-button" onClick={() => onStageChange('bom')}><ArrowLeft size={14} />物料 BOM</button><button type="button" className="unified-button primary" data-testid="image-calculate" disabled={disabled} onClick={calculate}><Calculator size={15} />计算并生成模拟报价</button></div>
    </>}

    {stage === 'quote' && <>
      {!quote ? <><div className="ibom-empty"><Calculator size={29} /><strong>还没有当前版本的报价草稿</strong><p>补齐用量、费率与定价口径后，按当前输入计算。</p><button type="button" className="unified-button primary" data-testid="image-calculate" disabled={disabled} onClick={calculate}>生成模拟报价</button></div><Blockers calculation={calculation} draft={draft} /></> : <>
        <div className="ibom-quote-top"><div><span>模拟报价总额</span><strong data-testid="image-quote-total">¥{formatCents(quote.totals.lineQuoteCents)}</strong><p>人民币 · 未税 · 不含运费</p></div><div><span>订单成本</span><strong data-testid="image-cost-total">¥{formatCents(quote.totals.totalCostCents)}</strong><p>{quote.totals.quantity} 件 · {quote.policy.method === 'cost_plus' ? '成本加成' : '目标毛利率'} {quote.policy.percent}%</p></div><span className="ibom-quote-watermark">模拟草稿<br />待审核</span></div>
        <div className="ibom-quote-scope" data-testid="image-quote-scope"><h3>本次报价覆盖范围</h3>{draft.rows.map(row => <div key={row.id}><strong>{row.name.value || row.partId}</strong>{row.processMode === 'processes' ? <><p><span>已计价工序：</span>{row.processes.filter(process => process.adopted).map(process => process.title).join('、') || '未采用'}</p>{row.processes.some(process => !process.adopted) ? <p className="omitted"><span>未纳入报价：</span>{row.processes.filter(process => !process.adopted).map(process => process.title).join('、')}。以上项目未计入合计，当前金额不代表全部加工要求的价格。</p> : <p><span>覆盖说明：</span>当前候选工序均已纳入；图纸漏提项、加工路线及费用包含范围仍待核对。</p>}</> : <><p><span>计价方式：</span>单件综合加工费，未另外叠加逐项工序费用。</p><p className="omitted"><span>包含范围待核对：</span>{row.processes.length ? `图纸工艺候选为${row.processes.map(process => process.title).join('、')}；` : ''}尚未逐项确认综合加工费是否覆盖全部要求。</p></>}</div>)}</div>
        <div className="ibom-table-scroll"><table className="ibom-quote-table"><thead><tr><th>零件 / 材料</th><th>数量</th><th>材料费</th><th>工序费</th><th>准备费</th><th>单件成本</th><th>模拟单价</th><th>模拟小计</th></tr></thead><tbody>{quote.rows.map(row => <tr key={row.rowId}><td><strong>{row.name}</strong><small>{row.material} · {row.thickness}</small></td><td>{row.quantity}</td><td>¥{formatCents(row.materialCostCents)}</td><td>¥{formatCents(row.operationCostCents)}</td><td>¥{formatCents(row.setupCostCents)}</td><td>¥{formatCents(row.unitCostCents)}</td><td>¥{formatCents(row.unitQuoteCents)}</td><td><strong>¥{formatCents(row.lineQuoteCents)}</strong></td></tr>)}</tbody><tfoot><tr><td colSpan={2}>合计</td><td>¥{formatCents(quote.totals.materialCostCents)}</td><td>¥{formatCents(quote.totals.operationCostCents)}</td><td>¥{formatCents(quote.totals.setupCostCents)}</td><td colSpan={2}>报价总额</td><td><strong>¥{formatCents(quote.totals.lineQuoteCents)}</strong></td></tr></tfoot></table></div>
        <section className="ibom-quote-processes" data-testid="image-quote-process-breakdown"><h3>采用的工序与计量依据</h3>{quote.rows.map(row => <details key={row.rowId} open><summary>{row.name} · 工序费 ¥{formatCents(row.operationCostCents)}</summary>{row.operations?.length ? <div className="ibom-table-scroll"><table className="ibom-quote-table"><thead><tr><th>工序</th><th>单件计价量</th><th>费率</th><th>订单件数</th><th>工序成本</th></tr></thead><tbody>{row.operations.map(operation => <tr key={operation.id}><td><strong>{operation.title}</strong></td><td>{operation.measure} {processUnitLabel(operation.unit)}</td><td>¥{operation.rate}/{processUnitLabel(operation.unit)}</td><td>{row.quantity}</td><td>¥{formatCents(operation.totalCostCents)}</td></tr>)}</tbody></table></div> : <p>采用单件综合加工费，逐项工序未另外计价；包含范围须与原图工艺清单核对。</p>}</details>)}</section>
        <section className="ibom-review-summary" data-testid="image-review-summary"><h3><ClipboardCheck size={18} />人工审核要点</h3><div><span>工程</span><p>零件归属、材料、板厚、图纸尺寸与数量仍需核对；本轮没有据图片生成机床加工文件。</p></div><div><span>成本</span><p>用量、损耗与工序费包含范围需按工厂标准确认；带“演示假设”的数据须在正式报价前替换。</p></div><div><span>商务</span><p>税费、运费、交期、付款条件与报价有效期尚未约定。本页没有提交正式审核或对外发送。</p></div></section>
        <details className="ibom-assumption-details"><summary>计算与舍入说明</summary>{quote.notes.map((note, index) => <p key={index}>{note}</p>)}</details>
        <div className="ibom-export-bar"><div><Download size={19} /><div><strong>下载演示资料</strong><p>供人工核对的物料表与模拟报价单。</p></div></div><button type="button" className="unified-button" data-testid="image-export-bom" disabled={disabled} onClick={() => download(bomCsv(draft), '物料BOM-演示草稿.csv', 'text/csv;charset=utf-8')}>物料 BOM · CSV</button><button type="button" className="unified-button primary" data-testid="image-export-quote" disabled={disabled} onClick={() => download(quoteHtml(draft, quote), '模拟报价单-待人工审核.html', 'text/html;charset=utf-8')}>报价草稿 · HTML<Download size={14} /></button></div>
      </>}
      <div className="ibom-actions"><button type="button" className="unified-button" onClick={() => onStageChange('cost')}><ArrowLeft size={14} />返回用料与成本</button><span>演示草稿，尚未形成工厂正式报价。</span></div>
    </>}
  </section>
}

function Blockers({ calculation, draft }: { calculation: ImageDemoQuote | null; draft: ImageBomDraft }) {
  if (!calculation || calculation.ok) return null
  return <div className="ibom-blockers" data-testid="image-cost-blockers" role="alert"><h3><Info size={15} />待补充或调整（{calculation.issues.length} 项）</h3>{calculation.issues.map((issue, index) => <p key={index}><strong>{issue.rowId ? `${draft.rows.find(row => row.id === issue.rowId)?.name.value || '当前物料'} · ` : ''}{fieldLabels[issue.field] || issue.field}：</strong>{issue.message}</p>)}</div>
}
