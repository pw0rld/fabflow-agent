/** Source-bound drawing observations and process candidates; never engineering approval. */
import type { BomEvidence, ImageBomSource } from './image-bom-demo'

export type EngineeringCategory = 'identity' | 'material' | 'thickness' | 'dimension' | 'hole' | 'slot' | 'bend' | 'weld' | 'surface' | 'tolerance' | 'other'
export type EngineeringAssociation = 'evidence' | 'predefined_sample_association' | 'unassigned'
export type EngineeringFeature = {
  id: string; sourceId: string; sourceKind: 'parameter' | 'process_note'; label: string
  value: unknown; displayValue: string; unit: string | null; category: EngineeringCategory
  evidence: BomEvidence[]; sourceStatus: string | null; status: 'pending_review'
  partId: string | null; association: EngineeringAssociation; usable: boolean
  direction: string | null; datum: string | null; feature: string | null; basis: string | null
  raw: Record<string, unknown>
}
export type EngineeringBendRow = {
  id: string; tag: string; direction: string; angleDeg: number | null; innerRadiusMm: number | null
  evidence: BomEvidence[]; sourceStatus: string | null; status: 'pending_review'; partId: string | null
  association: EngineeringAssociation; usable: boolean; raw: Record<string, unknown>
}
export type EngineeringQuantity = {
  value: string; unit: string; basis: 'drawing_text' | 'derived' | 'table_rows'; explanation: string
  evidence: BomEvidence[]; status: 'pending_review'
}
export type EngineeringProcess = {
  id: string; kind: 'cut' | 'bend' | 'weld' | 'deburr' | 'finish' | 'other'; title: string
  description: string; origin: 'drawing_requirement' | 'suggested'; sourceIds: string[]
  evidence: BomEvidence[]; quantity: EngineeringQuantity | null; status: 'pending_review'
}
export type EngineeringMaterialBasis = {
  flatRegionIds: string[]; materialFeatures: EngineeringFeature[]; thicknessFeatures: EngineeringFeature[]
  thicknessMm: EngineeringQuantity | null; netAreaMm2: EngineeringQuantity | null
  densityKgM3: EngineeringQuantity | null; theoreticalMassKg: EngineeringQuantity | null
  cutLengthMm: EngineeringQuantity | null; missing: string[]; notes: string[]
}
export type EngineeringPart = {
  partId: string; name: string; features: EngineeringFeature[]
  groups: Record<EngineeringCategory, EngineeringFeature[]>; bends: EngineeringBendRow[]
  processes: EngineeringProcess[]; materialBasis: EngineeringMaterialBasis
}
export type ImageEngineeringProfile = {
  sourceFingerprint: string; sourceName: string; parts: EngineeringPart[]
  unassigned: { features: EngineeringFeature[]; bends: EngineeringBendRow[]; processNotes: EngineeringFeature[] }
  counts: { parameters: number; processNotes: number; bendRows: number; assignedFeatures: number; unassigned: number }
  warnings: string[]; status: 'pending_review'; engineering_approved: false
}

