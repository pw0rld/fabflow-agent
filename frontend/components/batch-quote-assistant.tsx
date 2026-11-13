'use client'

import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ArrowDown, ArrowRight, Check, ExternalLink, LoaderCircle, MessageSquareText, RefreshCw, Send, X } from 'lucide-react'
import { quotationCount, quotationNumber } from '@/lib/batch-display-format'
import { PUBLIC_QUOTE_API, POLAND_QUOTE_API, type QuoteApiBase } from '@/lib/batch-quote-scope'
import './batch-quote-assistant.css'

type Scalar = string | number | boolean | null
type Totals = { total_price?: string | null; total_cost?: string | null; unpriced_lines?: number }
type Change = { label: string; before: Scalar; after: Scalar; field?: string; method?: string; line_ids?: string[] }
type Proposal = { id: string; status: 'queued' | 'running' | 'ready' | 'needs_clarification' | 'failed';
  message: string; source_run_id: string; expected_revision: number; changes: Change[];
  before_totals?: Totals | null; after_totals?: Totals | null; warnings?: string[] }
type Reply = { proposal: Proposal }
type Turn = { id: string; input: string; submitted: string; sourceRun: string; proposal?: Proposal; error?: string; savedRun?: string }
export type QuoteAssistantLine = { id: string; part_name: string; mode: string; engineering_pricing_methods?: string[] | null }
type Props = { batchId: string; runId: string; apiBase?: QuoteApiBase; open: boolean; disabled?: boolean; orderSets?: number;
  totalPrice?: string | null; lines: QuoteAssistantLine[]; onClose(): void; onApplied(batch: unknown): void }

const MAX_MESSAGE = 4000
const pending = (proposal?: Proposal) => proposal?.status === 'queued' || proposal?.status === 'running'
const money = (value?: string | null) => value == null ? '未计价' : `¥${quotationNumber(value)}`
const methodLabels: Record<string, string> = { cost_plus: '成本加成', gross_margin: '目标毛利率',
  carbon_steel: '碳钢（Q235类）', spcc: '冷轧钢（SPCC类）', ss304: '304不锈钢', ss316: '316类不锈钢', al5052: '5052铝合金' }
function changedValue(value: Scalar, field?: string) {
  if (value == null || value === '') return '未提供'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (field === 'order_sets') return `${quotationCount(value)} 套`
  const text = String(value)
  if (methodLabels[text]) return methodLabels[text]
  if (/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(text)) return quotationNumber(text) + (field === 'rate_percent' ? '%' : '')
  return text
}
class AssistantFailure extends Error {
  constructor(message: string, public status: number) { super(message) }
}
async function ask<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers); headers.set('X-Workbench-Request', '1')
  const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', redirect: 'error', ...init, headers })
  const data = await response.json().catch(() => { throw new Error('暂未收到有效响应，输入已保留，可以重试。') })
  if (!response.ok) throw new AssistantFailure(typeof data?.error === 'string' ? data.error : data?.error?.message || `请求尚未完成（${response.status}）`, response.status)
  return data as T
}
function validateReply(reply: Reply, sourceRun: string): Proposal {
  const p = reply?.proposal
  if (!p || !/^AP-[a-f0-9]{32}$/.test(p.id) || !/^RUN-[a-f0-9]{32}$/.test(sourceRun) || p.source_run_id !== sourceRun || !Number.isSafeInteger(p.expected_revision)
      || p.expected_revision < 0 || !['queued', 'running', 'ready', 'needs_clarification', 'failed'].includes(p.status)
      || typeof p.message !== 'string' || !Array.isArray(p.changes)
      || p.changes.some(change => !change || typeof change.label !== 'string'
        || ![change.before, change.after].every(value => value === null || ['string', 'number', 'boolean'].includes(typeof value))
        || (change.line_ids != null && (!Array.isArray(change.line_ids) || change.line_ids.some(id => typeof id !== 'string' || !/^L[0-9]{4,8}$/.test(id)))))
      || (p.warnings != null && (!Array.isArray(p.warnings) || p.warnings.some(warning => typeof warning !== 'string')))) throw new Error('修改预览与当前报价不一致，请重新预览。')
  return p
}
function restoreTurns(value: unknown): Turn[] {
  if (!Array.isArray(value)) return []
  return value.slice(-20).flatMap(item => {
    if (!item || typeof item !== 'object') return []
    const turn = item as Partial<Turn>
    if (typeof turn.id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(turn.id)
        || typeof turn.input !== 'string' || !turn.input.trim() || turn.input.length > MAX_MESSAGE
        || typeof turn.submitted !== 'string' || turn.submitted.length > MAX_MESSAGE
        || typeof turn.sourceRun !== 'string' || !/^RUN-[a-f0-9]{32}$/.test(turn.sourceRun)) return []
    try {
      const proposal = turn.proposal ? validateReply({ proposal: turn.proposal }, turn.sourceRun) : undefined
      return [{ id: turn.id, input: turn.input, submitted: turn.submitted, sourceRun: turn.sourceRun, proposal,
        error: typeof turn.error === 'string' ? turn.error.slice(0, MAX_MESSAGE) : undefined,
        savedRun: typeof turn.savedRun === 'string' && /^RUN-[a-f0-9]{32}$/.test(turn.savedRun) ? turn.savedRun : undefined }]
    } catch { return [] }
  })
}

