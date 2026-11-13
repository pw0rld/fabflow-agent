'use client'

import { useState } from 'react'
import { ArrowUpRight, ClipboardList } from 'lucide-react'
import './engineering-review-checklist.css'

export type ReviewStatus = 'recorded' | 'pending_review' | 'missing' | 'conflict' | 'unreadable' | 'not_evaluated'
export type ReviewEvidence = {
  reference?: string; page?: number | null; region?: string; solid_id?: string
  face_ids?: string[]; raw_text?: string; value?: unknown; unit?: string; basis: string
}
export type EngineeringReview = {
  schema_version: 1; status: 'not_started' | 'incomplete' | 'current' | 'stale'
  summary: Partial<Record<ReviewStatus, number>>
  items: { id: string; group_id?: string | null; part_key?: string | null; field: string; label: string
    status: ReviewStatus; message: string; evidence: ReviewEvidence[]; evidence_count?: number }[]
}
const labels: Record<ReviewStatus, string> = {
  recorded: '已记录 · 待核对', pending_review: '待核对', missing: '未提取到', conflict: '存在差异',
  unreadable: '尚未读清', not_evaluated: '尚未检查',
}
const basisLabels: Record<string, string> = {
  native_geometry: '原生几何', cad_property: 'CAD 属性', manual_entry: '人工填写',
  assembly_instance: '装配实例', drawing_text: '图纸原文', drawing_explicit: '图纸标注',
  model_observation: '识别原文', model_inference: '模型推断', inquiry: '询价说明',
}
function valueText(evidence: ReviewEvidence) {
  if (evidence.raw_text) return evidence.raw_text
  if (evidence.value == null) return ''
  const text = typeof evidence.value === 'string' || typeof evidence.value === 'number'
    ? String(evidence.value) : JSON.stringify(evidence.value)
  return text + (evidence.unit ? ` ${evidence.unit}` : '')
}
type Props = {
  review?: EngineeringReview; selectedId?: string | null; selectedName?: string
  scopeKey?: 'group_id' | 'part_key'; names?: Record<string, string>; dirty?: boolean; revision?: number
  onSelect?: (id: string) => void; onLocate?: (evidence: ReviewEvidence) => void
  canLocate?: (evidence: ReviewEvidence) => boolean; sourceLabel?: (evidence: ReviewEvidence) => string
  testId?: string; compact?: boolean
}

export default function EngineeringReviewChecklist({ review, selectedId, selectedName, scopeKey = 'group_id', names = {}, dirty = false, revision, onSelect, onLocate, canLocate, sourceLabel, testId = 'engineering-review', compact = false }: Props) {
  const [allIssues, setAllIssues] = useState(false)
  const allItems = review?.items || []
  const rows = allItems.filter(item => allIssues
    ? !['recorded'].includes(item.status)
    : !item[scopeKey] || item[scopeKey] === selectedId || !selectedId)
  const counts = rows.reduce((result, item) => ({ ...result, [item.status]: (result[item.status] || 0) + 1 }), {} as Partial<Record<ReviewStatus, number>>)
  return <section className={`engineering-review ${compact ? 'engineering-review-compact' : ''}`} data-testid={testId}>
    <div className="er-heading"><ClipboardList size={18}/><div><h3>审图清单</h3><p>{allIssues ? '全部零件的待核对问题' : selectedName ? `当前零件：${selectedName}` : '当前资料的读取检查'}</p></div></div>
    <p className="er-scope">根据已保存的读取结果整理。未提取到不等于原图没有；已记录的内容仍需工程核对。</p>
    {revision != null && <p className="er-revision">保存版本 {revision}</p>}
    {dirty && <p className="er-note" data-testid={`${testId}-dirty`}>当前有未保存修改，清单仍对应保存版本；保存后更新。</p>}
    {review?.status === 'stale' && <p className="er-note" data-testid={`${testId}-stale`}>附件已有更新，本清单未覆盖最新资料，请重新识别。</p>}
    {review?.status === 'incomplete' && <p className="er-note" data-testid={`${testId}-incomplete`}>本轮读取尚未完成，清单可能不全。</p>}
    {!review || review.status === 'not_started' ? <p className="er-empty" data-testid={`${testId}-empty`}>尚未形成审图清单，完成读取后查看。</p> : <>
      {selectedId && <div className="er-filters" aria-label="审图范围"><button type="button" aria-pressed={!allIssues} data-testid={`${testId}-current`} onClick={() => setAllIssues(false)}>当前零件</button><button type="button" aria-pressed={allIssues} data-testid={`${testId}-all`} onClick={() => setAllIssues(true)}>全部问题</button></div>}
      <div className="er-counts" data-testid={`${testId}-counts`}>{(Object.keys(labels) as ReviewStatus[]).filter(status => counts[status]).map(status => <span className={`er-count er-${status}`} key={status}>{labels[status]} {counts[status]}</span>)}</div>
      <div className="er-items">{rows.length ? rows.map(item => <article className={`er-item er-${item.status}`} key={item.id} data-testid={`${testId}-item-${item.id}`} data-review-field={item.field} data-review-status={item.status} data-review-group={item[scopeKey] || ''}>
        <div className="er-item-heading"><strong>{item.label}</strong><span>{labels[item.status]}</span></div>
        {allIssues && item[scopeKey] && <button type="button" className="er-group-link" disabled={!onSelect || !names[item[scopeKey]!]} onClick={() => onSelect?.(item[scopeKey]!)} data-testid={`${testId}-locate-${item.id}`}>{names[item[scopeKey]!] || item[scopeKey]}{onSelect && <ArrowUpRight size={12}/>}</button>}
        {scopeKey === 'part_key' && selectedId && !item.part_key && <small className="er-unassigned">整份资料 / 尚未归属</small>}
        <p className="er-message">{item.message}</p>
        {item.evidence?.length > 0 && <details className="er-evidence"><summary>查看依据 · {item.evidence_count && item.evidence_count > item.evidence.length ? `展示 ${item.evidence.length} / ${item.evidence_count} 条` : `${item.evidence.length} 条`}</summary>{item.evidence_count && item.evidence_count > item.evidence.length ? <p>更多原文保留在原始标注中。</p> : null}{item.evidence.map((evidence, index) => {
          const canOpen = Boolean(onLocate && canLocate?.(evidence)), text = valueText(evidence)
          return <div key={index} className="er-evidence-row"><span>{basisLabels[evidence.basis] || '读取依据'}{evidence.solid_id ? ` · ${evidence.solid_id}` : ''}</span>{text && <p>{text}</p>}{!!evidence.face_ids?.length && <small>原生面：{evidence.face_ids.join(' / ')}</small>}{evidence.reference && (canOpen ? <button type="button" data-testid={`${testId}-source-${item.id}-${index}`} onClick={() => onLocate?.(evidence)}>{sourceLabel?.(evidence) || '定位图纸来源'}<ArrowUpRight size={12}/></button> : <small>{sourceLabel?.(evidence) || '原文位置'}{evidence.region ? ` · ${evidence.region}` : ''}</small>)}</div>
        })}</details>}
      </article>) : <p className="er-empty">当前范围暂无已生成项目，仍需结合原图检查。</p>}</div>
    </>}
    <p className="er-footnote">清单用于整理读取结果与待确认项，不代表工程批准或生产放行。</p>
  </section>
}
