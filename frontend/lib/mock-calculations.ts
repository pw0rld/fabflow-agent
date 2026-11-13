import { MOCK_ASSUMPTIONS, type MockAssumption, type MockPartIdentity } from './demo-data'

export type MockPricingPolicy = 'cost_plus' | 'gross_margin'
export type MockCalculationInput = {
  parts: MockPartIdentity[]
  orderQty: number
  wastePercent: number | null
  pricingPercent: number | null
  policy: MockPricingPolicy
  materialRates: Record<string, number | null>
  incomplete: boolean
}
export type MockCalculatedRow = {
  identity: MockPartIdentity
  assumption: MockAssumption
  id: string
  name: string
  code: string
  version: string
  qty: number | null
  netKg: number | null
  chargedKg: number | null
  batchKg: number | null
  flatAreaM2: number | null
  materialRateYuanKg: number | null
  materialUnitCents: number | null
  processUnitCents: number
  preparationUnitCents: number
  costUnitCents: number | null
  costLineCents: number | null
  quoteUnitCents: number | null
  quoteLineCents: number | null
  issues: string[]
}
export type MockCalculation = {
  rows: MockCalculatedRow[]
  complete: boolean
  costTotalCents: number | null
  quoteTotalCents: number | null
  knownQuoteCents: number
  issueCount: number
}

const validNumber = (value: number | null, min: number, max: number): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max

export function calculateMockFlow(input: MockCalculationInput): MockCalculation {
  const orderValid = Number.isSafeInteger(input.orderQty) && input.orderQty > 0 && input.orderQty <= 1_000_000
  const wasteValid = validNumber(input.wastePercent, 0, 100)
  const pricingValid = validNumber(input.pricingPercent, 0, input.policy === 'gross_margin' ? 95 : 500)
  const rows: MockCalculatedRow[] = input.parts.map((identity, index) => {
    const assumption = MOCK_ASSUMPTIONS[index % MOCK_ASSUMPTIONS.length]
    const missing = input.incomplete && index === input.parts.length - 1
    const flatAreaM2 = missing ? null : assumption.flatAreaM2
    const providedRate = Object.prototype.hasOwnProperty.call(input.materialRates, identity.id)
      ? input.materialRates[identity.id]
      : assumption.materialRateYuanKg
    const materialRateYuanKg = missing ? null : validNumber(providedRate, 0.01, 1_000_000) ? providedRate : null
    const qty = orderValid ? assumption.perSet * input.orderQty : null
    const netKg = flatAreaM2 === null ? null : flatAreaM2 * assumption.thicknessMm * assumption.densityKgM3 / 1000
    const chargedKg = netKg === null || !wasteValid ? null : netKg * (1 + input.wastePercent! / 100)
    const materialUnitCents = chargedKg === null || materialRateYuanKg === null ? null : Math.round(chargedKg * materialRateYuanKg * 100)
    const costUnitCents = materialUnitCents === null ? null : materialUnitCents + assumption.processFeeCentsPiece + assumption.preparationFeeCentsPiece
    const quoteUnitCents = costUnitCents === null || !pricingValid ? null : Math.round(input.policy === 'gross_margin'
      ? costUnitCents / (1 - input.pricingPercent! / 100)
      : costUnitCents * (1 + input.pricingPercent! / 100))
    const issues: string[] = []
    if (flatAreaM2 === null) issues.push('模拟展开面积待补充')
    if (materialRateYuanKg === null) issues.push('模拟材料价格待补充')
    if (!orderValid) issues.push('演示订单套数无效')
    if (!wasteValid) issues.push('模拟损耗率需为 0–100%')
    if (!pricingValid) issues.push(input.policy === 'gross_margin' ? '模拟目标毛利率需为 0–95%' : '模拟成本加成率需为 0–500%')
    return {
      identity, assumption, id: identity.id,
      name: identity.name || `零件 ${index + 1}`, code: identity.code || '图号未识别', version: identity.version || '版本未识别',
      qty, netKg, chargedKg, flatAreaM2, materialRateYuanKg,
      batchKg: chargedKg === null || qty === null ? null : chargedKg * qty,
      materialUnitCents, processUnitCents: assumption.processFeeCentsPiece, preparationUnitCents: assumption.preparationFeeCentsPiece,
      costUnitCents, costLineCents: costUnitCents === null || qty === null ? null : costUnitCents * qty,
      quoteUnitCents, quoteLineCents: quoteUnitCents === null || qty === null ? null : quoteUnitCents * qty,
      issues,
    }
  })
  const complete = rows.length > 0 && rows.every(row => row.issues.length === 0)
  const knownQuoteCents = rows.reduce((sum, row) => sum + (row.quoteLineCents ?? 0), 0)
  return {
    rows, complete,
    costTotalCents: rows.length > 0 && rows.every(row => row.costLineCents !== null) ? rows.reduce((sum, row) => sum + row.costLineCents!, 0) : null,
    quoteTotalCents: complete ? knownQuoteCents : null,
    knownQuoteCents, issueCount: rows.reduce((sum, row) => sum + row.issues.length, 0),
  }
}

