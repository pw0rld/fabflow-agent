'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { Box, Check, Download, Eye, FileText, Layers, LoaderCircle, Merge, Play, RotateCcw, Ruler, Save, Scissors, X } from 'lucide-react'
import CadLaserPreview, { type LaserProfile } from './cad-laser-preview'
import CadBendPreview, { type BendProfile } from './cad-bend-preview'
import EngineeringReviewChecklist, { type EngineeringReview } from './engineering-review-checklist'
import CadQuotationPanel from './cad-quotation-panel'
import './cad-manufacturing-panel.css'

type Kind = 'make' | 'buy' | 'assembly' | 'needs_design' | 'unclassified' | 'exclude'
type Group = { id: string; name: string; classification: Kind; solid_ids: string[]; status: 'pending_review' | 'confirmed'; quantity_per_set: number | null; material: string | null; thickness_mm: number | null; notes: string }
type Thickness = { status: 'candidate' | 'unresolved' | 'not_applicable'; value_mm: number | null; method: 'parallel_faces' | 'coaxial_cylinders' | 'combined' | 'none'; confidence: 'high' | 'medium' | 'none'; reason: string; evidence: { kind: 'parallel_faces' | 'coaxial_cylinders'; face_ids: string[]; thickness_mm: number; support_area_mm2: number }[]; warnings: string[] }
type GroupThickness = { status: 'candidate' | 'unresolved' | 'not_applicable' | 'missing' | 'conflict'; value_mm: number | null; reason: string; rows: { id: string; measurement?: Thickness }[] }
type MaterialEntry = { name: string; description: string; material_label: string; assignment_label: string; scope: 'definition' | 'occurrence' | 'subshape' | 'inherited' }
type MaterialMeasurement = { status: 'candidate' | 'unresolved'; value: string | null; entries: MaterialEntry[]; reason: string; warnings: string[] }
type GroupMaterial = { status: 'candidate' | 'unresolved' | 'missing' | 'conflict'; value: string | null; reason: string; rows: { id: string; measurement?: MaterialMeasurement }[] }
type QuantityCandidate = { status: 'candidate' | 'unresolved' | 'not_applicable'; value: number | null; occurrence_ids: string[]; reason: string }
type BomSettings = { order_sets: number | null }
type Solid = { id: string; name: string; definition_id?: string | null; occurrence_path?: string | string[]; topology?: { faces?: number; edges?: number; vertices?: number; valid?: boolean; closed?: boolean }; bounds_mm?: { min: number[]; max: number[]; size: number[] }; area_mm2?: number; volume_mm3?: number; mesh: { positions: number[]; indices: number[] }; thumbnail?: string; step_file?: string; bend_features?: BendProfile }
type Geometry = { schema_version: number; source_sha256: string; output_unit: string; engine?: { name?: string; version?: string }; solids: Solid[]; warnings?: string[] }
type State = { status: string; stage?: string; stage_label?: string; job_id?: string | null; geometry_run_id?: string | null; revision: number; groups: Group[]; warnings?: string[]; error?: string | { message?: string }; geometry_url?: string; files_base_url?: string; bom_url?: string; ai?: { status: string; provenance?: unknown }; thickness_measurements?: Record<string, Thickness>; material_measurements?: Record<string, MaterialMeasurement>; quantity_candidates?: Record<string, QuantityCandidate>; bom_settings?: BomSettings; laser_profiles?: Record<string, LaserProfile>; bend_profiles?: Record<string, BendProfile>; engineering_review?: EngineeringReview }
type Viewer = { dispose(): void; select(id: string | null): void; isolate(id: string | null): void; restoreVisibility(): void; reset(): void }
const kinds: Record<Kind, string> = { make: '自制件', buy: '外购 / 标准件', assembly: '装配分组', needs_design: '待工程设计', unclassified: '待分类', exclude: '不纳入物料表' }
const numberText = (value: number | null | undefined, digits = 2) => value == null || !Number.isFinite(value) ? '待确认' : value.toLocaleString('zh-CN', { maximumFractionDigits: digits })
const numeric = (value: string) => value === '' ? null : Number(value)
const groupId = () => 'G-' + crypto.randomUUID().replaceAll('-', '').slice(0, 20)
const copyGroups = (groups: Group[]) => groups.map(group => ({ ...group, solid_ids: [...group.solid_ids] }))
const errorMessage = (failure: unknown) => failure instanceof Error ? failure.message : '操作未完成，请重试。'
const measurementMethods: Record<Thickness['method'], string> = { parallel_faces: '相对平面间距', coaxial_cylinders: '同轴曲面间距', combined: '平面与曲面交叉核对', none: '暂无可采用依据' }
function groupThickness(group: Group, measurements: Record<string, Thickness> = {}): GroupThickness {
  const rows = group.solid_ids.map(id => ({ id, measurement: measurements[id] }))
  const values = rows.flatMap(({ measurement }) => measurement?.status === 'candidate' && typeof measurement.value_mm === 'number' && Number.isFinite(measurement.value_mm) && measurement.value_mm > 0 ? [measurement.value_mm] : [])
  if (!rows.some(row => row.measurement)) return { status: 'missing', value_mm: null, rows, reason: '尚未量测板厚。可使用上方“补全物料属性”补充几何依据。' }
  if (rows.every(row => row.measurement?.status === 'not_applicable')) return { status: 'not_applicable', value_mm: null, rows, reason: '本组实体未提供适用的板厚候选，请查看量测依据。' }
  if (values.length > 1 && Math.max(...values) - Math.min(...values) > Math.max(0.001, Math.max(...values) * 0.001)) return { status: 'conflict', value_mm: null, rows, reason: '本组实体的候选板厚不一致，请分别核对或调整分组，不能直接采用一个厚度。' }
  if (values.length !== rows.length) return { status: 'unresolved', value_mm: null, rows, reason: rows.length === 1 && rows[0].measurement ? rows[0].measurement.reason : '本组部分实体缺少明确板厚依据，暂不合并成单一候选值。' }
  return { status: 'candidate', value_mm: Number((values.reduce((sum, value) => sum + value, 0) / values.length).toFixed(8)), rows, reason: rows.length === 1 ? rows[0].measurement!.reason : `${rows.length} 个实体的几何候选板厚一致，仍需工程核对。` }
}
function ThicknessCell({ group, measurement }: { group: Group; measurement: GroupThickness }) {
  return <td data-testid={'mf-bom-thickness-' + group.id}>{group.thickness_mm != null ? <>{numberText(group.thickness_mm, 4)}<small>{group.status === 'confirmed' ? '已填写 · 分组已确认' : '已填写 · 待核对'}{measurement.status === 'candidate' ? ` · 几何候选 ${numberText(measurement.value_mm, 4)}` : ''}</small></> : measurement.status === 'candidate' ? <><span className="mf-thickness-value">{numberText(measurement.value_mm, 4)}</span><small>几何候选 · 待核对</small></> : <>{measurement.status === 'conflict' ? '厚度不一致' : measurement.status === 'not_applicable' ? '不适用' : '待确认'}<small>{measurement.status === 'missing' ? '尚未量测' : measurement.status === 'conflict' ? '请核对分组' : measurement.status === 'not_applicable' ? '请查看几何依据' : '量测依据不足'}</small></>}</td>
}
function groupMaterial(group: Group, measurements: Record<string, MaterialMeasurement> = {}): GroupMaterial {
  const rows = group.solid_ids.map(id => ({ id, measurement: measurements[id] }))
  const values = rows.flatMap(({ measurement }) => measurement?.status === 'candidate' && typeof measurement.value === 'string' && measurement.value.trim() ? [measurement.value.trim()] : [])
  if (!rows.some(row => row.measurement)) return { status: 'missing', value: null, rows, reason: '尚未提取材料原文，可使用“补全物料属性”读取。' }
  if (new Set(values).size > 1) return { status: 'conflict', value: null, rows, reason: '本组实体的材料原文不一致，请分别核对或调整分组。' }
  if (values.length !== rows.length) return { status: 'unresolved', value: null, rows, reason: rows.length === 1 && rows[0].measurement ? rows[0].measurement.reason : '本组部分实体缺少明确材料原文，暂不合并成单一材料候选。' }
  return { status: 'candidate', value: values[0], rows, reason: rows.length === 1 ? rows[0].measurement!.reason : `${rows.length} 个实体的材料原文一致，牌号仍需工程核对。` }
}
function groupQuantity(group: Group, state: State | null): QuantityCandidate {
  const saved = state?.groups.find(row => row.id === group.id)
  if (!saved || saved.classification !== group.classification || [...saved.solid_ids].sort().join('|') !== [...group.solid_ids].sort().join('|')) return { status: 'unresolved', value: null, occurrence_ids: [], reason: '分组已修改，请保存后重新核对数量候选。' }
  const candidate = state?.quantity_candidates?.[group.id]
  if (!candidate) return { status: 'unresolved', value: null, occurrence_ids: [], reason: '尚未提取装配实例数量，可使用“补全物料属性”读取。' }
  if (candidate.status === 'candidate' && (!Number.isSafeInteger(candidate.value) || candidate.value! <= 0)) return { status: 'unresolved', value: null, occurrence_ids: [], reason: '装配实例数量未形成有效候选。' }
  return candidate
}
function groupTotal(group: Group, candidate: QuantityCandidate, sets: number | null) {
  if (sets == null) return { value: null, candidate: false, reason: '物料表套数待填写' }
  if (!Number.isSafeInteger(sets) || sets <= 0 || sets > 1000000) return { value: null, candidate: false, reason: '请填写有效物料表套数' }
  const adopted = group.quantity_per_set
  const perSet = adopted != null ? adopted : candidate.status === 'candidate' ? candidate.value : null
  if (perSet == null || !Number.isSafeInteger(perSet) || perSet <= 0 || perSet > 1000000) return { value: null, candidate: false, reason: '每套用量待核对' }
  const value = perSet * sets
  return Number.isSafeInteger(value) ? { value, candidate: adopted == null, reason: adopted == null ? '候选总数 · 待核对' : '按已填每套用量计算' } : { value: null, candidate: false, reason: '总件数超出支持范围' }
}
function MaterialCell({ group, measurement }: { group: Group; measurement: GroupMaterial }) {
  return <td data-testid={'mf-bom-material-' + group.id}>{group.material || (measurement.status === 'candidate' ? <span className="mf-thickness-value">{measurement.value}</span> : measurement.status === 'conflict' ? '材料不一致' : '待确认')}<small>{group.material ? '已填写' : measurement.status === 'candidate' ? 'CAD 原文候选 · 待核对' : measurement.status === 'missing' ? '尚未提取' : '请核对材料依据'}</small></td>
}
function QuantityCell({ group, candidate }: { group: Group; candidate: QuantityCandidate }) {
  return <td data-testid={'mf-bom-quantity-' + group.id} title={candidate.reason}>{group.quantity_per_set != null ? <>{numberText(group.quantity_per_set, 0)}<small>已填写</small></> : candidate.status === 'candidate' ? <><span className="mf-thickness-value">{numberText(candidate.value, 0)}</span><small>实例候选 · 待核对</small></> : <>{candidate.status === 'not_applicable' ? '不适用' : '待确认'}<small>{candidate.reason}</small></>}</td>
}
async function request<T>(url: string, options: RequestInit = {}): Promise<T> {
  const result = await fetch(url, { ...options, headers: { 'X-Workbench-Request': '1', ...options.headers } })
  const data = await result.json()
  if (!result.ok) throw new Error(data.error?.message || `请求未完成（${result.status}）`)
  return data as T
}

