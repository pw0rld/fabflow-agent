'use client'

import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { AlertTriangle, ArrowLeft, Calculator, Check, ChevronRight, ClipboardList, Download, FlaskConical, RotateCcw, ShieldCheck } from 'lucide-react'
import { DEMO_PARTS, type MockPartIdentity } from '@/lib/demo-data'
import { buildMockCsv, calculateMockFlow, centsToYuan, type MockCalculation, type MockPricingPolicy } from '@/lib/mock-calculations'

export type { MockPartIdentity } from '@/lib/demo-data'
type MockReviewStatus = 'draft' | 'approved' | 'returned'

/** Call in the root component to retain mock settings when returning to recognition. */
export function useMockFlow(parts: MockPartIdentity[], orderQty: number) {
  const fingerprint = JSON.stringify(parts.length ? parts : DEMO_PARTS)
  const identities = useMemo<MockPartIdentity[]>(() => JSON.parse(fingerprint), [fingerprint])
  const [waste, setWaste] = useState<number | null>(8)
  const [markup, setMarkup] = useState<number | null>(20)
  const [policy, setPolicy] = useState<MockPricingPolicy>('cost_plus')
  const [materialRates, setMaterialRates] = useState<Record<string, number | null>>({})
  const [incomplete, setIncomplete] = useState(false)
  const [comment, setComment] = useState('')
  const [reviewRecord, setReviewRecord] = useState<{ status: MockReviewStatus; key: string }>({ status: 'draft', key: '' })
  const calculation = useMemo(() => calculateMockFlow({ parts: identities, orderQty, wastePercent: waste, pricingPercent: markup, policy, materialRates, incomplete }), [identities, orderQty, waste, markup, policy, materialRates, incomplete])
  const calculationKey = JSON.stringify({ fingerprint, orderQty, waste, markup, policy, materialRates, incomplete })
  const review = reviewRecord.key === calculationKey ? reviewRecord.status : 'draft'

  useEffect(() => {
    setMaterialRates({})
    setIncomplete(false)
    setComment('')
    setReviewRecord({ status: 'draft', key: '' })
  }, [fingerprint])

  function setReview(status: MockReviewStatus) {
    if (status === 'approved' && !calculation.complete) return
    setReviewRecord({ status, key: calculationKey })
  }
  function fillMissing() {
    setIncomplete(false)
    setMaterialRates({})
    if (waste === null || waste < 0 || waste > 100) setWaste(8)
    if (markup === null || markup < 0 || markup > (policy === 'gross_margin' ? 95 : 500)) setMarkup(20)
  }
  return { calculation, waste, setWaste, markup, setMarkup, policy, setPolicy, materialRates, setMaterialRates, incomplete, setIncomplete, review, setReview, comment, setComment, fillMissing }
}

export type MockFlow = ReturnType<typeof useMockFlow>
export type MockStagesProps = {
  stage: 2 | 3 | 4
  parts: MockPartIdentity[]
  orderQty: number
  onNext: () => void
  onBack: () => void
  onStage?: (stage: 2 | 3 | 4) => void
  flow?: MockFlow
}

const inputValue = (value: string): number | null => value.trim() === '' ? null : Number.isFinite(Number(value)) ? Number(value) : null
const kg = (value: number | null, digits = 3) => value === null ? '待补充' : `${value.toFixed(digits)} kg`

function MockWarning() {
  return <div className="basis-note mock-notice" role="note" data-testid="mock-warning"><FlaskConical size={18}/><div><strong>用料、价格、BOM 与审核均为模拟演示</strong><br/>零件名称、图号可沿用识别结果；以下材料规格、展开面积、每套用量、工序和价格采用独立样例假设，不代表对上传图纸的工程计算或正式报价。</div></div>
}

function StepShell({ title, kicker, children, back, action, actionLabel }: { title: string; kicker: string; children: ReactNode; back: () => void; action?: () => void; actionLabel?: string }) {
  return <section className="step-page" data-testid="mock-stages"><div className="step-heading"><span className="eyebrow">{kicker} · 模拟</span><h1>{title}</h1></div><MockWarning/>{children}<div className="step-actions"><button className="text-button" onClick={back}><ArrowLeft size={14}/>返回上一步</button>{action && <button className="primary-button" onClick={action}>{actionLabel}<ChevronRight size={15}/></button>}</div></section>
}