type RecordValue = Record<string, unknown>
const CATEGORIES: EngineeringCategory[] = ['identity', 'material', 'thickness', 'dimension', 'hole', 'slot', 'bend', 'weld', 'surface', 'tolerance', 'other']
const rec = (value: unknown): RecordValue => value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {}
const arr = (value: unknown): unknown[] => Array.isArray(value) ? value : []
const text = (value: unknown): string => typeof value === 'string' ? value : ''
const optionalText = (value: unknown): string | null => text(value) || null
function display(value: unknown): string {
  if (value === null || value === undefined) return '未读取到值'
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value)
  try { return JSON.stringify(value) ?? '未读取到值' } catch { return '原始值无法显示' }
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') {
    const record = value as RecordValue
    return `{${Object.keys(record).filter(key => record[key] !== undefined).sort().map(key => `${JSON.stringify(key)}:${canonical(record[key])}`).join(',')}}`
  }
  return JSON.stringify(value) ?? 'null'
}
// Type-only import avoids a runtime cycle when the BOM calculator consumes this profile.
function fingerprint(source: ImageBomSource): string {
  const manifest = rec(source.manifest)
  return canonical({ job: source.jobId, attempt: source.attempt, revision: source.revision, sha: source.sourceSha256 || rec(manifest.source).sha256 || null, manifest })
}
function evidenceOf(value: RecordValue): BomEvidence[] {
  return arr(value.evidence).map(item => {
    const row = rec(item), bbox = arr(row.bbox)
    return { regionId: optionalText(row.region_id), rawText: text(row.raw_text), ...(bbox.length === 4 && bbox.every(v => typeof v === 'number' && Number.isFinite(v)) ? { bbox: bbox as number[] } : {}) }
  })
}
function uniqueEvidence(items: BomEvidence[]): BomEvidence[] {
  const seen = new Set<string>()
  return items.filter(item => { const key = canonical(item); if (seen.has(key)) return false; seen.add(key); return true })
}
function usable(raw: RecordValue): boolean {
  return !raw.review_invalidated_by_region && !['questioned', 'rejected', 'unreadable', 'excluded', 'invalid'].includes(text(raw.status)) && raw.basis !== 'uncertain'
}
function categoryOf(raw: RecordValue): EngineeringCategory {
  const category = text(raw.category)
  if (category === 'outer_size') return 'dimension'
  if (CATEGORIES.includes(category as EngineeringCategory) && category !== 'other') return category as EngineeringCategory
  const content = `${text(raw.label)} ${text(raw.title)} ${text(raw.id)}`
  if (/板厚|料厚|thickness/i.test(content)) return 'thickness'
  if (/材料|材质|牌号|material|alloy/i.test(content)) return 'material'
  if (/折弯|弯曲|bend/i.test(content)) return 'bend'
  if (/焊|weld/i.test(content)) return 'weld'
  if (/喷粉|表面处理|涂装|镀锌|抛光|powder|finish|coating|surface/i.test(content)) return 'surface'
  if (/孔|hole/i.test(content)) return 'hole'
  if (/槽|窗|slot|window/i.test(content)) return 'slot'
  if (/公差|tolerance/i.test(content)) return 'tolerance'
  if (/尺寸|长度|宽度|高度|倒角|半径|外形|size|dimension|length|width|height|chamfer|radius/i.test(content)) return 'dimension'
  if (/图号|版本|名称|drawing_number|revision|part_name/i.test(content)) return 'identity'
  return 'other'
}
function quantity(value: string, unit: string, basis: EngineeringQuantity['basis'], explanation: string, evidence: BomEvidence[]): EngineeringQuantity {
  return { value, unit, basis, explanation, evidence: uniqueEvidence(evidence), status: 'pending_review' }
}
const numeric = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value : null
function emptyGroups(): Record<EngineeringCategory, EngineeringFeature[]> {
  return { identity: [], material: [], thickness: [], dimension: [], hole: [], slot: [], bend: [], weld: [], surface: [], tolerance: [], other: [] }
}
type Rational = { n: bigint; d: bigint }
const ONE = BigInt(1), TEN = BigInt(10), ZERO = BigInt(0)
function decimal(value: unknown): Rational | null {
  const str = typeof value === 'string' ? value.trim() : typeof value === 'number' && Number.isFinite(value) ? String(value) : ''
  if (!/^(?:0|[1-9]\d{0,11})(?:\.\d{1,9})?$/.test(str)) return null
  const [whole, fraction = ''] = str.split('.'); let d = ONE
  for (let i = 0; i < fraction.length; i++) d *= TEN
  const n = BigInt(whole + fraction)
  return n > ZERO ? { n, d } : null
}
function rationalString(value: Rational, scale = 9): string {
  let factor = ONE
  for (let i = 0; i < scale; i++) factor *= TEN
  const rounded = (value.n * factor * BigInt(2) + value.d) / (value.d * BigInt(2))
  const digits = rounded.toString().padStart(scale + 1, '0')
  return scale ? `${digits.slice(0, -scale)}.${digits.slice(-scale)}`.replace(/\.?0+$/, '') : digits
}
function measurement(features: EngineeringFeature[], match: (feature: EngineeringFeature) => boolean, unit: string, units: Record<string, number>): EngineeringQuantity | null {
  const selected = features.filter(item => item.sourceKind === 'parameter' && item.usable && item.basis !== 'derived' && match(item))
  if (!selected.length) return null
  const normalized = selected.map(item => {
    const parsed = decimal(item.value), multiplier = units[(item.unit || '').replace(/\s/g, '').toLowerCase()]
    return parsed && multiplier ? { n: parsed.n * BigInt(multiplier), d: parsed.d } : null
  })
  if (normalized.some(item => item === null)) return null
  const values = normalized.map(item => rationalString(item!))
  if (new Set(values).size !== 1 || values[0] === '0') return null
  return quantity(values[0], unit, 'drawing_text', '来自明确标注的数值与原单位，保留待工程核对；没有按图像比例测量。', selected.flatMap(item => item.evidence))
}
function materialBasis(features: EngineeringFeature[], regions: RecordValue[]): EngineeringMaterialBasis {
  const thicknessMm = measurement(features, item => item.category === 'thickness', 'mm', { mm: 1, 毫米: 1 })
  // A face or projection can also be called "net area". Only an explicitly
  // flat/unfolded net area is a material basis; generic area labels/IDs stay facts.
  const netAreaMm2 = measurement(features, item => /^(?:flat_net_area(?:_mm2)?|unfolded_net_area(?:_mm2)?)$/.test(item.sourceId) || /^(?:净展开面积|展开净面积|净下料面积|net flat area|flat net area|unfolded net area)$/i.test(item.label), 'mm2', { mm2: 1, 'mm²': 1, '平方毫米': 1, m2: 1000000, 'm²': 1000000, 平方米: 1000000 })
  const densityKgM3 = measurement(features, item => /^(?:density|density_kg_m3)$/.test(item.sourceId) || /^(?:密度|材料密度|density)$/i.test(item.label), 'kg/m3', { 'kg/m3': 1, 'kg/m³': 1, '千克/立方米': 1 })
  const cutLengthMm = measurement(features, item => /^(?:cutting_length(?:_mm)?|cut_length(?:_mm)?|total_cut_length(?:_mm)?)$/.test(item.sourceId) || /^(?:切割总长|切割总长度|切割长度|轮廓切割总长|total cutting length)$/i.test(item.label), 'mm', { mm: 1, 毫米: 1, m: 1000, 米: 1000 })
  let theoreticalMassKg: EngineeringQuantity | null = null
  if (netAreaMm2 && thicknessMm && densityKgM3) {
    const a = decimal(netAreaMm2.value)!, t = decimal(thicknessMm.value)!, density = decimal(densityKgM3.value)!
    const mass = rationalString({ n: a.n * t.n * density.n, d: a.d * t.d * density.d * BigInt(1000000000) })
    if (mass !== '0') theoreticalMassKg = quantity(mass, 'kg', 'derived', `${netAreaMm2.value} mm² × ${thicknessMm.value} mm × ${densityKgM3.value} kg/m³ ÷ 1,000,000,000；理论净重未计排样、损耗与采购口径，须工程核对。`, [...netAreaMm2.evidence, ...thicknessMm.evidence, ...densityKgM3.evidence])
  }
  const flatRegionIds = regions.filter(item => item.type === 'flat_pattern').map(item => text(item.id))
  const missing = [!thicknessMm ? '可用于计算的明确板厚与毫米单位' : '', !netAreaMm2 ? '经核对的展开净面积或闭合下料轮廓' : '', !densityKgM3 ? '本材料采用的密度及来源' : '', !cutLengthMm ? '经核对的外轮廓与内孔槽切割总长' : ''].filter(Boolean)
  return { flatRegionIds, materialFeatures: features.filter(item => item.category === 'material'), thicknessFeatures: features.filter(item => item.category === 'thickness'), thicknessMm, netAreaMm2, densityKgM3, theoreticalMassKg, cutLengthMm, missing, notes: ['有展开图不代表已取得闭合、定尺的展开几何；像素裁图没有用于计量。', '成形后的长宽高不作为展开面积或采购用料；理论净重与含损耗的采购用料分别核对。', '孔槽标注只证明图纸特征，不据此决定激光切割、钻孔或攻丝的加工路线。'] }
}