function NativeViewer({ geometry, groups, selectedId, onSelect, upAxis }: { geometry: Geometry; groups: Group[]; selectedId: string | null; onSelect(id: string | null): void; upAxis: 'y' | 'z' }) {
  const host = useRef<HTMLDivElement>(null), viewer = useRef<Viewer | null>(null)
  const selectedRef = useRef(selectedId), callback = useRef(onSelect)
  const [status, setStatus] = useState('正在显示原生几何…'), [failed, setFailed] = useState(false), [isolated, setIsolated] = useState(false)
  selectedRef.current = selectedId; callback.current = onSelect
  const structure = JSON.stringify(groups.map(group => ({ id: group.id, name: group.id, solid_ids: group.solid_ids })))
  useEffect(() => {
    const abort = new AbortController(); let current: Viewer | null = null
    viewer.current = null; setFailed(false); setIsolated(false); setStatus('正在显示原生几何…')
    void (async () => {
      try {
        const moduleUrl = '/workbench/cad-preview/step.js'
        const module = await import(/* webpackIgnore: true */ moduleUrl)
        if (abort.signal.aborted || !host.current) return
        current = await module.mountGeometry({ element: host.current, geometry, groups: JSON.parse(structure), upAxis,
          signal: abort.signal, status: setStatus, onSelectionChange: (id: string | null) => callback.current(id === 'native-root' ? null : id) })
        if (abort.signal.aborted) { current?.dispose(); return }
        viewer.current = current
        if (selectedRef.current) current?.select(selectedRef.current)
        setStatus('橙色为当前分组；其他实体淡显。')
      } catch (failure) { if (!abort.signal.aborted) { setStatus(errorMessage(failure)); setFailed(true) } }
    })()
    return () => { abort.abort(); current?.dispose(); viewer.current = null }
  }, [geometry, structure, upAxis])
  useEffect(() => {
    if (!viewer.current) return
    viewer.current.select(selectedId)
    if (isolated) viewer.current.isolate(selectedId)
  }, [selectedId, isolated])
  return <div className="mf-viewer"><div className="mf-view-tools"><span>原生实体视图</span><div>
    <button type="button" className="cad-button cad-small" data-testid="mf-isolate" disabled={!selectedId || failed} aria-pressed={isolated} onClick={() => { const next = !isolated; setIsolated(next); viewer.current?.isolate(next ? selectedId : null) }}><Eye size={14} />{isolated ? '返回总装' : '单独查看'}</button>
    <button type="button" className="cad-icon-button" aria-label="适配制造实体视图" onClick={() => viewer.current?.reset()}><RotateCcw size={15} /></button>
  </div></div><div className="mf-canvas" ref={host} data-testid="mf-canvas" /><div className={`mf-view-status ${failed ? 'mf-failed' : ''}`} role="status">{status}</div></div>
}

