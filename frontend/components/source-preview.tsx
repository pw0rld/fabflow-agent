'use client'

import { useEffect, useRef, useState } from 'react'
import { ExternalLink, FileImage, Layers3, Maximize2, Minus, Plus } from 'lucide-react'
import { attachmentUrl, safeSourceUrl, validDrawingView, type DrawingHotspot, type DrawingView, type Evidence, type RecognitionTask, type RecognizedPart } from '@/lib/recognition-api'

type SourcePreviewProps = { task: RecognitionTask; part?: RecognizedPart; location: Evidence | null; onLocationChange: (evidence: Evidence) => void }
function validHotspots(view: DrawingView): DrawingHotspot[] {
  const [x, y, width, height] = view.bbox
  return (Array.isArray(view.hotspots) ? view.hotspots : []).slice(0, 16).filter(hotspot => {
    if (!hotspot || typeof hotspot.label !== 'string' || !Array.isArray(hotspot.polygon) || hotspot.polygon.length < 3 || hotspot.polygon.length > 64) return false
    const points = hotspot.polygon
    if (!points.every(point => Array.isArray(point) && point.length === 2 && point.every(Number.isFinite) && point[0] >= x && point[0] <= x + width && point[1] >= y && point[1] <= y + height)) return false
    return Math.abs(points.reduce((sum, point, index) => { const next = points[(index + 1) % points.length]; return sum + point[0] * next[1] - next[0] * point[1] }, 0)) > 0
  })
}
const fullBox = [0, 0, 1000, 1000]
const componentLabels = { lid: '盖板', body: '盒体', bung: '螺纹凸台', clamp: '夹扣', seal: '密封位置' } as const
type StructureComponent = keyof typeof componentLabels

/** This prepared illustration appears only for the server's pinned example metadata. */
function PreparedBox() {
  const [distance, setDistance] = useState(70)
  const [selected, setSelected] = useState<StructureComponent>('lid')
  const componentProps = (name: StructureComponent) => ({ className: `real-structure-component ${selected === name ? 'selected' : ''}`, role: 'button' as const, tabIndex: 0, 'aria-label': componentLabels[name], 'aria-pressed': selected === name, onClick: () => setSelected(name), onKeyDown: (event: React.KeyboardEvent<SVGGElement>) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setSelected(name) } } })
  const gap = distance / 100
  return <div className="real-structure"><div className="real-notice">已整理示例 · 装配关系示意 · 非模型自动重建</div><svg viewBox="0 0 540 370" role="group" aria-label="盖板、盒体和密封位置分层示意，不按比例，非制造模型">
    <g {...componentProps('body')}><path className="rs-back" d="M176 119 L319 38 L429 101 L286 182 Z"/><path className="rs-side" d="M319 38 L429 101 L429 248 L319 185 Z"/><path className="rs-floor" d="M176 266 L319 185 L429 248 L286 329 Z"/><path className="rs-opening" fillRule="evenodd" d="M176 119 L319 38 L319 185 L176 266 Z M182 122 L313 48 L313 181 L182 256 Z"/><path className="rs-edge" d="M286 182 V329 M176 119 L286 182 L429 101 M176 266 L286 329"/></g>
    <g {...componentProps('bung')}><path className="rs-side" d="M405 126 C415 110 440 121 449 140 L464 149 C455 130 430 119 420 135 Z"/><path className="rs-back" d="M405 126 C390 152 420 187 440 172 L455 181 C435 197 405 162 420 135 Z"/><ellipse className="rs-back" cx="443" cy="157" rx="18" ry="27" transform="rotate(-30 443 157)"/><ellipse className="rs-hole" cx="443" cy="157" rx="12" ry="21" transform="rotate(-30 443 157)"/></g>
    <g {...componentProps('clamp')}><path className="rs-side" d="M224 270 L250 256 L267 275 L282 282 L275 305 L255 315 L240 293 L219 289 Z"/><path className="rs-edge" d="M228 276 L246 266 L257 284 L272 289 L267 300 L257 305 L245 285 L228 284 Z"/><ellipse className="rs-hole" cx="263" cy="293" rx="5" ry="9" transform="rotate(-30 263 293)"/><path className="rs-side" d="M229 85 L247 75 L254 85 L247 89 L241 83 L232 89 Z"/></g>
    <g className="rs-guides"><path d={`M${176 - 106 * gap} ${119 - 29 * gap} L176 119 M${176 - 106 * gap} ${266 - 29 * gap} L176 266`}/></g>
    <g {...componentProps('seal')} transform={`translate(${-45 * gap} ${-12 * gap})`}><path className="rs-seal" d="M176 119 L319 38 L319 185 L176 266 Z"/></g>
    <g {...componentProps('lid')} transform={`translate(${-106 * gap} ${-29 * gap})`}><path className="rs-side" d="M176 119 L319 38 L325 41 L182 122 Z M319 38 L325 41 L325 188 L319 185 Z"/><path className="rs-lid" d="M176 119 L319 38 L319 185 L176 266 Z"/><path className="rs-edge" d="M181 123 L314 48 L314 181 L181 256 Z"/></g>
  </svg><div className="real-hotspot-options">{(Object.keys(componentLabels) as StructureComponent[]).map(key => <button key={key} aria-pressed={selected === key} onClick={() => setSelected(key)}>{componentLabels[key]}</button>)}</div><label className="real-range">分离距离 <span>{distance === 0 ? '合拢' : distance + '%'}</span><input aria-label="分离距离" type="range" min="0" max="100" value={distance} onChange={event => setDistance(Number(event.target.value))}/></label><p className="real-caption">{componentLabels[selected]} · 不按比例 · 非制造模型</p></div>
}

