'use client'

import { useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronUp, Layers3, LoaderCircle, RotateCcw } from 'lucide-react'
import { safePartPreviewUrl } from '@/components/batch-part-viewer'
import './source-assembly-preview.css'

const SOURCE_PART = /^\/(?:workbench\/customer\/api\/orders|ortho2cad-test\/customer-upload-admin\/api\/quote-batches)\/(BATCH-[a-f0-9]{32})\/runs\/(RUN-[a-f0-9]{32})\/customer-parts\/(L[0-9]{4,8})$/
const SHA = /^[a-f0-9]{64}$/
type Solid = { id: string; name?: string; mesh: { positions: number[]; indices: number[] } }
type Geometry = { source_sha256: string; output_unit: 'mm'; solids: Solid[] }
type SourceAssembly = { url: string; geometry: Geometry; upAxis: 'y' | 'z' }
type Viewer = { dispose(): void; reset(): void; select(id: string | null): void }

function assemblyPayload(value: unknown, url: string): SourceAssembly {
  const match = SOURCE_PART.exec(url)
  if (!match || !value || typeof value !== 'object') throw new Error('原图资料暂不可用。')
  const data = value as { batch_id?: unknown; run_id?: unknown; line_id?: unknown; source_sha256?: unknown; geometry?: unknown; up_axis?: unknown }
  if (data.batch_id !== match[1] || data.run_id !== match[2] || data.line_id !== match[3]
      || typeof data.source_sha256 !== 'string' || !SHA.test(data.source_sha256)) throw new Error('原图来源与当前订单不一致，请刷新订单。')
  const geometry = data.geometry as Geometry | undefined
  if (!geometry || geometry.source_sha256 !== data.source_sha256 || geometry.output_unit !== 'mm'
      || !Array.isArray(geometry.solids) || !geometry.solids.length || geometry.solids.length > 10000) throw new Error('当前来源尚无可显示的完整原图。')
  const ids = new Set<string>()
  for (const solid of geometry.solids) {
    if (!solid || typeof solid.id !== 'string' || !solid.id || ids.has(solid.id)
        || !Array.isArray(solid.mesh?.positions) || !Array.isArray(solid.mesh?.indices)) throw new Error('原图的实体显示数据不完整。')
    ids.add(solid.id)
  }
  if (data.up_axis != null && data.up_axis !== 'y' && data.up_axis !== 'z') throw new Error('原图坐标信息暂不可用。')
  return { url, geometry, upAxis: data.up_axis === 'y' ? 'y' : 'z' }
}