/** A strict, deliberately narrow source-text relation; not a general-purpose language inference. */
function weldQuantity(features: EngineeringFeature[]): EngineeringQuantity | null {
  const evidence = uniqueEvidence(features.filter(item => item.usable).flatMap(item => item.evidence))
  const texts = evidence.map(item => item.rawText).join(' / ')
  const per = [...texts.matchAll(/\b([1-9]\d{0,3})\s+(?:SPOT\s+)?WELDS?\s+PER\s+TAB\b/gi)].map(match => Number(match[1]))
  const tabs = [...texts.matchAll(/\bALL\s+([1-9]\d{0,3})\s+CORNERS?\s+OF\s+(?:THE\s+)?BOX\b/gi)].map(match => Number(match[1]))
  if (/\bSPOT\s+WELD\s+TAB\s+TO\s+(?:THE\s+)?BOX\b/i.test(texts) && new Set(per).size === 1 && new Set(tabs).size === 1 && per.length && tabs.length && per[0] * tabs[0] <= 1000000) {
    return quantity(String(per[0] * tabs[0]), 'spot', 'derived', `${per[0]} 个焊点/安装耳 × ${tabs[0]} 个角部安装耳 = ${per[0] * tabs[0]} 个焊点候选；仅按同一零件原文关系推导，实际位置和生产计量待核对。`, evidence)
  }
  return null
}