export default function SourcePreview({ task, part, location, onLocationChange }: SourcePreviewProps) {
  const [mode, setMode] = useState<'original' | 'located' | 'structure'>('original')
  const [selectedViewId, setSelectedViewId] = useState('')
  const [selectedHotspot, setSelectedHotspot] = useState<number | null>(null)
  const [zoom, setZoom] = useState(1)
  const [imageState, setImageState] = useState({ url: '', ratio: 1, failed: false })
  const canvas = useRef<HTMLDivElement>(null)
  const drag = useRef<{ x: number; y: number; left: number; top: number } | null>(null)
  const firstReference = part?.evidence.find(e => task.attachments.some(file => file.id === e.reference))?.reference
  const file = task.attachments.find(item => item.id === (location?.reference || firstReference)) || task.attachments[0]
  const page = Math.max(1, Math.min(10_000, location?.page || 1))
  const views = (part?.visualization?.views || []).filter(view => validDrawingView(task, view, part!))
  const matchingViews = views.filter(view => view.reference === file?.id && view.page === page)
  const locatedViews = matchingViews.filter(view => view.bbox.some((n, index) => n !== fullBox[index]) || view.hotspots?.length)
  const view = matchingViews.find(item => item.id === selectedViewId) || matchingViews[0]
  const original = file ? attachmentUrl(task, file) : null
  const isImage = file && (/^image\/(png|jpeg|webp|gif|bmp)$/.test(file.mime) || /\.(png|jpe?g|webp|gif|bmp)$/i.test(file.name))
  const isPdf = file && (file.mime === 'application/pdf' || /\.pdf$/i.test(file.name))
  const isCad = file && /\.(dxf|dwg|step|stp)$/i.test(file.name)
  const imageUrl = view ? safeSourceUrl(task, view.image_url, view.reference, page) : isImage ? original : null
  const imageFailed = imageState.url === imageUrl && imageState.failed
  const box = mode === 'located' && view ? view.bbox : fullBox
  const ratio = imageState.url === imageUrl ? imageState.ratio : view?.width && view.height ? view.width / view.height : 1.414
  const hotspots = mode === 'located' && view ? validHotspots(view) : []
  const prepared = part?.visualization?.schematic?.kind === 'prepared-example' && part.visualization.schematic.template === 'sealed-box-v1' && views.some(item => item.provenance === 'prepared')

  useEffect(() => { setMode('original'); setSelectedViewId(''); setSelectedHotspot(null); setZoom(1) }, [task.id, part?.key, location?.reference, location?.page])
  useEffect(() => { if (mode === 'structure' && !prepared || mode === 'located' && !locatedViews.length) setMode('original') }, [mode, prepared, locatedViews.length])
  if (!file) return <section className="drawing-panel real-source"><div className="real-empty">上传图纸后可在这里查看原文件。</div></section>
  const onSource = (reference: string, nextPage: number) => { setMode('original'); setSelectedHotspot(null); onLocationChange({ reference, page: nextPage, region: '' }) }
  const iframeUrl = original && isCad ? '/workbench/cad-preview/index.html?task=' + encodeURIComponent(task.id) + '&source=' + encodeURIComponent(file.id) : original && isPdf ? original + '#page=' + page : null
  return <section className="drawing-panel real-source" data-testid="source-preview"><div className="panel-toolbar"><div className="view-tabs"><button className={mode === 'original' ? 'active' : ''} onClick={() => setMode('original')}><FileImage size={15}/>原图视图</button>{locatedViews.length > 0 && <button className={mode === 'located' ? 'active' : ''} onClick={() => { setMode('located'); setSelectedViewId(locatedViews[0].id) }}>零件局部</button>}{prepared && <button className={mode === 'structure' ? 'active' : ''} onClick={() => setMode('structure')}><Layers3 size={15}/>分层结构示意</button>}</div><span className="sample-tag">{mode === 'structure' ? '已整理示例' : '真实原图'}</span></div>
    {mode !== 'structure' && <div className="real-source-tools"><label className="real-source-select">来源文件<select aria-label="选择来源图纸" data-testid="source-file-select" value={file.id} onChange={event => onSource(event.target.value, 1)}>{task.attachments.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>{isPdf && <label className="real-page-select">第<input aria-label="原图页码" data-testid="source-page-input" type="number" min="1" max="10000" value={page} onChange={event => onSource(file.id, Math.max(1, Math.min(10_000, Number(event.target.value) || 1)))}/>页</label>}{original && <a className="icon-button" href={original} target="_blank" rel="noopener noreferrer" aria-label="打开原文件"><ExternalLink size={16}/></a>}</div>}
    {mode === 'structure' ? <PreparedBox/> : <>
      {mode === 'located' && locatedViews.length > 1 && <div className="real-view-choices">{locatedViews.map(item => <button key={item.id} aria-pressed={view?.id === item.id} onClick={() => { setSelectedViewId(item.id); setSelectedHotspot(null); setZoom(1) }}>{item.label}</button>)}</div>}
      {part?.bom && !locatedViews.length && <div className="real-location-note" data-testid="part-unlocated">本条目来自物料表，尚未定位独立部件，当前显示原图。</div>}
      {location?.region && <div className="real-location-note"><span>来源位置</span>{location.region}</div>}
      <div className={`real-source-canvas ${imageUrl && !imageFailed ? 'has-image' : ''}`} ref={canvas} onPointerDown={event => { if (imageUrl && !imageFailed && zoom > 1 && !(event.target instanceof SVGPolygonElement)) { drag.current = { x: event.clientX, y: event.clientY, left: event.currentTarget.scrollLeft, top: event.currentTarget.scrollTop }; event.currentTarget.setPointerCapture(event.pointerId) } }} onPointerMove={event => { if (drag.current) { event.currentTarget.scrollLeft = drag.current.left + drag.current.x - event.clientX; event.currentTarget.scrollTop = drag.current.top + drag.current.y - event.clientY } }} onPointerUp={() => { drag.current = null }} onPointerCancel={() => { drag.current = null }}>
        {imageUrl && !imageFailed ? <div className="real-image-wrap" style={{ width: `${zoom * 100}%`, aspectRatio: String(ratio * box[2] / box[3]) }}><img className="real-source-preload" alt="" aria-hidden="true" src={imageUrl} onLoad={event => { const image = event.currentTarget; setImageState({ url: imageUrl, ratio: image.naturalWidth / image.naturalHeight || 1.414, failed: false }) }} onError={() => setImageState({ url: imageUrl, ratio: 1.414, failed: true })}/><svg viewBox={box.join(' ')} preserveAspectRatio="none" role={hotspots.length ? 'group' : 'img'} aria-label={`${file.name} 第 ${page} 页${mode === 'located' ? ' · ' + view?.label : ''}`}><image href={imageUrl} x="0" y="0" width="1000" height="1000" preserveAspectRatio="none"/>{hotspots.map((hotspot, index) => <polygon key={index} role="button" tabIndex={0} aria-label={hotspot.label} aria-pressed={selectedHotspot === index} className={`real-source-hotspot ${selectedHotspot === index ? 'selected' : ''}`} points={hotspot.polygon.map(point => point.join(',')).join(' ')} onClick={() => setSelectedHotspot(index)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setSelectedHotspot(index) } }}><title>{hotspot.label}</title></polygon>)}</svg></div> : iframeUrl ? <iframe key={iframeUrl} src={iframeUrl} title={`${file.name} 第 ${page} 页`} className="real-document-frame"/> : <div className="real-empty">{imageFailed ? '页面图片暂时无法加载。' : '此格式暂不支持直接预览。'}{original && <a href={original} target="_blank" rel="noopener noreferrer">打开原文件</a>}</div>}
      </div>
      {imageUrl && !imageFailed && <div className="real-preview-controls"><button className="icon-button" aria-label="缩小原图" disabled={zoom <= 1} onClick={() => setZoom(value => Math.max(1, value - 0.25))}><Minus size={15}/></button><span>{Math.round(zoom * 100)}%</span><button className="icon-button" aria-label="放大原图" disabled={zoom >= 4} onClick={() => setZoom(value => Math.min(4, value + 0.25))}><Plus size={15}/></button><button className="icon-button" aria-label="适应视图" onClick={() => setZoom(1)}><Maximize2 size={15}/></button><small>放大后可拖动查看</small></div>}
      {hotspots.length > 0 && <div className="real-hotspot-options">{hotspots.map((hotspot, index) => <button key={index} aria-pressed={selectedHotspot === index} onClick={() => setSelectedHotspot(index)}>{hotspot.label}</button>)}</div>}
      <div className="canvas-footer"><span>{mode === 'located' ? view?.provenance === 'prepared' ? '已整理局部示例 · 待核对' : 'AI 定位的原图局部 · 待核对' : '参数点击定位来源页；有明确定位时提供局部视图'}</span><span>第 {page} 页</span></div>
    </>}
  </section>
}
