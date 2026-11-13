'use client'

import { Plus } from 'lucide-react'
import { quotationNumber } from '@/lib/batch-display-format'
import type { CostAccountingSummary, CostSheetPricing, FactoryCostReference, FactoryReferenceItem, FactoryReferenceRate } from '@/lib/batch-cost-sheet-types'
import './batch-factory-reference-panel.css'

type AdoptedRate = Pick<FactoryReferenceRate, 'label' | 'rate' | 'unit'>
type Props = { reference: FactoryCostReference; pricing?: CostSheetPricing; accounting?: CostAccountingSummary;
  editable: boolean; lineName: string | null; pricingChanged: boolean; draftChanged: boolean;
  replacesRetained(item: FactoryReferenceItem): boolean; onAdd(item: FactoryReferenceItem, rate?: AdoptedRate): void;
  onPricing(pricing: CostSheetPricing): void; onRestorePricing(): void }

const units: Record<string, string> = { piece: '件', set: '套', order: '单', minute: '分钟', hour: '小时', kg: 'kg', m: '米', m2: 'm²', dm2: 'dm²', hole: '孔', bend: '折', point: '点' }
const cash = (value: string | null | undefined) => value == null ? '待核价'
  : value.startsWith('-') ? `-¥${quotationNumber(value.slice(1))}` : `¥${quotationNumber(value)}`
const percent = (value: string) => `${quotationNumber(value)}%`
const pricingLabel = (value: CostSheetPricing) => `${value.pricing_method === 'cost_plus' ? '成本加成' : '目标毛利率'} ${percent(value.rate_percent)}`