export default function SourceAssemblyPreview({ previewUrl, sourceName, active = true, refreshKey }: {
  previewUrl: string | null; sourceName: string; active?: boolean; refreshKey?: string | number;
}) {
  const [open, setOpen] = useState(true), [reload, setReload] = useState(0)
  const [data, setData] = useState<SourceAssembly | null>(null), [loading, setLoading] = useState(false)
  const [error, setError] = useState(''), [ready, setReady] = useState(false), [stageVisible, setStageVisible] = useState(false)
  const host = useRef<HTMLDivElement>(null), viewer = useRef<Viewer | null>(null)
  const safeUrl = safePartPreviewUrl(previewUrl), url = safeUrl && SOURCE_PART.test(safeUrl) ? safeUrl : null
  const current = data?.url === url ? data : null

  useEffect(() => {
    const abort = new AbortController()
    setData(null); setError(''); setReady(false); setLoading(open && !!url)
    if (!open || !url) return () => abort.abort()
    void (async () => {
      try {
        // This existing owner/staff-bound endpoint contains the complete one-file
        // source geometry. Do not reuse another line/file or cache across owners.
        const response = await fetch(url, { signal: abort.signal, credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: { 'X-Workbench-Request': '1' } })
        if (!response.ok) throw new Error('原图暂未载入，请刷新订单或重试。')
        const source = assemblyPayload(await response.json(), url)
        if (!abort.signal.aborted) setData(source)
      } catch (failure) { if (!abort.signal.aborted) setError(failure instanceof Error ? failure.message : '原图暂未载入。') }
      finally { if (!abort.signal.aborted) setLoading(false) }
    })()
    return () => abort.abort()
  }, [url, open, reload, refreshKey])

  useEffect(() => {
    const element = host.current
    if (!open || !element) { setStageVisible(false); return }
    const update = () => setStageVisible(!document.hidden && element.clientWidth > 0 && element.clientHeight > 0)
    const observer = new ResizeObserver(update)
    observer.observe(element); document.addEventListener('visibilitychange', update); update()
    return () => { observer.disconnect(); document.removeEventListener('visibilitychange', update) }
  }, [open])

  const renderable = open && active && stageVisible && !!current && !error
  useEffect(() => {
    setReady(false)
    if (!renderable || !current || !host.current) return
    const abort = new AbortController(); let instance: Viewer | null = null, canvas: HTMLCanvasElement | null = null
    const contextLost = () => {
      if (abort.signal.aborted) return
      setReady(false); setError('原图三维显示已中断，请重新载入。')
      if (viewer.current === instance) viewer.current = null
    }
    void (async () => {
      try {
        const moduleUrl = '/workbench/cad-preview/step.js'
        const module = await import(/* webpackIgnore: true */ moduleUrl)
        if (abort.signal.aborted || !host.current) return
        const groups = current.geometry.solids.map((solid, index) => ({ id: `source-entity-${index}`, name: solid.name || solid.id, solid_ids: [solid.id] }))
        instance = await module.mountGeometry({ element: host.current, geometry: current.geometry, groups, upAxis: current.upAxis,
          componentColors: true, signal: abort.signal,
          // All source entities stay opaque and retain their own colors.
          onSelectionChange: (id: string | null) => { if (id !== null) instance?.select(null) },
        })
        if (abort.signal.aborted) { instance?.dispose(); return }
        canvas = host.current?.querySelector('canvas') || null
        canvas?.addEventListener('webglcontextlost', contextLost)
        instance?.select(null); viewer.current = instance; setReady(true)
      } catch (failure) {
        if (!abort.signal.aborted) setError(failure instanceof Error ? failure.message : '原图暂未显示，请重试。')
      }
    })()
    return () => { abort.abort(); canvas?.removeEventListener('webglcontextlost', contextLost); instance?.dispose(); if (viewer.current === instance) viewer.current = null }
  }, [current, renderable])

  const shown = renderable ? current?.geometry.solids || [] : []
  const complete = ready && renderable && !error
  return <section className="sap" data-testid="customer-original-assembly" aria-label="原始总装图" data-state={!open ? 'collapsed' : !url ? 'unavailable' : error ? 'error' : complete ? 'ready' : !active ? 'paused' : 'loading'}>
    <div className="sap-heading"><div className="sap-title"><span className="sap-icon"><Layers3 size={19} /></span><div><p>ORIGINAL ASSEMBLY</p><h3>原始总装（STEP）</h3></div></div><button type="button" className="sap-toggle" data-testid="customer-original-toggle" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? <><ChevronUp size={14} />收起原图</> : <><ChevronDown size={14} />展开原图</>}</button></div>
    <div className="sap-source"><span data-testid="customer-original-source" title={sourceName}>{sourceName || '当前零件来源 STEP'}</span>{current && <span className="sap-count" data-testid="customer-original-solid-count">{current.geometry.solids.length} 个原生实体</span>}</div>
    {open && <><div className="sap-toolbar"><span>原始装配位置</span><button type="button" data-testid="customer-original-reset" disabled={!complete} onClick={() => viewer.current?.reset()}><RotateCcw size={13} />复位视角</button></div><div className="sap-stage"><div ref={host} className="sap-canvas" data-testid="customer-original-canvas" data-ready={complete ? 'true' : 'false'} data-renderer-active={complete ? 'true' : 'false'} data-preview-url={current?.url || ''} data-source-sha256={current?.geometry.source_sha256 || ''} data-output-unit={current?.geometry.output_unit || ''} data-visible-solid-count={shown.length} data-displayed-solid-ids={JSON.stringify(shown.map(solid => solid.id))} data-highlighted-solid-ids="[]" />{!url ? <div className="sap-message"><Layers3 size={26} /><strong>当前零件的原图暂不可用</strong><span>可继续查看下方已保存的拆件资料。</span></div> : error ? <div className="sap-message sap-error" role="alert" data-testid="customer-original-error"><strong>原图暂未显示</strong><span>{error}</span><button type="button" data-testid="customer-original-retry" onClick={() => setReload(value => value + 1)}>重新载入原图</button></div> : !active ? <div className="sap-message"><Layers3 size={26} /><span>查看单件时，原始总装已暂停。</span></div> : !complete && <div className="sap-message" role="status"><LoaderCircle size={23} className="sap-spin" /><span>{loading ? '正在读取原始总装' : '正在准备原始总装视图'}</span></div>}</div><p className="sap-note">拖动旋转 · 滚轮缩放 · 保留原始装配位置，可与下方拆件对照。</p></>}
  </section>
}
