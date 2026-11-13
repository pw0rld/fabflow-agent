'use client'

import { useEffect, useRef, useState } from 'react'
import { Box, Boxes, Check, ChevronRight, Download, Eye, EyeOff, FileBox, FolderOpen, LoaderCircle, RotateCcw, Upload, X } from 'lucide-react'
import './cad-workbench.css'
import CadManufacturingPanel from './cad-manufacturing-panel'

type CadNode = { id: string; parent_id: string | null; name: string; mesh_indices: number[]; children: string[] }
type CadAssembly = {
  schema_version: number; source_sha256: string; root_id: string
  hierarchy_origin?: 'source' | 'generated'; warnings?: string[]
  importer: { name: string; version: string; output_unit: string }
  nodes: CadNode[]; meshes: { id: string; index: number; name: string; vertex_count: number; triangle_count: number }[]
  stats: { mesh_count: number; triangle_count: number; vertex_count: number }
}
type CadView = { selected_node_id: string | null; isolated_node_id: string | null; hidden_node_ids: string[] }
type CadDocument = {
  id: string; name: string; sha256: string; size: number; created_at: string; updated_at: string; revision: number
  snapshot: CadAssembly | null; view: CadView; file_url: string
}
type CadViewer = {
  assembly?: CadAssembly; dispose: () => void; reset: () => void
  select: (id: string | null) => void; isolate: (id: string | null) => void
  setHidden: (id: string, hidden: boolean) => void; restoreVisibility: () => void
  getViewState: () => CadView; applyViewState: (state: CadView) => void
}
export type CadEntryAction = { id: string; type: 'file' | 'sample'; file?: File }
export type CadWorkflowState = {
  phase: 'idle' | 'processing' | 'review' | 'error'
  status: string; documentId: string | null; filename: string | null
  view: 'source' | 'manufacturing'; error: string | null
}
type CadWorkbenchProps = { embedded?: boolean; entryAction?: CadEntryAction | null; onWorkflowState?: (state: CadWorkflowState) => void }
const API = '/workbench/api/cad'
// The public sample's known camera orientation changes only the view, never its geometry or parsed structure.
const enclosureSha = '57a5ecf18133054f7498211d9120e7d101f1e7a0afb7a86e6de42ef473fbe935'
const emptyView = (): CadView => ({ selected_node_id: null, isolated_node_id: null, hidden_node_ids: [] })
const sizeOf = (bytes: number) => `${(bytes / 1024 / 1024).toFixed(2)} MB`
const dateOf = (value: string) => new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
const messageOf = (error: unknown) => error instanceof Error ? error.message : '暂时无法完成操作，请重试。'

async function api<T>(url: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(url, { ...options, headers: { 'X-Workbench-Request': '1', ...options.headers } })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.error?.message || `请求未完成（${response.status}）`)
  return payload as T
}

