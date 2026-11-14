'use client'

import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { ArrowRight, Building2, Check, CheckCircle2, ChevronDown, ChevronUp, LoaderCircle, MessageSquare, Pencil, RefreshCw, Send, ShieldCheck } from 'lucide-react'
import './trial-contact-profile.css'

const API = '/workbench/customer/api/trial'
const manufacturingLabels = { sheet_metal: '钣金加工', machining: '机加工', other: '其他制造业务' } as const
const interestLabels = { factory_trial: '申请工厂试用', consultation: '预约顾问沟通' } as const
type ManufacturingType = keyof typeof manufacturingLabels
type Interest = keyof typeof interestLabels
export type TrialProfile = {
  id?: string; lead_id?: string; revision: number; company: string; contact_name: string;
  contact_method: 'phone' | 'wechat'; contact_value: string; manufacturing_type: ManufacturingType;
  contact_consent: boolean; verification_status: 'unverified';
}
type ProfileFields = Omit<TrialProfile, 'id' | 'lead_id' | 'revision' | 'verification_status'>
type ProfileRequest = ProfileFields & { event_id: string; expected_revision: number }
export type TrialApplication = {
  id: string; status: string; interest: Interest; created_at: string; contact_consent: boolean;
  can_contact?: boolean; note?: string; batch_id?: string | null; run_id?: string | null;
}
type InterestRequest = { event_id: string; interest: Interest; note: string; contact_consent: boolean; batch_id?: string; run_id?: string }
class LeadRequestError extends Error { constructor(message: string, readonly status: number) { super(message) } }

async function leadRequest<T>(path: string, payload?: unknown, signal?: AbortSignal): Promise<T> {
  let response: Response
  try {
    response = await fetch(API + path, { method: payload === undefined ? 'GET' : 'POST', signal,
      credentials: 'same-origin', cache: 'no-store', redirect: 'error',
      headers: { 'X-Workbench-Request': '1', ...(payload === undefined ? {} : { 'Content-Type': 'application/json' }) },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }) })
  } catch { throw new LeadRequestError('连接未完成，请恢复本次请求。已填写内容会保留。', 0) }
  let value: unknown
  try { value = await response.json() } catch { throw new LeadRequestError('提交结果尚未确认，请恢复本次请求。', 0) }
  if (!response.ok) {
    const error = value as { error?: { message?: string } }
    throw new LeadRequestError(error?.error?.message || '资料暂未保存，请稍后重试。', response.status)
  }
  return value as T
}

function nativeProfile(value: unknown): TrialProfile {
  if (!value || typeof value !== 'object') throw new LeadRequestError('企业资料暂不可读，请重新载入。', 0)
  const p = value as TrialProfile
  if (!Number.isSafeInteger(p.revision) || p.revision < 1 || typeof p.company !== 'string'
      || typeof p.contact_name !== 'string' || !['phone', 'wechat'].includes(p.contact_method)
      || typeof p.contact_value !== 'string' || !Object.hasOwn(manufacturingLabels, p.manufacturing_type)
      || typeof p.contact_consent !== 'boolean' || p.verification_status !== 'unverified')
    throw new LeadRequestError('企业资料暂不可读，请重新载入。', 0)
  return p
}
function nativeApplication(value: unknown): TrialApplication {
  const item = value as TrialApplication | undefined
  if (!item || typeof item.id !== 'string' || !/^[A-Za-z0-9_-]{1,100}$/.test(item.id)
      || typeof item.created_at !== 'string' || !Object.hasOwn(interestLabels, item.interest)
      || typeof item.status !== 'string' || typeof item.contact_consent !== 'boolean'
      || item.can_contact != null && typeof item.can_contact !== 'boolean')
    throw new LeadRequestError('申请结果尚未确认，请恢复本次请求。', 0)
  return item
}
const emptyFields = (): ProfileFields => ({ company: '', contact_name: '', contact_method: 'phone', contact_value: '', manufacturing_type: 'sheet_metal', contact_consent: false })
const fieldsFrom = (profile: TrialProfile | null): ProfileFields => profile ? {
  company: profile.company, contact_name: profile.contact_name, contact_method: profile.contact_method,
  contact_value: profile.contact_value, manufacturing_type: profile.manufacturing_type, contact_consent: profile.contact_consent,
} : emptyFields()
const newEvent = () => 'trial-' + crypto.randomUUID()
const retryable = (error: unknown) => !(error instanceof LeadRequestError) || error.status === 0 || error.status >= 500
const shownContact = (profile: TrialProfile) => profile.contact_method === 'phone'
  ? profile.contact_value.replace(/(\d{3})\d{4}(\d{4})$/, '$1****$2') : profile.contact_value
