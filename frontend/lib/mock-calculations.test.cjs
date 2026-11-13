// Run with the project Node runtime: node lib/mock-calculations.test.cjs
const assert = require('node:assert/strict')
const fs = require('node:fs')
const ts = require('typescript')
const previousLoader = require.extensions['.ts']
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText, filename)
const { DEMO_PARTS } = require('./demo-data.ts')
const { calculateMockFlow, buildMockCsv } = require('./mock-calculations.ts')
if (previousLoader) require.extensions['.ts'] = previousLoader
else delete require.extensions['.ts']

const base = { parts: DEMO_PARTS, orderQty: 10, wastePercent: 8, pricingPercent: 20, policy: 'cost_plus', materialRates: {}, incomplete: false }
let checks = 0
function check(name, fn) { fn(); checks += 1; process.stdout.write(`PASS ${name}\n`) }
const full = calculateMockFlow(base)

check('complete scenario has material costs and full totals', () => {
  assert.equal(full.complete, true)
  assert.equal(full.rows.length, 4)
  assert.equal(full.issueCount, 0)
  assert.ok(full.quoteTotalCents > full.costTotalCents)
})
check('displayed per-piece cents multiply exactly into line cents and total', () => {
  for (const orderQty of [1, 7, 10, 37, 99, 1000]) {
    for (const pricingPercent of [0, 18, 20, 33.3, 95]) {
      const result = calculateMockFlow({ ...base, orderQty, pricingPercent })
      for (const row of result.rows) assert.equal(row.quoteLineCents, row.quoteUnitCents * row.qty)
      assert.equal(result.quoteTotalCents, result.rows.reduce((sum, row) => sum + row.quoteLineCents, 0))
    }
  }
})
check('mock order quantity scales each row and total exactly', () => {
  const double = calculateMockFlow({ ...base, orderQty: 20 })
  assert.equal(double.quoteTotalCents, full.quoteTotalCents * 2)
  for (let i = 0; i < double.rows.length; i++) assert.equal(double.rows[i].qty, full.rows[i].qty * 2)
})
check('mock waste changes charged weight without changing net weight', () => {
  const noWaste = calculateMockFlow({ ...base, wastePercent: 0 })
  assert.equal(noWaste.rows[0].netKg, full.rows[0].netKg)
  assert.equal(noWaste.rows[0].chargedKg, noWaste.rows[0].netKg)
  assert.ok(noWaste.quoteTotalCents < full.quoteTotalCents)
})
check('mock material price edit recalculates only its row', () => {
  const expensive = calculateMockFlow({ ...base, materialRates: { plate: 16.8 } })
  assert.ok(expensive.rows[0].quoteLineCents > full.rows[0].quoteLineCents)
  assert.equal(expensive.rows[1].quoteLineCents, full.rows[1].quoteLineCents)
})
check('missing geometry does not become zero and prevents a complete total', () => {
  const missing = calculateMockFlow({ ...base, incomplete: true })
  assert.equal(missing.complete, false)
  assert.equal(missing.rows.at(-1).netKg, null)
  assert.equal(missing.rows.at(-1).quoteUnitCents, null)
  assert.equal(missing.rows.at(-1).quoteLineCents, null)
  assert.equal(missing.rows.at(-1).materialRateYuanKg, null)
  assert.equal(missing.quoteTotalCents, null)
  assert.equal(missing.costTotalCents, null)
  assert.equal(missing.issueCount, 2)
})
check('null, zero, negative, infinite, and NaN rates remain unknown', () => {
  for (const rate of [null, 0, -1, Infinity, NaN]) {
    const result = calculateMockFlow({ ...base, materialRates: { plate: rate } })
    assert.equal(result.complete, false)
    assert.equal(result.rows[0].materialUnitCents, null)
    assert.equal(result.quoteTotalCents, null)
  }
})
check('invalid quantities and waste block calculation', () => {
  for (const orderQty of [0, -1, 1.5, Infinity]) assert.equal(calculateMockFlow({ ...base, orderQty }).quoteTotalCents, null)
  for (const wastePercent of [null, -1, 101, NaN]) assert.equal(calculateMockFlow({ ...base, wastePercent }).quoteTotalCents, null)
})
check('cost-plus and gross-margin use distinct formulas', () => {
  const margin = calculateMockFlow({ ...base, policy: 'gross_margin' })
  const row = full.rows[0]
  assert.equal(row.quoteUnitCents, Math.round(row.costUnitCents * 1.2))
  assert.equal(margin.rows[0].quoteUnitCents, Math.round(row.costUnitCents / 0.8))
  assert.ok(margin.quoteTotalCents > full.quoteTotalCents)
  assert.equal(calculateMockFlow({ ...base, policy: 'gross_margin', pricingPercent: 100 }).quoteTotalCents, null)
})
check('parsed dimensions, material and thickness are identity labels only', () => {
  const radicallyDifferent = DEMO_PARTS.map(row => ({ ...row, material: '航空钛材', thickness: '999 mm', size: '999 × 999 × 999 mm' }))
  const result = calculateMockFlow({ ...base, parts: radicallyDifferent })
  assert.equal(result.quoteTotalCents, full.quoteTotalCents)
  assert.equal(result.rows[0].identity.thickness, '999 mm')
  assert.equal(result.rows[0].assumption.thicknessMm, 2)
})
check('input objects and parsed evidence are never mutated', () => {
  const copy = JSON.stringify(base)
  calculateMockFlow(base)
  assert.equal(JSON.stringify(base), copy)
})
check('CSV includes mock watermark, actual identity labels, preparation fee and unknowns', () => {
  const csv = buildMockCsv(calculateMockFlow({ ...base, incomplete: true }), 10)
  assert.ok(csv.startsWith('\uFEFF'))
  for (const token of ['DEMO 模拟', '不可对外发送', '识别板厚（仅标签）', '模拟准备费分摊', '未完整', '待补充']) assert.ok(csv.includes(token))
})
check('CSV makes parsed formula-looking text inert and escapes quotes', () => {
  const parts = [{ ...DEMO_PARTS[0], name: '=1+1', code: ' =HYPERLINK("https://example.test")' }]
  const csv = buildMockCsv(calculateMockFlow({ ...base, parts }), 10)
  assert.ok(csv.includes('"\'=1+1"'))
  assert.ok(csv.includes('"\' =HYPERLINK(""https://example.test"")"'))
})
process.stdout.write(`${checks} mock-calculation checks passed\n`)