export default function CadWorkbench({ embedded = false, entryAction = null, onWorkflowState }: CadWorkbenchProps = {}) {
  const [documents, setDocuments] = useState<CadDocument[]>([])
  const [document, setDocument] = useState<CadDocument | null>(null)
  const [assembly, setAssembly] = useState<CadAssembly | null>(null)
  const [view, setView] = useState<CadView>(emptyView)
  const [busy, setBusy] = useState(false), [ready, setReady] = useState(false)
  const [status, setStatus] = useState('选择 STEP 总装，或打开公开机箱样例。')
  const [error, setError] = useState(''), [saveStatus, setSaveStatus] = useState('')
  const [dragging, setDragging] = useState(false), [search, setSearch] = useState('')
  const [tab, setTab] = useState<'source' | 'manufacturing'>('source')
  const inputRef = useRef<HTMLInputElement>(null), viewportRef = useRef<HTMLDivElement>(null)
  const viewerRef = useRef<CadViewer | null>(null), documentRef = useRef<CadDocument | null>(null)
  const abortRef = useRef<AbortController | null>(null), generationRef = useRef(0)
  const readyRef = useRef(false), aliveRef = useRef(true)
  const queueRef = useRef<Promise<void>>(Promise.resolve()), lastQueuedView = useRef(''), queueNumber = useRef(0)
  const syncRef = useRef<() => void>(() => {})
  const deliveredAction = useRef<string | null>(null)

  function current(value: CadDocument) { documentRef.current = value; setDocument(value) }
  async function refreshDocuments() {
    const result = await api<{ documents: CadDocument[] }>(`${API}/documents`)
    if (aliveRef.current) setDocuments(result.documents)
  }
  function saveView(next: CadView) {
    const source = documentRef.current
    if (!source || !readyRef.current) return
    const signature = JSON.stringify(next)
    if (signature === lastQueuedView.current) return
    lastQueuedView.current = signature
    const requestNumber = ++queueNumber.current, generation = generationRef.current
    setSaveStatus('正在保存查看状态…')
    queueRef.current = queueRef.current.catch(() => {}).then(async () => {
      const latest = documentRef.current
      if (!aliveRef.current || generation !== generationRef.current || latest?.id !== source.id) return
      try {
        const result = await api<CadDocument>(`${API}/documents/${source.id}/view`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ expected_revision: latest.revision, view: next }),
        })
        if (aliveRef.current && generation === generationRef.current) {
          current(result)
          if (requestNumber === queueNumber.current) setSaveStatus('查看状态已保存')
        }
      } catch (failure) {
        if (aliveRef.current && generation === generationRef.current) {
          lastQueuedView.current = ''; setSaveStatus('查看状态未保存')
          setError(`${messageOf(failure)} 可以重新打开已保存的文件。`)
        }
      }
    })
  }
  syncRef.current = () => {
    if (!viewerRef.current) return
    const next = viewerRef.current.getViewState(); setView(next); saveView(next)
  }

  async function activate(source: CadDocument) {
    const generation = ++generationRef.current
    abortRef.current?.abort(); viewerRef.current?.dispose(); viewerRef.current = null
    const controller = new AbortController(); abortRef.current = controller
    readyRef.current = false; setReady(false); setBusy(true); setAssembly(null); setView(emptyView())
    setSearch(''); setError(''); setSaveStatus(''); setStatus('正在打开原始 STEP…'); current(source)
    const location = new URL(window.location.href); location.searchParams.set('document', source.id)
    window.history.replaceState({}, '', location)
    try {
      const response = await fetch(source.file_url, { signal: controller.signal })
      if (!response.ok) throw new Error('原文件暂时无法读取，请重新打开。')
      const bytes = await response.arrayBuffer()
      if (!aliveRef.current || generation !== generationRef.current) return
      const moduleUrl = '/workbench/cad-preview/step.js'
      const module = await import(/* webpackIgnore: true */ moduleUrl)
      if (!viewportRef.current || generation !== generationRef.current) return
      let parsed: CadAssembly | undefined
      const viewer: CadViewer = await module.mount({
        element: viewportRef.current, bytes, signal: controller.signal,
        upAxis: source.sha256 === enclosureSha ? 'y' : 'z',
        componentColors: true,
        status: (value: string) => { if (generation === generationRef.current) setStatus(value) },
        onAssembly: (value: CadAssembly) => { parsed = value },
        onSelectionChange: () => { if (readyRef.current) syncRef.current() },
      })
      if (!aliveRef.current || generation !== generationRef.current) { viewer.dispose(); return }
      viewerRef.current = viewer
      const result = parsed || viewer.assembly
      if (!result || result.source_sha256 !== source.sha256) throw new Error('解析结果与原文件不一致，请重新打开。')
      setAssembly(result)
      let saved = source
      if (JSON.stringify(result) !== JSON.stringify(source.snapshot)) {
        setStatus('正在保存部件结构…')
        saved = await api<CadDocument>(`${API}/documents/${source.id}/snapshot`, {
          method: 'POST', signal: controller.signal, headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ expected_revision: source.revision, snapshot: result }),
        })
      }
      if (!aliveRef.current || generation !== generationRef.current) return
      current(saved); viewer.applyViewState(saved.view || emptyView())
      const next = viewer.getViewState(); setView(next); lastQueuedView.current = JSON.stringify(next)
      readyRef.current = true; setReady(true); setSaveStatus('原件与部件结构已保存')
      setStatus('模型已就绪 · 点击左侧部件或直接点击模型'); await refreshDocuments()
    } catch (failure) {
      if (generation !== generationRef.current || !aliveRef.current || controller.signal.aborted) return
      viewerRef.current?.dispose(); viewerRef.current = null; setError(messageOf(failure))
      setStatus('文件未能完成解析，已保存的原件仍可重新打开。')
    } finally { if (aliveRef.current && generation === generationRef.current) setBusy(false) }
  }

  async function upload(file: File, eventId?: string) {
    if (busy) return
    setError('')
    if (!/\.(step|stp)$/i.test(file.name)) { setError('请选择 STEP 或 STP 三维总装文件。'); return }
    if (!file.size || file.size > 20 * 1024 * 1024) { setError('请选择非空、大小不超过 20 MB 的 STEP 文件。'); return }
    setTab('source'); setBusy(true); setStatus('正在保存原始文件…')
    try {
      const header = await file.slice(0, 256).text()
      if (!aliveRef.current) return
      if (!/^\s*ISO-10303-21\s*;/i.test(header.replace(/^\uFEFF/, ''))) throw new Error('文件内容不是有效的 STEP 文本，请检查原文件。')
      await queueRef.current
      if (!aliveRef.current) return
      const saved = await api<CadDocument>(`${API}/documents?name=${encodeURIComponent(file.name)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Event-Id': eventId || crypto.randomUUID() }, body: file,
      })
      if (!aliveRef.current) return
      await refreshDocuments(); if (aliveRef.current) await activate(saved)
    } catch (failure) { if (aliveRef.current) { setError(messageOf(failure)); setStatus('上传未完成，已有文件保留。') } }
    finally { if (aliveRef.current) setBusy(false) }
  }
  async function sample(eventId?: string) {
    if (busy) return
    setTab('source'); setBusy(true); setError(''); setStatus('正在读取公开机箱样例…')
    try {
      const response = await fetch(`${API}/samples/enclosure/file`)
      if (!response.ok) throw new Error('此环境尚未安装公开机箱样例，可选择本地 STEP 文件。')
      const blob = await response.blob(); await queueRef.current
      if (!aliveRef.current) return
      const saved = await api<CadDocument>(`${API}/documents?name=Laser4DIY-Enclosure-v3-v9.step`, {
        method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Event-Id': eventId || crypto.randomUUID() }, body: blob,
      })
      if (!aliveRef.current) return
      await refreshDocuments(); if (aliveRef.current) await activate(saved)
    } catch (failure) { if (aliveRef.current) { setError(messageOf(failure)); setStatus('样例未能打开，可以重试。') } }
    finally { if (aliveRef.current) setBusy(false) }
  }
  async function reopen(id: string) {
    if (busy) return
    setBusy(true); setError('')
    try { await queueRef.current; await activate(await api<CadDocument>(`${API}/documents/${id}`)) }
    catch (failure) { setError(messageOf(failure)); setBusy(false) }
  }
  useEffect(() => {
    aliveRef.current = true
    refreshDocuments().catch(failure => { if (aliveRef.current) setError(messageOf(failure)) })
    const id = new URL(window.location.href).searchParams.get('document')
    if (new URL(window.location.href).searchParams.get('view') === 'manufacturing') setTab('manufacturing')
    if (id && !entryAction) void reopen(id)
    return () => { aliveRef.current = false; ++generationRef.current; abortRef.current?.abort(); viewerRef.current?.dispose() }
    // Entry is driven by the document query or explicit user actions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => {
    if (!embedded || !entryAction || deliveredAction.current === entryAction.id) return
    deliveredAction.current = entryAction.id
    if (entryAction.type === 'file' && entryAction.file) void upload(entryAction.file, entryAction.id)
    else if (entryAction.type === 'sample') void sample(entryAction.id)
    // A parent action is delivered once; progress or parent re-renders cannot upload again.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [embedded, entryAction?.id])
  useEffect(() => {
    onWorkflowState?.({ phase: error ? 'error' : busy ? 'processing' : ready ? 'review' : 'idle', status,
      documentId: document?.id || null, filename: document?.name || entryAction?.file?.name || null, view: tab, error: error || null })
  }, [onWorkflowState, busy, ready, status, error, document?.id, document?.name, tab, entryAction?.file?.name])

  const selected = assembly?.nodes.find(node => node.id === view.selected_node_id)
  const matches = assembly?.nodes.filter(node => !search || node.name.toLowerCase().includes(search.toLowerCase())) || []
  function action(run: (viewer: CadViewer) => void) {
    if (!readyRef.current || !viewerRef.current) return
    run(viewerRef.current); syncRef.current()
  }
  function descendantMeshes(node: CadNode): number {
    const indices = new Set<number>(), lookup = new Map(assembly?.nodes.map(item => [item.id, item]))
    const visit = (item: CadNode) => { item.mesh_indices.forEach(index => indices.add(index)); item.children.forEach(id => { const child = lookup.get(id); if (child) visit(child) }) }
    visit(node); return indices.size
  }
  function switchTab(next: 'source' | 'manufacturing') {
    setTab(next)
    const location = new URL(window.location.href)
    if (next === 'manufacturing') location.searchParams.set('view', next)
    else location.searchParams.delete('view')
    window.history.replaceState({}, '', location)
  }
  const Container = embedded ? 'section' : 'main'
  return <Container className={`cad-app${embedded ? ' cad-embedded' : ''}`} data-embedded={embedded ? 'true' : undefined}>
    <header className="cad-topbar"><a className="cad-brand" href="/workbench/"><span><Boxes size={20} /></span>好运来 <small>工程拆图工作台</small></a><nav><a href="/workbench/">工序演示</a><strong>真实 CAD 拆件</strong></nav></header>
    <section className="cad-heading"><div><p className="cad-eyebrow">STEP ASSEMBLY</p><h1>从总装开始，看清每一个部件</h1><p>上传三维总装，查看原有装配结构，并选中、单独查看具体部件。</p></div><div className="cad-heading-actions"><button className="cad-button" onClick={() => inputRef.current?.click()} disabled={busy}><Upload size={16} />上传 STEP</button><button className="cad-button cad-primary" data-testid="cad-sample" onClick={() => void sample()} disabled={busy}><FileBox size={16} />打开机箱样例</button></div><input ref={inputRef} data-testid="cad-file-input" type="file" accept=".step,.stp" hidden onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void upload(file) }} /></section>
    {error && <div className="cad-error" role="alert" data-testid="cad-errors"><span>{error}</span><button aria-label="关闭错误提示" onClick={() => setError('')}><X size={16} /></button></div>}
    {!document && <section className={`cad-intro ${dragging ? 'cad-dragging' : ''}`} onDragOver={event => { event.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); const file = event.dataTransfer.files[0]; if (file) void upload(file) }}><div className="cad-upload-icon"><Upload size={28} /></div><h2>拖入 STEP 总装文件</h2><p>支持 .step / .stp · 最大 20 MB</p><button className="cad-button" disabled={busy} onClick={() => inputRef.current?.click()}>选择文件</button><div className="cad-sample-note">公开样例：Laser4DIY 钣金机箱，包含箱体、盖板、隔板和连接件。<br />直接解析原始 CAD，显示模型已有的部件。</div></section>}
    <div className="cad-status-row" role="status" data-testid="cad-status">{busy ? <LoaderCircle size={15} className="cad-spin" /> : ready ? <Check size={15} /> : <FileBox size={15} />}<span>{status}</span>{saveStatus && <small>{saveStatus}</small>}</div>
    {document && <div className="cad-workflow-tabs" role="tablist" aria-label="CAD 工作阶段">
      <button type="button" role="tab" data-testid="mf-source-tab" aria-selected={tab === 'source'} onClick={() => switchTab('source')}>原装配查看</button>
      <button type="button" role="tab" data-testid="mf-tab" aria-selected={tab === 'manufacturing'} disabled={busy} onClick={() => switchTab('manufacturing')}>制造零件与物料表</button>
    </div>}
    <div style={{ display: tab === 'source' ? 'block' : 'none' }}>
    {document && <section className="cad-workspace">
      <aside className="cad-tree-panel">
        <div className="cad-panel-heading"><Boxes size={17} /><h2>{assembly?.hierarchy_origin === 'generated' ? '待归组几何' : '原装配结构'}</h2>{assembly && <span>{assembly.nodes.length} 个节点</span>}</div>
        <div className="cad-source-name" data-testid="cad-document-name">{document.name}</div>
        <input className="cad-search" aria-label="查找部件" placeholder="查找部件名称" value={search} onChange={event => setSearch(event.target.value)} disabled={!ready} />
        <div className="cad-tree" role="tree" aria-label="CAD 原装配结构" data-testid="cad-tree">
          {matches.map(node => <button key={node.id} role="treeitem" aria-level={node.id.split('-').length - 1} aria-selected={view.selected_node_id === node.id} className={`cad-node ${view.selected_node_id === node.id ? 'cad-node-selected' : ''}`} data-testid={`cad-node-${node.id}`} title={node.name} disabled={!ready} style={{ paddingLeft: `${12 + Math.min(node.id.split('-').length - 2, 8) * 14}px` }} onClick={() => action(viewer => viewer.select(node.id))}>
            {node.children.length ? <Boxes size={15} /> : <Box size={15} />}<span>{node.name || '未命名节点'}<small>{node.id}{view.hidden_node_ids.includes(node.id) ? ' · 已隐藏' : ''}</small></span>{view.selected_node_id === node.id && <ChevronRight size={14} />}
          </button>)}
          {!assembly && <p className="cad-muted">{busy ? '正在读取装配结构…' : '尚无可显示的结构。'}</p>}
          {assembly && !matches.length && <p className="cad-muted">没有匹配的部件。</p>}
        </div><p className="cad-tree-note">节点与显示网格不代表制造零件数量。</p>
        {!!assembly?.warnings?.length && <div className="cad-structure-note">{assembly.warnings.map((warning, index) => <p key={index}>{warning}</p>)}</div>}
      </aside>
      <div className="cad-view-panel">
        <div className="cad-view-toolbar"><span><span className="cad-dot" />{view.isolated_node_id ? '单独查看' : '总装视图'}</span><div>
          <button className="cad-icon-button" title="适配视图" aria-label="适配视图" disabled={!ready} onClick={() => viewerRef.current?.reset()}><RotateCcw size={16} /></button>
          <button className="cad-button cad-small" data-testid="cad-show-all" disabled={!ready} onClick={() => action(viewer => { viewer.restoreVisibility(); viewer.select(null) })}>显示全部</button>
        </div></div>
        <div className="cad-viewport" ref={viewportRef} data-testid="cad-viewport" aria-label="三维总装模型" />
        {busy && <div className="cad-viewport-loading"><LoaderCircle className="cad-spin" size={22} /><p>{status}</p></div>}
        <div className="cad-view-footer"><span>拖动旋转 · 滚轮缩放 · 右键平移</span><span className="cad-selection-key"><i />选中部件橙色，其余淡显</span></div>
      </div>
      <aside className="cad-details">
        <p className="cad-eyebrow">COMPONENT</p><h2 data-testid="cad-selected-name">{selected?.name || '选择一个部件'}</h2><span className="cad-derived">{assembly?.hierarchy_origin === 'generated' ? '显示几何 · 待归组' : '原装配部件 · 待工程确认'}</span>
        {selected ? <>
          <div className="cad-detail-actions">
            <button className="cad-button cad-primary" data-testid="cad-isolate" disabled={!ready} onClick={() => action(viewer => viewer.isolate(selected.id))}><Eye size={16} />单独查看</button>
            <button className="cad-button" disabled={!ready} onClick={() => action(viewer => viewer.setHidden(selected.id, !view.hidden_node_ids.includes(selected.id)))}><EyeOff size={16} />{view.hidden_node_ids.includes(selected.id) ? '取消隐藏' : '隐藏部件'}</button>
          </div>
          <dl><dt>原始名称</dt><dd>{selected.name || '原件未命名'}</dd><dt>节点编号</dt><dd>{selected.id}</dd><dt>父节点</dt><dd>{assembly?.nodes.find(node => node.id === selected.parent_id)?.name || '总装根节点'}</dd><dt>直接子项</dt><dd>{selected.children.length}</dd><dt>关联显示网格</dt><dd>{descendantMeshes(selected)}</dd></dl>
        </> : <p className="cad-muted">在左侧列表或三维模型中选择，查看其原始归属。</p>}
        <div className="cad-details-note">本轮保留原有装配结构。材料、板厚、制造拆分及工艺将在后续步骤确认。</div>
        <details className="cad-file-details"><summary>原文件与解析记录</summary>
          <dl><dt>文件大小</dt><dd>{sizeOf(document.size)}</dd><dt>文件指纹 SHA-256</dt><dd className="cad-fingerprint">{document.sha256}</dd>
            {assembly && <><dt>导入组件</dt><dd>{assembly.importer.name} {assembly.importer.version}</dd><dt>预览输出坐标单位</dt><dd>{assembly.importer.output_unit}（原文件单位待工程核对）</dd></>}
          </dl><a href={document.file_url} className="cad-download"><Download size={14} />下载原始 STEP</a>
        </details>
        <button className="cad-button cad-small" disabled={busy} onClick={() => void reopen(document.id)}><RotateCcw size={14} />重新打开已保存文件</button>
      </aside>
    </section>}
    </div>
    {document && <div style={{ display: tab === 'manufacturing' ? 'block' : 'none' }}><CadManufacturingPanel key={document.id} documentId={document.id} sourceSha={document.sha256} upAxis={document.sha256 === enclosureSha ? 'y' : 'z'} /></div>}
    <section className="cad-history"><div className="cad-history-heading"><h2><FolderOpen size={18} />已保存的 CAD</h2><span>保留原件和部件结构，可继续查看</span></div><div className="cad-document-list" data-testid="cad-document-list">{documents.map(item => <button key={item.id} className={`cad-document ${document?.id === item.id ? 'cad-document-active' : ''}`} disabled={busy} onClick={() => void reopen(item.id)}><FileBox size={20} /><span><strong>{item.name}</strong><small>{sizeOf(item.size)} · {dateOf(item.updated_at || item.created_at)}</small></span><ChevronRight size={17} /></button>)}{!documents.length && <p className="cad-muted">尚无已保存文件。</p>}</div></section>
    <footer className="cad-footer">公开样例来自 <a href="https://github.com/Laser4DIY/Laser4DIY_Enclosure" target="_blank" rel="noreferrer">FabLab München / Laser4DIY</a> · CERN-OHL-P v2 · 本轮完成原装配读取与部件查看</footer>
  </Container>
}
