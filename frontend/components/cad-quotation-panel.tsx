'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, Calculator, Check, Download, ExternalLink, FileText, LoaderCircle, Plus, Save, Trash2 } from 'lucide-react'
import './cad-quotation-panel.css'

type Operation = { name: string; rate: string | null; basis: 'CNY/piece' | 'CNY/order'; inclusions: string }
type Line = { group_id: string; selected: boolean; mode: 'make' | 'buy' | 'unresolved'; quantity_per_set: number | null; material: string; material_form?: string; thickness_mm: string | null; net_weight_kg?: string | null; material_usage: string | null; material_rate: string | null; purchase_price: string | null; operations: Operation[]; setup_fee: string | null; inclusions: string; price_refs?: { entry_id: string; version: number }[] }
type Inputs = { order_sets: number | null; product_family: string; lines: Line[]; pricing: { method: 'cost_plus' | 'gross_margin' | null; rate_percent: string | null; tax_excluded: boolean; freight_excluded: boolean }; shared_fees: { name: string; value: string | null; inclusions: string }[]; remarks: string; review_confirmed: boolean }
type Group = { id: string; name: string; classification: string; status: string; solid_ids: string[] }
type Issue = { message: string; field: string; group_id?: string }
type Totals = { total_cost: string; total_price: string; material_cost?: string; process_cost?: string; setup_cost?: string; shared_cost?: string }
type WeightLine = { net_weight_kg: string | null; set_net_weight_kg: string | null; total_net_weight_kg: string | null; total_material_usage_kg: string | null; source: 'entered' | 'unknown' }
type WeightSummary = { lines: Record<string, WeightLine>; totals: { total_net_weight_kg: string | null; known_net_weight_kg: string | null; missing_net_weight_lines: number; selected_lines: number; total_material_usage_kg: string | null; known_material_usage_kg: string | null; missing_material_usage_lines: number; material_usage_lines: number } }
type ResultLine = { group_id: string; name: string; quantity: number | string; unit_cost: string; total_cost: string; unit_price: string; total_price: string; net_weight_kg?: string | null; total_net_weight_kg?: string | null }
type Quote = { id?: string; created_at?: string; source_current?: boolean; status?: string; lines: ResultLine[]; totals: Totals; weight_summary?: WeightSummary; quote_url?: string; cost_url?: string; bom_url?: string; warnings?: string[] }
type Price = { entry_id: string; version: number; unit: string; rate?: string; value?: string; price?: string; inclusions?: string[]; operation?: string; simulated?: boolean; source?: string }
type Match = { kind: string; operation?: string; status: string; candidates: Price[] }
type Evidence = { volume_mm3?: number | null; area_mm2?: number | null; group_status?: string; notes?: string[] | string }
type State = { document_id: string; revision: number; source_revision: number; source_current: boolean; source_sha256: string; inputs: Inputs; groups: Group[]; weight_summary?: WeightSummary; geometry_evidence?: Record<string, Evidence>; catalog?: { status: string; matches: Record<string, { results: Match[] }> }; quotes: Quote[]; validation: { ready: boolean; issues: Issue[] }; quote?: Quote }
type Estimate = Quote & { validation?: { ready: boolean; issues: Issue[] }; ready?: boolean; issues?: Issue[] }

