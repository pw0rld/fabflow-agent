'use client'

import { useEffect, useState } from 'react'
import { ArrowLeft, ChevronRight, FileImage, FileText, Layers3, LoaderCircle, RotateCcw, Square, TriangleAlert } from 'lucide-react'
import { attachmentUrl, isRecognitionRunning, readingStatusLabel, safeSourceUrl, validDrawingView, type DrawingCandidate, type DrawingObservation, type Evidence, type RecognitionTask, type RecognizedPart } from '@/lib/recognition-api'
import SourcePreview from '@/components/source-preview'
import EngineeringReviewChecklist, { type ReviewEvidence } from '@/components/engineering-review-checklist'
import './recognition-workspace.css'

export type RecognitionWorkspaceProps = {
  task: RecognitionTask; selectedKey: string | null; onSelectedKeyChange: (key: string) => void
  onContinue: () => void; onBack: () => void; onRetry: () => void; onStop: () => void
  busy?: boolean; error?: string
}
const statusLabels = { observed: '原文识别', unit_unknown: '单位待核对', conflict: '标注有差异', unreadable: '尚未读清' }
function dimension(observation: DrawingObservation) { return /dimension|overall_size|尺寸|孔径|直径|外形|length|width|height/i.test(`${observation.source_name || ''} ${observation.label || ''}`) }
function formatValue(candidate: DrawingCandidate) {
  const value = candidate.value
  const text = typeof value === 'string' || typeof value === 'number' ? String(value) : Array.isArray(value) ? value.map(item => typeof item === 'object' ? JSON.stringify(item) : String(item)).join('、') : value == null ? '未识别到' : JSON.stringify(value)
  return text + (candidate.unit ? ' ' + (candidate.unit === 'piece' ? '件' : candidate.unit) : '')
}
function localDate(value?: string | number | null) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
function sourceName(task: RecognitionTask, evidence: Evidence) { return evidence.reference === 'inquiry' ? '识别说明' : task.attachments.find(file => file.id === evidence.reference)?.name || '来源文件' }

function ObservationRow({ task, observation, index, onLocate, active = false }: { task: RecognitionTask; observation: DrawingObservation; index: number; onLocate: (value: Evidence) => void; active?: boolean }) {
  const hasSource = observation.evidence.reference !== 'inquiry' && task.attachments.some(file => file.id === observation.evidence.reference)
  return <article className={`real-observation ${active ? 'active' : ''}`} data-testid={`observation-row-${index}`}><div className="real-observation-heading"><strong>{observation.label || observation.source_name || observation.name}</strong>{observation.status !== 'observed' && <span className="review-tag">{statusLabels[observation.status] || '待核对'}</span>}</div><button className="real-observation-text" disabled={!hasSource} onClick={() => onLocate(observation.evidence)}>{observation.raw_text}{hasSource && <ChevronRight size={14}/>}</button>{observation.reason && <p className="real-observation-reason">{observation.reason}</p>}{observation.classification_note && <p className="real-observation-reason">{observation.classification_note}{observation.source_label ? ` 原标签：${observation.source_label}` : ""}</p>}<div className="real-observation-source">{hasSource ? <button title={`${sourceName(task, observation.evidence)} · ${observation.evidence.region}`} onClick={() => onLocate(observation.evidence)}><FileText size={12}/>定位来源 · 第 {observation.evidence.page || 1} 页</button> : <span>来源：{sourceName(task, observation.evidence)}</span>}</div></article>
}
function PartThumbnail({ task, part }: { task: RecognitionTask; part: RecognizedPart }) {
  const view = part.visualization?.views?.find(item => validDrawingView(task, item, part))
  const source = view && safeSourceUrl(task, view.image_url, view.reference, view.page)
  return <span className="part-thumb real-part-thumb">{source ? <img src={source} alt=""/> : <Layers3 size={23}/>}</span>
}