export default function CadManufacturingPanel({ documentId, sourceSha, upAxis }: { documentId: string; sourceSha: string; upAxis: 'y' | 'z' }) {
  const base = `/workbench/api/cad/documents/${encodeURIComponent(documentId)}/manufacturing`
  const [state, setState] = useState<State | null>(null), [geometry, setGeometry] = useState<Geometry | null>(null)
  const [groups, setGroups] = useState<Group[]>([]), [selectedId, setSelectedId] = useState<string | null>(null)
  const [bomSettings, setBomSettings] = useState<BomSettings>({ order_sets: null })
  const [display, setDisplay] = useState<'solid' | 'laser' | 'bends' | 'review' | 'quotation'>('solid')
  const [quotationVisited, setQuotationVisited] = useState(false)
  const [checked, setChecked] = useState<Set<string>>(new Set()), [dirty, setDirty] = useState(false), [saving, setSaving] = useState(false)
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [search, setSearch] = useState('')
  const generation = useRef(0), dirtyRef = useRef(false)
  const inspector = useRef<HTMLElement>(null)
  dirtyRef.current = dirty
  const running = state?.status === 'queued' || state?.status === 'running'
  const selected = groups.find(group => group.id === selectedId)
  const solidMap = useMemo(() => new Map(geometry?.solids.map(solid => [solid.id, solid])), [geometry])
  const selectedSolids = selected?.solid_ids.map(id => solidMap.get(id)).filter((solid): solid is Solid => !!solid) || []
  const thicknessByGroup = useMemo(() => new Map(groups.map(group => [group.id, groupThickness(group, state?.thickness_measurements)])), [groups, state?.thickness_measurements])
  const materialByGroup = useMemo(() => new Map(groups.map(group => [group.id, groupMaterial(group, state?.material_measurements)])), [groups, state?.material_measurements])
  const quantityByGroup = useMemo(() => new Map(groups.map(group => [group.id, groupQuantity(group, state)])), [groups, state])
  const selectedThickness = selected ? thicknessByGroup.get(selected.id)! : null
  const selectedMaterial = selected ? materialByGroup.get(selected.id)! : null
  const selectedQuantity = selected ? quantityByGroup.get(selected.id)! : null
  useEffect(() => { if (inspector.current) inspector.current.scrollTop = 0 }, [selectedId])
  const availableCandidates = useMemo(() => {
    let count = 0
    const rows = groups.map(group => {
      if (group.classification === 'exclude') return group
      const values: Partial<Group> = {}, material = materialByGroup.get(group.id)!, thickness = thicknessByGroup.get(group.id)!, quantity = quantityByGroup.get(group.id)!
      if (group.material == null && material.status === 'candidate' && material.value != null) { values.material = material.value; count++ }
      if (group.thickness_mm == null && thickness.status === 'candidate' && thickness.value_mm != null) { values.thickness_mm = thickness.value_mm; count++ }
      if (group.quantity_per_set == null && quantity.status === 'candidate' && quantity.value != null) { values.quantity_per_set = quantity.value; count++ }
      return Object.keys(values).length ? { ...group, ...values, status: 'pending_review' as const } : group
    })
    return { rows, count }
  }, [groups, materialByGroup, thicknessByGroup, quantityByGroup])
  function takeState(next: State, takeDraft = false) {
    setState(next)
    if (takeDraft || !dirtyRef.current) {
      setGroups(copyGroups(next.groups || []))
      setBomSettings({ order_sets: next.bom_settings?.order_sets ?? null })
      setSelectedId(previous => next.groups?.some(group => group.id === previous) ? previous : next.groups?.[0]?.id || null)
    }
  }
  useEffect(() => {
    const epoch = ++generation.current; const abort = new AbortController()
    setState(null); setGeometry(null); setGroups([]); setBomSettings({ order_sets: null }); setDisplay('solid'); setQuotationVisited(false); setSelectedId(null); setChecked(new Set()); setDirty(false); setError(''); setNotice('')
    void request<State>(base, { signal: abort.signal }).then(next => { if (generation.current === epoch) takeState(next, true) }).catch(failure => { if (!abort.signal.aborted) setError(errorMessage(failure)) })
    return () => { ++generation.current; abort.abort() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base])
  useEffect(() => {
    if (!running) return
    const abort = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const next = await request<State>(base, { signal: abort.signal })
        if (!abort.signal.aborted) { setError(''); takeState(next) }
      } catch (failure) {
        if (!abort.signal.aborted) { setError(errorMessage(failure)); timer = setTimeout(poll, 3000) }
      }
    }
    timer = setTimeout(poll, 1500)
    return () => { clearTimeout(timer); abort.abort() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base, running, state])
  useEffect(() => {
    if (!state?.geometry_url) return
    const abort = new AbortController()
    void request<Geometry>(state.geometry_url, { signal: abort.signal }).then(result => {
      if (result.source_sha256 !== sourceSha) throw new Error('原生几何与当前原文件不匹配。')
      setGeometry(result)
    }).catch(failure => { if (!abort.signal.aborted) setError(errorMessage(failure)) })
    return () => abort.abort()
  }, [state?.geometry_url, state?.geometry_run_id, sourceSha])
  async function analyze() {
    if (running || saving) return
    setError(''); setNotice(''); setSaving(true)
    try {
      const next = await request<State>(base + '/analyze', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: state?.revision || 0, retry: state?.status === 'failed' }) })
      setDirty(false); takeState(next, true)
    } catch (failure) { setError(errorMessage(failure)) }
    finally { setSaving(false) }
  }
  async function completeProperties() {
    if (!state?.geometry_url || running || saving || dirty) return
    setError(''); setNotice(''); setSaving(true)
    try {
      const next = await request<State>(base + '/properties', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: state.revision }) })
      takeState(next)
      setNotice('已提交物料属性补全，原有分组和填写内容保留。')
    } catch (failure) { setError(errorMessage(failure)) }
    finally { setSaving(false) }
  }
  async function generateLaser() {
    if (!state?.geometry_url || running || saving || dirty) return
    setError(''); setNotice(''); setSaving(true); setDisplay('laser')
    try {
      const next = await request<State>(base + '/laser', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: state.revision }) })
      takeState(next); setNotice('已提交平板切割草稿生成，已有分组与填写内容保留。')
    } catch (failure) { setError(errorMessage(failure)) }
    finally { setSaving(false) }
  }
  async function recognizeBends() {
    if (!state?.geometry_url || running || saving || dirty) return
    setError(''); setNotice(''); setSaving(true); setDisplay('bends')
    try {
      const next = await request<State>(base + '/bends', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: state.revision }) })
      takeState(next); setNotice('已提交原生折弯识别，已有分组与填写内容保留。')
    } catch (failure) { setError(errorMessage(failure)) }
    finally { setSaving(false) }
  }
  function edit(values: Partial<Group>) {
    if (!selected) return
    setGroups(previous => previous.map(group => group.id === selected.id ? { ...group, ...values, status: 'pending_review' } : group))
    dirtyRef.current = true
    setDirty(true); setNotice('')
  }
  async function save(next = groups, success = '分组修改已保存。') {
    if (!state || saving || running) return
    setSaving(true); setError(''); setNotice('')
    try {
      const result = await request<State>(base + '/review', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: state.revision, groups: next, bom_settings: bomSettings }) })
      dirtyRef.current = false; setDirty(false); takeState(result, true); setChecked(new Set()); setNotice(success)
    } catch (failure) { setError(errorMessage(failure)) }
    finally { setSaving(false) }
  }
  function merge() {
    const chosen = groups.filter(group => checked.has(group.id))
    if (chosen.length < 2) return
    const merged: Group = { id: groupId(), name: chosen.map(group => group.name).join(' + ').slice(0, 160), classification: 'unclassified', solid_ids: chosen.flatMap(group => group.solid_ids), status: 'pending_review', quantity_per_set: null, material: null, thickness_mm: null, notes: '人工合并分组；原生实体几何未改变，制造属性待核对。' }
    const index = groups.findIndex(group => checked.has(group.id)); const remaining = groups.filter(group => !checked.has(group.id))
    remaining.splice(Math.max(0, index), 0, merged); setGroups(remaining); setSelectedId(merged.id); setChecked(new Set([merged.id])); dirtyRef.current = true; setDirty(true); setNotice('分组已在当前草稿合并，请保存修改。')
  }
  function split() {
    if (!selected || selected.solid_ids.length < 2) return
    const restored = selected.solid_ids.map(id => ({ id: groupId(), name: solidMap.get(id)?.name || id, classification: 'unclassified' as Kind, solid_ids: [id], status: 'pending_review' as const, quantity_per_set: null, material: null, thickness_mm: null, notes: `由分组“${selected.name}”拆回独立实体；未切割或修改原生几何。` }))
    setGroups(groups.flatMap(group => group.id === selected.id ? restored : [group])); setSelectedId(restored[0].id); setChecked(new Set()); dirtyRef.current = true; setDirty(true); setNotice('已拆回原文件中的独立实体，请保存修改。')
  }
  const visible = groups.filter(group => !search || `${group.name} ${group.id}`.toLowerCase().includes(search.toLowerCase()))
  const included = groups.filter(group => group.classification !== 'exclude')
  const errorText = error || (typeof state?.error === 'string' ? state.error : state?.error?.message || '')
  const fileUrl = (name: string) => (state?.files_base_url || base + '/files/').replace(/\/?$/, '/') + encodeURIComponent(name)
  return <section className="mf-panel" data-testid="manufacturing-panel">
    <div className="mf-heading"><div><p className="cad-eyebrow">MANUFACTURING PARTS</p><h2>制造零件候选</h2><p>以原生 CAD 实体为依据，整理分组和物料属性；确认后形成物料表草稿。</p></div><div className="mf-actions">
      {state?.status !== 'ready' && !groups.length && <button className="cad-button cad-primary" data-testid="mf-analyze" disabled={running || saving || !state} onClick={() => void analyze()}>{running || saving ? <LoaderCircle className="cad-spin" size={15} /> : <Play size={15} />}{state?.status === 'failed' ? '重试生成候选' : '生成制造零件候选'}</button>}
      {!!state?.geometry_url && <button className="cad-button" data-testid="mf-measure-thickness" disabled={running || saving || dirty} title={dirty ? '请先保存当前修改，再补全物料属性' : '读取原 CAD 材料、板厚和装配实例，不改变已有分组与填写内容'} onClick={() => void completeProperties()}><Ruler size={15} />{running && ['thickness', 'properties'].includes(state.stage || '') ? '正在补全物料属性' : '补全物料属性'}</button>}
      {!!groups.length && <button className="cad-button" data-testid="mf-adopt-available-candidates" disabled={running || saving || !availableCandidates.count} onClick={() => { if (!availableCandidates.count) return; setGroups(availableCandidates.rows); dirtyRef.current = true; setDirty(true); setNotice(`${availableCandidates.count} 项可用候选已带入空白字段，已有填写值保留；请核对并保存。`) }}><Check size={15} />带入可用候选{availableCandidates.count ? `（${availableCandidates.count} 项）` : ''}</button>}
      {!!groups.length && <button className="cad-button cad-primary" data-testid="mf-save" disabled={!dirty || saving || running} onClick={() => void save()}><Save size={15} />{saving ? '正在保存' : '保存修改'}</button>}
      {!!groups.length && (dirty ? <button className="cad-button" disabled title="导出读取已保存的分组，请先保存修改"><Download size={15} />先保存再导出</button> : <a className="cad-button" data-testid="mf-export" href={state?.bom_url || base + '/bom.csv'}><Download size={15} />导出物料表草稿</a>)}
      {!!groups.length && <button className="cad-button cad-primary" data-testid="mf-quotation-open" disabled={running || saving || dirty} title={dirty ? '请先保存制造分组' : '从当前 STEP 分组继续填写成本并生成报价草稿'} onClick={() => { setQuotationVisited(true); setDisplay('quotation') }}><FileText size={15} />生成报价单</button>}
    </div></div>
    {errorText && <div className="mf-error" role="alert" data-testid="mf-error">{errorText}{error && <button aria-label="关闭制造零件错误提示" onClick={() => setError('')}><X size={15} /></button>}</div>}
    <div className="mf-status" role="status" data-testid="mf-status">{running && <LoaderCircle className="cad-spin" size={14} />}<span>{state?.stage_label || (state?.status === 'ready' ? '候选已生成，待工程师核对' : state?.status === 'failed' ? '本轮未完成，原件保留' : '尚未生成制造零件候选')}</span>{state?.ai && <small>{state.ai.status === 'completed' ? 'AI 建议已返回 · 待人工确认' : state.ai.status === 'unavailable' ? 'AI 本轮不可用 · 仅显示几何候选' : state.ai.status === 'running' ? 'AI 正在分析' : '尚未进行 AI 分析'}</small>}</div>
    {notice && <div className="mf-notice">{notice}</div>}
    {!!state?.warnings?.length && <details className="mf-warnings"><summary>本轮说明与待确认事项（{state.warnings.length}）</summary>{state.warnings.map((warning, index) => <p key={index}>{warning}</p>)}</details>}
    {!!groups.length && display !== 'quotation' && <div className="mf-bom-settings"><label>当前物料表套数（可选）<input data-testid="mf-order-sets" type="number" min="1" max="1000000" step="1" placeholder="尚未填写" value={bomSettings.order_sets ?? ''} disabled={running || saving} onChange={event => { setBomSettings({ order_sets: numeric(event.target.value) }); dirtyRef.current = true; setDirty(true); setNotice('') }} /></label><p>填写后按每套用量计算总件数；数量候选仍需核对，未填套数不估算总量。</p></div>}
    {!groups.length && !running && <div className="mf-empty"><Layers size={34} /><h3>从原文件里的独立实体开始</h3><p>程序读取真实曲面、边界与实体，AI 给出制造分组建议。没有明确切分依据的整体结构会保留为待工程设计，不生成虚构板件。</p></div>}
    {!!groups.length && <><div hidden={display !== 'quotation'}>{quotationVisited && state && <CadQuotationPanel key={documentId} documentId={documentId} sourceRevision={state.revision} dirty={dirty} onBack={() => setDisplay('solid')} />}</div><div hidden={display === 'quotation'}><div className="mf-workspace">
      <aside className="mf-groups"><div className="mf-groups-heading"><h3>候选分组 <small>{groups.length}</small></h3><span>{dirty ? '有未保存修改' : '已保存'}</span></div><input className="cad-search" aria-label="查找制造零件" placeholder="查找名称或编号" value={search} onChange={event => setSearch(event.target.value)} /><div className="mf-batch"><button className="cad-button cad-small" data-testid="mf-merge" disabled={checked.size < 2 || saving || running} onClick={merge}><Merge size={13} />合并分组</button><button className="cad-button cad-small" data-testid="mf-confirm-selected" disabled={!checked.size || saving || running} onClick={() => void save(groups.map(group => checked.has(group.id) ? { ...group, status: 'confirmed' } : group), '所选分组已确认并保存；缺失的制造属性仍待补充。')}><Check size={13} />确认所选</button></div>
        <div className="mf-group-list" data-testid="mf-groups">{visible.map(group => <div key={group.id} className={`mf-group ${selectedId === group.id ? 'mf-selected' : ''} ${group.classification === 'exclude' ? 'mf-excluded' : ''}`}><input type="checkbox" aria-label={'选择分组 ' + group.name} checked={checked.has(group.id)} onChange={event => setChecked(previous => { const next = new Set(previous); if (event.target.checked) next.add(group.id); else next.delete(group.id); return next })} /><button data-testid={'mf-group-' + group.id} onClick={() => setSelectedId(group.id)}>{solidMap.get(group.solid_ids[0])?.thumbnail ? <img src={fileUrl(solidMap.get(group.solid_ids[0])!.thumbnail!)} alt="" /> : <Box size={24} />}<span><strong>{group.name}</strong><small>{kinds[group.classification]} · {group.solid_ids.length} 个实体</small><small>{group.status === 'confirmed' ? '分组已确认' : '待核对'} · {group.id}</small></span></button></div>)}</div>
      </aside>
      <div className="mf-display-stack"><div className="mf-display-tabs" role="tablist" aria-label="制造件图形视图"><button type="button" role="tab" data-testid="mf-solid-view" aria-selected={display === 'solid'} onClick={() => setDisplay('solid')}>原生实体</button><button type="button" role="tab" data-testid="mf-laser-view" aria-selected={display === 'laser'} onClick={() => setDisplay('laser')}>激光切割图</button><button type="button" role="tab" data-testid="mf-bend-view" aria-selected={display === 'bends'} onClick={() => setDisplay('bends')}>折弯图</button><button type="button" role="tab" data-testid="mf-review-view" aria-selected={display === 'review'} onClick={() => setDisplay('review')}>审图清单</button></div><div className="mf-native-slot" style={{ display: display === 'solid' ? 'flex' : 'none' }}>{geometry ? <NativeViewer geometry={geometry} groups={groups} selectedId={selectedId} onSelect={setSelectedId} upAxis={upAxis} /> : <div className="mf-empty"><LoaderCircle className="cad-spin" size={24} /><p>正在载入原生几何…</p></div>}</div><div className="mf-laser-slot" style={{ display: display === 'laser' ? 'block' : 'none' }}><CadLaserPreview group={selected || null} solids={solidMap} profiles={state?.laser_profiles} filesBaseUrl={state?.files_base_url} canGenerate={!!state?.geometry_url} running={running} saving={saving} dirty={dirty} onGenerate={() => void generateLaser()} /></div><div className="mf-bend-slot" style={{ display: display === 'bends' ? 'block' : 'none' }}><CadBendPreview group={selected || null} solids={solidMap} profiles={state?.bend_profiles} filesBaseUrl={state?.files_base_url} sourceSha={geometry?.source_sha256 || sourceSha} upAxis={upAxis} active={display === 'bends'} canGenerate={!!state?.geometry_url} running={running} saving={saving} dirty={dirty} onGenerate={() => void recognizeBends()} /></div><div className="mf-review-slot" style={{ display: display === 'review' ? 'block' : 'none' }}><EngineeringReviewChecklist key={documentId} review={state?.engineering_review} selectedId={selectedId} selectedName={selected?.name} names={Object.fromEntries((state?.groups || []).map(group => [group.id, group.name]))} dirty={dirty} revision={state?.revision} onSelect={id => { if (groups.some(group => group.id === id)) setSelectedId(id) }} testId="mf-engineering-review" /></div></div>
      <aside className="mf-inspector" ref={inspector}>{selected ? <><p className="cad-eyebrow">PART REVIEW</p><h3>{selected.name}</h3><span className="mf-state-tag">{selected.status === 'confirmed' ? '分组已确认' : '候选 · 待人工核对'}</span>
        <label>零件 / 分组名称<input data-testid="mf-name" value={selected.name} onChange={event => edit({ name: event.target.value })} disabled={saving || running} /></label>
        <label>制造属性<select data-testid="mf-classification" value={selected.classification} onChange={event => edit({ classification: event.target.value as Kind })} disabled={saving || running}>{Object.entries(kinds).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        <div className="mf-input-pair"><label>每套用量<input data-testid="mf-quantity" type="number" min="1" step="1" placeholder="待确认" value={selected.quantity_per_set ?? ''} onChange={event => edit({ quantity_per_set: numeric(event.target.value) })} disabled={saving || running} /></label><label>采用板厚（mm）<input data-testid="mf-thickness" type="number" min="0.000001" step="any" placeholder={selectedThickness?.status === 'candidate' ? `候选 ${numberText(selectedThickness.value_mm, 4)}` : '待确认'} value={selected.thickness_mm ?? ''} onChange={event => edit({ thickness_mm: numeric(event.target.value) })} disabled={saving || running} /></label></div>
        {selectedQuantity && <section className={`mf-property-card mf-property-${selectedQuantity.status}`} data-testid="mf-quantity-candidate"><div className="mf-thickness-heading"><span>每套用量候选</span><strong>{selectedQuantity.status === 'candidate' ? `${numberText(selectedQuantity.value, 0)} 件 / 套` : selectedQuantity.status === 'not_applicable' ? '不适用' : '待核对'}</strong></div><p>{selectedQuantity.reason}</p>{selectedQuantity.status === 'candidate' && <button className="cad-button cad-small" data-testid="mf-adopt-quantity" disabled={saving || running || selected.quantity_per_set === selectedQuantity.value} onClick={() => { edit({ quantity_per_set: selectedQuantity.value }); setNotice('数量候选已带入当前草稿，请核对并保存。') }}><Check size={13} />采用数量候选</button>}{!!selectedQuantity.occurrence_ids.length && <details className="mf-property-evidence"><summary>装配实例依据（{selectedQuantity.occurrence_ids.length}）</summary>{selectedQuantity.occurrence_ids.map(id => <small key={id}>{id}</small>)}</details>}</section>}
        {selectedThickness && <section className={`mf-thickness-card mf-thickness-${selectedThickness.status}`} data-testid="mf-thickness-candidate"><div className="mf-thickness-heading"><span>几何量测板厚</span><strong>{selectedThickness.status === 'candidate' ? `${numberText(selectedThickness.value_mm, 4)} mm` : selectedThickness.status === 'conflict' ? '候选厚度不一致' : selectedThickness.status === 'not_applicable' ? '不适用' : selectedThickness.status === 'missing' ? '尚未量测' : '依据不足'}</strong></div><p>{selectedThickness.reason}</p>
          {selectedThickness.status === 'candidate' && <><span className="mf-thickness-tag">候选 · 待工程核对</span><button className="cad-button cad-small" data-testid="mf-adopt-thickness" disabled={saving || running || selected.thickness_mm === selectedThickness.value_mm} onClick={() => { edit({ thickness_mm: selectedThickness.value_mm }); setNotice('候选板厚已带入当前草稿，请核对并保存。') }}><Check size={13} />采用候选板厚</button></>}
          {selectedThickness.rows.some(row => row.measurement) && <details className="mf-thickness-evidence" data-testid="mf-thickness-evidence"><summary>查看量测依据</summary>{selectedThickness.rows.map(({ id, measurement }) => <div key={id}><strong>{id} · {solidMap.get(id)?.name || '原生实体'}</strong>{measurement ? <><span>{measurementMethods[measurement.method]}{measurement.value_mm != null ? ` · ${numberText(measurement.value_mm, 4)} mm` : ''}</span><span>{measurement.confidence === 'high' ? '几何依据较充分' : measurement.confidence === 'medium' ? '几何依据需进一步核对' : '未形成明确候选'}</span><p>{measurement.reason}</p>{measurement.evidence.map((evidence, index) => <small key={index}>{evidence.face_ids.join(' / ')}：{evidence.kind === 'parallel_faces' ? '平面间距' : '曲面间距'} {numberText(evidence.thickness_mm, 4)} mm；参考面积 {numberText(evidence.support_area_mm2)} mm²</small>)}{measurement.warnings.map((warning, index) => <p key={index}>{warning}</p>)}</> : <p>此实体尚未量测。</p>}</div>)}</details>}
        </section>}
        <label>材料<input data-testid="mf-material" placeholder="待确认" value={selected.material ?? ''} onChange={event => edit({ material: event.target.value || null })} disabled={saving || running} /></label>
        {selectedMaterial && <section className={`mf-property-card mf-property-${selectedMaterial.status}`} data-testid="mf-material-candidate"><div className="mf-thickness-heading"><span>CAD 材料原文</span><strong>{selectedMaterial.status === 'candidate' ? selectedMaterial.value : selectedMaterial.status === 'conflict' ? '材料不一致' : '待核对'}</strong></div><p>{selectedMaterial.reason}</p>{selectedMaterial.status === 'candidate' && <button className="cad-button cad-small" data-testid="mf-adopt-material" disabled={saving || running || selected.material === selectedMaterial.value} onClick={() => { edit({ material: selectedMaterial.value }); setNotice('材料原文已带入当前草稿，请核对牌号后保存。') }}><Check size={13} />采用材料原文</button>}{selectedMaterial.rows.some(row => row.measurement) && <details className="mf-property-evidence" data-testid="mf-material-evidence"><summary>查看材料依据</summary>{selectedMaterial.rows.map(({ id, measurement }) => <div key={id}><strong>{id} · {solidMap.get(id)?.name || '原生实体'}</strong>{measurement ? <><p>{measurement.reason}</p>{measurement.entries.map((entry, index) => <div className="mf-material-entry" key={index}><span>{entry.name || '未命名材料'}</span>{entry.description && <small>{entry.description}</small>}<small>材料 {entry.material_label} · 赋值 {entry.assignment_label}</small><small>归属：{({ definition: '部件定义', occurrence: '装配实例', subshape: '局部形状', inherited: '继承属性' })[entry.scope]}</small></div>)}{measurement.warnings.map((warning, index) => <p key={index}>{warning}</p>)}</> : <p>此实体尚未读取材料原文。</p>}</div>)}</details>}</section>}
        <label>工程说明<textarea data-testid="mf-notes" value={selected.notes} onChange={event => edit({ notes: event.target.value })} disabled={saving || running} /></label>
        <div className="mf-actions"><button className="cad-button cad-primary" data-testid="mf-confirm" disabled={saving || running} onClick={() => void save(groups.map(group => group.id === selected.id ? { ...group, status: 'confirmed' } : group), '当前分组已确认并保存；不代表制造工艺已批准。')}><Check size={14} />确认分组</button><button className="cad-button" data-testid="mf-split" disabled={selected.solid_ids.length < 2 || saving || running} onClick={split}><Scissors size={14} />拆回独立实体</button></div>
        <p className="mf-scope-note">确认的是分组与本次填写内容；空白仍为待确认。合并 / 拆分仅调整分组，不切割原 CAD。</p>
        <details className="mf-native-details"><summary>原生实体依据（{selectedSolids.length}）</summary>{selectedSolids.map(solid => <div key={solid.id}><strong>{solid.name || solid.id}</strong><small>{solid.id} · {solid.topology?.faces ?? '未知'} 个原生面</small><small>包围范围：{solid.bounds_mm?.size.map(value => numberText(value)).join(' × ') || '待确认'} mm</small><small>表面积：{numberText(solid.area_mm2)} mm² · 实体体积：{numberText(solid.volume_mm3)} mm³</small>{solid.step_file && <a href={fileUrl(solid.step_file)}><Download size={12} />下载此原生实体 STEP</a>}</div>)}</details>
      </> : <p className="cad-muted">选择一个候选分组查看。</p>}</aside>
    </div><section className="mf-bom"><div className="mf-bom-heading"><h3>物料图表草稿</h3><span>分组已确认 {included.filter(group => group.status === 'confirmed').length} / {included.length} · {dirty ? '当前为未保存草稿' : '当前保存版本'}</span></div><div className="mf-table-scroll"><table data-testid="mf-bom"><thead><tr><th>零件 / 编号</th><th>材料</th><th>板厚 mm</th><th>每套用量</th><th>总件数</th><th>制造属性</th><th>核对状态</th></tr></thead><tbody>{included.map(group => {
      const quantity = quantityByGroup.get(group.id)!, total = groupTotal(group, quantity, bomSettings.order_sets)
      const thumbnail = solidMap.get(group.solid_ids[0])?.thumbnail
      return <tr key={group.id} data-testid={'mf-bom-row-' + group.id} onClick={() => setSelectedId(group.id)}><td><div className="mf-bom-part">{thumbnail ? <img src={fileUrl(thumbnail)} alt="" /> : <Box size={28} />}<div><strong>{group.name}</strong><small>{group.id} · 关联 {group.solid_ids.length} 个原生实体</small></div></div></td><MaterialCell group={group} measurement={materialByGroup.get(group.id)!} /><ThicknessCell group={group} measurement={thicknessByGroup.get(group.id)!} /><QuantityCell group={group} candidate={quantity} /><td data-testid={'mf-bom-total-' + group.id}><span className={total.candidate ? 'mf-thickness-value' : ''}>{numberText(total.value, 0)}</span><small>{total.reason}</small></td><td>{kinds[group.classification]}</td><td>{group.status === 'confirmed' ? '已确认分组' : '待核对'}</td></tr>
    })}</tbody></table></div><p>材料、板厚及实例数量均可保留为候选，采用并保存后才写入填写值。此表用于工程核对，尚未生成加工图、展开图或生产批准记录。实体引用数量不代替每套制造用量。</p></section></div></>}
  </section>
}
