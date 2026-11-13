/** Browser-only, source-bound BOM and simulated costing. No business record or model writes. */
import { buildImageEngineering } from './image-engineering'
export type BomOrigin = 'candidate' | 'manual' | 'demo' | 'unknown'
export type BomEvidence = { regionId: string | null; rawText: string; bbox?: number[] }
export type BomField = {
  value: string | null
  unit: string | null
  origin: BomOrigin
  status: 'pending_review'
  evidence: BomEvidence[]
  sourceParameterIds: string[]
  candidateValue?: string | null
}
export type ImageBomSource = {
  jobId: string | null
  attempt: number | null
  revision: number | null
  sourceSha256?: string | null
  manifest: unknown
}
export type ImageBomRow = {
  id: string
  partId: string
  name: BomField
  material: BomField
  thickness: BomField
  outerSize: BomField
  surfaceFinish: BomField
  quantityPerSet: BomField
  materialUsageKg: BomField
  materialRateCnyKg: BomField
  operationFeeCnyPiece: BomField
  setupFeeCny: BomField
  processMode: 'comprehensive' | 'processes'
  processes: ImageProcessCharge[]
  materialBasis: ImageMaterialBasis
}
export type ImageBomRowField = Exclude<keyof ImageBomRow, 'id' | 'partId' | 'processMode' | 'processes' | 'materialBasis'>
export type ImageProcessUnit = 'piece' | 'bend' | 'spot' | 'hole' | 'm' | 'm2' | 'hour' | 'unknown'
export type ImageProcessCharge = {
  id: string
  candidateId: string
  title: string
  kind: string
  adopted: boolean
  basis: 'drawing_requirement' | 'suggested'
  evidence: BomEvidence[]
  sourceIds: string[]
  measure: BomField
  unit: ImageProcessUnit
  rate: BomField
  quantityBasis: string
  explanation: string
}
export type ImageMaterialBasis = { netAreaMm2: BomField; densityKgM3: BomField; utilizationPercent: BomField; adoptedUsageKg: string | null }
export type ImageMaterialBasisField = 'netAreaMm2' | 'densityKgM3' | 'utilizationPercent'
export type ImageMaterialBasisResult = { ok: true; netMassKg: string; materialUsageKg: string; formula: string } | { ok: false; issues: string[] }
export type ImageBomDraftField = 'orderSets' | 'policyMethod' | 'policyPercent'
export type UnassignedBomParameter = { id: string; label: string; value: string | null; unit: string | null; evidence: BomEvidence[] }
export type ImageBomDraft = {
  schemaVersion: 1
  sourceFingerprint: string
  sourceName: string
  sourceMode: string
  rows: ImageBomRow[]
  orderSets: BomField
  policyMethod: BomField
  policyPercent: BomField
  warnings: string[]
  unassignedParameters: UnassignedBomParameter[]
  simulated: true
  engineering_approved: false
}
export type ImageQuoteIssue = { rowId?: string; field: string; message: string }
export type ImageQuoteRow = {
  rowId: string
  partId: string
  name: string
  material: string
  thickness: string
  quantityPerSet: string
  orderSets: string
  quantity: string
  usageKg: string
  materialRateCnyKg: string
  operationFeeCnyPiece: string
  materialCostCents: string
  operationCostCents: string
  setupCostCents: string
  totalCostCents: string
  unitCostCents: string
  unitQuoteCents: string
  lineQuoteCents: string
  processMode: 'comprehensive' | 'processes'
  operations: ImageQuotedProcess[]
  materialBasis: ImageMaterialBasisResult
}
export type ImageQuotedProcess = { id: string; title: string; unit: ImageProcessUnit; measure: string; rate: string; totalCostCents: string; measureOrigin: BomOrigin; rateOrigin: BomOrigin; quantityBasis: string; explanation: string }
export type ImageDemoQuoteSuccess = {
  ok: true
  simulated: true
  engineering_approved: false
  sourceFingerprint: string
  policy: { method: 'cost_plus' | 'gross_margin'; percent: string }
  rows: ImageQuoteRow[]
  totals: { quantity: string; materialCostCents: string; operationCostCents: string; setupCostCents: string; totalCostCents: string; lineQuoteCents: string }
  notes: string[]
}
export type ImageDemoQuote = ImageDemoQuoteSuccess | { ok: false; issues: ImageQuoteIssue[] }

