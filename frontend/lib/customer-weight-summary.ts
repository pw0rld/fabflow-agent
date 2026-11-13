export type CustomerWeightSummary = {
  status: 'complete' | 'partial' | 'unavailable'; complete: boolean; unit: 'kg'; role: 'cross_check';
  source: 'saved_line_weights'; includes_packaging: false; order_sets: number | null;
  line_count: number; known_weight_lines: number; missing_estimated_lines: number;
  estimated_set_net_weight_kg: string | null; estimated_total_net_weight_kg: string | null;
  known_set_net_weight_kg: string | null; known_total_net_weight_kg: string | null;
  low_confidence_lines: number; low_confidence_total_net_weight_kg: string | null;
}

export type MaterialRequirementHint = {
  source: 'filename'; family: 'stainless_steel'; grade: null; label: string; conflicts_with_adopted_material: boolean;
}
export type CustomerWeightAssumptions = {
  density_kg_m3: string | null; cad_material_text: string | null; adopted_material_label: string | null;
  source: 'cad_candidate' | 'order_assumption' | 'engineering_candidate' | 'proxy_estimate' | 'low_confidence' | 'unknown';
  description: string; includes_packaging: false; requirement_hint?: MaterialRequirementHint | null;
}

/** Display the saved scope explicitly; never reconstruct a total from parts or rounded values. */
export function savedWeightDisplay(summary?: CustomerWeightSummary | null) {
  const complete = summary?.status === 'complete' && summary.complete === true
  const partial = summary?.status === 'partial' && summary.complete === false
  return {
    status: complete ? 'complete' : partial ? 'partial' : 'unavailable',
    set: complete ? summary?.estimated_set_net_weight_kg ?? null : partial ? summary?.known_set_net_weight_kg ?? null : null,
    total: complete ? summary?.estimated_total_net_weight_kg ?? null : partial ? summary?.known_total_net_weight_kg ?? null : null,
    setLabel: partial ? '每套已知重量小计' : '每套预估净重',
    totalLabel: partial ? '整单已知重量小计' : '整单预估净重',
  }
}
