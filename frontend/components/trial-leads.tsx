'use client'

import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { ArrowLeft, ArrowRight, Building2, Check, ChevronRight, ClipboardList, Clock3, Filter, LoaderCircle, LockKeyhole, MessageSquareText, RefreshCw, Save, ShieldCheck, UsersRound } from 'lucide-react'
import './trial-leads.css'

const API = '/ortho2cad-test/customer-upload-admin/api/trial-leads'
const STATUSES = { new: '待联系', contacted: '已联系', trialing: '试用中', proposal: '方案／报价中', won: '已成交', do_not_contact: '暂不联系' } as const
type LeadStatus = keyof typeof STATUSES
const TYPES: Record<string, string> = { sheet_metal: '钣金加工', machining: '机加工', other: '其他加工' }
const INTERESTS: Record<string, string> = { factory_trial: '申请工厂试用', consultation: '预约顾问沟通' }
const TRIAL_STAGES: Record<string, string> = { uploaded: '资料已上传', queued: '等待解析', running: '正在解析', completed: '解析已完成', completed_with_issues: '部分待核对', failed: '本轮未完成', interrupted: '体验已中断' }
type OrderBinding = { batch_id?: string; run_id?: string; name?: string; order_name?: string; status?: string; part_count?: number }
type Trial = OrderBinding & { id: string; created_at: string; updated_at: string; order_binding?: OrderBinding }
type Application = { id: string; interest: string; note?: string; contact_consent: boolean; created_at: string; order_binding?: OrderBinding | null }
type History = { id?: string | number; action?: string; event?: string; created_at?: string; at?: string; note?: string; actor?: string; status?: string; details?: { note?: string; status?: string; assignee?: string } }
type Lead = { id: string; revision: number; company: string; contact_name?: string; manufacturing_type: string;
  contact_method: 'phone' | 'wechat'; contact_masked: string; contact_value?: string | null; contact_consent: boolean;
  verification_status: 'unverified'; status: LeadStatus; assignee?: string; note?: string;
  created_at: string; updated_at: string; application_count?: number; latest_application?: Application | null;
  applications?: Application[]; audit?: History[]; can_contact?: boolean;
  trial_count?: number; latest_trial?: Trial | null; trials?: Trial[] }