type RecordValue = Record<string, unknown>
const ZERO = BigInt(0), HUNDRED = BigInt(100)
const ROW_FIELDS: ImageBomRowField[] = ['name', 'material', 'thickness', 'outerSize', 'surfaceFinish', 'quantityPerSet', 'materialUsageKg', 'materialRateCnyKg', 'operationFeeCnyPiece', 'setupFeeCny']
const DRAFT_FIELDS: ImageBomDraftField[] = ['orderSets', 'policyMethod', 'policyPercent']
const FIELD_UNITS: Partial<Record<ImageBomRowField | ImageBomDraftField, string>> = {
  quantityPerSet: '件/套', materialUsageKg: 'kg/件', materialRateCnyKg: 'CNY/kg', operationFeeCnyPiece: 'CNY/件', setupFeeCny: 'CNY/行', orderSets: '套', policyPercent: '%',
}
const LABELS: Record<string, string> = {
  name: '零件名称', material: '材料', thickness: '板厚', quantityPerSet: '每套数量', materialUsageKg: '单件用料 kg', materialRateCnyKg: '材料单价 CNY/kg', operationFeeCnyPiece: '单件综合工序费', setupFeeCny: '本行一次准备费', orderSets: '订单套数', policyMethod: '定价方式', policyPercent: '定价比例',
}
const record = (value: unknown): RecordValue => value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {}
const array = (value: unknown): unknown[] => Array.isArray(value) ? value : []
const text = (value: unknown, max = 4000): string => typeof value === 'string' ? value.slice(0, max) : ''
const strValue = (value: unknown): string | null => {
  if (typeof value === 'string') return value.trim() ? value.slice(0, 4000) : null
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  if (Array.isArray(value) && value.length <= 20 && value.every(item => typeof item === 'string' || typeof item === 'number' && Number.isFinite(item))) return value.join(' × ').slice(0, 4000)
  return null
}
const blank = (unit: string | null = null): BomField => ({ value: null, unit, origin: 'unknown', status: 'pending_review', evidence: [], sourceParameterIds: [] })
const copyField = (field: BomField): BomField => ({ ...field, evidence: field.evidence.map(item => ({ ...item, ...(item.bbox ? { bbox: [...item.bbox] } : {}) })), sourceParameterIds: [...field.sourceParameterIds] })
const cloneDraft = (draft: ImageBomDraft): ImageBomDraft => ({
  ...draft,
  rows: draft.rows.map(row => {
    const next = { ...row }
    for (const field of ROW_FIELDS) next[field] = copyField(row[field])
    next.processes = row.processes.map(process => ({ ...process, evidence: process.evidence.map(item => ({ ...item })), sourceIds: [...process.sourceIds], measure: copyField(process.measure), rate: copyField(process.rate) }))
    next.materialBasis = { ...row.materialBasis, netAreaMm2: copyField(row.materialBasis.netAreaMm2), densityKgM3: copyField(row.materialBasis.densityKgM3), utilizationPercent: copyField(row.materialBasis.utilizationPercent) }
    return next
  }),
  orderSets: copyField(draft.orderSets), policyMethod: copyField(draft.policyMethod), policyPercent: copyField(draft.policyPercent),
  warnings: [...draft.warnings], unassignedParameters: draft.unassignedParameters.map(item => ({ ...item, evidence: item.evidence.map(e => ({ ...e })) })),
})

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`
  if (value && typeof value === 'object') {
    const obj = value as RecordValue
    return `{${Object.keys(obj).filter(key => obj[key] !== undefined).sort().map(key => `${JSON.stringify(key)}:${stableJson(obj[key])}`).join(',')}}`
  }
  return JSON.stringify(value) ?? 'null'
}

/** Full canonical content avoids treating an edited manifest with an unchanged revision as current. */
export function sourceFingerprint(source: ImageBomSource): string {
  const manifest = record(source.manifest), src = record(manifest.source)
  return stableJson({ job: source.jobId, attempt: source.attempt, revision: source.revision, sha: source.sourceSha256 || src.sha256 || null, manifest })
}
export function isDraftCurrent(draft: ImageBomDraft, source: ImageBomSource): boolean {
  return draft.schemaVersion === 1 && draft.sourceFingerprint === sourceFingerprint(source)
}

function evidenceOf(parameter: RecordValue): BomEvidence[] {
  return array(parameter.evidence).map(item => {
    const evidence = record(item), bbox = array(evidence.bbox)
    return {
      regionId: text(evidence.region_id, 200) || null,
      rawText: text(evidence.raw_text, 8000),
      ...(bbox.length === 4 && bbox.every(n => typeof n === 'number' && Number.isFinite(n)) ? { bbox: bbox as number[] } : {}),
    }
  })
}

function fieldFromParameters(parameters: RecordValue[], fallbackUnit: string | null = null): BomField {
  if (!parameters.length) return blank(fallbackUnit)
  const values = parameters.map(item => strValue(item.value)).filter((value): value is string => value !== null)
  const distinctValues = [...new Set(values)]
  const units = [...new Set(parameters.map(item => text(item.unit, 100) || '').filter(Boolean))]
  const usable = parameters.every(item => !item.review_invalidated_by_region && !['questioned', 'rejected', 'unreadable', 'excluded', 'invalid'].includes(text(item.status)) && item.basis !== 'uncertain')
  const value = usable && distinctValues.length === 1 && values.length === parameters.length && units.length <= 1 ? distinctValues[0] : null
  return {
    value, unit: units[0] || fallbackUnit, origin: 'candidate', status: 'pending_review',
    evidence: parameters.flatMap(evidenceOf), sourceParameterIds: parameters.map(item => text(item.id, 200)).filter(Boolean), candidateValue: value,
  }
}

function parameterKind(parameter: RecordValue): ImageBomRowField | 'orderSets' | null {
  const id = text(parameter.id, 200).toLowerCase(), category = text(parameter.category, 100).toLowerCase(), label = text(parameter.label, 200)
  // Hole counts and generic order totals cannot be reinterpreted as per-set BOM quantities.
  if (/^(order_sets|set_count|order_set_count)$/.test(id) || /^(订单套数|订购套数|订单组数)$/.test(label)) return 'orderSets'
  if (/^(quantity_per_set|qty_per_set|pieces_per_set)$/.test(id) || /^(每套数量|单套数量|每组数量)$/.test(label)) return 'quantityPerSet'
  if (category === 'material' || /^(material|material_grade|material_name)$/.test(id) || /^(材料|材料牌号|材质|客户指定牌号)$/.test(label)) return 'material'
  if (category === 'thickness' || /^(thickness|plate_thickness|sheet_thickness|thickness_mm)$/.test(id) || /^(板厚|料厚|材料厚度)$/.test(label)) return 'thickness'
  if (category === 'outer_size' || /^(overall_size|outer_size|bounding_dimensions)$/.test(id) || /^(外形尺寸|标注外形尺寸)$/.test(label)) return 'outerSize'
  if (category === 'surface' || /^(surface_finish|surface_treatment|finish)$/.test(id) || /^(表面处理|表面要求)$/.test(label)) return 'surfaceFinish'
  return null
}

/** A parameter belongs to one part only if every evidence reference resolves to that part. */
function parameterPart(parameter: RecordValue, regionParts: Map<string, string>, validParts: Set<string>): string | null {
  const evidence = evidenceOf(parameter)
  if (!evidence.length) return null
  const owners = new Set<string>()
  for (const item of evidence) {
    const owner = item.regionId ? regionParts.get(item.regionId) : null
    if (!owner || !validParts.has(owner)) return null
    owners.add(owner)
  }
  return owners.size === 1 ? [...owners][0] : null
}

function engineeringEvidence(value: unknown): BomEvidence[] {
  return array(value).map(item => {
    const source = record(item), bbox = array(source.bbox)
    return { regionId: text(source.regionId || source.region_id, 200) || null, rawText: text(source.rawText || source.raw_text, 8000), ...(bbox.length === 4 && bbox.every(n => typeof n === 'number' && Number.isFinite(n)) ? { bbox: bbox as number[] } : {}) }
  })
}
function engineeringField(value: unknown, unit: string): BomField {
  const source = record(value), result = strValue(source.value)
  if (!result || source.usable === false) return blank(unit)
  return { ...blank(unit), value: result, candidateValue: result, origin: 'candidate', evidence: engineeringEvidence(source.evidence), sourceParameterIds: array(source.sourceIds).map(item => text(item, 200)).filter(Boolean) }
}
const PROCESS_UNITS: ImageProcessUnit[] = ['piece', 'bend', 'spot', 'hole', 'm', 'm2', 'hour', 'unknown']
export function processUnitLabel(unit: ImageProcessUnit): string {
  return ({ piece: '件', bend: '折', spot: '焊点', hole: '孔', m: '米', m2: '平方米', hour: '小时', unknown: '待明确' })[unit]
}
function processUnit(quantityUnit: unknown, kind: string): ImageProcessUnit {
  const value = text(quantityUnit, 100).trim().toLowerCase().replace(/\s/g, '')
  if (/^(件|piece|pieces|pcs?|ea|each)$/.test(value)) return 'piece'
  if (/^(折|道|折弯|bends?|table_rows|rows?)$/.test(value)) return 'bend'
  if (/^(焊点|点|spots?|weld_spots?)$/.test(value)) return 'spot'
  if (/^(孔|holes?)$/.test(value)) return 'hole'
  if (/^(m|米|mm|毫米)$/.test(value)) return 'm'
  if (/^(m2|m²|平方米|mm2|mm²|平方毫米)$/.test(value)) return 'm2'
  if (/^(h|hr|hours?|小时)$/.test(value)) return 'hour'
  return kind === 'cut' ? 'm' : kind === 'bend' ? 'bend' : kind === 'deburr' || kind === 'finish' ? 'piece' : 'unknown'
}
function scaledDecimal(value: string, divisor: bigint): string | null {
  const parsed = decimal(value, 9, 12)
  return parsed ? rationalText(parsed.numerator, parsed.denominator * divisor, 9) : null
}
function processCharge(value: unknown, index: number): ImageProcessCharge {
  const item = record(value), quantity = record(item.quantity), kind = text(item.kind, 100) || 'other'
  const unit = processUnit(quantity.unit, kind), measure = engineeringField(item.quantity, processUnitLabel(unit))
  const rawUnit = text(quantity.unit, 100).trim().toLowerCase().replace(/\s/g, '')
  if (measure.value && /^(mm|毫米)$/.test(rawUnit)) measure.value = scaledDecimal(measure.value, BigInt(1000))
  if (measure.value && /^(mm2|mm²|平方毫米)$/.test(rawUnit)) measure.value = scaledDecimal(measure.value, BigInt(1000000))
  measure.candidateValue = measure.value
  return { id: text(item.id, 300) || `process-${index + 1}`, candidateId: text(item.id, 300), title: text(item.title, 300) || '待核对工序', kind, adopted: false, basis: item.origin === 'suggested' ? 'suggested' : 'drawing_requirement', evidence: engineeringEvidence(item.evidence), sourceIds: array(item.sourceIds).map(id => text(id, 300)).filter(Boolean), measure, unit, rate: blank(`CNY/${processUnitLabel(unit)}`), quantityBasis: text(quantity.basis, 100) || 'unknown', explanation: text(quantity.explanation, 4000) || text(item.description, 4000) }
}

export function createImageBomDraft(source: ImageBomSource): ImageBomDraft {
  const manifest = record(source.manifest), src = record(manifest.source)
  const engineering = buildImageEngineering(source)
  const foundParts = new Map<string, RecordValue>()
  const parts = [...array(manifest.parts), ...(Object.keys(record(manifest.part)).length ? [manifest.part] : [])]
  for (const item of parts) {
    const part = record(item), id = text(part.id, 200)
    if (id && !foundParts.has(id)) foundParts.set(id, part)
  }
  const validParts = new Set(foundParts.keys()), regionParts = new Map<string, string>()
  const seenRegionOwners = new Map<string, string | null>()
  const ambiguousRegionIds = new Set<string>()
  for (const item of array(manifest.regions)) {
    const region = record(item), id = text(region.id, 200), owner = text(region.part_id, 200)
    if (!id) continue
    const validOwner = validParts.has(owner) ? owner : null
    if (seenRegionOwners.has(id) && seenRegionOwners.get(id) !== validOwner) ambiguousRegionIds.add(id)
    seenRegionOwners.set(id, validOwner)
    if (validOwner) regionParts.set(id, validOwner)
  }
  for (const id of ambiguousRegionIds) regionParts.delete(id)
  const parameters = array(manifest.parameters).map(record)
  const grouped = new Map<string, Partial<Record<ImageBomRowField, RecordValue[]>>>()
  const unassignedParameters: UnassignedBomParameter[] = []
  const orderParameters: RecordValue[] = []
  for (const parameter of parameters) {
    const kind = parameterKind(parameter)
    if (kind === 'orderSets') { orderParameters.push(parameter); continue }
    const owner = parameterPart(parameter, regionParts, validParts)
    if (!owner) {
      unassignedParameters.push({ id: text(parameter.id, 200), label: text(parameter.label, 200), value: strValue(parameter.value), unit: text(parameter.unit, 100) || null, evidence: evidenceOf(parameter) })
      continue
    }
    if (kind) {
      const fields = grouped.get(owner) || {}
      ;(fields[kind] ||= []).push(parameter)
      grouped.set(owner, fields)
    }
  }
  const rows = [...foundParts].map(([partId, part], index): ImageBomRow => {
    const fields = grouped.get(partId) || {}, name = strValue(part.name) || strValue(part.drawing_title)
    const nameEvidence = evidenceOf(part)
    if (!nameEvidence.length && name) nameEvidence.push({ regionId: null, rawText: text(part.raw_text, 8000) || name })
    const profile = record(engineering.parts.find(item => item.partId === partId)), materialBasis = record(profile.materialBasis)
    return {
      id: `bom-${index + 1}`, partId,
      name: { ...blank(), value: name, candidateValue: name, origin: name ? 'candidate' : 'unknown', evidence: nameEvidence },
      material: fieldFromParameters(fields.material || []), thickness: fieldFromParameters(fields.thickness || []),
      outerSize: fieldFromParameters(fields.outerSize || []), surfaceFinish: fieldFromParameters(fields.surfaceFinish || []),
      quantityPerSet: fieldFromParameters(fields.quantityPerSet || [], '件/套'),
      materialUsageKg: blank('kg/件'), materialRateCnyKg: blank('CNY/kg'), operationFeeCnyPiece: blank('CNY/件'), setupFeeCny: blank('CNY/行'),
      processMode: 'processes', processes: array(profile.processes).map(processCharge),
      materialBasis: { netAreaMm2: engineeringField(materialBasis.netAreaMm2, 'mm²'), densityKgM3: engineeringField(materialBasis.densityKgM3, 'kg/m³'), utilizationPercent: blank('%'), adoptedUsageKg: null },
    }
  })
  const warnings = [
    '视图、展开图和表格是同一零件的资料，不按分图数量生成物料行。',
    '识别值均待核对；数量、用料及费率未提供时保持空白。本页不写入正式业务记录。',
  ]
  if (!rows.length) warnings.push('尚未识别出可归属的零件，请返回图纸确认零件和视图归属后再生成 BOM。')
  if (unassignedParameters.length) warnings.push(`${unassignedParameters.length} 项参数来源未唯一归属某个零件，未复制到物料行。`)
  for (const row of rows) for (const key of ['material', 'thickness', 'quantityPerSet'] as const) {
    if (row[key].origin === 'candidate' && row[key].value === null) warnings.push(`${row.name.value || row.partId}的${LABELS[key]}存在空值或冲突，请核对原始证据。`)
  }
  return {
    schemaVersion: 1, sourceFingerprint: sourceFingerprint(source), sourceName: text(src.filename, 300) || '图纸资料', sourceMode: text(manifest.mode, 100), rows,
    orderSets: fieldFromParameters(orderParameters, '套'), policyMethod: blank(), policyPercent: blank('%'), warnings, unassignedParameters,
    simulated: true, engineering_approved: false,
  }
}

function changedField(field: BomField, value: string | null, origin: 'manual' | 'demo', unit?: string): BomField {
  return { ...copyField(field), value: value !== null && value.trim() ? value.slice(0, 4000) : null, unit: unit || field.unit, origin, status: 'pending_review' }
}

export function editBomRow(draft: ImageBomDraft, rowId: string, patch: Partial<Record<ImageBomRowField, string | null>>): ImageBomDraft {
  const next = cloneDraft(draft), row = next.rows.find(item => item.id === rowId)
  if (!row) return next
  for (const key of ROW_FIELDS) if (Object.prototype.hasOwnProperty.call(patch, key)) {
    const value = patch[key]
    if (typeof value === 'string' || value === null) row[key] = changedField(row[key], value, 'manual', FIELD_UNITS[key])
  }
  if (Object.prototype.hasOwnProperty.call(patch, 'materialUsageKg')) row.materialBasis.adoptedUsageKg = null
  else if (row.materialBasis.adoptedUsageKg !== null && (Object.prototype.hasOwnProperty.call(patch, 'thickness') || Object.prototype.hasOwnProperty.call(patch, 'material'))) {
    row.materialUsageKg = blank('kg/件'); row.materialBasis.adoptedUsageKg = null
  }
  return next
}
export function editBomDraft(draft: ImageBomDraft, patch: Partial<Record<ImageBomDraftField, string | null>>): ImageBomDraft {
  const next = cloneDraft(draft)
  for (const key of DRAFT_FIELDS) if (Object.prototype.hasOwnProperty.call(patch, key)) {
    const value = patch[key]
    if (typeof value === 'string' || value === null) next[key] = changedField(next[key], value, 'manual', FIELD_UNITS[key])
  }
  return next
}

export function setProcessMode(draft: ImageBomDraft, rowId: string, mode: 'comprehensive' | 'processes'): ImageBomDraft {
  const next = cloneDraft(draft), row = next.rows.find(item => item.id === rowId)
  if (row && (mode === 'comprehensive' || mode === 'processes')) row.processMode = mode
  return next
}
export function adoptProcessCandidates(draft: ImageBomDraft, rowId?: string): ImageBomDraft {
  const next = cloneDraft(draft)
  for (const row of next.rows) if (!rowId || row.id === rowId) {
    row.processMode = 'processes'
    for (const process of row.processes) process.adopted = true
  }
  return next
}
export function editProcessCharge(draft: ImageBomDraft, rowId: string, processId: string, patch: { adopted?: boolean; measure?: string | null; rate?: string | null; unit?: ImageProcessUnit }): ImageBomDraft {
  const next = cloneDraft(draft), row = next.rows.find(item => item.id === rowId), process = row?.processes.find(item => item.id === processId)
  if (!row || !process) return next
  if (typeof patch.adopted === 'boolean') process.adopted = patch.adopted
  if (patch.unit && PROCESS_UNITS.includes(patch.unit) && patch.unit !== process.unit) {
    process.unit = patch.unit
    // A price or measure in a previous unit cannot be silently reused.
    process.measure = blank(processUnitLabel(patch.unit)); process.rate = blank(`CNY/${processUnitLabel(patch.unit)}`)
    process.quantityBasis = 'unknown'; process.explanation = '计费单位已人工更改，数量与费率需重新填写。'
  }
  if (Object.prototype.hasOwnProperty.call(patch, 'measure') && (typeof patch.measure === 'string' || patch.measure === null)) {
    process.measure = changedField(process.measure, patch.measure, 'manual', processUnitLabel(process.unit))
    process.quantityBasis = 'manual'; process.explanation = '本次操作者填写的单件计费量，待工厂核对。'
  }
  if (Object.prototype.hasOwnProperty.call(patch, 'rate') && (typeof patch.rate === 'string' || patch.rate === null)) process.rate = changedField(process.rate, patch.rate, 'manual', `CNY/${processUnitLabel(process.unit)}`)
  return next
}
/** Explicit rates-only demo action. Source measures remain pending; unknown measures stay unknown. */
export function applyProcessDemoRates(draft: ImageBomDraft, rowId?: string): ImageBomDraft {
  const next = adoptProcessCandidates(draft, rowId)
  const rates: Partial<Record<ImageProcessUnit, string>> = { piece: '2.00', bend: '0.50', spot: '0.30', hole: '0.20', m: '3.00', m2: '15.00', hour: '60.00' }
  for (const row of next.rows) if (!rowId || row.id === rowId) for (const process of row.processes) {
    const rate = rates[process.unit]
    if (rate && process.rate.value === null) process.rate = changedField(process.rate, rate, 'demo', `CNY/${processUnitLabel(process.unit)}`)
  }
  return next
}
export function editMaterialBasis(draft: ImageBomDraft, rowId: string, patch: Partial<Record<ImageMaterialBasisField, string | null>>): ImageBomDraft {
  const next = cloneDraft(draft), row = next.rows.find(item => item.id === rowId)
  if (!row) return next
  const units: Record<ImageMaterialBasisField, string> = { netAreaMm2: 'mm²', densityKgM3: 'kg/m³', utilizationPercent: '%' }
  let changed = false
  for (const key of Object.keys(units) as ImageMaterialBasisField[]) if (Object.prototype.hasOwnProperty.call(patch, key)) {
    const value = patch[key]
    if (typeof value === 'string' || value === null) { row.materialBasis[key] = changedField(row.materialBasis[key], value, 'manual', units[key]); changed = true }
  }
  if (changed && row.materialBasis.adoptedUsageKg !== null) { row.materialUsageKg = blank('kg/件'); row.materialBasis.adoptedUsageKg = null }
  return next
}
export function calculateMaterialBasis(row: ImageBomRow): ImageMaterialBasisResult {
  const issues: string[] = [], area = decimal(row.materialBasis.netAreaMm2.value, 6, 12), thickness = decimal(row.thickness.value, 6), density = decimal(row.materialBasis.densityKgM3.value, 6), utilization = decimal(row.materialBasis.utilizationPercent.value, 4, 3)
  if (!area || area.numerator <= ZERO || !/^(mm2|mm²|平方毫米)$/i.test(row.materialBasis.netAreaMm2.unit?.trim() || '')) issues.push('缺少明确的展开净面积（mm²）；成形外包尺寸或图像像素面积不能替代。')
  if (!thickness || thickness.numerator <= ZERO || !/^(mm|毫米)$/i.test(row.thickness.unit?.trim() || '')) issues.push('板厚须为明确的正数，且原单位为 mm。')
  if (!density || density.numerator <= ZERO || !/^(kg\/m3|kg\/m³|千克\/立方米)$/i.test(row.materialBasis.densityKgM3.unit?.trim() || '')) issues.push('需明确提供材料密度（kg/m³），不按材料名称自动套默认值。')
  if (!utilization || utilization.numerator <= ZERO || utilization.numerator > HUNDRED * utilization.denominator || row.materialBasis.utilizationPercent.unit !== '%') issues.push('材料利用率需明确填写大于 0 且不超过 100 的百分数。')
  if (issues.length || !area || !thickness || !density || !utilization) return { ok: false, issues }
  const numerator = area.numerator * thickness.numerator * density.numerator
  const denominator = area.denominator * thickness.denominator * density.denominator * BigInt(1000000000)
  const netMassKg = rationalText(numerator, denominator, 9)
  const materialUsageKg = rationalText(numerator * HUNDRED * utilization.denominator, denominator * utilization.numerator, 6)
  const adopted = decimal(materialUsageKg, 6)
  if (!adopted || adopted.numerator <= ZERO) return { ok: false, issues: ['计算用料超出当前 kg 计量范围或小于六位 kg 精度，请采用更合适的计量方式。'] }
  return { ok: true, netMassKg, materialUsageKg, formula: `${row.materialBasis.netAreaMm2.value} mm² × ${row.thickness.value} mm × ${row.materialBasis.densityKgM3.value} kg/m³ ÷ 1000000000 = ${netMassKg} kg；按 ${row.materialBasis.utilizationPercent.value}% 利用率，采用 ${materialUsageKg} kg/件（四舍五入至六位）。` }
}
export function applyMaterialBasis(draft: ImageBomDraft, rowId: string): ImageBomDraft {
  const next = cloneDraft(draft), row = next.rows.find(item => item.id === rowId)
  if (!row) return next
  const result = calculateMaterialBasis(row)
  if (!result.ok) return next
  row.materialUsageKg = changedField(row.materialUsageKg, result.materialUsageKg, 'manual', 'kg/件')
  row.materialUsageKg.evidence = [...row.materialBasis.netAreaMm2.evidence, ...row.thickness.evidence, ...row.materialBasis.densityKgM3.evidence, ...row.materialBasis.utilizationPercent.evidence].map(item => ({ ...item }))
  row.materialBasis.adoptedUsageKg = result.materialUsageKg
  return next
}

/** Called only by the explicit demo button. Never infers real material, thickness or prices. */
export function applyDemoAssumptions(draft: ImageBomDraft): ImageBomDraft {
  const next = cloneDraft(draft)
  const rowAssumptions: Partial<Record<ImageBomRowField, string>> = {
    quantityPerSet: '1', materialUsageKg: '0.25', materialRateCnyKg: '20.00', operationFeeCnyPiece: '8.00', setupFeeCny: '30.00',
  }
  for (const row of next.rows) for (const [key, value] of Object.entries(rowAssumptions) as [ImageBomRowField, string][]) {
    if (row[key].value === null) row[key] = changedField(row[key], value, 'demo', FIELD_UNITS[key])
  }
  for (const row of next.rows) row.processMode = 'comprehensive'
  const assumptions: Record<ImageBomDraftField, string> = { orderSets: '10', policyMethod: 'cost_plus', policyPercent: '20' }
  for (const key of DRAFT_FIELDS) if (next[key].value === null) next[key] = changedField(next[key], assumptions[key], 'demo', FIELD_UNITS[key])
  return next
}

/** Restore only source-identical editable fields, always rebuilding source evidence from the current manifest. */
export function readImageBomDraft(serialized: unknown, source: ImageBomSource): ImageBomDraft | null {
  let value: unknown = serialized
  if (typeof serialized === 'string') {
    try { value = JSON.parse(serialized) } catch { return null }
  }
  const raw = record(value), base = createImageBomDraft(source)
  if (raw.schemaVersion !== 1 || raw.sourceFingerprint !== base.sourceFingerprint || raw.simulated !== true || raw.engineering_approved !== false) return null
  const storedRows = array(raw.rows)
  if (storedRows.length !== base.rows.length) return null
  function restoreField(current: BomField, candidate: unknown, normalizedUnit?: string): BomField | null {
    const saved = record(candidate)
    if (saved.status !== 'pending_review' || !['candidate', 'manual', 'demo', 'unknown'].includes(text(saved.origin))) return null
    if (!(saved.value === null || typeof saved.value === 'string' && saved.value.length <= 4000)) return null
    if (saved.origin === 'manual' || saved.origin === 'demo') return changedField(current, saved.value as string | null, saved.origin, normalizedUnit)
    return saved.value === current.value && saved.origin === current.origin ? copyField(current) : null
  }
  for (let index = 0; index < base.rows.length; index++) {
    const row = base.rows[index], saved = record(storedRows[index])
    if (row.id !== saved.id || row.partId !== saved.partId) return null
    for (const key of ROW_FIELDS) {
      const restored = restoreField(row[key], saved[key], FIELD_UNITS[key])
      if (!restored) return null
      row[key] = restored
    }
    if (!['comprehensive', 'processes'].includes(text(saved.processMode)) || !Array.isArray(saved.processes) || saved.processes.length !== row.processes.length) return null
    row.processMode = saved.processMode as 'comprehensive' | 'processes'
    for (let processIndex = 0; processIndex < row.processes.length; processIndex++) {
      const process = row.processes[processIndex], stored = record(saved.processes[processIndex])
      if (stored.id !== process.id || stored.candidateId !== process.candidateId || typeof stored.adopted !== 'boolean' || !PROCESS_UNITS.includes(stored.unit as ImageProcessUnit)) return null
      const changedUnit = stored.unit !== process.unit
      process.unit = stored.unit as ImageProcessUnit
      const measure = restoreField(changedUnit ? blank(processUnitLabel(process.unit)) : process.measure, stored.measure, processUnitLabel(process.unit))
      const rate = restoreField(changedUnit ? blank(`CNY/${processUnitLabel(process.unit)}`) : process.rate, stored.rate, `CNY/${processUnitLabel(process.unit)}`)
      if (!measure || !rate) return null
      process.measure = measure; process.rate = rate; process.adopted = stored.adopted
      if (measure.origin === 'manual' || changedUnit) { process.quantityBasis = 'manual'; process.explanation = '本次操作者填写的单件计费量，待工厂核对。' }
    }
    const storedBasis = record(saved.materialBasis)
    for (const key of ['netAreaMm2', 'densityKgM3', 'utilizationPercent'] as const) {
      const restored = restoreField(row.materialBasis[key], storedBasis[key], row.materialBasis[key].unit || undefined)
      if (!restored) return null
      row.materialBasis[key] = restored
    }
    if (storedBasis.adoptedUsageKg !== null) {
      const derived = calculateMaterialBasis(row)
      if (typeof storedBasis.adoptedUsageKg !== 'string' || !derived.ok || storedBasis.adoptedUsageKg !== derived.materialUsageKg || row.materialUsageKg.value !== derived.materialUsageKg) return null
      row.materialBasis.adoptedUsageKg = derived.materialUsageKg
      row.materialUsageKg.evidence = [...row.materialBasis.netAreaMm2.evidence, ...row.thickness.evidence, ...row.materialBasis.densityKgM3.evidence, ...row.materialBasis.utilizationPercent.evidence].map(item => ({ ...item }))
    }
  }
  for (const key of DRAFT_FIELDS) {
    const restored = restoreField(base[key], raw[key], FIELD_UNITS[key])
    if (!restored) return null
    base[key] = restored
  }
  return base
}
export const restoreImageBomDraft = (source: ImageBomSource, raw: unknown): ImageBomDraft | null => readImageBomDraft(raw, source)

type Decimal = { numerator: bigint; denominator: bigint }
function decimal(value: string | null, maxScale: number, maxWholeDigits = 9): Decimal | null {
  if (value === null || !new RegExp(`^(?:0|[1-9][0-9]{0,${maxWholeDigits - 1}})(?:\\.[0-9]{1,${maxScale}})?$`).test(value.trim())) return null
  const [whole, fraction = ''] = value.trim().split('.')
  // Avoid exponentiation: the app targets ES6 and downlevel Math.pow does not accept BigInt.
  let denominator = BigInt(1)
  for (let index = 0; index < fraction.length; index++) denominator *= BigInt(10)
  return { numerator: BigInt(whole + fraction), denominator }
}
function integer(value: string | null): bigint | null {
  return value !== null && /^[1-9][0-9]{0,5}$/.test(value.trim()) ? BigInt(value.trim()) : null
}
function rounded(numerator: bigint, denominator: bigint): bigint {
  return (numerator * BigInt(2) + denominator) / (denominator * BigInt(2))
}
function rationalText(numerator: bigint, denominator: bigint, scale: number): string {
  let factor = BigInt(1)
  for (let index = 0; index < scale; index++) factor *= BigInt(10)
  const digits = rounded(numerator * factor, denominator).toString().padStart(scale + 1, '0')
  return scale ? `${digits.slice(0, -scale)}.${digits.slice(-scale)}`.replace(/\.?0+$/, '') : digits
}
const asCents = (value: Decimal): bigint => rounded(value.numerator * HUNDRED, value.denominator)

export function calculateDemoQuote(draft: ImageBomDraft): ImageDemoQuote {
  const issues: ImageQuoteIssue[] = []
  const issue = (field: string, message: string, rowId?: string) => issues.push({ ...(rowId ? { rowId } : {}), field, message })
  if (!draft.rows.length) issue('rows', '尚无可归属的物料行，先核对零件拆分。')
  const sets = integer(draft.orderSets.value)
  if (sets === null) issue('orderSets', '订单套数需明确填写 1–999999 的整数。')
  if (draft.orderSets.unit && !/^(套|组|sets?)$/i.test(draft.orderSets.unit.trim())) issue('orderSets', '订单套数的原始单位不支持，请人工明确为套。')
  const method = draft.policyMethod.value
  if (method !== 'cost_plus' && method !== 'gross_margin') issue('policyMethod', '请选择成本加成或目标毛利率。')
  const percent = decimal(draft.policyPercent.value, 4, 4)
  if (!percent || percent.numerator > BigInt(1000) * percent.denominator) issue('policyPercent', '定价比例需填写 0–1000 的百分数，最多四位小数。')
  else if (method === 'gross_margin' && percent.numerator >= HUNDRED * percent.denominator) issue('policyPercent', '目标毛利率必须小于 100%。')
  const parsed = draft.rows.map(row => {
    for (const key of ['name', 'material', 'thickness'] as const) if (!row[key].value?.trim()) issue(key, `请补充${LABELS[key]}；未知值不能用于成本演示。`, row.id)
    const qty = integer(row.quantityPerSet.value)
    if (qty === null) issue('quantityPerSet', '每套数量需明确填写 1–999999 的整数，视图数量不能代替零件数量。', row.id)
    if (row.quantityPerSet.unit && !/^(件\/套|件|pcs?|pieces?|each|ea)$/i.test(row.quantityPerSet.unit.trim())) issue('quantityPerSet', '每套数量的原始单位不支持，请人工明确为件/套。', row.id)
    const usage = decimal(row.materialUsageKg.value, 6)
    if (!usage) issue('materialUsageKg', '单件用料需填写非负 kg 数值，最多六位小数；不得从图片外形尺寸直接假定重量。', row.id)
    const rate = decimal(row.materialRateCnyKg.value, 2)
    if (!rate) issue('materialRateCnyKg', '材料单价需明确填写非负 CNY/kg 数值，最多两位小数。', row.id)
    if (row.materialBasis.adoptedUsageKg !== null) {
      const basis = calculateMaterialBasis(row)
      if (!basis.ok || basis.materialUsageKg !== row.materialUsageKg.value || row.materialBasis.adoptedUsageKg !== row.materialUsageKg.value) issue('materialUsageKg', '展开用料依据已变化，请重新采用计算用量。', row.id)
    }
    const operation = row.processMode === 'comprehensive' ? decimal(row.operationFeeCnyPiece.value, 2) : null
    if (row.processMode === 'comprehensive' && !operation) issue('operationFeeCnyPiece', '单件综合工序费需明确填写非负金额，最多两位小数。', row.id)
    if (row.processMode !== 'comprehensive' && row.processMode !== 'processes') issue('processMode', '请明确采用分工序计价或综合加工费。', row.id)
    const processes = row.processMode === 'processes' ? row.processes.filter(item => item.adopted).map(process => {
      const measure = decimal(process.measure.value, 6), rate = decimal(process.rate.value, 2)
      if (process.unit === 'unknown' || !PROCESS_UNITS.includes(process.unit)) issue(`process:${process.id}:unit`, `${process.title}的计费单位尚未明确。`, row.id)
      if (!measure) issue(`process:${process.id}:measure`, `${process.title}缺少明确的单件计费量；图纸未给出的长度、面积或工时不能补为 1。`, row.id)
      else if (['piece', 'bend', 'spot', 'hole'].includes(process.unit) && measure.denominator !== BigInt(1)) {
        if (measure.numerator % measure.denominator !== ZERO) issue(`process:${process.id}:measure`, `${process.title}按件、折、点或孔计费时数量必须是整数。`, row.id)
      }
      if (!rate) issue(`process:${process.id}:rate`, `${process.title}缺少明确费率；不收费时请明确填写 0。`, row.id)
      return { process, measure, rate }
    }) : []
    if (row.processMode === 'processes' && !processes.length) issue('processes', '尚未采用任何工序计价候选；请先核对并采用工序，或明确选择综合加工费。', row.id)
    const setup = decimal(row.setupFeeCny.value, 2)
    if (!setup) issue('setupFeeCny', '本行一次准备费需明确填写非负金额；不计费时请明确填 0。', row.id)
    return { row, qty, usage, rate, operation, setup, processes }
  })
  if (issues.length || sets === null || !percent || method !== 'cost_plus' && method !== 'gross_margin') return { ok: false, issues }
  const rows: ImageQuoteRow[] = parsed.map(({ row, qty, usage, rate, operation, setup, processes }) => {
    // All nullable values were validated above; BigInt is kept internal and results serialize safely.
    const quantity = qty! * sets
    const material = rounded(usage!.numerator * rate!.numerator * quantity * HUNDRED, usage!.denominator * rate!.denominator)
    const operationRows: ImageQuotedProcess[] = processes.map(({ process, measure, rate: processRate }) => ({
      id: process.id, title: process.title, unit: process.unit, measure: process.measure.value!, rate: process.rate.value!, totalCostCents: rounded(measure!.numerator * processRate!.numerator * quantity * HUNDRED, measure!.denominator * processRate!.denominator).toString(), measureOrigin: process.measure.origin, rateOrigin: process.rate.origin, quantityBasis: process.quantityBasis, explanation: process.explanation,
    }))
    const operations = row.processMode === 'comprehensive' ? asCents(operation!) * quantity : operationRows.reduce((sum, item) => sum + BigInt(item.totalCostCents), ZERO)
    const setupCents = asCents(setup!), cost = material + operations + setupCents
    const factor = HUNDRED * percent.denominator
    const quote = method === 'cost_plus' ? rounded(cost * (factor + percent.numerator), factor) : rounded(cost * factor, factor - percent.numerator)
    return {
      rowId: row.id, partId: row.partId, name: row.name.value!, material: row.material.value!, thickness: `${row.thickness.value!}${row.thickness.unit ? ` ${row.thickness.unit}` : ''}`,
      quantityPerSet: qty!.toString(), orderSets: sets.toString(), quantity: quantity.toString(),
      usageKg: row.materialUsageKg.value!, materialRateCnyKg: row.materialRateCnyKg.value!, operationFeeCnyPiece: row.processMode === 'comprehensive' ? row.operationFeeCnyPiece.value! : '',
      materialCostCents: material.toString(), operationCostCents: operations.toString(), setupCostCents: setupCents.toString(), totalCostCents: cost.toString(),
      unitCostCents: rounded(cost, quantity).toString(), unitQuoteCents: rounded(quote, quantity).toString(), lineQuoteCents: quote.toString(),
      processMode: row.processMode, operations: operationRows, materialBasis: calculateMaterialBasis(row),
    }
  })
  const sum = (field: 'quantity' | 'materialCostCents' | 'operationCostCents' | 'setupCostCents' | 'totalCostCents' | 'lineQuoteCents') => rows.reduce((total, row) => total + BigInt(row[field]), ZERO).toString()
  return {
    ok: true, simulated: true, engineering_approved: false, sourceFingerprint: draft.sourceFingerprint,
    policy: { method, percent: draft.policyPercent.value! }, rows,
    totals: { quantity: sum('quantity'), materialCostCents: sum('materialCostCents'), operationCostCents: sum('operationCostCents'), setupCostCents: sum('setupCostCents'), totalCostCents: sum('totalCostCents'), lineQuoteCents: sum('lineQuoteCents') },
    notes: [
      '模拟测算草稿，待人工审核，未经工程或商务批准；未对接工厂价格库。',
      '人民币、未税、不含运费。分工序计价与综合加工费互斥，本行准备费对整行订单仅计一次；采用费率前需人工核对包含项。',
      '分工序金额 = 单件采用计费量 × 对应单位费率 × 订单件数，各工序整行金额分别四舍五入到分。折弯表行数、焊点推导及所有计量仍待工程核对，不代表生产批准。',
      ...draft.rows.filter(row => row.processMode === 'processes' && row.processes.some(process => !process.adopted)).map(row => `${row.name.value || row.partId}未纳入计价的工序候选：${row.processes.filter(process => !process.adopted).map(process => process.title).join('、')}。当前报价只覆盖已明确采用的范围。`),
      '材料费用按整行订单用量计算后四舍五入到分；成本加成与目标毛利率分别计算，金额在每行汇总时四舍五入。',
      '报价总额以每行合计为准；展示单价由行合计分摊并四舍五入，乘以数量时可能产生尾差。',
    ],
  }
}

export function formatCents(value: string): string {
  if (!/^-?[0-9]{1,40}$/.test(value)) return '—'
  const cents = BigInt(value), negative = cents < ZERO, amount = negative ? -cents : cents
  return `${negative ? '-' : ''}${(amount / HUNDRED).toString()}.${(amount % HUNDRED).toString().padStart(2, '0')}`
}
export function htmlEscape(value: unknown): string {
  return String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]!)
}
function csvCell(value: unknown): string {
  let result = String(value ?? '').replace(/\r\n?/g, '\n')
  // Quoting alone does not stop spreadsheet formulas; guard control/space-prefixed expressions too.
  if (/^[\s\u0000-\u001f\u007f-\u009f]*[=+@-]/.test(result) || /^[\t\r\n]/.test(result)) result = `'${result}`
  return `"${result.replace(/"/g, '""')}"`
}
const originLabel = (origin: BomOrigin): string => ({ candidate: '识别候选待核对', manual: '人工输入待审核', demo: '演示假设', unknown: '未提供' })[origin]