function processList(partId: string, features: EngineeringFeature[], bends: EngineeringBendRow[], basis: EngineeringMaterialBasis, regions: RecordValue[]): EngineeringProcess[] {
  const eligible = features.filter(item => item.usable), notes = eligible.filter(item => item.sourceKind === 'process_note')
  const result: EngineeringProcess[] = []
  const append = (kind: EngineeringProcess['kind'], title: string, description: string, source: EngineeringFeature[], counted: EngineeringQuantity | null, origin: EngineeringProcess['origin'] = 'drawing_requirement', extra: BomEvidence[] = [], suffix = '') => {
    result.push({ id: `${partId}:${kind}${suffix}`, kind, title, description, origin, sourceIds: [...new Set(source.map(item => item.sourceId))], evidence: uniqueEvidence([...source.flatMap(item => item.evidence), ...extra]), quantity: counted, status: 'pending_review' })
  }
  const cutNotes = notes.filter(item => /激光|下料|laser|cutting|cut contour/i.test(`${item.label} ${item.displayValue}`) && !/relief cut/i.test(item.displayValue))
  if (basis.flatRegionIds.length || cutNotes.length) {
    const regionEvidence = regions.filter(item => basis.flatRegionIds.includes(text(item.id))).map(item => ({ regionId: text(item.id), rawText: '图纸存在展开视图；仅作为下料工序建议依据，不是尺寸或切割长度测量。' }))
    append('cut', '下料 / 切割', '根据展开视图或明确下料说明形成工序候选。切割方式、轮廓闭合性、内外轮廓总长及机床排样须工程核对。', cutNotes, basis.cutLengthMm, cutNotes.length ? 'drawing_requirement' : 'suggested', regionEvidence)
  }
  const bendFeatures = eligible.filter(item => item.category === 'bend')
  if (bends.length || bendFeatures.length) {
    const rows = bends.filter(item => item.usable), tags = rows.map(item => item.tag).filter(Boolean)
    const counted = rows.length === bends.length && rows.length && tags.length === rows.length && new Set(tags).size === rows.length ? quantity(String(rows.length), 'bend', 'table_rows', `折弯表保留 ${rows.length} 行不同标签；行数仅作待核对候选，尚未确认实际折弯次数或顺序。`, rows.flatMap(item => item.evidence)) : null
    append('bend', '折弯', '按原标签、方向、角度和内半径逐行保留；工程师需核对折弯线、次数、刀具与生产顺序。', bendFeatures, counted, 'drawing_requirement', bends.flatMap(item => item.evidence))
  }
  const weld = eligible.filter(item => item.category === 'weld' || item.sourceKind === 'process_note' && /点焊|焊接|spot weld|weld/i.test(`${item.label} ${item.displayValue}`))
  if (weld.length) append('weld', '焊接', '保留焊接原文与可解释的计量候选；焊接方式、位置、长度及焊点适用范围待工程核对。', weld, weldQuantity(weld))
  const deburr = notes.filter(item => /去毛刺|锐边|deburr|free of burrs|break all sharp edges/i.test(`${item.label} ${item.displayValue} ${item.evidence.map(e => e.rawText).join(' ')}`))
  if (deburr.length) append('deburr', '去毛刺 / 锐边处理', '采用图纸明确的去毛刺或锐边要求；未假定统一倒角、加工时长或每件费率。', deburr, null)
  const finish = eligible.filter(item => item.category === 'surface')
  if (finish.length) append('finish', '表面处理', '按图纸保留表面处理要求；处理面积、工艺标准和工厂计价口径待确认。', finish, null)
  const usedNotes = new Set([...cutNotes, ...bendFeatures, ...weld, ...deburr, ...finish].filter(item => item.sourceKind === 'process_note').map(item => item.id))
  for (const note of notes) if (!usedNotes.has(note.id)) append('other', note.label || '其他工艺说明', note.displayValue, [note], null, 'drawing_requirement', [], `:${note.id}`)
  return result
}

