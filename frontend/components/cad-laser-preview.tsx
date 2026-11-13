'use client'

import { useEffect, useMemo, useState } from 'react'
import { Download, FileBox, LoaderCircle, Scissors } from 'lucide-react'
import './cad-laser-preview.css'

type Point = [number, number]
type Line = { type: 'line'; start: Point; end: Point }
type Arc = { type: 'arc'; start: Point; end: Point; center: Point; radius: number; clockwise: boolean }
type Circle = { type: 'circle'; center: Point; radius: number }
type Segment = Line | Arc | Circle
export type LaserProfile = {
  status: 'ready' | 'unsupported'; reason: string; unit: 'mm'; thickness_mm: number | null; source_face_id: string | null
  plane: { origin: number[]; u: number[]; v: number[]; normal: number[] } | null
  bounds_mm: { min: Point; max: Point; size: Point } | null
  contours: { id: string; role: 'outer' | 'inner'; segments: Segment[] }[]
  holes: { id: string; center: Point; diameter_mm: number }[]; dxf_file: string | null; warnings: string[]
}
type Group = { id: string; name: string; solid_ids: string[] }
type Props = { group: Group | null; solids: Map<string, { name?: string }>; profiles?: Record<string, LaserProfile>; filesBaseUrl?: string; canGenerate: boolean; running: boolean; dirty: boolean; saving: boolean; onGenerate(): void }
const TAU = Math.PI * 2
const positive = (value: number) => ((value % TAU) + TAU) % TAU
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= 1e9
const point = (value: unknown): value is Point => Array.isArray(value) && value.length === 2 && value.every(finite)
const show = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 4 })
const raw = (value: number) => Number(value.toPrecision(12)).toString()
function arcData(arc: Arc) {
  const start = Math.atan2(arc.start[1] - arc.center[1], arc.start[0] - arc.center[0])
  const end = Math.atan2(arc.end[1] - arc.center[1], arc.end[0] - arc.center[0])
  return { start, sweep: positive(arc.clockwise ? start - end : end - start) }
}
function arcPath(arc: Arc) {
  const { sweep } = arcData(arc)
  // Keep CAD coordinates and reverse Y once on the enclosing group. Positive
  // SVG-local sweep therefore becomes CAD counterclockwise on the screen.
  return `M ${raw(arc.start[0])} ${raw(arc.start[1])} A ${raw(arc.radius)} ${raw(arc.radius)} 0 ${sweep > Math.PI ? 1 : 0} ${arc.clockwise ? 0 : 1} ${raw(arc.end[0])} ${raw(arc.end[1])}`
}
function renderable(profile: LaserProfile | undefined, solidId: string) {
  if (profile?.status !== 'ready' || profile.unit !== 'mm' || profile.dxf_file !== solidId + '-cut.dxf' || !profile.bounds_mm || !profile.plane) return false
  const bounds = profile.bounds_mm
  if (!point(bounds.min) || !point(bounds.max) || !point(bounds.size) || bounds.size.some(value => value <= 0) || bounds.size.some((value, axis) => Math.abs(bounds.max[axis] - bounds.min[axis] - value) > Math.max(0.00001, value * 0.000001))) return false
  if (!finite(profile.thickness_mm) || profile.thickness_mm <= 0 || !Array.isArray(profile.contours) || !profile.contours.length || profile.contours.length > 2000 || !Array.isArray(profile.holes)) return false
  const fits = (p: Point) => p.every((value, axis) => value >= bounds.min[axis] - 0.001 && value <= bounds.max[axis] + 0.001)
  let count = 0
  for (const contour of profile.contours) {
    if (!['outer', 'inner'].includes(contour.role) || !Array.isArray(contour.segments) || !contour.segments.length) return false
    for (const segment of contour.segments) {
      if (++count > 20000) return false
      if (segment.type === 'line') {
        if (!point(segment.start) || !point(segment.end) || !fits(segment.start) || !fits(segment.end)) return false
      } else if (segment.type === 'circle') {
        if (!point(segment.center) || !finite(segment.radius) || segment.radius <= 0 || !fits([segment.center[0] - segment.radius, segment.center[1] - segment.radius]) || !fits([segment.center[0] + segment.radius, segment.center[1] + segment.radius])) return false
      } else if (segment.type === 'arc') {
        if (!point(segment.start) || !point(segment.end) || !point(segment.center) || !finite(segment.radius) || segment.radius <= 0 || typeof segment.clockwise !== 'boolean' || !fits(segment.start) || !fits(segment.end)) return false
        const tolerance = Math.max(0.001, segment.radius * 0.000001)
        if (Math.abs(Math.hypot(segment.start[0] - segment.center[0], segment.start[1] - segment.center[1]) - segment.radius) > tolerance || Math.abs(Math.hypot(segment.end[0] - segment.center[0], segment.end[1] - segment.center[1]) - segment.radius) > tolerance) return false
        const { start, sweep } = arcData(segment)
        if (sweep < 1e-8) return false // Full circles use their explicit primitive.
        for (const angle of [0, Math.PI / 2, Math.PI, Math.PI * 1.5]) {
          const delta = positive(segment.clockwise ? start - angle : angle - start)
          if (delta <= sweep + 1e-8 && !fits([segment.center[0] + segment.radius * Math.cos(angle), segment.center[1] + segment.radius * Math.sin(angle)])) return false
        }
      } else return false
    }
  }
  return profile.holes.length <= 2000 && profile.holes.every(hole => point(hole.center) && finite(hole.diameter_mm) && hole.diameter_mm > 0 && fits(hole.center))
}

