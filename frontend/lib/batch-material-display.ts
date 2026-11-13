export type BatchMaterialLine = {
  mode?: string
  material_raw?: string | null
  material_profile?: string | null
  confidence?: string
  material_estimation_label?: string | null
  weight_assumptions?: { adopted_material_label?: string | null } | null
  material_rate?: string | null
  material_price_source_kind?: string | null
  material_price_snapshot?: Record<string, unknown> | null
  purchase_price_source_kind?: string | null
  adopted_material_source?: unknown[] | null
  engineering_pricing_methods?: string[] | null
}

const materials: Record<string, string> = {
  carbon_steel: '碳钢（Q235类）',
  spcc: '冷轧钢板（SPCC类）',
  ss304: '304不锈钢',
  ss316: '316类不锈钢',
  al5052: '5052铝合金',
  stainless_mesh: '8目不锈钢纱网（牌号未定）',
}
const sources: Record<string, string> = {
  market_reference: '行情参考价',
  supplier_quote: '供应商录入价',
  manual: '手工录入价',
}

export function batchMaterialDisplay(line: BatchMaterialLine) {
  const rawText = typeof line.material_raw === 'string' ? line.material_raw.trim() : ''
  const raw = /^(?:COMPANY|PTC_MASTER_MATERIAL|PTC_SYSTEM_MTRL_PROPS|设计单位)$/i.test(rawText) ? '' : rawText
  const profile = line.material_profile ?? line.material_price_snapshot?.material_profile
  const referenceStainless = profile === 'ss304' && line.confidence === 'assumed_material' && !raw
    && (line.material_estimation_label === '不锈钢估价（参考304）' || line.weight_assumptions?.adopted_material_label === '不锈钢估价（参考304）')
  const adopted = referenceStainless ? '不锈钢估价（参考304）' : typeof profile === 'string' && Object.hasOwn(materials, profile) ? materials[profile] : ''
  const rate = Object.hasOwn(line, 'material_rate') ? line.material_rate : line.material_price_snapshot?.normalized_net_rate
  const hasRate = rate != null && rate !== ''
  const kind = line.material_price_source_kind || line.material_price_snapshot?.source_kind
  const price = line.mode === 'buy' ? line.purchase_price_source_kind === 'manual' ? '本单指定采购价' : '外购件演示价' : !hasRate ? '暂未录价' : typeof kind === 'string' && Object.hasOwn(sources, kind) ? sources[kind] : kind ? '价格来源待核对' : '演示材料价'
  const engineering = !!line.adopted_material_source?.length && !!line.engineering_pricing_methods?.some(method => method === 'adopt_material' || method === 'purchase_mesh')
  return {
    cad_text: raw,
    adopted_label: engineering ? '按图纸核价采用' : line.mode === 'buy' ? '估重采用' : '估价采用',
    adopted_text: adopted || '暂未采用',
    price_text: price,
    note: !adopted ? '尚未采用估价材料。' : referenceStainless ? '本单估价采用参考304条件，具体牌号及实际采购规格待核对。' : engineering && profile === 'stainless_mesh' && line.purchase_price_source_kind === 'manual' ? '依据图纸纱网要求按件计价，采用本单指定采购价；丝径及采购规格待核对，模型估重不等同于实际网重。' : engineering && profile === 'stainless_mesh' ? '依据图纸纱网要求按件演示计价；丝径及采购规格待核对，模型估重不等同于实际网重。' : engineering ? `已按图纸材料类别核价；${price}，供货规格与实际采购条件仍需核对。` : raw ? 'CAD原文已保留，具体牌号和采购规格需核对。' : '按本单所选材料估算，材料牌号需核对。',
  }
}