/** Every parameter, process note and bend row remains assigned or explicitly unassigned. */
export function buildImageEngineering(source: ImageBomSource): ImageEngineeringProfile {
  const manifest = rec(source.manifest), rawParts = [...arr(manifest.parts), ...(Object.keys(rec(manifest.part)).length ? [manifest.part] : [])]
  const partMap = new Map<string, RecordValue>(), ambiguousParts = new Set<string>()
  for (const item of rawParts) {
    const part = rec(item), id = text(part.id)
    if (!id) continue
    if (partMap.has(id) && text(partMap.get(id)!.name) !== text(part.name)) ambiguousParts.add(id)
    if (!partMap.has(id)) partMap.set(id, part)
  }
  const regions = arr(manifest.regions).map(rec), owners = new Map<string, string | null>(), ambiguousRegions = new Set<string>()
  for (const region of regions) {
    const id = text(region.id), claimed = text(region.part_id), owner = partMap.has(claimed) && !ambiguousParts.has(claimed) && usable(partMap.get(claimed)!) && usable(region) ? claimed : null
    if (!id) continue
    if (owners.has(id) && owners.get(id) !== owner) ambiguousRegions.add(id)
    owners.set(id, owner)
  }
  for (const id of ambiguousRegions) owners.set(id, null)
  function assigned(evidence: BomEvidence[]): string | null {
    if (!evidence.length) return null
    const parts = new Set<string>()
    for (const item of evidence) { const part = item.regionId ? owners.get(item.regionId) : null; if (!part) return null; parts.add(part) }
    return parts.size === 1 ? [...parts][0] : null
  }
  const features: EngineeringFeature[] = []
  function feature(item: unknown, index: number, sourceKind: EngineeringFeature['sourceKind']): EngineeringFeature {
    const raw = rec(item), evidence = evidenceOf(raw), partId = assigned(evidence), sourceId = text(raw.id) || `${sourceKind}-${index + 1}`
    return { id: `${sourceKind}:${sourceId}:${index}`, sourceId, sourceKind, label: text(raw.label) || text(raw.title) || (sourceKind === 'process_note' ? '工艺说明' : '未命名标注'), value: sourceKind === 'process_note' ? raw.text ?? null : raw.value ?? null, displayValue: display(sourceKind === 'process_note' ? raw.text : raw.value), unit: optionalText(raw.unit), category: categoryOf(raw), evidence, sourceStatus: optionalText(raw.status), status: 'pending_review', partId, association: partId ? 'evidence' : 'unassigned', usable: usable(raw) && !!partId, direction: optionalText(raw.direction), datum: optionalText(raw.datum), feature: optionalText(raw.feature), basis: optionalText(raw.basis), raw }
  }
  arr(manifest.parameters).forEach((item, index) => features.push(feature(item, index, 'parameter')))
  arr(manifest.process_notes).forEach((item, index) => features.push(feature(item, index, 'process_note')))
  const sampleTables = manifest.mode === 'predefined_sample_regions' && partMap.size === 1 ? regions.filter(item => item.type === 'process_table' && owners.get(text(item.id)) && /折弯|bend/i.test(`${text(item.title)} ${text(item.description)}`)) : []
  const bends: EngineeringBendRow[] = arr(manifest.bend_rows).map((item, index) => {
    const raw = rec(item); let evidence = evidenceOf(raw), partId = assigned(evidence), association: EngineeringAssociation = partId ? 'evidence' : 'unassigned'
    // Old manually prepared sample omitted per-row references. This explicit
    // association is permitted only for its one assigned, named bend table.
    const tag = text(raw.tag)
    const escapedTag = tag.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const tableEvidence = sampleTables.length === 1 && tag ? features.filter(item => item.category === 'bend' && item.usable).flatMap(item => item.evidence).filter(item => item.regionId === text(sampleTables[0].id) && new RegExp(`(^|[^A-Za-z0-9])${escapedTag}($|[^A-Za-z0-9])`, 'i').test(item.rawText)) : []
    if (!evidence.length && sampleTables.length === 1 && tableEvidence.length) {
      partId = owners.get(text(sampleTables[0].id)) || null
      if (partId) { evidence = uniqueEvidence(tableEvidence); association = 'predefined_sample_association' }
    }
    const angle = numeric(raw.angle_deg), radius = numeric(raw.inner_radius_mm)
    return { id: `bend-row:${index}`, tag: text(raw.tag), direction: text(raw.direction), angleDeg: angle !== null && angle >= 0 && angle <= 360 ? angle : null, innerRadiusMm: radius !== null && radius >= 0 ? radius : null, evidence, sourceStatus: optionalText(raw.status), status: 'pending_review', partId, association, usable: usable(raw) && !!partId && angle !== null && angle >= 0 && angle <= 360 && radius !== null && radius >= 0, raw }
  })
  const parts: EngineeringPart[] = [...partMap].map(([partId, raw]) => {
    const own = features.filter(item => item.partId === partId), ownBends = bends.filter(item => item.partId === partId), ownRegions = regions.filter(item => owners.get(text(item.id)) === partId), groups = emptyGroups()
    for (const item of own) groups[item.category].push(item)
    const basis = materialBasis(own, ownRegions)
    return { partId, name: text(raw.name) || text(raw.drawing_title) || '名称待核对', features: own, groups, bends: ownBends, materialBasis: basis, processes: processList(partId, own, ownBends, basis, ownRegions) }
  })
  const unassigned = { features: features.filter(item => item.partId === null && item.sourceKind === 'parameter'), processNotes: features.filter(item => item.partId === null && item.sourceKind === 'process_note'), bends: bends.filter(item => item.partId === null) }
  const unassignedCount = unassigned.features.length + unassigned.processNotes.length + unassigned.bends.length
  const warnings = ['工程特征、工艺建议与计量均为待核对资料，不代表已确认加工路线、生产顺序或工时。', '同一零件的多个视图、重复标注和表格行数均不作为订单数量。']
  if (unassignedCount) warnings.push(`${unassignedCount} 项资料未能唯一归属零件，保留在未归属清单，没有分摊到各零件。`)
  if (ambiguousParts.size || ambiguousRegions.size) warnings.push('存在身份重复或区域归属冲突，相关资料保持未归属。')
  if (sampleTables.length && bends.some(item => item.association === 'predefined_sample_association')) warnings.push('预设样例的折弯行按其唯一折弯表关联，未新增模型识别；自动上传图纸必须保留逐行原文依据。')
  return { sourceFingerprint: fingerprint(source), sourceName: text(rec(manifest.source).filename), parts, unassigned, counts: { parameters: arr(manifest.parameters).length, processNotes: arr(manifest.process_notes).length, bendRows: arr(manifest.bend_rows).length, assignedFeatures: features.length - unassigned.features.length - unassigned.processNotes.length, unassigned: unassignedCount }, warnings, status: 'pending_review', engineering_approved: false }
}