function DemoCompleteness({ flow }: { flow: MockFlow }) {
  return <div className="mock-scenario-controls"><label className="inline-control"><input type="checkbox" checked={flow.incomplete} data-testid="mock-incomplete-toggle" onChange={event => flow.setIncomplete(event.target.checked)}/>体验缺项样例：最后一件缺模拟展开面积及材料价格</label>{(!flow.calculation.complete || flow.incomplete) && <button className="outline-button" data-testid="mock-fill-missing" onClick={flow.fillMissing}><RotateCcw size={14}/>补齐演示数据</button>}</div>
}

function Materials({ flow, next, back }: { flow: MockFlow; next: () => void; back: () => void }) {
  return <StepShell kicker="03 · 计算与匹配" title="模拟用料与价格匹配" back={back} action={next} actionLabel="生成模拟 BOM 与报价"><DemoCompleteness flow={flow}/><div className="dual-grid"><div className="data-card"><div className="card-title"><Calculator size={18}/><div><h2>模拟材料用量</h2><p>演示展开面积 × 演示板厚 × 演示密度；不使用 AI 外形尺寸反推</p></div></div><label className="inline-control">模拟损耗率 <input aria-label="模拟损耗率" data-testid="mock-waste-input" type="number" min="0" max="100" step="1" value={flow.waste ?? ''} onChange={event => flow.setWaste(inputValue(event.target.value))}/> %</label><div className="mock-table-scroll"><table><thead><tr><th>零件 / 演示依据</th><th>每件净用量</th><th>每件计价用量</th><th>模拟本批用量</th></tr></thead><tbody>{flow.calculation.rows.map(row => <tr key={row.id}><td><strong>{row.name}</strong><small>{row.code}</small><small>模拟：{row.flatAreaM2 === null ? '展开面积待补充' : `${row.flatAreaM2} m²`} × {row.assumption.thicknessMm} mm</small><small>模拟密度 {row.assumption.densityKgM3} kg/m³ · 每套 {row.assumption.perSet} 件</small></td><td>{kg(row.netKg)}</td><td>{kg(row.chargedKg)}</td><td>{kg(row.batchKg, 2)}</td></tr>)}</tbody></table></div><div className="basis-note"><AlertTriangle size={15}/>{flow.calculation.rows.some(row => row.netKg === null) ? '模拟展开面积缺失的零件保持待补充，不计入完整报价。' : '当前为完整模拟样例。净用量与计价用量分别显示，损耗只计入一次。'}</div></div><div className="data-card"><div className="card-title"><ClipboardList size={18}/><div><h2>模拟价格匹配</h2><p>内置演示费率，不查询真实价格库</p></div></div>{flow.calculation.rows.map(row => <div className="match-row" key={row.id}><div><strong>{row.name} · {row.assumption.material} / {row.assumption.thicknessMm} mm</strong><small>识别标签：{row.identity.material || '材料未识别'} · {row.identity.thickness || '板厚未识别'}</small><small>来源：内置模拟价格表 · 仅用于演示</small><label className="inline-control">模拟材料价 <input type="number" step="0.1" min="0.01" max="1000000" aria-label={`${row.name}模拟材料单价`} data-testid={`mock-material-rate-${row.id}`} disabled={flow.incomplete && row.id === flow.calculation.rows.at(-1)?.id} value={row.materialRateYuanKg ?? ''} placeholder="待补充" onChange={event => flow.setMaterialRates(current => ({ ...current, [row.id]: inputValue(event.target.value) }))}/> 元/kg</label><small>{row.assumption.process} · {centsToYuan(row.processUnitCents)}/件</small><small>模拟准备费分摊 {centsToYuan(row.preparationUnitCents)}/件</small></div><span className={row.materialRateYuanKg === null ? 'pending' : 'matched'}>{row.materialRateYuanKg === null ? '模拟待补充' : '模拟已匹配'}</span></div>)}<div className="basis-note"><FlaskConical size={15}/>工序和准备费均按件采用演示假设，准备费已单独列明，不另收按单准备费。</div></div></div></StepShell>
}

