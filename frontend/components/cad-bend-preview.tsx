'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { FoldVertical, LoaderCircle, RotateCcw } from 'lucide-react'
import './cad-bend-preview.css'

type Point3 = [number, number, number]
type Mesh = { positions: number[]; indices: number[] }
export type Bend = {
  id: string; status: 'pending_review'; inner_face_ids: string[]; outer_face_ids: string[]; flange_face_ids: string[][]
  axis: { point: Point3; direction: Point3 }; inner_radius_mm: number; outer_radius_mm: number; thickness_mm: number
  rotation_angle_deg: number; included_angle_deg: number; length_mm: number
  tangent_lines: { start: Point3; end: Point3 }[]; mesh: Mesh; direction: 'unconfirmed'; reference_face_id: null; warnings: string[]
}
export type BendProfile = { status: 'candidate' | 'unresolved' | 'not_applicable'; reason: string; unit: 'mm'; bends: Bend[]; warnings: string[] }
type Solid = { id: string; name?: string; mesh: Mesh; bend_features?: BendProfile }
type Group = { id: string; name: string; solid_ids: string[] }
type Viewer = { dispose(): void; select(id: string | null): void; reset(): void }
type Props = { group: Group | null; solids: Map<string, Solid>; profiles?: Record<string, BendProfile>; filesBaseUrl?: string; sourceSha: string; upAxis: 'y' | 'z'; active: boolean; canGenerate: boolean; running: boolean; dirty: boolean; saving: boolean; onGenerate(): void }
const show = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 3 })
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= 1e9
const point = (value: unknown): value is Point3 => Array.isArray(value) && value.length === 3 && value.every(finite)
function displayable(profile: BendProfile | undefined) {
  if (profile?.status !== 'candidate' || profile.unit !== 'mm' || !Array.isArray(profile.bends) || !profile.bends.length || profile.bends.length > 1000) return false
  const ids = new Set<string>()
  return profile.bends.every(bend => {
    if (!/^B\d{4,}$/.test(bend.id) || ids.has(bend.id)) return false
    ids.add(bend.id)
    return bend.status === 'pending_review' && bend.direction === 'unconfirmed' && bend.reference_face_id === null &&
      [bend.inner_radius_mm, bend.outer_radius_mm, bend.thickness_mm, bend.length_mm].every(value => finite(value) && value > 0) &&
      finite(bend.rotation_angle_deg) && bend.rotation_angle_deg > 0 && bend.rotation_angle_deg <= 180 &&
      finite(bend.included_angle_deg) && bend.included_angle_deg >= 0 && bend.included_angle_deg < 180 &&
      point(bend.axis?.point) && point(bend.axis?.direction) &&
      Array.isArray(bend.tangent_lines) && bend.tangent_lines.length === 2 && bend.tangent_lines.every(line => point(line.start) && point(line.end)) &&
      Array.isArray(bend.mesh?.positions) && bend.mesh.positions.length > 0 && Array.isArray(bend.mesh?.indices) && bend.mesh.indices.length > 0
  })
}
function BendSection({ bend }: { bend: Bend }) {
  const theta = bend.rotation_angle_deg * Math.PI / 180, scale = 58 / bend.outer_radius_mm
  const inner = bend.inner_radius_mm * scale, outer = bend.outer_radius_mm * scale
  const at = (radius: number, angle: number) => [88 + radius * Math.cos(angle), 101 - radius * Math.sin(angle)]
  const p = (radius: number, angle: number) => at(radius, angle).join(' ')
  const large = theta > Math.PI ? 1 : 0
  const band = `M ${p(outer, 0)} A ${outer} ${outer} 0 ${large} 0 ${p(outer, theta)} L ${p(inner, theta)} A ${inner} ${inner} 0 ${large} 1 ${p(inner, 0)} Z`
  return <figure className="mf-bend-section" data-testid="mf-bend-section"><figcaption>弯曲带截面示意 · 基于本处识别几何</figcaption><svg viewBox="0 0 320 174" role="img" aria-label={`${bend.id} 弯曲带截面，折弯角${show(bend.rotation_angle_deg)}度，内夹角${show(bend.included_angle_deg)}度，内半径${show(bend.inner_radius_mm)}毫米，外半径${show(bend.outer_radius_mm)}毫米`}>
    <path className="mf-bend-section-band" data-testid="mf-bend-section-band" d={band} />
    <path className="mf-bend-section-rays" d={`M ${p(0, 0)} L ${p(76, 0)} M ${p(0, 0)} L ${p(76, theta)}`} />
    <path className="mf-bend-section-angle" d={`M ${p(72, 0)} A 72 72 0 ${large} 0 ${p(72, theta)}`} />
    <text x="176" y="39" className="mf-bend-section-angle-label">折弯角 θ = {show(bend.rotation_angle_deg)}°</text><text x="176" y="60">成形内夹角 α = {show(bend.included_angle_deg)}°</text>
    <text x="176" y="87">Ri {show(bend.inner_radius_mm)} mm</text><text x="176" y="106">Ro {show(bend.outer_radius_mm)} mm</text><text x="176" y="125">t {show(bend.thickness_mm)} mm</text>
    <text x="13" y="153">仅绘制已识别曲带；示意朝向不表示 UP / DOWN。</text>
  </svg></figure>
}
function BendViewer({ solid, profile, sourceSha, upAxis, active, selected, onSelect }: { solid: Solid; profile: BendProfile; sourceSha: string; upAxis: 'y' | 'z'; active: boolean; selected: string | null; onSelect(id: string | null): void }) {
  const host = useRef<HTMLDivElement>(null), viewer = useRef<Viewer | null>(null), selectedRef = useRef(selected), callback = useRef(onSelect)
  const [started, setStarted] = useState(active), [status, setStatus] = useState('正在显示折弯几何…'), [failed, setFailed] = useState(false)
  selectedRef.current = selected; callback.current = onSelect
  useEffect(() => { if (active) setStarted(true) }, [active])
  useEffect(() => {
    if (!started) return
    const abort = new AbortController(); let current: Viewer | null = null
    setStatus('正在显示折弯几何…'); setFailed(false)
    void (async () => {
      try {
        const moduleUrl = '/workbench/cad-preview/step.js'
        const module = await import(/* webpackIgnore: true */ moduleUrl)
        if (abort.signal.aborted || !host.current) return
        current = await module.mountBends({ element: host.current, solid, profile, sourceSha, upAxis, signal: abort.signal, status: setStatus,
          onSelectionChange: (id: string | null) => callback.current(id?.startsWith('B') ? id : null) })
        if (abort.signal.aborted) { current?.dispose(); return }
        viewer.current = current; current?.select(selectedRef.current)
        setStatus('仅显示当前实体 · 拖动旋转，点击橙色曲带或下方编号核对。')
      } catch (failure) { if (!abort.signal.aborted) { setFailed(true); setStatus(failure instanceof Error ? failure.message : '折弯几何未能显示。') } }
    })()
    return () => { abort.abort(); current?.dispose(); viewer.current = null }
  }, [started, solid, profile, sourceSha, upAxis])
  useEffect(() => { viewer.current?.select(selected) }, [selected])
  return <div className="mf-bend-visual"><div className="mf-bend-view-tools"><span>成形几何 · 单件隔离</span><button type="button" className="cad-button cad-small" data-testid="mf-bend-reset-view" aria-label="复位折弯视角" disabled={failed} onClick={() => viewer.current?.reset()}><RotateCcw size={13} />复位视角</button></div><div className="mf-bend-canvas" data-testid="mf-bend-canvas" ref={host} /><p className={'mf-bend-view-status' + (failed ? ' mf-failed' : '')} role="status">{status}</p></div>
}
export default function CadBendPreview({ group, solids, profiles, filesBaseUrl, sourceSha, upAxis, active, canGenerate, running, dirty, saving, onGenerate }: Props) {
  const [solidId, setSolidId] = useState(''), [selection, setSelection] = useState<{ owner: string; id: string } | null>(null)
  const members = group?.solid_ids.join('|') || ''
  useEffect(() => { setSolidId(group?.solid_ids.length === 1 ? group.solid_ids[0] : '') }, [filesBaseUrl, group?.id, members])
  const solid = solidId && group?.solid_ids.includes(solidId) ? solids.get(solidId) : undefined
  const profile = solid ? profiles?.[solidId] || solid.bend_features : undefined
  const valid = useMemo(() => displayable(profile), [profile])
  const owner = JSON.stringify([filesBaseUrl, group?.id, members, solidId])
  useEffect(() => { setSelection(null) }, [owner, profile])
  const selectedId = selection?.owner === owner && profile?.bends.some(bend => bend.id === selection.id) ? selection.id : null
  const selected = selectedId ? profile?.bends.find(bend => bend.id === selectedId) : undefined
  const select = (id: string | null) => setSelection(id ? { owner, id } : null)
  return <section className="mf-bend-panel" data-testid="mf-bend-panel" onKeyDown={event => { if (event.key === 'Escape') select(null) }}>
    <div className="mf-bend-heading"><div><h3>折弯图</h3><p>原生曲面识别 · 几何标注草稿</p></div><button type="button" className="cad-button cad-small" data-testid="mf-bend-generate" disabled={!canGenerate || running || saving || dirty} title={dirty ? '请先保存当前修改，再识别折弯' : '从原 CAD 识别等厚、直轴圆柱折弯'} onClick={onGenerate}>{running ? <LoaderCircle size={14} className="cad-spin" /> : <FoldVertical size={14} />}{running ? '正在处理' : '识别折弯'}</button></div>
    {group && <label className="mf-bend-solid-select">选择原生实体<select data-testid="mf-bend-solid" value={solidId} onChange={event => setSolidId(event.target.value)}><option value="">{group.solid_ids.length > 1 ? `本组含 ${group.solid_ids.length} 个实体，请选择一件` : '选择实体'}</option>{group.solid_ids.map(id => <option key={id} value={id}>{id} · {solids.get(id)?.name || '原生实体'}</option>)}</select></label>}
    {valid && solid && profile ? <><div className="mf-bend-legend"><span><i />真实弯曲带</span><span><i />选中后显示外侧切线</span><small>{profile.bends.length} 处候选 · 工程待核对</small></div><BendViewer key={owner} solid={solid} profile={profile} sourceSha={sourceSha} upAxis={upAxis} active={active} selected={selectedId} onSelect={select} />
      <div className="mf-bend-list" data-testid="mf-bend-list" aria-label="折弯候选">{profile.bends.map(bend => <button key={bend.id} type="button" data-testid={'mf-bend-' + bend.id} aria-pressed={selectedId === bend.id} onClick={() => select(bend.id)}><strong>{bend.id}</strong><span>折弯 {show(bend.rotation_angle_deg)}°</span><span>Ri {show(bend.inner_radius_mm)} mm</span></button>)}<button type="button" className="mf-bend-clear" data-testid="mf-bend-clear" disabled={!selectedId} onClick={() => select(null)}>显示全部曲带</button></div>
      {selected ? <div className="mf-bend-details" data-testid="mf-bend-details" role="status"><div className="mf-bend-detail-heading"><strong>{selected.id} · 折弯几何</strong><span>待工程核对</span></div><dl><div><dt>折弯角（旋转角）</dt><dd data-testid="mf-bend-rotation">{show(selected.rotation_angle_deg)}°</dd></div><div><dt>成形内夹角</dt><dd data-testid="mf-bend-included">{show(selected.included_angle_deg)}°</dd></div><div><dt>内半径 Ri</dt><dd>{show(selected.inner_radius_mm)} mm</dd></div><div><dt>外半径 Ro</dt><dd>{show(selected.outer_radius_mm)} mm</dd></div><div><dt>板厚 t</dt><dd>{show(selected.thickness_mm)} mm</dd></div><div><dt>有效长度</dt><dd>{show(selected.length_mm)} mm</dd></div></dl><p>折弯方向：待确认 · 尚未指定参考面。</p><BendSection bend={selected} /><details data-testid="mf-bend-evidence"><summary>几何来源与两条切线 · mm</summary><p>内曲面：{selected.inner_face_ids.join('、')}；外曲面：{selected.outer_face_ids.join('、')}</p><p>相邻平面：{selected.flange_face_ids.map(pair => pair.join(' / ')).join('；')}</p>{selected.tangent_lines.map((line, index) => <p key={index}>切线 {index + 1}：({line.start.map(show).join(', ')}) → ({line.end.map(show).join(', ')})</p>)}{selected.warnings?.map((warning, index) => <p key={index}>{warning}</p>)}</details></div> : <div className="mf-bend-selection-hint" data-testid="mf-bend-selection-hint">点击曲带或 B 编号，查看角度、半径、板厚与有效长度。</div>}
    </> : <div className="mf-bend-empty" data-testid="mf-bend-empty"><FoldVertical size={29} /><strong>{!group ? '请选择一个候选分组' : !solid ? '先选择一个原生实体' : !profile ? '尚未识别折弯' : profile.status === 'not_applicable' ? '此实体没有适用的圆柱折弯' : '此实体暂未形成可显示的折弯候选'}</strong><p>{!group ? '从左侧选择分组，再核对单件几何。' : !solid ? '多实体不会合并为一件折弯图。' : profile?.reason || '点击“识别折弯”，读取原 CAD 的曲面与相邻平面。'}</p></div>}
    {!!profile?.warnings?.length && <details className="mf-bend-warnings"><summary>识别说明（{profile.warnings.length}）</summary>{profile.warnings.map((warning, index) => <p key={index}>{warning}</p>)}</details>}
    <p className="mf-bend-scope">仅识别等厚、直轴圆柱折弯；方向、顺序与工艺仍待工程核对。本图为成形几何标注，不是展开图或机床程序。</p>
  </section>
}