function csvCell(value: unknown): string {
  let result = String(value ?? '')
  if (/^[\s\u0000-\u001f\u007f-\u009f]*[=+@-]/.test(result) || /^[\t\r\n]/.test(result)) result = `'${result}`
  return `"${result.replace(/"/g, '""')}"`
}
/** One export retains feature facts, original notes, rows and unassigned observations. */
export function engineeringCsv(profile: ImageEngineeringProfile, sourceName = profile.sourceName): string {
  const header = ['状态', '零件ID', '零件名称', '资料类型', '原ID', '类别', '标签', '值/说明', '单位', '原核对状态', '关联方式', '原文依据', '来源文件']
  const rows: unknown[][] = []
  const addFeature = (item: EngineeringFeature, name: string) => rows.push(['待核对，未工程批准', item.partId, name, item.sourceKind, item.sourceId, item.category, item.label, item.displayValue, item.unit, item.sourceStatus, item.association, item.evidence.map(e => `${e.regionId || '未定位'}: ${e.rawText}`).join('\n')])
  const addBend = (item: EngineeringBendRow, name: string) => rows.push(['待核对，未工程批准', item.partId, name, 'bend_row', item.id, 'bend', item.tag, `${item.direction} / ${item.angleDeg ?? '未知'}° / R${item.innerRadiusMm ?? '未知'}`, 'mm', item.sourceStatus, item.association, item.evidence.map(e => `${e.regionId || '未定位'}: ${e.rawText}`).join('\n')])
  for (const part of profile.parts) {
    for (const item of part.features) addFeature(item, part.name)
    for (const item of part.bends) addBend(item, part.name)
    for (const item of part.processes) rows.push(['待核对工序候选，未工程批准', part.partId, part.name, 'process_candidate', item.id, item.kind, item.title, item.quantity ? `${item.quantity.value} ${item.quantity.unit}；${item.quantity.explanation}` : item.description, item.quantity?.unit, 'pending_review', item.origin, item.evidence.map(e => `${e.regionId || '未定位'}: ${e.rawText}`).join('\n')])
    for (const [key, label] of [['thicknessMm', '板厚'], ['netAreaMm2', '展开净面积'], ['densityKgM3', '材料密度'], ['theoreticalMassKg', '理论净重'], ['cutLengthMm', '切割总长']] as const) {
      const value = part.materialBasis[key]
      rows.push(['待核对用料依据，未工程批准', part.partId, part.name, 'material_basis', key, 'material', label, value ? `${value.value}；${value.explanation}` : '尚无可采用的数值', value?.unit, 'pending_review', value?.basis || 'unknown', value?.evidence.map(e => `${e.regionId || '未定位'}: ${e.rawText}`).join('\n')])
    }
    for (const missing of part.materialBasis.missing) rows.push(['待补充，未工程批准', part.partId, part.name, 'material_missing', '', 'material', missing, '', '', 'pending_review', 'unknown', ''])
  }
  for (const item of [...profile.unassigned.features, ...profile.unassigned.processNotes]) addFeature(item, '未明确归属')
  for (const item of profile.unassigned.bends) addBend(item, '未明确归属')
  return '\uFEFF' + [header, ...rows.map(row => [...row, sourceName])].map(row => row.map(csvCell).join(',')).join('\r\n') + '\r\n'
}