const decimal = (value: string) => value === '' ? null : value
const integer = (value: string) => value === '' ? null : Number(value)
const money = (value: string | undefined) => value == null ? '—' : `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
const weight = (value: string | null | undefined) => value == null || value === '' ? '待补' : `${value} kg`
const modes = { make: '自制件', buy: '外购件', unresolved: '待选择' }
const operationNames = ['激光切割', '折弯', '钻孔', '焊接', '打磨', '抛光', '表面处理', '检验包装']
const statusNames: Record<string, string> = { not_found: '尚无匹配价格', missing_conditions: '匹配条件待补充', unsupported: '暂不支持此匹配条件', expired: '匹配价格已过期', unique: '找到匹配价格', ambiguous: '多个候选，请核对' }
async function request<T>(url: string, payload?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { method: payload === undefined ? 'GET' : 'POST', signal, headers: { 'X-Workbench-Request': '1', ...(payload === undefined ? {} : { 'Content-Type': 'application/json' }) }, ...(payload === undefined ? {} : { body: JSON.stringify(payload) }) })
  const data = await response.json()
  if (!response.ok) throw new Error(data.error?.message || `请求未完成（${response.status}）`)
  return data as T
}
function Links({ quote, base }: { quote: Quote; base: string }) {
  if (!quote.id) return null
  const root = `${base}/quotes/${encodeURIComponent(quote.id)}`
  return <div className="cq-links"><a className="cad-button cad-primary" data-testid="cq-open-quote" href={quote.quote_url || `${root}/quote.html`} target="_blank" rel="noopener noreferrer"><ExternalLink size={14} />打开报价单</a><a className="cad-button" href={quote.cost_url || `${root}/cost.csv`}><Download size={14} />成本明细</a><a className="cad-button" href={quote.bom_url || `${root}/bom.csv`}><Download size={14} />BOM</a></div>
}

function WeightSummaryCard({ summary, dirty, title = '已保存重量汇总' }: { summary?: WeightSummary; dirty: boolean; title?: string }) {
  const totals = summary?.totals
  return <section className="cq-weight-summary" data-testid="cq-weight-summary" aria-label={title}>
    <div className="cq-weight-heading"><h4>{title}</h4>{dirty && <span>有修改待保存</span>}</div>
    {dirty && <p className="cq-weight-note" data-testid="cq-weight-stale">以下重量仍按已保存的数量和输入汇总，请保存参数后更新。</p>}
    {!totals ? <p className="cq-weight-note">尚无重量记录。净重可按图纸或称重结果补充，未知项保持待补。</p> : <div className="cq-weight-grid">
      <div><span>整单净重</span><strong data-testid="cq-total-net-weight">{weight(totals.total_net_weight_kg)}</strong>{totals.total_net_weight_kg == null && <small data-testid="cq-weight-known">{totals.known_net_weight_kg != null ? `已知部分 ${weight(totals.known_net_weight_kg)}；` : ''}{!totals.selected_lines ? '尚未选择报价零件' : totals.missing_net_weight_lines > 0 ? `${totals.missing_net_weight_lines} 行重量或数量待补` : '套数或每套用量待补'}</small>}</div>
      <div><span>原料采购计价总量</span><strong data-testid="cq-total-material-weight">{!totals.material_usage_lines ? '无自制原料项' : weight(totals.total_material_usage_kg)}</strong>{totals.material_usage_lines > 0 && totals.total_material_usage_kg == null && <small>{totals.known_material_usage_kg != null ? `已知部分 ${weight(totals.known_material_usage_kg)}；` : ''}{totals.missing_material_usage_lines > 0 ? `${totals.missing_material_usage_lines} 行用料或数量待补` : '套数或每套用量待补'}</small>}</div>
    </div>}
    <p className="cq-weight-note">净重与采购计价用量分别记录；原料总量仅汇总自制件，不包含外购件或包装重量。</p>
  </section>
}

export default function CadQuotationPanel({ documentId, sourceRevision, dirty: sourceDirty, onBack }: { documentId: string; sourceRevision: number; dirty: boolean; onBack(): void }) {
  const base = `/workbench/api/cad/documents/${encodeURIComponent(documentId)}/quotation`
  const [state, setState] = useState<State | null>(null), [inputs, setInputs] = useState<Inputs | null>(null)
  const [stage, setStage] = useState(0), [activeId, setActiveId] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false), [busy, setBusy] = useState(''), [error, setError] = useState(''), [notice, setNotice] = useState('')
  const [estimate, setEstimate] = useState<Estimate | null>(null), [issued, setIssued] = useState<Quote | null>(null), [showIssues, setShowIssues] = useState(false), [demoPrompt, setDemoPrompt] = useState(false)
  const generation = useRef(0)
  const issueRequest = useRef<{ key: string; id: string } | null>(null)
  const selected = inputs?.lines.filter(line => line.selected) || []
  const names = useMemo(() => new Map((state?.groups || []).map(group => [group.id, group.name])), [state?.groups])
  const active = inputs?.lines.find(line => line.group_id === activeId && line.selected) || selected[0]
  const activeSavedWeight = active ? state?.weight_summary?.lines[active.group_id] : undefined
  const stale = !!state && (!state.source_current || state.source_revision !== sourceRevision)
  const blocked = !!busy || sourceDirty || stale
  const issues = estimate?.validation?.issues || estimate?.issues || state?.validation.issues || []
  function take(next: State) { setState(next); setInputs(next.inputs); setDirty(false); setActiveId(previous => next.inputs.lines.some(line => line.group_id === previous && line.selected) ? previous : next.inputs.lines.find(line => line.selected)?.group_id || null) }
  useEffect(() => {
    const epoch = ++generation.current, abort = new AbortController()
    setState(null); setInputs(null); setBusy('载入报价参数'); setError(''); setEstimate(null); setIssued(null); setDirty(false)
    void request<State>(base, undefined, abort.signal).then(next => { if (epoch === generation.current) take(next) }).catch(failure => { if (!abort.signal.aborted) setError(failure instanceof Error ? failure.message : '载入未完成。') }).finally(() => { if (epoch === generation.current) setBusy('') })
    return () => { ++generation.current; abort.abort() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base, sourceRevision])
  function edit(update: (value: Inputs) => void, preserveReview = false) {
    setInputs(previous => { if (!previous) return previous; const next = structuredClone(previous); update(next); if (!preserveReview) next.review_confirmed = false; return next })
    setDirty(true); setEstimate(null); setIssued(null); setShowIssues(false); setNotice('')
  }
  function editLine(groupId: string, values: Partial<Line>) { edit(next => { const line = next.lines.find(row => row.group_id === groupId); if (line) Object.assign(line, values) }) }
  function editOperation(index: number, values: Partial<Operation>) { if (active) editLine(active.group_id, { operations: active.operations.map((row, i) => i === index ? { ...row, ...values } : row) }) }
  function detachPriceReferences() {
    if (!active?.price_refs?.length) return
    const references = active.price_refs.map(ref => `${ref.entry_id} v${ref.version}`).join('、')
    const note = `操作员已选择将本件费率转为手工输入，保留当前数值；价格来源与适用条件需重新核对。已取消有效价格库引用：${references}。`
    editLine(active.group_id, { price_refs: [], inclusions: note + (active.inclusions ? `\n历史参考（不作为本次有效价格库来源）：${active.inclusions}` : ''), operations: active.operations.map(row => row.inclusions.includes('价格库 ') ? { ...row, inclusions: note + `\n历史参考：${row.inclusions}` } : row) })
    setNotice('已明确转为手工费率，请核对价格来源与包含范围并重新保存。')
  }
  async function persist(): Promise<State> {
    if (!state || !inputs) throw new Error('报价参数尚未载入。')
    if (!dirty) return state
    const next = await request<State>(base + '/inputs', { expected_revision: state.revision, source_revision: sourceRevision, inputs }); take(next); return next
  }
  async function action(kind: 'save' | 'estimate' | 'issue' | 'demo' | 'rebase') {
    if (!state || !inputs || busy || sourceDirty || (stale && kind !== 'rebase')) return
    setBusy(({ save: '保存参数', estimate: '计算成本', issue: '生成报价', demo: '填入演示参数', rebase: '更新来源' })[kind]); setError(''); setNotice('')
    try {
      if (kind === 'rebase' || kind === 'demo') {
        setIssued(null); setEstimate(null)
        const current = kind === 'demo' ? await persist() : state
        const next = await request<State>(base + '/' + kind, { expected_revision: current.revision, source_revision: sourceRevision }); take(next); setEstimate(null); setShowIssues(false); setDemoPrompt(false)
        setNotice(kind === 'demo' ? '已填入明确标记的演示假设；定价比例仍请自行选择。' : '已更新到当前 CAD 分组，请重新核对报价输入。')
      } else {
        const current = await persist()
        if (kind === 'save') { setNotice('报价参数已保存。'); return }
        if (kind === 'issue') {
          const key = `${documentId}:${sourceRevision}:${current.revision}`
          if (issueRequest.current?.key !== key) issueRequest.current = { key, id: crypto.randomUUID() }
        }
        const next = await request<Estimate & State>(base + (kind === 'estimate' ? '/estimate' : '/quotes'), { expected_revision: current.revision, source_revision: sourceRevision, ...(kind === 'issue' ? { request_id: issueRequest.current!.id } : {}) })
        if (kind === 'estimate') { setEstimate(next); setShowIssues(true); setNotice(next.validation?.ready === false || next.ready === false ? '请补充下面列出的参数，再计算。' : '成本已按保存参数计算。') }
        else { const quote = next.quote || next; setIssued(quote); setEstimate(quote); setNotice('报价草稿已生成，可以直接打开查看。'); setState(await request<State>(base)) }
      }
    } catch (failure) { setError(failure instanceof Error ? failure.message : '操作未完成，请重试。'); setShowIssues(true) }
    finally { setBusy('') }
  }
  const matches = active ? state?.catalog?.matches?.[active.group_id]?.results || [] : []
  function adoptPrice(match: Match, candidate: Price) {
    if (!active) return
    const value = candidate.rate ?? candidate.value ?? candidate.price
    if (value == null) return
    const source = `价格库 ${candidate.entry_id} v${candidate.version}；${candidate.inclusions?.join('；') || ''}${candidate.simulated ? '；模拟价格' : ''}`
    const replaced = matches.filter(item => item.kind === match.kind && item.operation === match.operation).flatMap(item => item.candidates.map(price => price.entry_id))
    const price_refs = [...(active.price_refs || []).filter(ref => !replaced.includes(ref.entry_id)), { entry_id: candidate.entry_id, version: candidate.version }]
    if (match.kind === 'material' && candidate.unit === 'CNY/kg') editLine(active.group_id, { material_rate: String(value), inclusions: [active.inclusions, source].filter(Boolean).join('\n'), price_refs })
    else if (match.kind === 'setup' && candidate.unit === 'CNY/order') editLine(active.group_id, { setup_fee: String(value), inclusions: [active.inclusions, source].filter(Boolean).join('\n'), price_refs })
    else if (match.kind === 'operation' && (candidate.unit === 'CNY/piece' || candidate.unit === 'CNY/order')) {
      const name = match.operation || candidate.operation || '加工', index = active.operations.findIndex(row => row.name === name)
      const item: Operation = { name, rate: String(value), basis: candidate.unit, inclusions: source }
      editLine(active.group_id, { operations: index < 0 ? [...active.operations, item] : active.operations.map((row, i) => i === index ? item : row), price_refs })
    }
  }
  return <section className="cq-panel" data-testid="cad-quotation-panel">
    <header className="cq-heading"><div><button className="cq-back" onClick={onBack}><ArrowLeft size={14} />返回制造零件</button><h2><FileText size={21} />从 STEP 生成报价草稿</h2><p>沿用已保存的制造分组，补充用料与价格后，由程序统一核算。</p></div><div className="cq-toolbar"><span className="cq-draft-tag">模拟报价 · 待审核</span><button className="cad-button" data-testid="cq-save" disabled={blocked || !dirty} onClick={() => void action('save')}><Save size={14} />保存参数</button></div></header>
    {sourceDirty && <div className="cq-alert">制造分组还有未保存的修改。请先返回保存，再继续报价。</div>}
    {stale && <div className="cq-alert">当前报价输入来自旧版 CAD 分组。<button className="cad-button cad-small" disabled={!!busy || sourceDirty} data-testid="cq-rebase" onClick={() => void action('rebase')}>更新到当前分组</button></div>}
    {error && <div className="cq-error" role="alert" data-testid="cq-error">{error}</div>}
    {notice && <div className="cq-notice" role="status">{notice}</div>}
    {!!busy && <div className="cq-progress" role="status"><LoaderCircle className="cad-spin" size={15} />{busy}…</div>}
    {!inputs ? !busy && <p className="cq-muted">暂未载入报价参数，请返回后重试。</p> : <>
      <nav className="cq-steps" aria-label="报价步骤">{['选择报价零件', '用料与加工价格', '核算并生成报价'].map((label, index) => <button key={label} aria-current={stage === index ? 'step' : undefined} className={stage === index ? 'cq-current-step' : ''} onClick={() => setStage(index)} data-testid={`cq-stage-${index}`}><span>{index + 1}</span>{label}</button>)}</nav>
      <fieldset disabled={blocked} className="cq-fields">
        {stage === 0 && <section className="cq-section"><div className="cq-section-heading"><div><h3>报价范围</h3><p>每套用量来自已保存分组；本次报价的调整不会改动原 CAD。</p></div><div className="cq-inline"><label>订购套数<input data-testid="cq-order-sets" type="number" min="1" step="1" value={inputs.order_sets ?? ''} placeholder="请输入" onChange={event => edit(next => { next.order_sets = integer(event.target.value) })} /></label><label>产品类别<input data-testid="cq-product-family" value={inputs.product_family} placeholder="用于价格库匹配" onChange={event => edit(next => { next.product_family = event.target.value })} /></label></div></div>
          <div className="cq-table-scroll"><table className="cq-table" data-testid="cq-lines"><thead><tr><th><input type="checkbox" aria-label="选择全部报价零件" checked={inputs.lines.length > 0 && inputs.lines.every(line => line.selected)} onChange={event => edit(next => { next.lines.forEach(line => { line.selected = event.target.checked }) })} /></th><th>制造零件</th><th>计价类别</th><th>每套用量</th><th>本单总件数</th><th>材料 / 板厚</th></tr></thead><tbody>{inputs.lines.map(line => <tr key={line.group_id} className={!line.selected ? 'cq-unselected' : ''} data-testid={'cq-line-' + line.group_id}><td><input type="checkbox" aria-label={'报价 ' + (names.get(line.group_id) || line.group_id)} checked={line.selected} onChange={event => editLine(line.group_id, { selected: event.target.checked })} /></td><td><strong>{names.get(line.group_id) || line.group_id}</strong><small>{line.group_id}</small></td><td><select aria-label={'计价类别 ' + line.group_id} value={line.mode} onChange={event => editLine(line.group_id, { mode: event.target.value as Line['mode'] })}>{Object.entries(modes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></td><td><input aria-label={'每套用量 ' + line.group_id} type="number" min="1" step="1" value={line.quantity_per_set ?? ''} placeholder="待确认" onChange={event => editLine(line.group_id, { quantity_per_set: integer(event.target.value) })} /></td><td>{line.quantity_per_set && inputs.order_sets ? line.quantity_per_set * inputs.order_sets : '待确认'}</td><td>{line.material || '材料待补'}<small>{line.thickness_mm ? `${line.thickness_mm} mm` : '板厚待补'}</small></td></tr>)}</tbody></table></div>
          <WeightSummaryCard summary={state?.weight_summary} dirty={dirty} />
          <div className="cq-step-footer"><span>已选 {selected.length} / {inputs.lines.length} 个分组</span><button className="cad-button cad-primary" disabled={!selected.length} onClick={() => { setActiveId(selected[0]?.group_id || null); setStage(1) }}>填写用料与价格<ArrowRight size={14} /></button></div>
        </section>}
        {stage === 1 && <section className="cq-section"><div className="cq-section-heading"><div><h3>用料与加工价格</h3><p>材料按 kg、工序按件或按单核算。展开用料未知时需补充，表面积和体积不直接充当采购用量。</p></div><button className="cad-button" data-testid="cq-demo" onClick={() => setDemoPrompt(!demoPrompt)}>填入演示参数</button></div>
          {demoPrompt && <div className="cq-demo-prompt"><strong>用模拟数据跑通流程</strong><p>演示用量、费率和费用包含范围会写入报价备注，仅用于展示。它们不来自图纸或工厂报价。</p><div className="cq-toolbar"><button className="cad-button cad-primary" data-testid="cq-confirm-demo" onClick={() => void action('demo')}>确认使用演示参数</button><button className="cad-button" onClick={() => setDemoPrompt(false)}>取消</button></div></div>}
          {!selected.length ? <p className="cq-muted">请先选择需要报价的零件。</p> : <div className="cq-part-layout"><aside className="cq-part-list">{selected.map(line => <button key={line.group_id} className={active?.group_id === line.group_id ? 'cq-active-part' : ''} data-testid={'cq-edit-' + line.group_id} onClick={() => setActiveId(line.group_id)}><strong>{names.get(line.group_id) || line.group_id}</strong><small>{modes[line.mode]} · {line.quantity_per_set ?? '—'} 件 / 套</small></button>)}</aside>{active && <div className="cq-part-inputs"><h4>{names.get(active.group_id) || active.group_id}</h4>
            <div className="cq-input-grid"><label>计价类别<select data-testid="cq-mode" value={active.mode} onChange={event => editLine(active.group_id, { mode: event.target.value as Line['mode'] })}>{Object.entries(modes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label>每套用量<input type="number" data-testid="cq-quantity" min="1" step="1" value={active.quantity_per_set ?? ''} onChange={event => editLine(active.group_id, { quantity_per_set: integer(event.target.value) })} placeholder="待确认" /></label></div>
            <div className="cq-weight-entry"><label>单件净重（kg / 件）<input data-testid="cq-net-weight" type="number" min="0" step="any" value={active.net_weight_kg ?? ''} onChange={event => editLine(active.group_id, { net_weight_kg: decimal(event.target.value) })} placeholder="可填图纸或称重值，未知留空" /></label><p>用于重量汇总，与原料采购计价用量分别记录。</p></div>
            <div className="cq-weight-line" data-testid="cq-weight-line"><div className="cq-weight-heading"><h4>本件已保存重量</h4>{dirty && <span>保存后更新</span>}</div>{dirty && <p className="cq-weight-note">下方数值仍对应已保存参数。</p>}<div className="cq-weight-grid"><div><span>每套本件净重</span><strong data-testid="cq-set-net-weight">{weight(activeSavedWeight?.set_net_weight_kg)}</strong></div><div><span>本单本件净重</span><strong data-testid="cq-line-total-net-weight">{weight(activeSavedWeight?.total_net_weight_kg)}</strong></div>{active.mode === 'make' && <div><span>本单原料计价量</span><strong data-testid="cq-line-material-weight">{weight(activeSavedWeight?.total_material_usage_kg)}</strong></div>}</div></div>
            {active.mode === 'make' && <><div className="cq-input-grid"><label>材料牌号<input data-testid="cq-material" value={active.material} onChange={event => editLine(active.group_id, { material: event.target.value })} placeholder="如 Q235B、SUS304" /></label><label>板厚（mm）<input data-testid="cq-thickness" type="number" min="0" step="any" value={active.thickness_mm ?? ''} onChange={event => editLine(active.group_id, { thickness_mm: decimal(event.target.value) })} placeholder="待确认" /></label><label>材料形态<input data-testid="cq-material-form" value={active.material_form || ''} onChange={event => editLine(active.group_id, { material_form: event.target.value })} placeholder="如板材；用于价格匹配" /></label><label>原料采购计价用量（kg / 件）<input data-testid="cq-material-usage" type="number" min="0" step="any" value={active.material_usage ?? ''} onChange={event => editLine(active.group_id, { material_usage: decimal(event.target.value) })} placeholder="展开 / 工程核定用料" /></label><label>材料单价（元 / kg）<input data-testid="cq-material-rate" type="number" min="0" step="any" value={active.material_rate ?? ''} onChange={event => editLine(active.group_id, { material_rate: decimal(event.target.value) })} placeholder="实际价格或演示假设" /></label></div>
              <div className="cq-subheading"><h4>加工工序</h4><button className="cad-button cad-small" onClick={() => editLine(active.group_id, { operations: [...active.operations, { name: '', rate: null, basis: 'CNY/piece', inclusions: '' }] })}><Plus size={13} />添加工序</button></div><div className="cq-operation-shortcuts">{operationNames.filter(name => !active.operations.some(row => row.name === name)).map(name => <button key={name} onClick={() => editLine(active.group_id, { operations: [...active.operations, { name, rate: null, basis: 'CNY/piece', inclusions: '' }] })}>+ {name}</button>)}</div>
              {!active.operations.length && <p className="cq-muted">选择本件需要的工序，并填写费率与包含范围。</p>}
              {active.operations.map((row, index) => <div className="cq-operation" key={index} data-testid={`cq-operation-${index}`}><div className="cq-operation-row"><input aria-label={`工序 ${index + 1} 名称`} value={row.name} placeholder="工序名称" onChange={event => editOperation(index, { name: event.target.value })} /><input aria-label={`工序 ${index + 1} 费率`} type="number" min="0" step="any" value={row.rate ?? ''} placeholder="费率" onChange={event => editOperation(index, { rate: decimal(event.target.value) })} /><select aria-label={`工序 ${index + 1} 计价单位`} value={row.basis} onChange={event => editOperation(index, { basis: event.target.value as Operation['basis'] })}><option value="CNY/piece">元 / 件</option><option value="CNY/order">元 / 单</option></select><button className="cq-icon" aria-label={`删除工序 ${index + 1}`} onClick={() => editLine(active.group_id, { operations: active.operations.filter((_, i) => i !== index) })}><Trash2 size={15} /></button></div><input aria-label={`工序 ${index + 1} 包含范围`} value={row.inclusions} placeholder="包含范围，如仅加工费、含去毛刺；不含其他工序" onChange={event => editOperation(index, { inclusions: event.target.value })} /></div>)}
              <div className="cq-input-grid"><label>本件一次性准备费（元 / 单）<input data-testid="cq-setup-fee" type="number" min="0" step="any" value={active.setup_fee ?? ''} placeholder="明确不收时填 0" onChange={event => editLine(active.group_id, { setup_fee: decimal(event.target.value) })} /></label></div>
            </>}
            {active.mode === 'buy' && <label>采购单价（元 / 件）<input data-testid="cq-purchase-price" type="number" min="0" step="any" value={active.purchase_price ?? ''} onChange={event => editLine(active.group_id, { purchase_price: decimal(event.target.value) })} placeholder="待补充" /></label>}
            {active.mode === 'unresolved' && <div className="cq-alert">请先选择自制件或外购件。待设计分组不能直接当成已完成的制造零件。</div>}
            <label>价格来源与费用包含范围<textarea data-testid="cq-inclusions" value={active.inclusions} onChange={event => editLine(active.group_id, { inclusions: event.target.value })} placeholder="说明用料依据、价格来源及准备费范围；避免按单工序费与准备费重复收费" /></label>
            {!!active.price_refs?.length && <div className="cq-demo-prompt" data-testid="cq-price-references"><strong>已引用 {active.price_refs.length} 个价格库版本</strong><p>修改数量、材料、费率或工序后，系统仍会校验这些版本。可以重新匹配价格；如需自行承担手工费率的核对，请明确转换。</p><button className="cad-button" data-testid="cq-detach-prices" onClick={detachPriceReferences}>转为手工费率，取消本件价格库引用</button></div>}
            <details className="cq-catalog"><summary>查看价格库匹配结果{dirty ? '（保存后更新）' : ''}</summary>{!matches.length ? <p>尚无可用的价格库候选。请填写实际价格，或明确选择演示参数。</p> : matches.map((match, index) => <div key={index}><strong>{match.operation || ({ material: '材料', setup: '准备费', operation: '加工工序' } as Record<string, string>)[match.kind] || match.kind} · {statusNames[match.status] || match.status}</strong>{match.candidates?.map((candidate, i) => <div className="cq-price-candidate" key={i}><span>{String(candidate.rate ?? candidate.value ?? candidate.price ?? '—')} {candidate.unit}<small>{candidate.entry_id} · 版本 {candidate.version}</small></span><button className="cad-button cad-small" onClick={() => adoptPrice(match, candidate)}>采用此价格</button></div>)}</div>)}</details>
            {state?.geometry_evidence?.[active.group_id] && <details className="cq-catalog"><summary>原 CAD 几何依据</summary><p>实体表面积：{state.geometry_evidence[active.group_id].area_mm2 ?? '—'} mm²；实体体积：{state.geometry_evidence[active.group_id].volume_mm3 ?? '—'} mm³。以上是原生几何量，不代表展开毛坯或采购用量。</p></details>}
          </div>}</div>}
          <div className="cq-step-footer"><button className="cad-button" onClick={() => setStage(0)}><ArrowLeft size={14} />报价范围</button><button className="cad-button cad-primary" disabled={!selected.length} onClick={() => setStage(2)}>核算与定价<ArrowRight size={14} /></button></div>
        </section>}
        {stage === 2 && <section className="cq-section"><div className="cq-section-heading"><div><h3>核算与定价</h3><p>材料成本 + 各工序费用 + 一次性费用，再按本单定价方式计算报价。</p></div><span className="cq-muted">{selected.length} 个报价分组 · {inputs.order_sets ?? '待填'} 套</span></div>
          {selected.length < inputs.lines.length && <div className="cq-alert">本报价仅包含所选 {selected.length} / {inputs.lines.length} 个分组，其余未选或待设计分组不计入总价。</div>}
          <WeightSummaryCard summary={estimate ? estimate.weight_summary : state?.weight_summary} dirty={dirty} title={estimate ? '本次报价重量' : '已保存重量汇总'} />
          <div className="cq-input-grid cq-pricing"><label>定价方式<select data-testid="cq-pricing-method" value={inputs.pricing.method || ''} onChange={event => edit(next => { next.pricing.method = (event.target.value || null) as Inputs['pricing']['method'] })}><option value="">请选择</option><option value="cost_plus">成本加成</option><option value="gross_margin">目标毛利率</option></select></label><label>{inputs.pricing.method === 'gross_margin' ? '目标毛利率（%）' : '加成比例（%）'}<input data-testid="cq-pricing-rate" type="number" min="0" max={inputs.pricing.method === 'gross_margin' ? '99.999' : undefined} step="any" value={inputs.pricing.rate_percent ?? ''} placeholder="请输入本单比例" onChange={event => edit(next => { next.pricing.rate_percent = decimal(event.target.value) })} /></label></div>
          <p className="cq-formula">{inputs.pricing.method === 'cost_plus' ? '报价 = 成本 ×（1 + 加成比例）' : inputs.pricing.method === 'gross_margin' ? '报价 = 成本 ÷（1 − 目标毛利率）' : '请明确选择本单定价方式，系统不预设比例。'}</p><div className="cq-checkboxes"><label><input type="checkbox" data-testid="cq-tax" checked={inputs.pricing.tax_excluded} onChange={event => edit(next => { next.pricing.tax_excluded = event.target.checked })} />人民币，未税</label><label><input type="checkbox" data-testid="cq-freight" checked={inputs.pricing.freight_excluded} onChange={event => edit(next => { next.pricing.freight_excluded = event.target.checked })} />不含运费</label></div>
          <details className="cq-shared"><summary>整单附加费用（{inputs.shared_fees.length} 项）</summary><p>只填未包含在零件费率中的整单费用。</p>{inputs.shared_fees.map((fee, index) => <div className="cq-shared-row" key={index}><input aria-label={`附加费用 ${index + 1} 名称`} value={fee.name} placeholder="费用名称" onChange={event => edit(next => { next.shared_fees[index].name = event.target.value })} /><input aria-label={`附加费用 ${index + 1} 金额`} type="number" min="0" step="any" value={fee.value ?? ''} placeholder="金额（元 / 单）" onChange={event => edit(next => { next.shared_fees[index].value = decimal(event.target.value) })} /><input aria-label={`附加费用 ${index + 1} 包含范围`} value={fee.inclusions} placeholder="价格来源与包含范围" onChange={event => edit(next => { next.shared_fees[index].inclusions = event.target.value })} /><button className="cq-icon" aria-label={`删除附加费用 ${index + 1}`} onClick={() => edit(next => { next.shared_fees.splice(index, 1) })}><Trash2 size={15} /></button></div>)}<button className="cad-button cad-small" onClick={() => edit(next => { next.shared_fees.push({ name: '', value: null, inclusions: '' }) })}><Plus size={13} />添加整单费用</button></details>
          <label>报价备注 / 演示假设<textarea data-testid="cq-remarks" value={inputs.remarks} onChange={event => edit(next => { next.remarks = event.target.value })} placeholder="交期、付款条件未约定时保持待约定；演示参数请保留说明" /></label>
          <label className="cq-review-check"><input type="checkbox" data-testid="cq-review-confirmed" checked={inputs.review_confirmed} onChange={event => edit(next => { next.review_confirmed = event.target.checked }, true)} />我已核对本次报价的零件范围、用料、费率及费用包含项。报价生成后仍需人工审核。</label>
          <div className="cq-calculation-actions"><button className="cad-button" data-testid="cq-estimate" disabled={!selected.length} onClick={() => void action('estimate')}><Calculator size={15} />计算成本</button><button className="cad-button cad-primary" data-testid="cq-issue" disabled={!selected.length || !inputs.review_confirmed || !inputs.pricing.method || inputs.pricing.rate_percent == null || !inputs.pricing.tax_excluded || !inputs.pricing.freight_excluded} onClick={() => void action('issue')}><FileText size={15} />生成报价草稿</button></div>
          {showIssues && issues.length > 0 && <div className="cq-validation" data-testid="cq-validation"><h4>还有 {issues.length} 项需要补充或核对</h4><ul>{issues.slice(0, 12).map((issue, index) => <li key={index}>{issue.group_id ? `${names.get(issue.group_id) || issue.group_id}：` : ''}{issue.message}</li>)}</ul>{issues.length > 12 && <p>其余事项可在对应零件中继续补充。</p>}<button className="cad-button cad-small" onClick={() => setStage(1)}>返回用料与价格</button></div>}
          {estimate?.totals && <div className="cq-result" data-testid="cq-result"><div className="cq-amounts"><div><span>成本合计</span><strong>{money(estimate.totals.total_cost)}</strong></div><div className="cq-total"><span>报价总额</span><strong>{money(estimate.totals.total_price)}</strong></div><div><span>报价分组</span><strong>{estimate.lines?.length ?? selected.length} 项</strong></div></div>{!!estimate.lines?.length && <div className="cq-table-scroll"><table className="cq-table"><thead><tr><th>零件</th><th>数量</th><th>单件净重</th><th>本单净重</th><th>单件成本</th><th>报价单价</th><th>报价金额</th></tr></thead><tbody>{estimate.lines.map((line, index) => <tr key={line.group_id || index}><td>{line.name || names.get(line.group_id)}</td><td>{line.quantity}</td><td>{weight(line.net_weight_kg)}</td><td>{weight(line.total_net_weight_kg)}</td><td>{money(line.unit_cost)}</td><td>{money(line.unit_price)}</td><td>{money(line.total_price)}</td></tr>)}</tbody></table></div>}<p>人民币 · 未税 · 不含运费 · 模拟报价，待人工审核</p></div>}
        </section>}
      </fieldset>
      {issued && <div className="cq-issued" data-testid="cq-issued"><div><Check size={20} /><div><h3>报价草稿已保存</h3><p>{issued.id} · 已保存本次 CAD 来源、参数和定价快照。</p></div></div><Links quote={issued} base={base} /></div>}
      {!!state?.quotes.length && <details className="cq-history"><summary>历史报价（{state.quotes.length}）</summary>{state.quotes.map(quote => <div key={quote.id}><div><strong>{quote.id}</strong><small>{quote.created_at ? new Date(quote.created_at).toLocaleString('zh-CN') : ''}{quote.source_current === false ? ' · 来源已更新，保留历史快照' : ''}</small></div><Links quote={quote} base={base} /></div>)}</details>}
      <footer className="cq-footer">采购用料、费率与制造范围需工程及商务核对。原 STEP、制造分组与历史报价会保留。</footer>
    </>}
  </section>
}