export function centsToYuan(cents: number | null): string {
  return cents === null ? '待补充' : `¥ ${(cents / 100).toFixed(2)}`
}

function csvCell(value: string | number): string {
  // Spreadsheet formula prefixes must remain text even when a parsed name is malicious.
  const text = String(value)
  const safe = /^[\s\uFEFF]*[=+\-@]/.test(text) ? `'${text}` : text
  return `"${safe.replace(/"/g, '""')}"`
}

export function buildMockCsv(calculation: MockCalculation, orderQty: number): string {
  const lines: (string | number)[][] = [
    ['DEMO 模拟 BOM 与报价，不可对外发送'],
    ['零件身份可来自 AI 解析；以下用量、数量、价格、工序与金额全部为独立演示假设'],
    ['模拟订单套数', orderQty, '口径', 'CNY / 未税 / 不含运费'],
    ['零件名称', '识别图号', '识别版本', '识别材料（仅标签）', '识别板厚（仅标签）', '模拟材料', '模拟板厚 mm', '模拟每套用量', '模拟总需求量', '模拟净用量 kg/件', '模拟计价用量 kg/件', '模拟材料单价 元/kg', '模拟材料费 元/件', '模拟加工费 元/件', '模拟准备费分摊 元/件', '模拟报价单价 元/件', '模拟报价行金额 元', '模拟工序', '状态'],
    ...calculation.rows.map(row => [
      row.name, row.code, row.version, row.identity.material || '未识别', row.identity.thickness || '未识别', row.assumption.material,
      row.assumption.thicknessMm, row.assumption.perSet, row.qty ?? '待补充',
      row.netKg === null ? '待补充' : row.netKg.toFixed(6), row.chargedKg === null ? '待补充' : row.chargedKg.toFixed(6), row.materialRateYuanKg ?? '待补充',
      row.materialUnitCents === null ? '待补充' : (row.materialUnitCents / 100).toFixed(2),
      (row.processUnitCents / 100).toFixed(2), (row.preparationUnitCents / 100).toFixed(2),
      row.quoteUnitCents === null ? '待补充' : (row.quoteUnitCents / 100).toFixed(2),
      row.quoteLineCents === null ? '待补充' : (row.quoteLineCents / 100).toFixed(2), row.assumption.process,
      row.issues.length ? row.issues.join('；') : '模拟数据完整，非正式报价',
    ]),
    ['模拟合计', calculation.quoteTotalCents === null ? '未完整' : (calculation.quoteTotalCents / 100).toFixed(2)],
    ['金额口径', '材料费按件保留分；报价单价保留分；行金额=已显示单价×模拟数量；合计逐行相加'],
  ]
  return '\uFEFF' + lines.map(line => line.map(csvCell).join(',')).join('\r\n')
}