export default function RecognitionWorkspace({ task, selectedKey, onSelectedKeyChange, onContinue, onBack, onRetry, onStop, busy = false, error = '' }: RecognitionWorkspaceProps) {
  const recognition = task.recognition
  const parts = recognition?.parts || []
  const part = parts.find(item => item.key === selectedKey) || parts[0]
  const bomCount = parts.filter(item => item.bom).length
  const [location, setLocation] = useState<Evidence | null>(null)
  const [activeObservation, setActiveObservation] = useState<DrawingObservation | null>(null)
  const running = isRecognitionRunning(task)
  const allObservations = recognition?.observations || []
  const rows = part?.observations || allObservations
  const messages = [...new Set([...(recognition?.warnings || []), ...(recognition?.questions || [])])]
  const canContinue = task.run?.status === 'completed' && Boolean(parts.length) && !busy && recognition?.source_current !== false
  useEffect(() => {
    const first = part?.evidence.find(item => task.attachments.some(file => file.id === item.reference))
    setLocation(first || (task.attachments[0] ? { reference: task.attachments[0].id, page: 1, region: '' } : null)); setActiveObservation(null)
  }, [task.id, part?.key])
  const locate = (evidence: Evidence, observation?: DrawingObservation) => { setLocation(evidence); setActiveObservation(observation || null) }
  const reviewPosition = (evidence: ReviewEvidence): Evidence | null => evidence.reference && evidence.reference !== 'inquiry' && Number.isInteger(evidence.page) && evidence.page! > 0 && task.attachments.some(file => file.id === evidence.reference)
    ? { reference: evidence.reference, page: evidence.page!, region: evidence.region || '' } : null
  const groups: { title: string; observations: DrawingObservation[] }[] = [
    { title: '材料', observations: rows.filter(item => ['material_family', 'material_grade'].includes(item.name)) },
    { title: '板厚', observations: rows.filter(item => item.name === 'thickness') },
    { title: '尺寸与孔位', observations: rows.filter(dimension) },
    { title: '表面处理', observations: rows.filter(item => item.name === 'surface_treatment') },
    { title: '加工与技术要求', observations: rows.filter(item => item.name === 'process_route' || item.name === 'engineering_open_items' && !dimension(item)) },
  ]
  return <section className="real-recognition" data-testid="real-recognition-workspace"><div className={`real-progress ${running ? 'running' : ''}`} data-testid="recognition-progress" aria-live="polite"><div className="real-progress-heading">{running ? <LoaderCircle className="real-spinner" size={18}/> : task.run?.status === 'failed' ? <TriangleAlert size={18}/> : <FileImage size={18}/>}<div><strong>{readingStatusLabel(task.run?.status)}</strong><p>{recognition?.partial ? '首轮结果已显示，后台仍在继续核对。' : running ? '识别在服务器后台运行，可以离开页面，稍后从识别记录查看。' : '图纸信息为本轮 AI 识别结果，尚未经人工核对。'}</p></div></div><div className="real-progress-actions">{running ? <button className="text-button" data-testid="recognition-stop" disabled={busy || task.run?.status === 'stopping'} onClick={onStop}><Square size={13}/>{task.run?.status === 'stopping' ? '正在停止' : '停止识别'}</button> : <button className="text-button" data-testid="recognition-retry" disabled={busy || !task.attachments.length} onClick={onRetry}><RotateCcw size={14}/>{task.run?.status === 'failed' ? '重试识别' : '重新识别'}</button>}</div>{task.run?.progress?.label && <div className="real-progress-label">{task.run.progress.label}</div>}{task.run?.progress?.events?.length ? <details className="real-progress-history"><summary>查看识别进度记录 · {task.run.progress.events.length} 项</summary><ol>{task.run.progress.events.map((item, index) => <li key={index}>{item.label}<time>{localDate(item.at)}</time></li>)}</ol></details> : null}</div>
    {(error || task.run?.error) && <div className="real-error" role="alert"><TriangleAlert size={17}/><div><strong>操作未完成</strong><p>{error || task.run?.error}</p></div></div>}
    {recognition?.run_id && recognition.source_current === false && <div className="real-warning">附件已有更新，当前结果尚未覆盖全部资料，可重新识别。</div>}
    <div className="work-area real-work-area"><aside className="parts-sidebar"><div className="sidebar-head"><div><span className="section-kicker">图纸识别与拆件</span><h2>{parts.length ? bomCount ? `${bomCount} 项物料` : `${parts.length} 个识别条目` : running ? '正在整理零件' : '暂无明确分件'}</h2></div><Layers3 size={20}/></div><div className="source-file"><FileText size={17}/><div><strong>{task.attachments.length} 份真实资料</strong><span>{allObservations.length} 条原始标注{recognition?.partial ? ' · 首轮结果' : ''}</span></div></div><div className="part-list" aria-label="识别出的条目">{parts.map((item, index) => <button className={`part-item ${part?.key === item.key ? 'selected' : ''}`} key={item.key} data-testid={`part-button-${item.key}`} aria-pressed={part?.key === item.key} onClick={() => onSelectedKeyChange(item.key)}><PartThumbnail task={task} part={item}/><span className="part-copy"><strong>{item.name}</strong><small>{item.bom ? `BOM 第 ${item.bom.item_no} 项 · ${item.bom.quantity == null ? '数量待核对' : `原表数量 ${item.bom.quantity}`}` : item.part_number || '图号未识别'}{item.version ? ' · ' + item.version : ''}</small></span><span className="part-index">{String(index + 1).padStart(2, '0')}</span></button>)}</div>{!parts.length && <div className="real-parts-empty">{running ? '已读出的零件会陆续显示。' : recognition?.parts_supported ? '未识别出有明确依据的独立零件，可查看原图与原始标注。' : '本轮尚无明确分件信息，原始识别内容仍可查看。'}</div>}<div className="sidebar-bottom">{bomCount > 0 && <p>{parts.length - bomCount > 0 ? `另有 ${parts.length - bomCount} 个图纸条目；` : ""}物料项数不代表制造零件数量</p>}<span className="legend-dot blue"/>真实识别结果 <span className="legend-dot orange"/>待人工核对</div></aside>
      <SourcePreview task={task} part={part} location={location} onLocationChange={evidence => locate(evidence)}/>
      <aside className="info-panel real-info-panel"><div className="info-head"><div><span className="section-kicker">{part ? 'PART DETAILS' : 'DRAWING CONTENT'}</span><h2>{part?.bom ? '物料识别信息' : part ? '零件识别参数' : '图纸识别内容'}</h2><p>仅展示图纸有依据的内容，缺失项不猜测</p></div></div><div className="real-pending-tag">AI 识别 · 待核对{recognition?.partial ? ' · 首轮结果' : ''}</div>
        <EngineeringReviewChecklist key={task.id} review={recognition?.engineering_review} selectedId={part?.key} selectedName={part?.name} scopeKey="part_key" names={Object.fromEntries(parts.map(item => [item.key, item.name]))} onSelect={onSelectedKeyChange} onLocate={evidence => { const position = reviewPosition(evidence); if (position) locate(position) }} canLocate={evidence => Boolean(reviewPosition(evidence))} sourceLabel={evidence => { const position = reviewPosition(evidence); return position ? `${sourceName(task, position)} · 第 ${position.page} 页` : evidence.reference === 'inquiry' ? '询价说明' : '图纸来源' }} testId="recognition-engineering-review" compact/>
        {part && <div className="info-section"><div className="info-title">基础信息</div><div className="real-identity"><span>零件名称</span><strong>{part.name}</strong><span>图号</span><strong>{part.part_number || '未识别到'}</strong><span>版本</span><strong>{part.version || '未识别到'}</strong></div></div>}
        {!part && Boolean(recognition?.candidates.length) && <div className="info-section"><div className="info-title">识别摘要</div>{recognition!.candidates.map(candidate => <button className="real-candidate" key={candidate.name} onClick={() => locate(candidate.evidence)}><span>{candidate.label}</span><strong>{formatValue(candidate)}</strong></button>)}</div>}
        {part?.bom && <div className="info-section real-bom-card" data-testid="part-bom"><div className="info-title">物料表第 {part.bom.item_no} 项</div><div className="real-identity"><span>原表数量</span><strong data-testid="part-bom-quantity">{part.bom.quantity == null ? '存在差异，待核对' : part.bom.quantity}</strong><span>数量口径</span><strong>本行物料用量；订单套数需另外提供</strong><span>料号</span><strong>{part.bom.part_numbers.join(' / ') || '待核对'}</strong></div>{part.bom.warnings.map((warning, index) => <p className="real-bom-warning" key={index}>{warning}</p>)}{part.bom.evidence[0] && <button className="text-button" onClick={() => locate(part.bom!.evidence[0])}>查看原表来源</button>}</div>}
        {groups.map(group => <div className="info-section real-parameter-group" key={group.title}><div className="info-title"><span>{group.title}</span><span className="source-label">{group.observations.length ? '图纸原文' : ''}</span></div>{group.observations.length ? group.observations.slice(0, 3).map((observation, index) => <ObservationRow task={task} observation={observation} index={rows.indexOf(observation)} key={index} active={activeObservation === observation} onLocate={evidence => locate(evidence, observation)}/>) : <p className="real-missing">未识别到</p>}{group.observations.length > 3 && <small className="real-more-note">另有 {group.observations.length - 3} 条，可展开全部原文查看。</small>}</div>)}
        {part && <details className="real-all-observations" data-testid="part-raw-observations" key={`${task.id}:${part.key}`}><summary>本件全部原始标注 · {rows.length} 条</summary>{rows.map((observation, index) => <ObservationRow task={task} observation={observation} index={index} key={index} active={activeObservation === observation} onLocate={evidence => locate(evidence, observation)}/>)}</details>}
        <details className="real-all-observations" data-testid="global-raw-observations"><summary>全部资料原始标注 · {allObservations.length} 条</summary>{allObservations.length ? allObservations.map((observation, index) => <ObservationRow task={task} observation={observation} index={index} key={index} onLocate={evidence => locate(evidence, observation)}/>) : <p className="real-missing">暂无本轮原始标注。</p>}</details>
        {messages.length > 0 && <details className="real-all-observations real-open-questions"><summary>待核对问题 · {messages.length} 项</summary><ul>{messages.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}
        <div className="real-next-note">以下用料、价格、BOM 与审核为模拟演示，不会把识别内容标为人工已确认。</div><button className="primary-button wide" data-testid="recognition-continue" disabled={!canContinue} onClick={onContinue}>进入模拟用料与价格<ChevronRight size={15}/></button>{!canContinue && <p className="real-missing real-next-help">{running ? '识别完成后可继续演示。' : !parts.length ? '识别出明确零件后可继续演示。' : '请先完成本轮识别。'}</p>}<button className="text-button" disabled={busy} onClick={onBack}><ArrowLeft size={14}/>返回上传</button>
      </aside>
    </div>
  </section>
}