type Hole = LaserProfile['holes'][number]

function CuttingSvg({ profile, selectedHole, onSelect }: { profile: LaserProfile; selectedHole?: Hole; onSelect(holeId: string | null): void }) {
  const bounds = profile.bounds_mm!, span = Math.max(...bounds.size), padding = Math.max(span * 0.07, 1)
  const mark = span * 0.012
  const viewBox = [bounds.min[0] - padding, -bounds.max[1] - padding, bounds.size[0] + padding * 2, bounds.size[1] + padding * 2].map(raw).join(' ')
  function selectAtPoint(svg: SVGSVGElement, clientX: number, clientY: number, fallback: string | null = null) {
    const transform = svg.querySelector<SVGGElement>('[data-testid="mf-laser-cut-geometry"]')?.getScreenCTM()
    if (!transform) { onSelect(fallback); return }
    const at = new DOMPoint(clientX, clientY).matrixTransform(transform.inverse())
    // Enlarged transparent touch targets may overlap in dense perforations.
    // Prefer the actual hole containing the pointer, then the nearest edge.
    const nearest = profile.holes.reduce<{ hole: Hole; distance: number } | null>((best, hole) => {
      const distance = Math.max(0, Math.hypot(at.x - hole.center[0], at.y - hole.center[1]) - hole.diameter_mm / 2)
      return !best || distance < best.distance ? { hole, distance } : best
    }, null)
    const pixelsPerMm = Math.hypot(transform.a, transform.b)
    onSelect(nearest && nearest.distance * pixelsPerMm <= 12 ? nearest.hole.id : fallback)
  }
  return <svg data-testid="mf-laser-svg" viewBox={viewBox} role="group" aria-label="原生平板切割轮廓，单位毫米；选择圆孔查看尺寸和坐标" preserveAspectRatio="xMidYMid meet" onClick={event => selectAtPoint(event.currentTarget, event.clientX, event.clientY)}>
    <g transform="scale(1 -1)" data-testid="mf-laser-cut-geometry">{profile.contours.map(contour => <g key={contour.id} data-testid={'mf-laser-contour-' + contour.id} className={'mf-laser-' + contour.role}>{contour.segments.map((segment, index) => segment.type === 'circle' ? <circle data-type="circle" key={index} cx={segment.center[0]} cy={segment.center[1]} r={segment.radius} /> : segment.type === 'line' ? <line data-type="line" key={index} x1={segment.start[0]} y1={segment.start[1]} x2={segment.end[0]} y2={segment.end[1]} /> : <path data-type="arc" data-clockwise={segment.clockwise} key={index} d={arcPath(segment)} />)}</g>)}
      {selectedHole && <g className="mf-laser-selected-hole" data-testid="mf-laser-selected-hole" data-hole-id={selectedHole.id} aria-hidden="true">
        <circle cx={selectedHole.center[0]} cy={selectedHole.center[1]} r={selectedHole.diameter_mm / 2} />
        <path data-testid="mf-laser-centers" d={`M ${raw(selectedHole.center[0] - mark)} ${raw(selectedHole.center[1])} L ${raw(selectedHole.center[0] + mark)} ${raw(selectedHole.center[1])} M ${raw(selectedHole.center[0])} ${raw(selectedHole.center[1] - mark)} L ${raw(selectedHole.center[0])} ${raw(selectedHole.center[1] + mark)}`} />
      </g>}
      <g className="mf-laser-hole-targets">{profile.holes.map(hole => <circle key={hole.id} className="mf-laser-hole-target" data-testid={'mf-laser-hole-' + hole.id} role="button" tabIndex={0} aria-label={'选择孔 ' + hole.id} aria-pressed={selectedHole?.id === hole.id} cx={hole.center[0]} cy={hole.center[1]} r={hole.diameter_mm / 2} onClick={event => { event.stopPropagation(); if (event.detail === 0) onSelect(hole.id); else selectAtPoint(event.currentTarget.ownerSVGElement!, event.clientX, event.clientY, hole.id) }} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); event.stopPropagation(); onSelect(hole.id) } }} />)}</g>
    </g>
  </svg>
}

