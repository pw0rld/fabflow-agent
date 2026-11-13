export type CostCategory = 'material' | 'purchase' | 'process' | 'setup' | 'tooling' | 'other'
export type CostMethod = 'retained' | 'fixed' | 'quantity_rate' | 'material' | 'tooling_allocation'
export type CostEntry = {
  id: string; line_id: string | null; category: CostCategory; label: string; method: CostMethod;
  source_kind: 'legacy_estimate' | 'engineering_adopted' | 'manual_adopted'; note?: string;
  amount?: string; quantity?: string; rate?: string; unit?: string; process_id?: string;
  net_kg?: string; loss_kg?: string; unit_price?: string; scrap_price?: string;
  tooling_id?: string; tooling_amount?: string; amortization_quantity?: string;
}
export type CostTerms = { delivery: string; payment: string; validity: string; tax_shipping: string }
export type CostSheetPricing = { management_rate_percent: string; pricing_method: 'cost_plus' | 'gross_margin'; rate_percent: string; rounding: 'whole_yuan' | 'cent' }
export type FactoryReferenceCell = { sheet: string; cells: string[] | string }
export type FactoryReferenceRate = { label: string; rate: string; unit: string; source: FactoryReferenceCell }
export type FactoryReferenceItem = { id: string; label: string; category: CostCategory; scope: 'line' | 'order';
  method: Exclude<CostMethod, 'retained'>; unit: string; sample_amount: string; sample_rate: string | null;
  sample_unit: string; sample_label: string; source: FactoryReferenceCell; note: string; sample_rates?: FactoryReferenceRate[] }
export type FactoryCostReference = { version: 'customer-costing-reference-v1';
  source: { name: string; sha256: string; sheet_names: string[]; formula_count: number; value_kind: 'hardcoded' };
  auto_applied: false; current_pricing: CostSheetPricing; items: FactoryReferenceItem[];
  pricing_sample: CostSheetPricing; limitations: string[] }
export type CostAccountingSummary = { direct_cost: string | null; management_cost: string | null; full_cost: string | null; profit: string | null; quoted_price: string | null;
  management_rate_percent: string; pricing_method: CostSheetPricing['pricing_method']; rate_percent: string;
  rounding: CostSheetPricing['rounding']; scope: 'priced_lines'; unpriced_lines: number }
export type CostSheetPolicy = { base_digest: string; entries: CostEntry[]; customer_terms?: CostTerms; source_binding?: unknown; adopt_unpriced_line_ids?: string[]; pricing?: CostSheetPricing }
export type ProcessCatalogItem = { id: string; label: string; group: string; default_unit: string; default_rate: null }
export type CostSheet = { version: 'cost-sheet-v1'; base_digest: string; entries: CostEntry[];
  catalog: ProcessCatalogItem[]; process_catalog?: ProcessCatalogItem[]; entry_amounts?: Record<string, string | null>;
  totals: Record<string, string | number | null>; customer_terms: CostTerms; status: string; policy?: CostSheetPolicy;
  factory_reference?: FactoryCostReference; accounting_summary?: CostAccountingSummary }
export type CostSheetResponse = { sheet: CostSheet; expected_revision: number; source_digest: string;
  source_binding?: unknown; current_run_id?: string; can_reprice: boolean }
export type CostSheetLine = { id: string; part_name: string; document: string; quantity_total: number | null;
  estimated_total_net_weight_kg?: string | null; material_rate?: string | null; total_cost?: string | null;
  total_price?: string | null; priced: boolean }