const applicationDate = (value: string) => new Date(value).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })

export function useTrialLeadProfile(ready: boolean) {
  const [enabled, setEnabled] = useState<boolean | null>(null), [profile, setProfile] = useState<TrialProfile | null>(null)
  const [loading, setLoading] = useState(false), [error, setError] = useState('')
  const mounted = useRef(false), epoch = useRef(0)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const reload = useCallback(async () => {
    if (!ready) return
    const current = ++epoch.current
    setLoading(true); setError('')
    try {
      const value = await leadRequest<{ enabled: boolean; verification_available: boolean; profile: unknown }>('/profile')
      if (typeof value.enabled !== 'boolean' || value.verification_available !== false) throw new LeadRequestError('体验资料配置暂不可读。', 0)
      const p = value.profile == null ? null : nativeProfile(value.profile)
      if (mounted.current && current === epoch.current) { setEnabled(value.enabled); setProfile(p) }
    } catch { if (mounted.current && current === epoch.current) { setError('企业资料暂未载入，请重试。已有模型和报价仍可查看。'); setEnabled(null) } }
    finally { if (mounted.current && current === epoch.current) setLoading(false) }
  }, [ready])
  useEffect(() => { if (ready) void reload() }, [ready, reload])
  const adopt = useCallback((value: unknown) => setProfile(nativeProfile(value)), [])
  return { enabled, profile, loading, error, reload, adopt }
}