type List = { leads: Lead[]; total: number; limit?: number; offset?: number }
type Draft = { status: LeadStatus; assignee: string; note: string }
type SaveAttempt = { leadId: string; payload: Draft & { event_id: string; expected_revision: number } }
class LeadsFailure extends Error { constructor(message: string, readonly status: number) { super(message) } }

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers); headers.set('X-Workbench-Request', '1')
  let response: Response
  try { response = await fetch(url, { ...init, headers, credentials: 'same-origin', cache: 'no-store', redirect: 'error' }) }
  catch { throw new LeadsFailure('连接未完成，当前输入已保留，请恢复本次保存或重新读取。', 0) }
  if (response.status === 401) throw new LeadsFailure('此页需要公司内部账号，请使用已授权的内部入口打开。', 401)
  const data = await response.json().catch(() => { throw new LeadsFailure('连接未完成，当前输入已保留，请重试。', 0) })
  if (!response.ok) throw new LeadsFailure(typeof data?.error === 'string' ? data.error : data?.error?.message || '暂时无法读取线索，请重试。', response.status)
  return data as T
}
function checkedLead(value: Lead, id?: string): Lead {
  if (!value || !/^LEAD-[a-f0-9]{32}$/.test(value.id) || (id && value.id !== id)
      || !Number.isSafeInteger(value.revision) || value.revision < 0 || typeof value.company !== 'string'
      || typeof value.contact_masked !== 'string' || typeof value.contact_consent !== 'boolean'
      || value.verification_status !== 'unverified' || !Object.prototype.hasOwnProperty.call(STATUSES, value.status)) throw new Error('线索资料尚未完整载入，请刷新列表。')
  return value
}
const date = (value?: string) => {
  if (!value || !Number.isFinite(new Date(value).getTime())) return '—'
  return new Date(value).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
const shortId = (value: string) => value.slice(0, 13).toUpperCase()
const canContact = (value: Lead) => value.can_contact !== false && value.contact_consent && value.status !== 'do_not_contact'
const initialDraft = (value: Lead): Draft => ({ status: value.status, assignee: value.assignee || '', note: value.note || '' })
const trialStage = (value?: string) => value && TRIAL_STAGES[value] || '阶段待更新'

function LatestTrial({ lead }: { lead: Lead }) {
  if (!lead.latest_trial) return <span className="ftl-row-trial ftl-no-trial">暂无模型体验</span>
  return <span className="ftl-row-trial" data-testid="trial-lead-latest-trial"><span>{trialStage(lead.latest_trial.status)}</span><small>{lead.latest_trial.name || 'STEP 体验'}{typeof lead.trial_count === 'number' ? ` · ${lead.trial_count} 次体验` : ''}</small></span>
}

export default function TrialLeads() {
  const [rows, setRows] = useState<Lead[]>([]), [total, setTotal] = useState(0), [offset, setOffset] = useState(0)
  const [filter, setFilter] = useState(''), [contactOnly, setContactOnly] = useState(false), [tick, setTick] = useState(0)
  const [loading, setLoading] = useState(true), [listError, setListError] = useState(''), [unauthorized, setUnauthorized] = useState(false)
  const [selected, setSelected] = useState<string | null>(null), [detail, setDetail] = useState<Lead | null>(null)
  const [detailLoading, setDetailLoading] = useState(false), [detailError, setDetailError] = useState(''), [draft, setDraft] = useState<Draft>({ status: 'new', assignee: '', note: '' })
  const [saving, setSaving] = useState(false), [saved, setSaved] = useState(false), [uncertain, setUncertain] = useState(false)
  const attempt = useRef<SaveAttempt | null>(null), savingLock = useRef(false), detailPane = useRef<HTMLElement>(null)
  const limit = 50
  const loadDetail = useCallback(async (id: string, signal?: AbortSignal) => {
    setDetailLoading(true); setDetailError(''); setDetail(null); setSaved(false)
    try {
      const value = checkedLead((await request<{ lead: Lead }>(`${API}/${id}`, { signal })).lead, id)
      if (!signal?.aborted) { setDetail(value); setDraft(initialDraft(value)); setUncertain(false); attempt.current = null }
    } catch (failure) { if (!signal?.aborted) setDetailError(failure instanceof Error ? failure.message : '详情暂不可用。') }
    finally { if (!signal?.aborted) setDetailLoading(false) }
  }, [])

  useEffect(() => {
    const abort = new AbortController(); setLoading(true); setListError('')
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
    if (filter) params.set('status', filter)
    if (contactOnly) params.set('contactable', '1')
    void request<List>(`${API}?${params}`, { signal: abort.signal }).then(value => {
      if (!Array.isArray(value.leads) || !Number.isSafeInteger(value.total) || value.total < 0) throw new Error('线索列表暂不可用，请重试。')
      value.leads.forEach(row => checkedLead(row))
      if (!abort.signal.aborted) { setRows(value.leads); setTotal(value.total); setUnauthorized(false) }
    }).catch(failure => {
      if (!abort.signal.aborted) { setListError(failure instanceof Error ? failure.message : '线索列表暂不可用。'); setUnauthorized(failure instanceof LeadsFailure && failure.status === 401) }
    }).finally(() => { if (!abort.signal.aborted) setLoading(false) })
    return () => abort.abort()
  }, [offset, filter, contactOnly, tick])

  useEffect(() => {
    if (!selected) { setDetail(null); return }
    const abort = new AbortController(); void loadDetail(selected, abort.signal)
    return () => abort.abort()
  }, [selected, loadDetail])

  function choose(row: Lead) {
    if (saving || uncertain) return
    setSelected(row.id); setDetailError(''); setSaved(false)
    if (window.matchMedia('(max-width:740px)').matches) requestAnimationFrame(() => detailPane.current?.scrollIntoView({ block: 'start', behavior: window.matchMedia('(prefers-reduced-motion:reduce)').matches ? 'auto' : 'smooth' }))
  }
  function changeFilter(value: string) { if (saving || uncertain) return; setFilter(value); setOffset(0) }
  async function save(event?: FormEvent, recover = false) {
    event?.preventDefault()
    if (!detail || savingLock.current) return
    const previous = attempt.current
    const next: SaveAttempt = recover && previous?.leadId === detail.id ? previous : { leadId: detail.id,
      payload: { ...draft, assignee: draft.assignee.trim(), note: draft.note.trim(), expected_revision: detail.revision, event_id: 'followup-' + crypto.randomUUID() } }
    if (uncertain && !recover) return
    attempt.current = next; savingLock.current = true; setSaving(true); setSaved(false); setDetailError('')
    try {
      const reply = await request<{ lead: Lead }>(`${API}/${next.leadId}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(next.payload) })
      const value = checkedLead(reply.lead, next.leadId)
      setDetail(value); setDraft(initialDraft(value)); setRows(previousRows => previousRows.map(row => row.id === value.id ? value : row)); setSaved(true); setUncertain(false); attempt.current = null; setTick(value => value + 1)
    } catch (failure) {
      setDetailError(failure instanceof Error ? failure.message : '保存结果未确认，输入已保留。')
      setUncertain(!(failure instanceof LeadsFailure) || failure.status >= 500 || failure.status === 0)
    } finally { savingLock.current = false; setSaving(false) }
  }
  const pageContactable = rows.filter(canContact).length
  const applications = detail?.applications || (detail?.latest_application ? [detail.latest_application] : [])
  const trials = detail?.trials || (detail?.latest_trial ? [detail.latest_trial] : [])
  const locked = saving || uncertain
  return <main className="ftl" data-testid="trial-leads-page">
    <header className="ftl-header"><a href="/workbench/leads/" className="ftl-brand"><span><ClipboardList size={22}/></span><strong>FabFlow<small>体验线索</small></strong></a><span className="ftl-private"><LockKeyhole size={14}/>内部工作台</span></header>
    <div className="ftl-shell"><section className="ftl-intro"><div><p>TRIALS & RELATIONSHIPS</p><h1>从一次体验，开始了解客户。</h1><span>企业资料、体验申请与跟进记录集中查看。</span></div><button type="button" className="ftl-secondary" disabled={loading || locked} onClick={() => setTick(value => value + 1)}><RefreshCw size={15}/>刷新线索</button></section>
      <div className="ftl-summary"><div><span className="ftl-stat-icon"><UsersRound size={21}/></span><section><small>{filter || contactOnly ? '筛选结果' : '体验线索'}</small><strong data-testid="trial-leads-total">{loading ? '—' : total}<em>条</em></strong></section></div><div><span className="ftl-stat-icon mint"><MessageSquareText size={21}/></span><section><small>本页允许联系</small><strong>{loading ? '—' : pageContactable}<em>条</em></strong></section></div><div className="ftl-verification"><ShieldCheck size={19}/><span><strong>验证能力待接入</strong><small>当前联系方式为客户自填，均未通过短信验证。</small></span></div></div>
      <section className="ftl-workspace"><div className="ftl-list-pane"><div className="ftl-filters"><label><Filter size={14}/><span className="ftl-sr-only">跟进状态</span><select data-testid="trial-leads-status-filter" value={filter} disabled={locked} onChange={event => changeFilter(event.target.value)}><option value="">全部状态</option>{Object.entries(STATUSES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label className="ftl-contact-filter"><input type="checkbox" data-testid="trial-leads-contactable-filter" checked={contactOnly} disabled={locked} onChange={event => { setContactOnly(event.target.checked); setOffset(0) }}/>只看允许联系</label></div>
        {loading ? <div className="ftl-empty" role="status"><LoaderCircle size={24} className="ftl-spin"/>正在读取线索</div> : listError ? <div className="ftl-empty ftl-error" role="alert" data-testid="trial-leads-list-error"><LockKeyhole size={26}/><strong>{unauthorized ? '需要内部账号' : '线索暂未载入'}</strong><p>{listError}</p>{!unauthorized && <button type="button" className="ftl-secondary" onClick={() => setTick(value => value + 1)}>重新读取</button>}</div> : !rows.length ? <div className="ftl-empty"><Building2 size={31}/><strong>{filter || contactOnly ? '暂无符合条件的线索' : '等待第一位体验客户'}</strong><p>客户保存企业资料或提交试用申请后，会显示在这里。</p></div> : <><div className="ftl-list-heading"><span>企业 / 联系方式 / 最近体验</span><span>跟进状态</span></div><div className="ftl-list" role="group" aria-label="体验线索列表" data-testid="trial-leads-list">{rows.map(row => <button type="button" key={row.id} data-testid="trial-lead-row" className={selected === row.id ? 'selected' : ''} disabled={locked} aria-pressed={selected === row.id} onClick={() => choose(row)}><span className="ftl-company-icon"><Building2 size={21}/></span><span className="ftl-row-main"><strong>{row.company}</strong><small>{TYPES[row.manufacturing_type] || '其他加工'}<i/> {row.contact_masked}</small><LatestTrial lead={row}/><em><Clock3 size={11}/>{date(row.updated_at)}{row.latest_application && <b>{INTERESTS[row.latest_application.interest] || '已提交体验意向'}</b>}</em></span><span className="ftl-row-side"><span className={`ftl-status ${row.status}`}>{STATUSES[row.status]}</span><small className={canContact(row) ? 'contactable' : ''}>{canContact(row) ? '允许联系' : '暂不联系'}</small></span><ChevronRight size={15}/></button>)}</div></>}
        <div className="ftl-pagination"><small>{total ? `${offset + 1}—${Math.min(offset + rows.length, total)} / ${total}` : '0 条线索'}</small><div><button type="button" aria-label="上一页线索" disabled={offset === 0 || loading || locked} onClick={() => setOffset(value => Math.max(0, value - limit))}><ArrowLeft size={15}/></button><button type="button" aria-label="下一页线索" disabled={offset + limit >= total || loading || locked} onClick={() => setOffset(value => value + limit)}><ArrowRight size={15}/></button></div></div>
      </div><aside ref={detailPane} className="ftl-detail-pane" aria-label="线索详情" data-testid="trial-lead-detail">
        {!selected ? <div className="ftl-detail-placeholder"><span><ClipboardList size={31}/></span><h2>选择一条体验线索</h2><p>查看客户需求，安排负责人，记录下一次沟通。</p></div> : detailLoading ? <div className="ftl-empty" role="status"><LoaderCircle className="ftl-spin" size={24}/>正在读取详情</div> : !detail ? <div className="ftl-empty ftl-error" role="alert"><p>{detailError || '线索暂不可用。'}</p><button type="button" className="ftl-secondary" onClick={() => void loadDetail(selected)}>重新读取</button></div> : <>
          <div className="ftl-detail-heading"><span>{shortId(detail.id)}</span><span className={`ftl-status ${detail.status}`}>{STATUSES[detail.status]}</span><h2 data-testid="trial-lead-company">{detail.company}</h2><p>{TYPES[detail.manufacturing_type] || '其他加工'}{detail.contact_name ? ` · ${detail.contact_name}` : ''}</p></div>
          <div className={`ftl-contact-permission ${canContact(detail) ? 'allowed' : ''}`} data-testid="trial-lead-consent"><ShieldCheck size={17}/><span><strong>{canContact(detail) ? '客户允许就本次需求联系' : '暂不安排销售联系'}</strong><small>{detail.contact_consent ? detail.status === 'do_not_contact' ? '当前跟进状态为暂不联系。' : '请围绕客户提交的试用需求沟通。' : '客户未授权或已撤回联系意愿。'}</small></span></div>
          <dl className="ftl-facts"><div><dt>{detail.contact_method === 'phone' ? '手机号' : '微信联系方式'}</dt><dd data-testid="trial-lead-contact">{canContact(detail) && detail.contact_value ? detail.contact_value : detail.contact_masked}<small>客户自填 · 未验证</small></dd></div><div><dt>首次留下资料</dt><dd>{date(detail.created_at)}</dd></div></dl>
          <section className="ftl-applications ftl-trials" data-testid="trial-lead-trials"><h3>模型体验<span>{detail.trial_count ?? trials.length}</span></h3>{trials.length ? trials.map(trial => <article key={trial.id || trial.batch_id} data-testid="trial-lead-trial"><div><strong>{trial.name || 'STEP 体验任务'}</strong><span className="ftl-trial-stage">{trialStage(trial.status)}</span></div><p>{typeof trial.part_count === 'number' ? `${trial.part_count} 项零件 · ` : ''}最近更新 {date(trial.updated_at || trial.created_at)}</p><small className="ftl-trial-id">{trial.batch_id}</small>{trial.run_id && <small className="ftl-trial-id">{trial.run_id}</small>}</article>) : <p className="ftl-muted">尚无模型体验记录。</p>}<p className="ftl-muted">体验记录不代表同意销售联系，请以联系意愿为准。</p></section>
          <section className="ftl-applications"><h3>试用申请与需求<span>{applications.length}</span></h3>{applications.length ? applications.map(application => <article key={application.id} data-testid="trial-lead-application"><div><strong>{INTERESTS[application.interest] || '体验意向'}</strong><small>{date(application.created_at)}</small></div>{application.note && <p>{application.note}</p>}{application.order_binding?.batch_id && <div className="ftl-order-binding"><ClipboardList size={14}/><span><strong>{application.order_binding.name || application.order_binding.order_name || '已关联体验任务'}</strong><small>{application.order_binding.batch_id}</small></span></div>}<em>{shortId(application.id)}</em></article>) : <p className="ftl-muted">尚未提交工厂试用或顾问沟通申请。</p>}</section>
          <form className="ftl-followup" onSubmit={event => void save(event)} data-testid="trial-lead-followup-form"><h3>安排下一步</h3><div className="ftl-followup-grid"><label>跟进状态<select data-testid="trial-lead-status" value={draft.status} disabled={locked} onChange={event => { setSaved(false); setDraft(value => ({ ...value, status: event.target.value as LeadStatus })) }}>{Object.entries(STATUSES).map(([value, label]) => <option key={value} value={value} disabled={!detail.contact_consent && ['contacted', 'trialing', 'proposal'].includes(value)}>{label}</option>)}</select></label><label>负责人<input data-testid="trial-lead-assignee" value={draft.assignee} maxLength={80} disabled={locked} placeholder="填写负责同事" onChange={event => { setSaved(false); setDraft(value => ({ ...value, assignee: event.target.value })) }}/></label></div><label>内部跟进备注<textarea data-testid="trial-lead-note" rows={3} maxLength={1000} value={draft.note} disabled={locked} placeholder="例如：客户希望接入现有材料价目表，下次沟通确认工序费率。" onChange={event => { setSaved(false); setDraft(value => ({ ...value, note: event.target.value })) }}/></label>
            {detailError && <div className="ftl-save-error" role="alert" data-testid="trial-lead-save-error">{detailError}</div>}{uncertain && <p className="ftl-muted">保存结果尚未确认，请先恢复本次保存，再继续修改。</p>}<div className="ftl-save-actions">{uncertain ? <button type="button" data-testid="trial-lead-recover" className="ftl-primary" disabled={saving} onClick={() => void save(undefined, true)}>{saving ? <LoaderCircle size={15} className="ftl-spin"/> : <RefreshCw size={15}/>}恢复上次保存</button> : <button type="submit" data-testid="trial-lead-save" className="ftl-primary" disabled={saving}>{saving ? <LoaderCircle size={15} className="ftl-spin"/> : <Save size={15}/>}保存跟进</button>}{saved && <span role="status" data-testid="trial-lead-saved"><Check size={15}/>已保存</span>}</div>
          </form>
          {detailError && !uncertain && <button type="button" className="ftl-secondary ftl-reload-detail" disabled={saving} onClick={() => void loadDetail(detail.id)}>重新读取最新详情</button>}
          {!!detail.audit?.length && <details className="ftl-audit"><summary>查看操作记录<span>{detail.audit.length}</span></summary><ol>{detail.audit.slice(0, 15).map((entry, index) => <li key={entry.id || index}><div><strong>{['staff_updated', 'follow_up_updated'].includes(entry.action || '') ? '跟进已更新' : entry.action === 'interest_submitted' ? '提交体验申请' : entry.action === 'profile_saved' ? '企业资料已保存' : entry.action === 'contact_viewed' ? '查看联系资料' : '线索记录已更新'}</strong><time>{date(entry.created_at || entry.at)}</time></div>{entry.details?.note && <p>{entry.details.note}</p>}</li>)}</ol></details>}
          <p className="ftl-access-note"><LockKeyhole size={12}/>内部备注仅供团队跟进，客户页面不显示。</p>
        </>}
      </aside></section>
    </div>
  </main>
}