export default function BatchQuoteAssistant({ batchId, runId, apiBase = PUBLIC_QUOTE_API, open, disabled, orderSets, totalPrice, lines, onClose, onApplied }: Props) {
  const [draft, setDraft] = useState(''), [turns, setTurns] = useState<Turn[]>([])
  const [sending, setSending] = useState(false), [saving, setSaving] = useState(false)
  const [error, setError] = useState(''), [pollError, setPollError] = useState(''), [pollRetry, setPollRetry] = useState(0)
  const [followup, setFollowup] = useState(true), [smallScreen, setSmallScreen] = useState(false)
  const [sessionReady, setSessionReady] = useState(false)
  const dialog = useRef<HTMLDialogElement>(null), input = useRef<HTMLTextAreaElement>(null), bottom = useRef<HTMLDivElement>(null)
  const mounted = useRef(true), requests = useRef(new Set<AbortController>())
  const previewAttempt = useRef<{ signature: string; eventId: string; turnId: string } | null>(null)
  const commitAttempt = useRef<{ signature: string; eventId: string } | null>(null)
  const pollFailures = useRef(0)
  const prefix = `${apiBase}/${batchId}/runs/${runId}/assistant`
  const sessionKey = apiBase === POLAND_QUOTE_API ? `hyl-quote-assistant-v1:poland:${batchId}` : `hyl-quote-assistant-v1:${batchId}`
  const latest = turns.at(-1), current = latest?.proposal
  const waiting = pending(current), latestIsStale = !!latest && latest.sourceRun !== runId && latest.savedRun !== runId
  const clarification = followup && current?.status === 'needs_clarification' && !latestIsStale && !latest?.savedRun
  const buy = lines.find(line => line.mode === 'buy')
  const hotdip = lines.find(line => line.engineering_pricing_methods?.includes('hotdip'))
  const examples = [{ label: '调整套数和加成', text: '这单改成20套，成本加成15%。' },
    ...(buy ? [{ label: '调整外购单价', text: `将 ${buy.id} 的采购单价改成0.90元/件。` }] : []),
    ...(hotdip ? [{ label: '调整镀锌费用', text: `将 ${hotdip.id} 的热浸锌费率改成3元/kg，本单该工序组最低收费100元。` }] : [])]

  useEffect(() => {
    mounted.current = true
    const media = window.matchMedia('(max-width: 1279px)'), change = () => setSmallScreen(media.matches)
    change(); media.addEventListener('change', change)
    return () => { mounted.current = false; media.removeEventListener('change', change); requests.current.forEach(request => request.abort()) }
  }, [])
  useEffect(() => {
    try {
      const raw = window.sessionStorage.getItem(sessionKey)
      const state = raw && raw.length <= 2_000_000 ? JSON.parse(raw) : null
      if (state?.schema_version === 1) {
        const restored = restoreTurns(state.turns), last = restored.at(-1)
        setTurns(restored)
        if (typeof state.draft === 'string' && state.draft.length <= MAX_MESSAGE) setDraft(state.draft)
        setFollowup(state.followup !== false)
        if (last && state.previewAttempt?.signature === JSON.stringify([last.sourceRun, last.submitted])
            && state.previewAttempt.turnId === last.id && typeof state.previewAttempt.eventId === 'string'
            && /^[A-Za-z0-9_-]{1,128}$/.test(state.previewAttempt.eventId)) previewAttempt.current = state.previewAttempt
        if (last?.proposal && state.commitAttempt?.signature === JSON.stringify([last.proposal.id, last.proposal.expected_revision])
            && typeof state.commitAttempt.eventId === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(state.commitAttempt.eventId)) commitAttempt.current = state.commitAttempt
      }
    } catch { /* A blocked or damaged browser cache does not affect server quotes. */ }
    setSessionReady(true)
  }, [batchId, sessionKey])
  useEffect(() => {
    if (!sessionReady) return
    try {
      window.sessionStorage.setItem(sessionKey, JSON.stringify({ schema_version: 1,
        draft, turns: turns.slice(-20), followup, previewAttempt: previewAttempt.current, commitAttempt: commitAttempt.current }))
    } catch { /* Browser storage may be unavailable; the active conversation still works. */ }
  }, [batchId, sessionKey, sessionReady, draft, turns, followup, sending, saving])
  useEffect(() => {
    if (!open || !dialog.current) return
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null, panel = dialog.current
    if (panel.open) panel.close()
    if (smallScreen) panel.showModal(); else panel.show()
    return () => { if (panel.open) panel.close(); if (previous?.isConnected) previous.focus() }
  }, [open, smallScreen])
  useEffect(() => { if (open) bottom.current?.scrollIntoView({ block: 'nearest', behavior: 'auto' }) }, [open, turns, sending, saving, pollError])
  useEffect(() => { setError(''); setPollError('') }, [runId])
  useEffect(() => {
    if (!latest?.proposal || !pending(latest.proposal)) return
    const p = latest.proposal, turnId = latest.id, sourceRun = latest.sourceRun
    const abort = new AbortController(); requests.current.add(abort)
    const timer = window.setTimeout(() => {
      void ask<Reply>(`${apiBase}/${batchId}/runs/${sourceRun}/assistant/proposals/${p.id}`, { signal: abort.signal }).then(reply => {
        if (!mounted.current || abort.signal.aborted) return
        const next = validateReply(reply, sourceRun)
        pollFailures.current = 0; setPollError('')
        setTurns(old => old.map(turn => turn.id === turnId ? { ...turn, proposal: next, error: undefined } : turn))
      }).catch(failure => {
        if (!mounted.current || abort.signal.aborted) return
        pollFailures.current += 1
        setPollError(failure instanceof Error ? failure.message : '进度更新暂时失败，后台任务保留。')
        if (pollFailures.current < 3) setPollRetry(value => value + 1)
      }).finally(() => requests.current.delete(abort))
    }, 1500)
    return () => { window.clearTimeout(timer); abort.abort(); requests.current.delete(abort) }
  }, [apiBase, batchId, latest?.id, latest?.proposal, pollRetry])

  async function submitText(value: string, originalSubmitted?: string, restart = false) {
    const text = value.trim()
    if (!text || disabled || sending || saving || waiting) return
    const submitted = originalSubmitted || (clarification && latest
      ? `原修改请求：\n${latest.submitted}\n\n用户最新补充：\n${text}` : text)
    if (submitted.length > MAX_MESSAGE) { setError('本次修改和补充超过4000字，请合并精简后重新发送。'); return }
    const signature = JSON.stringify([runId, submitted])
    if (restart || previewAttempt.current?.signature !== signature) previewAttempt.current = { signature, eventId: crypto.randomUUID(), turnId: crypto.randomUUID() }
    const attempt = previewAttempt.current, abort = new AbortController()
    requests.current.add(abort); setSending(true); setError(''); setPollError(''); pollFailures.current = 0
    setTurns(old => old.some(turn => turn.id === attempt.turnId)
      ? old.map(turn => turn.id === attempt.turnId ? { ...turn, error: undefined } : turn)
      : [...old.slice(-19), { id: attempt.turnId, input: text, submitted, sourceRun: runId }])
    try {
      const reply = await ask<Reply>(prefix + '/preview', { method: 'POST', signal: abort.signal,
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ event_id: attempt.eventId, message: submitted }) })
      if (!mounted.current || abort.signal.aborted) return
      const next = validateReply(reply, runId)
      setTurns(old => old.map(turn => turn.id === attempt.turnId ? { ...turn, proposal: next, error: undefined } : turn))
      setDraft(''); setFollowup(true)
    } catch (failure) {
      if (!mounted.current || abort.signal.aborted) return
      const message = failure instanceof Error ? failure.message : '修改请求尚未完成，输入已保留。'
      setDraft(text); setError(message)
      setTurns(old => old.map(turn => turn.id === attempt.turnId ? { ...turn, error: message } : turn))
    } finally { requests.current.delete(abort); if (mounted.current && !abort.signal.aborted) setSending(false) }
  }
  function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); void submitText(draft) }
  async function apply() {
    if (!latest || !current || current.status !== 'ready' || latest.savedRun || latestIsStale || disabled || saving || sending) return
    const signature = JSON.stringify([current.id, current.expected_revision])
    if (commitAttempt.current?.signature !== signature) commitAttempt.current = { signature, eventId: crypto.randomUUID() }
    const abort = new AbortController(); requests.current.add(abort); setSaving(true); setError('')
    try {
      const reply = await ask<{ batch: unknown; run_id: string; source_run_id: string; replayed: boolean }>(prefix + '/commit', {
        method: 'POST', signal: abort.signal, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ event_id: commitAttempt.current.eventId, proposal_id: current.id, expected_revision: current.expected_revision }) })
      if (!mounted.current || abort.signal.aborted) return
      if (!reply.batch || !/^RUN-[a-f0-9]{32}$/.test(reply.run_id) || reply.source_run_id !== current.source_run_id) throw new Error('新报价返回格式有误，请保持预览并重试。')
      setTurns(old => old.map(turn => turn.id === latest.id ? { ...turn, savedRun: reply.run_id } : turn))
      onApplied(reply.batch)
      setFollowup(false); previewAttempt.current = null
    } catch (failure) {
      if (!mounted.current || abort.signal.aborted) return
      setError(failure instanceof AssistantFailure && failure.status === 409
        ? `${failure.message} 本次输入和预览已保留，请重新预览后再采用。`
        : failure instanceof Error ? failure.message : '保存尚未完成，预览已保留，可以重试。')
    } finally { requests.current.delete(abort); if (mounted.current && !abort.signal.aborted) setSaving(false) }
  }
  function repreview() {
    if (!latest || saving || sending || waiting) return
    setFollowup(false); void submitText(latest.input, latest.submitted, true)
  }
  if (!open) return null
  return <dialog id="bqa-panel" ref={dialog} className="bqa-panel" aria-labelledby="bqa-title" aria-describedby="bqa-description" data-testid="bq-assistant-panel"
    onCancel={event => { event.preventDefault(); if (!saving) onClose() }} onKeyDown={event => { if (event.key === 'Escape' && !saving) { event.preventDefault(); onClose() } }}>
    <header className="bqa-header"><div><span className="bqa-mark"><MessageSquareText size={19} /></span><div><h2 id="bqa-title">报价修改助手</h2><p id="bqa-description">先预览变化，采用后另存新报价</p></div></div><button type="button" className="bqa-icon" aria-label="收起报价修改助手" onClick={onClose} disabled={saving} data-testid="bq-assistant-close"><X size={19} /></button></header>
    <div className="bqa-current"><span>当前待审核报价</span><strong data-testid="bq-assistant-current-total">{money(totalPrice)}</strong>{orderSets != null && <small>{quotationCount(orderSets)} 套</small>}</div>
    <div className="bqa-thread" role="log" aria-label="报价修改对话" aria-live="polite" aria-relevant="additions text" data-testid="bq-assistant-thread">
      {!turns.length && <div className="bqa-intro"><strong>想调整哪里，直接告诉我。</strong><p>可以修改套数、加成或毛利率、采购价和已列出的加工费用。指定零件时写行号或完整名称。</p><div className="bqa-examples">{examples.map(example => <button type="button" key={example.label} disabled={!!disabled || saving || sending} onClick={() => { setDraft(example.text); setFollowup(false); input.current?.focus() }} data-testid={`bq-assistant-example-${example.label}`}><span>{example.label}</span><small>{example.text}</small><ArrowRight size={13} /></button>)}</div></div>}
      {turns.map((turn, index) => {
        const p = turn.proposal, isLatest = index === turns.length - 1, stale = turn.sourceRun !== runId && turn.savedRun !== runId
        const ready = p?.status === 'ready'
        return <article className="bqa-turn" key={turn.id} data-testid="bq-assistant-turn"><div className="bqa-user"><span>你</span><p>{turn.input}</p></div>
          <div className="bqa-answer"><div className="bqa-answer-label"><MessageSquareText size={13} /><span>修改助手</span>{p && pending(p) && <LoaderCircle size={13} className="bq-spin" />}</div>
            {!p && !turn.error && <p className="bqa-status" role="status">{isLatest && sending ? '正在提交修改请求…' : '上次请求的结果尚未读到；重新发送可继续读取，输入已保留。'}</p>}
            {p && <><p className={p.status === 'failed' ? 'bqa-failure-text' : undefined} data-testid={isLatest ? 'bq-assistant-response' : undefined}>{p.message || (pending(p) ? '后台正在理解修改内容…' : '请核对本次修改。')}</p>
              {pending(p) && <p className="bqa-status" role="status" data-testid="bq-assistant-pending">后台处理中，收起助手后仍可继续查看。</p>}
              {ready && <section className="bqa-proposal" data-testid={isLatest ? 'bq-assistant-proposal' : undefined}><div className="bqa-proposal-title"><strong>{turn.savedRun ? '已保存为新报价' : '本次修改预览'}</strong><span>{turn.savedRun ? '待审核' : '尚未保存'}</span></div><ul className="bqa-changes">{p.changes.map((change, changeIndex) => <li key={changeIndex}><strong>{change.label}</strong>{!!change.line_ids?.length && <small>{change.line_ids.join('、')}</small>}<div><span>{changedValue(change.before, change.field)}</span><ArrowRight size={12} /><b>{changedValue(change.after, change.field)}</b></div></li>)}</ul><div className="bqa-totals"><div><span>原报价</span><strong>{money(p.before_totals?.total_price)}</strong></div><ArrowRight size={16} /><div><span>预览报价</span><strong data-testid={isLatest ? 'bq-assistant-preview-total' : undefined}>{money(p.after_totals?.total_price)}</strong></div></div>
                {p.after_totals?.unpriced_lines ? <p className="bqa-scope-note">仍有 {quotationCount(p.after_totals.unpriced_lines)} 行未计价，未作为零成本纳入。</p> : null}
                {!!p.warnings?.length && <details className="bqa-warnings"><summary>查看核价范围与价格说明</summary><ul>{p.warnings.map((warning, warningIndex) => <li key={warningIndex}>{warning}</li>)}</ul></details>}
                {turn.savedRun ? <div className="bqa-saved" data-testid="bq-assistant-saved"><Check size={15} /><span>新报价已保存，仍待工程与商务审核。</span></div> : isLatest && !stale ? <button type="button" className="bq-button primary bqa-apply" disabled={!!disabled || saving || sending || !p.changes.length} onClick={() => void apply()} data-testid="bq-assistant-apply">{saving ? <LoaderCircle size={14} className="bq-spin" /> : <Check size={14} />}{saving ? '正在另存新报价…' : '采用修改并保存新版本'}</button> : <p className="bqa-scope-note">已有新的修改或报价版本，本预览仅供回看。</p>}
                <a className="bqa-prior" href={`${apiBase}/${batchId}/runs/${p.source_run_id}/quote.html`} target="_blank" rel="noopener noreferrer" data-testid={isLatest ? 'bq-assistant-prior-quote' : undefined}><ExternalLink size={12} />查看修改前报价</a>
              </section>}
              {isLatest && p.status === 'needs_clarification' && <p className="bqa-clarify-note">请在下方补充，确认范围后再生成金额预览。</p>}
              {isLatest && p.status === 'failed' && <button type="button" className="bqa-text-button" disabled={sending || saving || !!disabled} onClick={repreview} data-testid="bq-assistant-retry"><RefreshCw size={12} />重新分析这条修改</button>}
            </>}
            {turn.error && <p className="bqa-failure-text">请求未完成，输入已保留。</p>}
          </div></article>
      })}
      {latestIsStale && <div className="bqa-warning" role="status">报价版本已更新，本次输入保留，请重新预览后采用。<button type="button" disabled={sending || saving || waiting || !!disabled} onClick={repreview} data-testid="bq-assistant-repreview">重新预览</button></div>}
      {pollError && <div className="bqa-warning" role="status" data-testid="bq-assistant-poll-error">{pollError}<button type="button" onClick={() => { pollFailures.current = 0; setPollError(''); setPollRetry(value => value + 1) }} data-testid="bq-assistant-poll-retry">继续读取进度</button></div>}
      <div ref={bottom} className="bqa-bottom" />
    </div>
    <form className="bqa-composer" onSubmit={submit}>{error && <p className="bqa-error" role="alert" data-testid="bq-assistant-error">{error}{latest?.proposal?.status === 'ready' && !latest.savedRun && <button type="button" onClick={repreview} disabled={saving || sending || waiting || !!disabled}>重新预览</button>}</p>}
      {clarification && <div className="bqa-reply-context"><span>正在补充上一条修改</span><button type="button" onClick={() => { setFollowup(false); previewAttempt.current = null; input.current?.focus() }} data-testid="bq-assistant-clear-context">改为新请求</button></div>}
      {!!disabled && <p className="bqa-scope-note">订单正在处理或调整条件，完成后可继续修改。</p>}
      <label className="bqa-input-label" htmlFor="bqa-input">{clarification ? '补充修改条件' : '输入报价修改'}</label><textarea id="bqa-input" ref={input} rows={3} maxLength={MAX_MESSAGE} value={draft} onChange={event => setDraft(event.target.value)} placeholder={clarification ? '例如：全部同型号来源，采购价按未税采用。' : '例如：这单改成20套，成本加成15%。'} disabled={!!disabled || sending || saving || waiting} data-testid="bq-assistant-input" onKeyDown={event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); void submitText(draft) } }} />
      <div className="bqa-composer-footer"><small>采用价按本单指定条件保存，模拟报价待审核。</small><button type="submit" className="bq-button primary" disabled={!draft.trim() || !!disabled || sending || saving || waiting} data-testid="bq-assistant-send">{sending || waiting ? <LoaderCircle size={14} className="bq-spin" /> : <Send size={14} />}{sending || waiting ? '处理中' : '预览修改'}</button></div>
      <span className="bqa-shortcut"><ArrowDown size={10} /> Ctrl / ⌘ + Enter 发送</span>
    </form>
  </dialog>
}