export default function CadLaserPreview({ group, solids, profiles = {}, filesBaseUrl, canGenerate, running, dirty, saving, onGenerate }: Props) {
  const [solidId, setSolidId] = useState('')
  const [selection, setSelection] = useState<{ owner: string; holeId: string } | null>(null)
  const members = group?.solid_ids.join('|') || ''
  useEffect(() => { setSolidId(group?.solid_ids.length === 1 ? group.solid_ids[0] : '') }, [filesBaseUrl, group?.id, members])
  const profile = solidId && group?.solid_ids.includes(solidId) ? profiles[solidId] : undefined
  const valid = useMemo(() => renderable(profile, solidId), [profile, solidId])
  // Guard during render as well as resetting state, so identical contour IDs
  // from a different document or entity cannot briefly reuse the selection.
  const selectionOwner = JSON.stringify([filesBaseUrl, group?.id, members, solidId])
  useEffect(() => { setSelection(null) }, [selectionOwner, profile])
  const selectedHole = valid && selection?.owner === selectionOwner ? profile?.holes.find(hole => hole.id === selection.holeId) : undefined
  const selectHole = (holeId: string | null) => setSelection(holeId ? { owner: selectionOwner, holeId } : null)
  const baseValid = !!filesBaseUrl && /^\/workbench\/api\/cad\/documents\/CAD-[a-f0-9]{32}\/manufacturing\/files\/$/.test(filesBaseUrl)
  const downloadable = valid && baseValid && !running && !saving && !dirty
  return <section className="mf-laser-panel" data-testid="mf-laser-panel" onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); selectHole(null) } }}>
    <div className="mf-laser-heading"><div><h3>激光切割图</h3><p>平板切割轮廓 · 工程审核草稿</p></div><button className="cad-button cad-small" data-testid="mf-laser-generate" disabled={!canGenerate || running || saving || dirty} title={dirty ? '请先保存当前修改，再生成切割草稿' : '读取原生平板边界，不进行排版或模型推理'} onClick={onGenerate}>{running ? <LoaderCircle className="cad-spin" size={14} /> : <Scissors size={14} />}{running ? '正在处理' : '生成平板切割草稿'}</button></div>
    {group && <label className="mf-laser-select">选择原生实体<select data-testid="mf-laser-solid" value={solidId} onChange={event => setSolidId(event.target.value)}><option value="">{group.solid_ids.length > 1 ? `本组含 ${group.solid_ids.length} 个实体，请选择一件` : '选择实体'}</option>{group.solid_ids.map(id => <option key={id} value={id}>{id} · {solids.get(id)?.name || '原生实体'}</option>)}</select></label>}
    {valid && profile ? <><div className="mf-laser-metadata"><span data-testid="mf-laser-units">DXF：mm / 1:1</span><span>板厚 {show(profile.thickness_mm!)} mm</span><span>{show(profile.bounds_mm!.size[0])} × {show(profile.bounds_mm!.size[1])} mm</span></div><div className="mf-laser-svg-frame"><CuttingSvg profile={profile} selectedHole={selectedHole} onSelect={selectHole} /></div><div className="mf-laser-legend"><span><i className="mf-laser-outer-key" />外轮廓</span><span><i className="mf-laser-inner-key" />内轮廓 / 孔</span><span className="mf-laser-select-hint">点击圆孔查看孔径与坐标</span></div><div className="mf-laser-hole-info" data-testid="mf-laser-hole-info" role="status" aria-live="polite">{selectedHole ? <><div><strong data-testid="mf-laser-hole-number">孔 {selectedHole.id}</strong><span data-testid="mf-laser-hole-diameter">直径 ⌀{show(selectedHole.diameter_mm)} mm</span><span data-testid="mf-laser-hole-x">X {show(selectedHole.center[0])} mm</span><span data-testid="mf-laser-hole-y">Y {show(selectedHole.center[1])} mm</span><small>局部平面坐标 · Y 向上</small></div><button type="button" className="cad-button cad-small" data-testid="mf-laser-hole-clear" onClick={() => selectHole(null)}>清除选择</button></> : <span>孔标注默认隐藏，选择一个圆孔后显示。可按 Tab 选择孔，Enter 或空格查看，Esc 清除。</span>}</div>{!!profile.holes.length && <details className="mf-laser-holes" data-testid="mf-laser-holes"><summary>圆孔位置（{profile.holes.length}）· 局部平面坐标 mm</summary><table><thead><tr><th>孔</th><th>X</th><th>Y</th><th>直径</th></tr></thead><tbody>{profile.holes.map(hole => <tr key={hole.id} aria-selected={selectedHole?.id === hole.id}><td><button type="button" className="mf-laser-hole-table-button" data-testid={'mf-laser-hole-row-' + hole.id} aria-pressed={selectedHole?.id === hole.id} onClick={() => selectHole(hole.id)}>{hole.id}</button></td><td>{show(hole.center[0])}</td><td>{show(hole.center[1])}</td><td>⌀{show(hole.diameter_mm)}</td></tr>)}</tbody></table></details>}<div className="mf-laser-download">{downloadable ? <a className="cad-button cad-small" data-testid="mf-laser-download" href={filesBaseUrl + encodeURIComponent(profile.dxf_file!)} download={profile.dxf_file!}><Download size={14} />下载此实体 DXF</a> : <button className="cad-button cad-small" disabled>{dirty ? '保存修改后下载 DXF' : '处理完成后下载 DXF'}</button>}<small>辅助标记仅用于预览，不进入切割 DXF。</small></div></> : <div className="mf-laser-empty" data-testid="mf-laser-empty"><FileBox size={30} /><strong>{!group ? '请选择一个候选分组' : !solidId ? '先选择一个原生实体' : profile?.status === 'unsupported' ? '此实体暂不能生成平板切割图' : profile?.status === 'ready' ? '切割数据未通过显示校验' : '尚未生成切割草稿'}</strong><p>{!group ? '可从左侧列表选择，再查看具体实体的切割草稿。' : !solidId ? '多实体分组不会拼成一张单件切割图。' : profile?.status === 'unsupported' ? profile.reason : profile?.status === 'ready' ? '请重新生成并核对原生边界；当前不展示推测轮廓。' : '点击上方按钮，从原 CAD 读取可支持的平板轮廓。'}</p></div>}
    {!!profile?.warnings?.length && <details className="mf-laser-warnings"><summary>此实体说明（{profile.warnings.length}）</summary>{profile.warnings.map((warning, index) => <p key={index}>{warning}</p>)}</details>}
    <p className="mf-laser-scope">未做排版和切缝补偿；折弯件不会用三维投影代替展开图。</p>
  </section>
}