export default function BatchFactoryReferencePanel({ reference, pricing, accounting, editable, lineName, pricingChanged, draftChanged, replacesRetained, onAdd, onPricing, onRestorePricing }: Props) {
  const current = reference.current_pricing
  const sample = reference.pricing_sample
  return <section className="bcs-reference" aria-label="客户核价模板" data-testid="bcs-factory-reference">
    <div className="bcs-reference-heading"><div><span className="bcs-eyebrow">FACTORY COSTING</span><h3>按客户核价表，逐项落实费用</h3><p>八项费用结构可直接使用。样品用量、材质和金额不会带入本单。</p></div><span className="bcs-reference-badge">内部核价参考</span></div>
    <div className="bcs-reference-grid">
      {reference.items.map((item, index) => {
        const replace = replacesRetained(item)
        const unavailable = item.scope === 'line' && !lineName
        const rate = item.sample_rate != null ? { label: item.label, rate: item.sample_rate, unit: item.sample_unit || item.unit } : undefined
        const rateOptions = item.sample_rates?.length ? item.sample_rates : rate ? [rate] : []
        return <article className="bcs-reference-item" key={item.id} data-testid={`bcs-factory-item-${item.id}`}>
          <div className="bcs-reference-item-title"><span>{String(index + 1).padStart(2, '0')}</span><h4>{item.label}</h4><small>{item.scope === 'order' ? '整单费用' : '零件费用'}</small></div>
          <p className="bcs-reference-note">{item.note}</p>
          <div className="bcs-reference-item-actions"><button type="button" disabled={!editable || unavailable} onClick={() => onAdd(item)} data-testid={`bcs-factory-add-${item.id}`}><Plus size={12} />{replace ? '替换原费用并添加' : item.scope === 'order' ? '添加整单费用' : '添加到当前零件'}</button><span>{item.scope === 'order' ? '金额另填，只计一次' : unavailable ? '请先在上方选择零件' : '填写本单用量与价格'}</span></div>
          {!!rateOptions.length && <div className="bcs-reference-rates">{rateOptions.map((value, rateIndex) => <div key={`${value.label}-${rateIndex}`}><span>{rateOptions.length > 1 ? `${value.label} · ` : ''}样品估价 <strong>{quotationNumber(value.rate)} 元/{units[value.unit] || value.unit}</strong></span><button type="button" disabled={!editable || unavailable} onClick={() => onAdd(item, value)} data-testid={`bcs-factory-adopt-rate-${item.id}${item.sample_rates?.length ? `-${rateIndex}` : ''}`}>{replace ? '替换并采用此费率' : '采用此费率'}</button></div>)}<small>仅填写所选费率；本单工程量、适用工艺和采购条件仍需核对。</small></div>}
          {replace && <p className="bcs-reference-replace-note">将替换本零件同类原保留费用{item.category === 'process' ? '及旧准备费' : ''}，避免重复计费。</p>}
        </article>
      })}
    </div>
    <section className="bcs-reference-pricing" data-testid="bcs-factory-pricing" aria-label="管理费与报价定价">
      <div className="bcs-reference-pricing-heading"><div><h4>管理费与报价定价</h4><p>当前已保存：管理费 {percent(current.management_rate_percent)} · {pricingLabel(current)} · 管理费{current.rounding === 'whole_yuan' ? '取整到元' : '保留到分'}。</p></div><span>{pricing ? '已列入本次核价条件' : '沿用当前条件'}</span></div>
      <div className="bcs-reference-pricing-sample"><p>表中参考：管理费 <strong>{percent(sample.management_rate_percent)}</strong>，完全成本上<strong>{pricingLabel(sample)}</strong>，管理费{sample.rounding === 'whole_yuan' ? '取整到元' : '保留到分'}。</p><div><button type="button" disabled={!editable} onClick={() => onPricing({ ...sample })} data-testid="bcs-adopt-factory-pricing">采用表中定价</button>{!pricing && <button type="button" disabled={!editable} onClick={() => onPricing({ ...current })} data-testid="bcs-edit-pricing">按当前条件调整</button>}</div></div>
      {!pricing && <p className="bcs-reference-pricing-hint">查看参考表不会改变报价。点击采用或调整后，才会将定价条件一并保存。</p>}
      {pricing && <div className="bcs-reference-pricing-editor" data-testid="bcs-pricing-editor">
        <div className="bcs-edit-grid">
          <label className="bcs-field">管理费率（%）<input type="text" inputMode="decimal" maxLength={32} disabled={!editable} value={pricing.management_rate_percent} onChange={event => onPricing({ ...pricing, management_rate_percent: event.target.value })} data-testid="bcs-pricing-management" /><small>按直接成本计算一次；不收取时明确填 0。</small></label>
          <label className="bcs-field">报价方式<select disabled={!editable} value={pricing.pricing_method} onChange={event => onPricing({ ...pricing, pricing_method: event.target.value as CostSheetPricing['pricing_method'] })} data-testid="bcs-pricing-method"><option value="cost_plus">成本加成</option><option value="gross_margin">目标毛利率</option></select></label>
          <label className="bcs-field">{pricing.pricing_method === 'cost_plus' ? '成本加成比例' : '目标毛利率'}（%）<input type="text" inputMode="decimal" maxLength={32} disabled={!editable} value={pricing.rate_percent} onChange={event => onPricing({ ...pricing, rate_percent: event.target.value })} data-testid="bcs-pricing-rate" /></label>
          <label className="bcs-field">管理费取整规则<select disabled={!editable} value={pricing.rounding} onChange={event => onPricing({ ...pricing, rounding: event.target.value as CostSheetPricing['rounding'] })} data-testid="bcs-pricing-rounding"><option value="cent">保留到分</option><option value="whole_yuan">按样表取整到元</option></select><small>{pricing.rounding === 'whole_yuan' ? '管理费按整元四舍五入；报价单价和金额保留两位小数。' : '管理费保留两位小数。'}样表 1,626.50 元取整为 1,627.00 元。</small></label>
        </div>
        <div className="bcs-reference-pricing-foot"><p>{pricing.pricing_method === 'cost_plus' ? '完全成本 = 直接成本 + 管理费，按加成比例计算售价。' : '完全成本 = 直接成本 + 管理费，按目标毛利率计算售价。'}保存时由程序核算，单价到分后汇总订单金额。</p><button type="button" disabled={!editable || !pricingChanged} onClick={onRestorePricing} data-testid="bcs-restore-pricing">撤销本次定价修改</button></div>
      </div>}
    </section>
    {accounting && <section className="bcs-accounting" aria-label="已保存核价汇总" data-testid="bcs-accounting-summary"><div className="bcs-accounting-title"><strong>已保存核价结果</strong><span>{draftChanged ? '当前草稿待保存，以下仍为已保存金额' : accounting.unpriced_lines ? `已计价范围 · ${accounting.unpriced_lines} 项仍待核价` : '本单成本与报价'}</span></div><div className="bcs-accounting-grid">{([
      ['direct_cost', '直接成本'], ['management_cost', '管理费'], ['full_cost', '完全成本'], ['profit', '利润'], ['quoted_price', '报价金额'],
    ] as const).map(([key, label]) => <div key={key} data-testid={`bcs-accounting-${key}`}><span>{label}</span><strong>{cash(accounting[key])}</strong></div>)}</div></section>}
    <details className="bcs-reference-source" data-testid="bcs-factory-source"><summary>查看样表来源与适用范围</summary><p>{reference.source.name}</p><p>来源工作表：{reference.source.sheet_names.join('、')}。表内金额为样品录入值，未提供计算公式。</p><ul>{reference.limitations.map((text, index) => <li key={index}>{text}</li>)}</ul><div className="bcs-reference-source-values">{reference.items.map(item => <p key={item.id}><strong>{item.label}</strong>：样品金额 {cash(item.sample_amount)}；{item.source.sheet} · {Array.isArray(item.source.cells) ? item.source.cells.join('、') : item.source.cells}。{item.sample_label}</p>)}</div></details>
  </section>
}