export function bomCsv(draft: ImageBomDraft): string {
  const headers = ['状态', '零件编号', '零件名称', '材料', '材料来源', '板厚原值', '板厚原单位', '板厚来源', '标注外形尺寸', '外形原单位', '表面处理', '每套数量', '数量来源', '订单套数', '订单套数来源', '单件用料 kg', '用料来源', '材料单价 CNY/kg', '单价来源', '综合工序费 CNY/件', '工序费来源', '一次准备费 CNY/行', '准备费来源', '定价方式', '定价比例 %', '来源文件', '原文依据', '工序计价方式', '工序采用与计量明细', '展开净面积 mm²', '面积来源', '密度 kg/m³', '密度来源', '利用率 %', '利用率来源', '用料推导采用状态']
  const rows = draft.rows.map(row => [
    '模拟 BOM 草稿，未工程批准', row.partId, row.name.value, row.material.value, originLabel(row.material.origin), row.thickness.value, row.thickness.unit, originLabel(row.thickness.origin), row.outerSize.value, row.outerSize.unit, row.surfaceFinish.value,
    row.quantityPerSet.value, originLabel(row.quantityPerSet.origin), draft.orderSets.value, originLabel(draft.orderSets.origin), row.materialUsageKg.value, originLabel(row.materialUsageKg.origin), row.materialRateCnyKg.value, originLabel(row.materialRateCnyKg.origin), row.operationFeeCnyPiece.value, originLabel(row.operationFeeCnyPiece.origin), row.setupFeeCny.value, originLabel(row.setupFeeCny.origin),
    draft.policyMethod.value, draft.policyPercent.value, draft.sourceName, ['name', 'material', 'thickness', 'outerSize', 'surfaceFinish', 'quantityPerSet'].flatMap(key => row[key as ImageBomRowField].evidence.map(item => `${item.regionId || '零件资料'}: ${item.rawText}`)).join('\n'),
    row.processMode === 'processes' ? '分工序；综合加工费不计入' : '综合加工费；分工序费用不计入',
    row.processes.map(process => `${process.adopted ? '采用' : '未采用'} ${process.title}：${process.measure.value ?? '未知'} ${processUnitLabel(process.unit)}/件（${originLabel(process.measure.origin)}；${process.quantityBasis}），费率 ${process.rate.value ?? '未知'} CNY/${processUnitLabel(process.unit)}（${originLabel(process.rate.origin)}）；${process.explanation}；${process.evidence.map(item => item.rawText).join(' / ')}`).join('\n'),
    row.materialBasis.netAreaMm2.value, originLabel(row.materialBasis.netAreaMm2.origin), row.materialBasis.densityKgM3.value, originLabel(row.materialBasis.densityKgM3.origin), row.materialBasis.utilizationPercent.value, originLabel(row.materialBasis.utilizationPercent.origin), row.materialBasis.adoptedUsageKg !== null ? '已显式采用面积/厚度/密度/利用率计算量，待工程核对' : '未采用面积推导；当前采用用料按其独立来源核对',
  ])
  return '\uFEFF' + [headers, ...rows].map(row => row.map(csvCell).join(',')).join('\r\n') + '\r\n'
}