export function TrialContactProfile({ enabled, profile, loading, error, openKey = 0, onReload, onSaved }: {
  enabled: boolean | null; profile: TrialProfile | null; loading: boolean; error: string; openKey?: number;
  onReload(): void; onSaved(profile: TrialProfile): void;
}) {
  const [editing, setEditing] = useState(false), [fields, setFields] = useState<ProfileFields>(() => fieldsFrom(profile))
  const [busy, setBusy] = useState(false), [failure, setFailure] = useState(''), [uncertain, setUncertain] = useState(false)
  const attempt = useRef<ProfileRequest | null>(null), lock = useRef(false), section = useRef<HTMLElement>(null)
  useEffect(() => { if (!editing && !busy && !uncertain) setFields(fieldsFrom(profile)) }, [profile, editing, busy, uncertain])
  useEffect(() => {
    if (openKey < 1) return
    setEditing(true); section.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }, [openKey])
  if (enabled === false) return null
  if (loading && !profile || !enabled && !error) return <section className="tlf tlf-loading" data-testid="trial-contact-profile" aria-live="polite"><LoaderCircle size={18} className="fft-spin"/><span>正在准备体验资料</span></section>
  if (error && !enabled) return <section className="tlf tlf-loading" data-testid="trial-contact-profile" role="status"><span>{error}</span><button type="button" onClick={onReload} data-testid="trial-profile-retry"><RefreshCw size={15}/>重新载入</button></section>
  const expanded = !profile || editing
  function change<K extends keyof ProfileFields>(key: K, value: ProfileFields[K]) {
    setFields(previous => ({ ...previous, [key]: value })); setFailure(''); attempt.current = null
  }
  async function save(event?: FormEvent, recovery = false) {
    event?.preventDefault()
    if (lock.current) return
    const p = { ...fields, company: fields.company.trim(), contact_name: fields.contact_name.trim(), contact_value: fields.contact_value.trim() }
    if (!recovery && (!p.company || !p.contact_value)) { setFailure('请填写公司名称和联系方式。'); return }
    if (!recovery && p.contact_method === 'phone' && !/^(?:\+?86)?1[3-9][0-9]{9}$/.test(p.contact_value.replace(/[\s-]/g, ''))) { setFailure('请填写有效的中国大陆手机号。'); return }
    if (!recovery && p.contact_method === 'phone') p.contact_value = p.contact_value.replace(/[\s-]/g, '')
    if (!recovery && uncertain) return
    const payload = recovery ? attempt.current : { ...p, event_id: newEvent(), expected_revision: profile?.revision || 0 }
    if (!payload) return
    attempt.current = payload; lock.current = true; setBusy(true); setFailure('')
    try {
      const result = await leadRequest<{ profile: unknown }>('/profile', payload)
      const saved = nativeProfile(result.profile)
      onSaved(saved); setFields(fieldsFrom(saved)); setEditing(false); setUncertain(false); attempt.current = null
    } catch (error) {
      const pending = retryable(error); setUncertain(pending)
      if (!pending) attempt.current = null
      setFailure(error instanceof LeadRequestError ? error.message : '资料尚未保存，请稍后重试。')
      if (error instanceof LeadRequestError && error.status === 409) onReload()
    } finally { lock.current = false; setBusy(false) }
  }
  return <section ref={section} className={`tlf tlf-profile${expanded ? ' expanded' : ''}`} data-testid="trial-contact-profile" data-verification="unverified">
    <div className="tlf-heading"><span className="tlf-icon"><Building2 size={21}/></span><div><span className="tlf-eyebrow">YOUR FACTORY</span><h2>{expanded ? '先介绍一下你的工厂' : '企业资料已保存'}</h2><p>{expanded ? '填写一次，关联本次体验与后续工厂试用。' : `${profile!.company} · ${manufacturingLabels[profile!.manufacturing_type]} · ${shownContact(profile!)}`}</p></div><span className="tlf-unverified">内测体验 · 联系方式未验证</span>{profile && !expanded && <button type="button" className="tlf-link" data-testid="trial-profile-edit" onClick={() => { setFields(fieldsFrom(profile)); setEditing(true); setFailure('') }}><Pencil size={14}/>修改资料</button>}</div>
    {expanded ? <form className="tlf-form" data-testid="trial-profile-form" onSubmit={event => void save(event)}>
      <fieldset disabled={busy || uncertain}>
        <div className="tlf-fields"><label>公司名称<input data-testid="trial-company" autoComplete="organization" maxLength={120} required value={fields.company} placeholder="填写你的公司或工厂名称" onChange={event => change('company', event.target.value)}/></label><label>主要加工业务<select data-testid="trial-manufacturing-type" value={fields.manufacturing_type} onChange={event => change('manufacturing_type', event.target.value as ManufacturingType)}>{Object.entries(manufacturingLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label>联系方式<div className="tlf-contact-input"><select aria-label="联系方式类型" data-testid="trial-contact-method" value={fields.contact_method} onChange={event => { change('contact_method', event.target.value as 'phone' | 'wechat'); change('contact_value', '') }}><option value="phone">手机号</option><option value="wechat">微信号</option></select><input data-testid="trial-contact-value" aria-label="联系方式" type="text" inputMode={fields.contact_method === 'phone' ? 'tel' : 'text'} autoComplete={fields.contact_method === 'phone' ? 'tel' : 'off'} required maxLength={80} value={fields.contact_value} placeholder={fields.contact_method === 'phone' ? '中国大陆手机号' : '填写可联系的微信号'} onChange={event => change('contact_value', event.target.value)}/></div></label></div>
        <details className="tlf-optional"><summary>补充联系人称呼（选填）</summary><label>联系人称呼<input data-testid="trial-contact-name" maxLength={60} autoComplete="name" value={fields.contact_name} placeholder="方便顾问称呼你" onChange={event => change('contact_name', event.target.value)}/></label></details>
        <label className="tlf-consent"><input data-testid="trial-contact-consent" type="checkbox" checked={fields.contact_consent} onChange={event => change('contact_consent', event.target.checked)}/><span>我愿意由顾问通过以上方式联系，了解 AI 拆图与报价系统。<small>可选；不勾选也可继续体验，之后可以修改。</small></span></label>
      </fieldset>
      {failure && <p className="tlf-error" role="alert" data-testid="trial-profile-error">{failure}</p>}
      <div className="tlf-form-actions"><p><ShieldCheck size={14}/>资料用于本次体验与试用沟通，当前尚未进行短信或微信验证。</p>{uncertain ? <button type="button" className="tlf-primary" disabled={busy} data-testid="trial-profile-retry" onClick={() => void save(undefined, true)}>{busy ? <LoaderCircle size={16} className="fft-spin"/> : <RefreshCw size={16}/>}恢复本次保存</button> : <button type="submit" className="tlf-primary" disabled={busy} data-testid="trial-profile-save">{busy ? <LoaderCircle size={16} className="fft-spin"/> : <ArrowRight size={16}/>}保存资料，继续体验</button>}{profile && !uncertain && <button type="button" className="tlf-link" disabled={busy} onClick={() => { setEditing(false); setFailure('') }}>取消</button>}</div>
    </form> : <div className="tlf-saved" data-testid="trial-profile-saved"><CheckCircle2 size={15}/><span>可继续上传模型，已有体验记录会保留。</span><small>{profile?.contact_consent ? '已允许顾问联系' : '暂不安排销售联系'}</small></div>}
  </section>
}

export function TrialInterestPanel({ profile, batchId, runId, openKey = 0, onProfile, onNeedProfile, onOpenChange }: {
  profile: TrialProfile | null; batchId?: string; runId?: string; openKey?: number;
  onProfile(profile: TrialProfile): void; onNeedProfile(): void; onOpenChange?(open: boolean): void;
}) {
  const [open, setOpen] = useState(false), [interest, setInterest] = useState<Interest>('factory_trial'), [note, setNote] = useState('')
  const [consent, setConsent] = useState(false), [busy, setBusy] = useState(false), [uncertain, setUncertain] = useState(false)
  const [failure, setFailure] = useState(''), [application, setApplication] = useState<TrialApplication | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false), [historyLoading, setHistoryLoading] = useState(false), [history, setHistory] = useState<TrialApplication[]>([]), [historyError, setHistoryError] = useState('')
  const attempt = useRef<InterestRequest | null>(null), lock = useRef(false), section = useRef<HTMLElement>(null), submittedScope = useRef<string | null>(null)
  const scope = `${batchId || ''}/${runId || ''}`
  function reveal() {
    if (!profile) { onNeedProfile(); return }
    if (!uncertain) { setConsent(profile.contact_consent); if (submittedScope.current !== scope) setApplication(null) }
    setOpen(true); onOpenChange?.(true)
  }
  useEffect(() => {
    if (openKey < 1) return
    if (profile) { reveal(); section.current?.scrollIntoView({ behavior: 'smooth', block: 'center' }) }
    else onNeedProfile()
    // The navigation explicitly opens this panel; profile changes must not discard draft input.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openKey])
  useEffect(() => {
    setConsent(profile?.contact_consent === true)
    setApplication(previous => previous ? { ...previous, can_contact: profile?.contact_consent === true } : null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profile?.contact_consent])
  useEffect(() => {
    if (busy || uncertain || submittedScope.current === null || submittedScope.current === scope) return
    setApplication(null); setOpen(false); onOpenChange?.(false)
    // A saved application belongs to its submitted model, never a later selection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, busy, uncertain])
  async function loadHistory() {
    if (historyLoading) return
    setHistoryLoading(true); setHistoryError('')
    try {
      const value = await leadRequest<{ applications: unknown[] }>('/applications')
      if (!Array.isArray(value.applications)) throw new Error()
      setHistory(value.applications.map(nativeApplication))
    } catch { setHistoryError('申请记录暂未载入，请重试。') }
    finally { setHistoryLoading(false) }
  }
  async function submit(event?: FormEvent, recovery = false) {
    event?.preventDefault()
    if (!profile) { onNeedProfile(); return }
    if (lock.current || uncertain && !recovery) return
    const payload = recovery ? attempt.current : { event_id: newEvent(), interest, note: note.trim(), contact_consent: consent,
      ...(batchId ? { batch_id: batchId } : {}), ...(runId ? { run_id: runId } : {}) }
    if (!payload) return
    lock.current = true; attempt.current = payload; setBusy(true); setFailure('')
    try {
      const value = await leadRequest<{ application: unknown; profile: unknown }>('/applications', payload)
      const saved = nativeApplication(value.application), p = nativeProfile(value.profile)
      submittedScope.current = `${payload.batch_id || ''}/${payload.run_id || ''}`
      onProfile(p); setApplication(saved); setHistory(previous => [saved, ...previous.filter(item => item.id !== saved.id)]); setUncertain(false); attempt.current = null
    } catch (error) {
      const pending = retryable(error); setUncertain(pending)
      if (!pending) attempt.current = null
      setFailure(error instanceof LeadRequestError ? error.message : '申请暂未完成，请稍后重试。')
    } finally { lock.current = false; setBusy(false) }
  }
  return <section ref={section} className="tlf tlf-interest" data-testid="trial-interest-panel">
    <div className="tlf-interest-heading"><span className="tlf-icon"><MessageSquare size={23}/></span><div><span className="tlf-eyebrow">BRING IT TO YOUR FACTORY</span><h2>让这套流程，接上你的工厂。</h2><p>申请软件试用，了解如何接入本厂材料、设备与计价规则。</p></div><button type="button" className="tlf-primary" data-testid="trial-interest-open" onClick={reveal}>申请工厂试用<ArrowRight size={16}/></button></div>
    {open && <div className="tlf-interest-body">
      {application ? <div className="tlf-application-saved" data-testid="trial-interest-success" role="status"><span><CheckCircle2 size={24}/></span><div><h3>试用申请已保存</h3><p>{application.can_contact === true ? '已进入待联系列表，顾问可根据你的体验需求跟进。' : '当前仅保存申请，暂不安排销售联系。'}</p><small>申请编号：{application.id}</small>{batchId && <small>已关联申请时的模型与体验结果，原始报价保持不变。</small>}</div></div> : <form onSubmit={event => void submit(event)}>
        <fieldset disabled={busy || uncertain}><div className="tlf-request-fields"><label>想进一步了解<select data-testid="trial-interest-kind" value={interest} onChange={event => { setInterest(event.target.value as Interest); setFailure('') }}>{Object.entries(interestLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label><label>你的需求（选填）<textarea data-testid="trial-interest-note" rows={3} maxLength={500} value={note} placeholder="例如：每月报价较多，希望接入工厂的工序费率。" onChange={event => setNote(event.target.value)}/></label></div><label className="tlf-consent"><input data-testid="trial-interest-consent" type="checkbox" checked={consent} onChange={event => setConsent(event.target.checked)}/><span>我愿意让顾问通过已保存的联系方式，就本次软件试用联系我。<small>不勾选时仅保存申请；可以随时在企业资料中修改联系意愿。</small></span></label></fieldset>
        {failure && <p className="tlf-error" role="alert">{failure}</p>}
        <div className="tlf-form-actions"><p>{uncertain ? '恢复时沿用首次提交的资料，不会重新解析或修改报价。' : batchId ? '关联当前模型和体验结果，帮助顾问了解实际需求。' : '先保存试用意向，之后也可以继续上传模型体验。'}</p>{uncertain ? <button type="button" className="tlf-primary" data-testid="trial-interest-retry" disabled={busy} onClick={() => void submit(undefined, true)}>{busy ? <LoaderCircle size={16} className="fft-spin"/> : <RefreshCw size={16}/>}恢复本次申请</button> : <button type="submit" className="tlf-primary" data-testid="trial-interest-submit" disabled={busy}>{busy ? <LoaderCircle size={16} className="fft-spin"/> : <Send size={16}/>}提交试用申请</button>}<button type="button" className="tlf-link" disabled={busy || uncertain} onClick={() => { setOpen(false); onOpenChange?.(false) }}>收起</button></div>
      </form>}
    </div>}
    <button type="button" className="tlf-applications-toggle" data-testid="trial-applications-toggle" aria-expanded={historyOpen} onClick={() => { const next = !historyOpen; setHistoryOpen(next); if (next) void loadHistory() }}>查看已提交的试用申请{historyOpen ? <ChevronUp size={15}/> : <ChevronDown size={15}/>}</button>
    {historyOpen && <div className="tlf-application-history" data-testid="trial-application-history">{historyLoading ? <p><LoaderCircle size={16} className="fft-spin"/>正在载入申请记录</p> : historyError ? <p>{historyError}<button type="button" onClick={() => void loadHistory()}>重试</button></p> : !history.length ? <p>还没有提交试用申请。</p> : history.map(item => <article key={item.id}><span><Check size={15}/>{interestLabels[item.interest]}</span><div><strong>{item.id}</strong><small>{applicationDate(item.created_at)} · {item.can_contact && profile?.contact_consent ? '允许顾问联系' : '仅保存，暂不联系'}</small></div><em>已提交</em></article>)}</div>}
  </section>
}
