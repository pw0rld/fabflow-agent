'use client'
import { Fragment } from 'react'
type Process = { id: string; label: string; quantity_per_piece?: string | null; quantity_total?: string | null; unit: string; rate_cny?: string | null; amount_cny?: string | null; status: string }
type Plan = { processes?: Process[]; blockers?: { code: string; message: string }[]; estimated_cost_subtotal?: string | null }
const value = (raw: unknown) => raw == null || !Number.isFinite(Number(raw)) ? '待核定' : Number(raw).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
export default function BatchProcessCostPanel({ lines }: { lines: unknown[] }) {
  return <section className="bq-process-costs" data-testid="bq-process-costs"><div className="bq-section-heading"><div><h2>逐件工艺核价</h2><p>工序工程量 × 采用费率；材料、重量与计价范围分别保留。</p></div><span className="bq-badge">演示采用值 · 待工程核对</span></div><div className="bq-material-price-table"><table><thead><tr><th>零件</th><th>工序</th><th>单件工程量</th><th>本单工程量</th><th>单位</th><th>采用费率（元）</th><th>工序费用（元）</th></tr></thead><tbody>{lines.map((raw, index) => {
    const line = raw as { id?: string; part_name?: string; manufacturing?: Plan }, plan = line.manufacturing
    return <Fragment key={line.id || index}>{(plan?.processes || []).map((process, processIndex) => <tr key={process.id}><td>{processIndex === 0 ? <><strong>{line.part_name}</strong><small>{line.id}</small></> : null}</td><td>{process.label}{process.status === 'pending_review' && <small>原生候选 · 待核对</small>}</td><td>{process.quantity_per_piece == null ? '按单' : value(process.quantity_per_piece)}</td><td>{value(process.quantity_total)}</td><td>{process.unit}</td><td>{value(process.rate_cny)}</td><td>{value(process.amount_cny)}</td></tr>)}{!!plan?.blockers?.length && <tr><td><strong>{line.part_name}</strong><small>{line.id}</small></td><td colSpan={6}>{plan.blockers.map(blocker => <p key={blocker.code}>{blocker.message}</p>)}<small>缺项未按零费用处理；本件工艺范围尚未完整。</small></td></tr>}</Fragment>
  })}</tbody></table></div><p className="bq-note">原生贯通孔槽在切割轮廓中计量，不重复认定为钻孔。暂估用料与切割工程量用于估算方案价，不作为生产展开图或真实刀路；折弯方向、顺序及实际工厂费率仍需审核。核价调整另存报价版本。</p></section>
}