export function quoteHtml(draft: ImageBomDraft, quote: ImageDemoQuoteSuccess): string {
  if (quote.sourceFingerprint !== draft.sourceFingerprint) throw new Error('报价草稿来源已变化，请重新计算。')
  const current = calculateDemoQuote(draft)
  if (!current.ok || stableJson(current) !== stableJson(quote)) throw new Error('报价输入已变化，请重新计算。')
  const escape = htmlEscape, amount = (value: string) => escape(formatCents(value))
  const rows = quote.rows.map(row => `<tr><td>${escape(row.partId)}</td><td>${escape(row.name)}</td><td>${escape(row.material)}<br>${escape(row.thickness)}</td><td>${escape(row.quantityPerSet)} × ${escape(row.orderSets)} = ${escape(row.quantity)}</td><td>${amount(row.materialCostCents)}</td><td>${amount(row.operationCostCents)}</td><td>${amount(row.setupCostCents)}</td><td>${amount(row.totalCostCents)}</td><td>${amount(row.unitQuoteCents)}</td><td>${amount(row.lineQuoteCents)}</td></tr>`).join('')
  const fieldSummary = draft.rows.map(row => `<li>${escape(row.name.value || row.partId)}：用料 ${escape(row.materialUsageKg.value || '未提供')} kg/件（${escape(originLabel(row.materialUsageKg.origin))}），材料 ${escape(row.materialRateCnyKg.value || '未提供')} CNY/kg（${escape(originLabel(row.materialRateCnyKg.origin))}），${row.processMode === 'processes' ? '采用分工序计价（见明细），综合加工费不计入' : `采用综合工序费 ${escape(row.operationFeeCnyPiece.value || '未提供')} CNY/件（${escape(originLabel(row.operationFeeCnyPiece.origin))}）`}，准备费 ${escape(row.setupFeeCny.value || '未提供')} CNY/行（${escape(originLabel(row.setupFeeCny.origin))}）。</li>`).join('')
  const operationDetails = quote.rows.map(row => {
    if (row.processMode === 'comprehensive') return `<h3>${escape(row.name)}</h3><p>采用综合加工费 ${escape(row.operationFeeCnyPiece)} CNY/件；分工序候选费用不另计。</p>`
    return `<h3>${escape(row.name)}</h3><table><thead><tr><th>采用工序</th><th>单件计费量</th><th>费率</th><th>订单量</th><th>工序金额</th><th>依据与状态</th></tr></thead><tbody>${row.operations.map(process => `<tr><td>${escape(process.title)}</td><td>${escape(process.measure)} ${escape(processUnitLabel(process.unit))}/件（${escape(originLabel(process.measureOrigin))}）</td><td>${escape(process.rate)} CNY/${escape(processUnitLabel(process.unit))}（${escape(originLabel(process.rateOrigin))}）</td><td>${escape(row.quantity)} 件</td><td>${amount(process.totalCostCents)}</td><td>${escape(process.quantityBasis)}；${escape(process.explanation)}；待工程核对</td></tr>`).join('')}</tbody></table>`
  }).join('')
  const materialDetails = draft.rows.map(row => {
    const result = calculateMaterialBasis(row)
    return `<li>${escape(row.name.value || row.partId)}：${row.materialBasis.adoptedUsageKg !== null && result.ok ? escape(result.formula) : '本次未采用面积/厚度/密度推导的采购用量。'} 面积来源：${escape(originLabel(row.materialBasis.netAreaMm2.origin))}；密度来源：${escape(originLabel(row.materialBasis.densityKgM3.origin))}；利用率来源：${escape(originLabel(row.materialBasis.utilizationPercent.origin))}。理论净重与采购用量仍需工程核对。</li>`
  }).join('')
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>模拟报价草稿</title><style>body{font:14px/1.6 system-ui,sans-serif;color:#24354b;margin:32px auto;max-width:1280px;padding:0 20px}h1{margin-bottom:8px}.notice{background:#fff5df;border:1px solid #efca86;padding:14px;border-radius:8px}table{width:100%;border-collapse:collapse;margin:24px 0}th,td{padding:10px;text-align:left;border:1px solid #d9e2eb;vertical-align:top}th{background:#f0f5fa}tfoot{font-weight:bold}li{margin:6px 0}@media print{body{margin:0;font-size:11px}.notice{background:white}}</style></head><body><h1>模拟报价草稿 · 待人工审核</h1><p class="notice">演示测算，未经工程或商务批准。人民币、未税、不含运费。未连接工厂价格库；不可作为正式对外报价。</p><p>来源图纸：${escape(draft.sourceName)}</p><p>定价方式：${escape(quote.policy.method === 'cost_plus' ? '成本加成' : '目标毛利率')} ${escape(quote.policy.percent)}%（${escape(originLabel(draft.policyPercent.origin))}）。数量与识别参数均待人工核对。</p><table><thead><tr><th>零件编号</th><th>零件</th><th>材料 / 板厚原值</th><th>每套 × 套数 = 件数</th><th>材料成本</th><th>工序成本</th><th>一次准备费</th><th>成本合计</th><th>分摊报价单价</th><th>报价行合计</th></tr></thead><tbody>${rows}</tbody><tfoot><tr><td colspan="4">合计（${escape(quote.totals.quantity)} 件）</td><td>${amount(quote.totals.materialCostCents)}</td><td>${amount(quote.totals.operationCostCents)}</td><td>${amount(quote.totals.setupCostCents)}</td><td>${amount(quote.totals.totalCostCents)}</td><td>—</td><td>${amount(quote.totals.lineQuoteCents)}</td></tr></tfoot></table><h2>分工序计价明细</h2>${operationDetails}<h2>用料计算依据</h2><ul>${materialDetails}</ul><h2>采用输入与来源</h2><ul>${fieldSummary}</ul><h2>计算口径</h2><ul>${quote.notes.map(note => `<li>${escape(note)}</li>`).join('')}</ul></body></html>`
}
