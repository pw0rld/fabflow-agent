'use client'

import './batch-cost-chart.css'

export type BatchCostTotals = {
  material_cost?: string | null
  process_cost?: string | null
  purchase_cost?: string | null
  setup_cost?: string | null
  tooling_cost?: string | null
  other_cost?: string | null
  total_cost?: string | null
}

type Props = { totals: BatchCostTotals; unpricedLines: number }
const ZERO = BigInt(0), HUNDRED = BigInt(100)
const definitions = [
  { id: 'material', key: 'material_cost', name: '材料费', number: '01' },
  { id: 'process', key: 'process_cost', name: '加工费', number: '02' },
  { id: 'purchase', key: 'purchase_cost', name: '外购费', number: '03' },
  { id: 'setup', key: 'setup_cost', name: '准备费', number: '04' },
  { id: 'tooling', key: 'tooling_cost', name: '模具费', number: '05' },
  { id: 'other', key: 'other_cost', name: '其他费用', number: '06' },
] as const

// Backend monetary totals are non-negative CNY strings, rounded to cents.
// Exact integer comparison is used only to check chart consistency. Prices
// and the quotation itself are never recalculated in this component.
function cents(value: string | null | undefined): bigint | null {
  if (typeof value !== 'string' || value.length > 60) return null
  const match = /^(0|[1-9]\d*)(?:\.(\d{1,2}))?$/.exec(value)
  if (!match) return null
  return BigInt(match[1]) * HUNDRED + BigInt((match[2] || '').padEnd(2, '0'))
}

function money(value: bigint | null): string {
  if (value === null) return '未提供'
  const whole = (value / HUNDRED).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return `¥${whole}.${(value % HUNDRED).toString().padStart(2, '0')}`
}

function percentage(value: bigint | null, total: bigint | null, mismatch: boolean): string {
  if (value === null) return '占比未提供'
  if (mismatch) return '占比待核对'
  if (value === ZERO) return '0.00%'
  if (total === null || total === ZERO) return '暂无占比'
  const hundredths = (value * BigInt(10000) + total / BigInt(2)) / total
  if (hundredths === ZERO) return '小于 0.01%'
  const whole = hundredths / HUNDRED, fraction = hundredths % HUNDRED
  return `${whole}.${fraction.toString().padStart(2, '0')}%`
}

function width(value: bigint, total: bigint): string {
  // Six decimal places are sufficient for a display width; all source money
  // stays in exact integer cents and visible labels retain backend amounts.
  return `${Number(value * BigInt(100000000) / total) / 1000000}%`
}

export default function BatchCostChart({ totals, unpricedLines }: Props) {
  const structured = Object.prototype.hasOwnProperty.call(totals, 'tooling_cost') || Object.prototype.hasOwnProperty.call(totals, 'other_cost')
  const rows = definitions.filter(item => structured || !['tooling', 'other'].includes(item.id)).map(item => ({ ...item, amount: cents(totals[item.key]) }))
  const total = cents(totals.total_cost)
  const known = rows.filter(row => row.amount !== null)
  const missing = rows.length - known.length
  const subtotal = known.reduce((sum, row) => sum + (row.amount as bigint), ZERO)
  const mismatch = total !== null && (subtotal > total || (missing === 0 && subtotal !== total))
  const drawable = total !== null && total > ZERO && subtotal > ZERO && !mismatch
  const unpriced = Number.isSafeInteger(unpricedLines) && unpricedLines >= 0 ? unpricedLines : null
  const barLabel = rows.map(row => `${row.name} ${money(row.amount)}，${percentage(row.amount, total, mismatch)}`).join('；')
  const remainder = drawable && total !== null && total > subtotal ? total - subtotal : ZERO
  let emptyText = '尚未提供成本明细，暂无可展示的成本构成。'
  if (mismatch) emptyText = '各项明细与成本合计尚未对应，暂不绘制占比。'
  else if (known.length && total === null) emptyText = '成本合计尚未提供，已知明细保留在下方。'
  else if (total === ZERO) emptyText = '当前没有正的已计价成本，暂无可展示的占比。'

  return <section className="bqc" data-categories={rows.length} data-testid="bq-cost-chart" aria-label="已计价部分的成本构成">
    <div className="bqc-heading">
      <div><p className="bqc-eyebrow">COST BREAKDOWN</p><h3>已计价部分的成本构成</h3><p className="bqc-description">按成本金额展示，报价加成另计。</p></div>
      <div className="bqc-total"><span>成本合计</span><strong data-testid="bq-cost-total">{money(total)}</strong></div>
    </div>
    {drawable && total !== null ? <div className="bqc-bar" role="img" aria-label={barLabel + (remainder > ZERO ? '；其余成本构成待核对' : '')} data-testid="bq-cost-bar" data-complete={missing === 0 && subtotal === total ? 'true' : 'false'}>
      {rows.map(row => row.amount !== null && <span key={row.id} className={`bqc-segment bqc-${row.id}`} style={{ width: width(row.amount, total) }} title={`${row.name} ${money(row.amount)} · ${percentage(row.amount, total, false)}`} aria-hidden="true" data-testid={`bq-cost-segment-${row.id}`} />)}
      {remainder > ZERO && <span className="bqc-segment bqc-unallocated" style={{ width: width(remainder, total) }} title="成本构成待核对" aria-hidden="true" data-testid="bq-cost-unallocated" />}
    </div> : <div className="bqc-empty" role="status" data-testid="bq-cost-empty">{emptyText}</div>}
    <ul className="bqc-legend" aria-label={`${rows.length === 6 ? '六' : '四'}类成本明细`}>
      {rows.map(row => <li key={row.id} className={`bqc-item bqc-${row.id}`} data-testid={`bq-cost-item-${row.id}`}>
        <span className="bqc-marker" aria-hidden="true">{row.number}</span>
        <div className="bqc-item-text"><span className="bqc-name">{row.name}</span><strong data-testid={`bq-cost-amount-${row.id}`}>{money(row.amount)}</strong><span className="bqc-share" data-testid={`bq-cost-share-${row.id}`}>{percentage(row.amount, total, mismatch)}</span></div>
      </li>)}
    </ul>
    {mismatch && <p className="bqc-caution" role="status" data-testid="bq-cost-mismatch">已提供明细小计 {money(subtotal)} 与后端成本合计 {money(total)} 不一致，请核对。</p>}
    {!mismatch && missing > 0 && known.length > 0 && <p className="bqc-caution" data-testid="bq-cost-missing">{missing} 类成本未提供，已知明细小计 {money(subtotal)}；尚不能确认完整成本构成。</p>}
    <p className="bqc-footnote" data-testid="bq-cost-scope">{unpriced === null ? '未计价项数待核对。' : unpriced > 0 ? `${unpriced} 行未计价项目未包含在图中。` : '图中仅含本次已计价项目。'}预估与演示费用的依据见报价明细。</p>
  </section>
}