function exportCsv(calculation: MockCalculation, orderQty: number) {
  const blob = new Blob([buildMockCsv(calculation, orderQty)], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = 'DEMO-模拟BOM与报价.csv'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function Quote({ flow, orderQty, next, back }: { flow: MockFlow; orderQty: number; next: () => void; back: () => void }) {
  const result = flow.calculation
  return <StepShell kicker="04 · BOM 与报价草稿" title="模拟 BOM 与报价草稿" back={back} action={next} actionLabel="进入模拟审核"><div className="quote-summary"><div><span>模拟报价状态</span><strong>{result.complete ? '模拟草稿' : '未完整 · 待补充'}</strong></div><div><span>模拟内部成本</span><strong>{result.costTotalCents === null ? '未完整' : centsToYuan(result.costTotalCents)}</strong></div><div><span>模拟报价总额（未税）</span><strong className="accent" data-testid="mock-total" data-cents={result.quoteTotalCents ?? ''}>{result.quoteTotalCents === null ? '未完整' : centsToYuan(result.quoteTotalCents)}</strong></div><label>模拟定价方式 <select aria-label="模拟定价方式" data-testid="mock-policy" value={flow.policy} onChange={event => flow.setPolicy(event.target.value as MockPricingPolicy)}><option value="cost_plus">成本加成</option><option value="gross_margin">目标毛利率</option></select><input aria-label={flow.policy === 'gross_margin' ? '模拟目标毛利率' : '模拟成本加成率'} data-testid="mock-markup-input" type="number" min="0" max={flow.policy === 'gross_margin' ? 95 : 500} value={flow.markup ?? ''} onChange={event => flow.setMarkup(inputValue(event.target.value))}/> %</label></div><div className="data-card table-card"><div className="card-title"><ClipboardList size={18}/><div><h2>模拟 BOM 与逐件报价</h2><p>数量与工艺均为模拟假设 · CNY · 未税 · 不含运费</p></div><button className="outline-button" data-testid="mock-export-csv" onClick={() => exportCsv(result, orderQty)}><Download size={15}/>导出模拟 BOM / 报价</button></div><div className="mock-table-scroll"><table><thead><tr><th>零件身份</th><th>模拟材料 / 规格</th><th>模拟每套用量</th><th>模拟总数量</th><th>材料费 / 件</th><th>加工费 / 件</th><th>准备分摊 / 件</th><th>模拟报价单价</th><th>模拟行金额</th></tr></thead><tbody>{result.rows.map(row => <tr key={row.id}><td><strong>{row.name}</strong><small>{row.code} · {row.version}</small><small>{row.assumption.process}</small></td><td>{row.assumption.material}<small>{row.assumption.thicknessMm} mm · 演示假设</small></td><td>{row.assumption.perSet}</td><td data-testid={`mock-qty-${row.id}`}>{row.qty ?? '待补充'}</td><td>{centsToYuan(row.materialUnitCents)}</td><td>{centsToYuan(row.processUnitCents)}</td><td>{centsToYuan(row.preparationUnitCents)}</td><td data-testid={`mock-unit-${row.id}`} data-cents={row.quoteUnitCents ?? ''}>{centsToYuan(row.quoteUnitCents)}</td><td><strong data-testid={`mock-line-${row.id}`} data-cents={row.quoteLineCents ?? ''}>{centsToYuan(row.quoteLineCents)}</strong></td></tr>)}</tbody></table></div><div className="table-foot"><span>单价保留两位小数；行金额 = 显示单价 × 模拟数量；合计逐行相加</span><strong>{result.quoteTotalCents === null ? '模拟合计未完整' : `模拟合计 ${centsToYuan(result.quoteTotalCents)}`}</strong></div></div>{!result.complete && <div className="basis-note"><AlertTriangle size={15}/>有 {result.issueCount} 项模拟资料待补充，当前不能形成完整模拟报价或通过模拟审核。已知行小计：{centsToYuan(result.knownQuoteCents)}。</div>}<div className="basis-note"><FlaskConical size={15}/>{flow.policy === 'gross_margin' ? '演示目标毛利率：报价单价 = 单件成本 ÷（1 − 毛利率）。' : '演示成本加成：报价单价 = 单件成本 ×（1 + 加成率）。'}此页不生成正式业务报价记录。</div></StepShell>
}

function Review({ flow, back, goMaterials }: { flow: MockFlow; back: () => void; goMaterials: () => void }) {
  const result = flow.calculation
  const missingWeights = result.rows.filter(row => row.netKg === null)
  const missingPrices = result.rows.filter(row => row.materialRateYuanKg === null)
  const statusText = flow.review === 'approved' ? '模拟已审核' : flow.review === 'returned' ? '模拟退回补充' : '模拟草稿'
  return <StepShell kicker="05 · 人工审核" title="模拟人工审核" back={back}><div className="review-grid"><div className="data-card"><div className="card-title"><ShieldCheck size={18}/><div><h2>模拟审核清单</h2><p>展示审核交互，不写入正式审核记录</p></div></div><div className={`review-item ${missingWeights.length ? 'pending' : ''}`}>{missingWeights.length ? <AlertTriangle size={16}/> : <Check size={16}/>}<div><strong>{missingWeights.length ? `${missingWeights.length} 件缺模拟展开面积` : '模拟用料参数完整'}</strong><span>{missingWeights.length ? missingWeights.map(row => row.name).join('、') : '仅表示演示参数齐全，实际解析结果仍待工程核对'}</span></div>{missingWeights.length > 0 && <button onClick={goMaterials}>返回补充</button>}</div><div className={`review-item ${missingPrices.length ? 'pending' : ''}`}>{missingPrices.length ? <AlertTriangle size={16}/> : <Check size={16}/>}<div><strong>{missingPrices.length ? `${missingPrices.length} 件模拟价格待补充` : '模拟价格匹配完整'}</strong><span>{missingPrices.length ? missingPrices.map(row => row.name).join('、') : '采用内置演示材料与工序费率，非实际采购价格'}</span></div>{missingPrices.length > 0 && <button onClick={goMaterials}>返回补充</button>}</div><div className={`review-item ${!result.complete ? 'pending' : ''}`}>{result.complete ? <Check size={16}/> : <AlertTriangle size={16}/>}<div><strong>{result.complete ? '模拟金额校验一致' : '模拟报价尚未完整'}</strong><span>{result.complete ? `逐行金额合计 ${centsToYuan(result.quoteTotalCents)}；单价、数量与合计一致` : `${result.issueCount} 项待补充；不能标记模拟通过`}</span></div>{!result.complete && <button onClick={goMaterials}>返回补充</button>}</div><DemoCompleteness flow={flow}/></div><div className="data-card approval-card"><div className="approval-status"><span className={`status-pill ${flow.review}`} data-testid="mock-review-status">{statusText}</span><strong>仅模拟本地状态，不对外发送</strong></div><label>模拟审核意见<textarea value={flow.comment} data-testid="mock-review-comment" onChange={event => flow.setComment(event.target.value)} placeholder="填写演示用审核意见或补充要求…"/></label>{!result.complete && <div className="basis-note"><AlertTriangle size={15}/>请先补齐模拟资料，才能演示审核通过。</div>}<div className="approval-actions"><button className="outline-button" onClick={() => flow.setReview('returned')}><RotateCcw size={15}/>模拟退回补充</button><button className="primary-button" data-testid="mock-approve" disabled={!result.complete} onClick={() => flow.setReview('approved')}><Check size={15}/>模拟审核通过</button></div>{flow.review === 'approved' && <div className="basis-note"><Check size={15}/>已完成演示审核。修改用量、价格或定价方式后，会自动回到模拟草稿。</div>}</div></div></StepShell>
}

export function MockStages(props: MockStagesProps) {
  const localFlow = useMockFlow(props.parts, props.orderQty)
  const flow = props.flow ?? localFlow
  if (props.stage === 2) return <Materials flow={flow} next={props.onNext} back={props.onBack}/>
  if (props.stage === 3) return <Quote flow={flow} orderQty={props.orderQty} next={props.onNext} back={props.onBack}/>
  return <Review flow={flow} back={props.onBack} goMaterials={() => props.onStage ? props.onStage(2) : props.onBack()}/>
}
