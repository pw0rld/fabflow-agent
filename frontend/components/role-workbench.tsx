'use client'

import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Box, FolderOpen, Layers3, LoaderCircle, LogIn, Plus, RefreshCw, ShieldCheck, Wrench } from 'lucide-react'
import CustomerUploadPortal from './customer-upload-portal'
import BatchQuotationWorkbench from './batch-quotation-workbench'
import UnifiedWorkbench from './unified-workbench'
import type { QuoteScope } from '@/lib/batch-quote-scope'
import './role-workbench.css'

const IDENTITY = '/ortho2cad-test/customer-upload-admin/api/identity'
const LOGIN = '/ortho2cad-test/customer-upload-admin/api/login'
type Role = 'customer' | 'staff'
type Workspace = 'inquiry' | 'orders' | 'engineering'
type Identity = { role: Role; capabilities: string[] }
type Route = { workspace: Workspace; scope: QuoteScope; id?: string | null }
const validBatch = (value: unknown): value is string => typeof value === 'string' && /^BATCH-[a-f0-9]{32}$/.test(value)
const validWorkspace = (value: string | null): value is Workspace => value === 'inquiry' || value === 'orders' || value === 'engineering'
const validScope = (value: string | null): value is QuoteScope => value === 'customer' || value === 'public' || value === 'poland'
const routeFromLocation = (role: Role): Route => {
  const params = new URL(window.location.href).searchParams
  const raw = params.get('workspace'), flow = params.get('flow')
  const workspace = validWorkspace(raw) ? raw : flow === 'step' || flow === 'image' ? 'engineering'
    : role === 'staff' ? 'orders' : 'inquiry'
  return { workspace, scope: validScope(params.get('scope')) ? params.get('scope') as QuoteScope : 'customer', id: validBatch(params.get('id')) ? params.get('id') : undefined }
}
function engineeringQuery(search: string): URLSearchParams {
  const params = new URLSearchParams(search), clean = new URLSearchParams()
  const rules: Record<string, RegExp> = { flow: /^(step|image)$/, document: /^CAD-[a-f0-9]{32}$/, job: /^SPL-[a-f0-9]{32}$/, sample: /^(step|image)$/, view: /^(source|manufacturing)$/ }
  for (const [key, pattern] of Object.entries(rules)) if (params.getAll(key).length === 1 && pattern.test(params.get(key) || '')) clean.set(key, params.get(key)!)
  return clean
}
function canonicalUrl(route: Route, engine = new URLSearchParams()): string {
  const params = new URLSearchParams({ workspace: route.workspace })
  if (route.workspace === 'orders') params.set('scope', route.scope)
  if (route.workspace !== 'engineering' && validBatch(route.id)) params.set('id', route.id)
  if (route.workspace === 'engineering') engine.forEach((value, key) => params.set(key, value))
  return `/workbench/?${params.toString()}`
}

export default function RoleWorkbench() {
  const [identity, setIdentity] = useState<Identity | null>(null), [identityError, setIdentityError] = useState(''), [reload, setReload] = useState(0)
  const [route, setRoute] = useState<Route>({ workspace: 'inquiry', scope: 'customer' })
  const [visited, setVisited] = useState<Set<Workspace>>(new Set())
  const [inquiryId, setInquiryId] = useState<string | null | undefined>(undefined)
  const [order, setOrder] = useState<{ scope: QuoteScope; id?: string | null }>({ scope: 'customer' })
  const [loginNext, setLoginNext] = useState('/workbench/?workspace=orders')
  const currentRoute = useRef(route), engineLocation = useRef(new URLSearchParams())
  currentRoute.current = route
  const staff = identity?.role === 'staff'
  const canOrders = !!staff && identity?.capabilities.includes('staff_orders') === true
  const canEngineering = !!staff && (identity?.capabilities.includes('step_engineering') === true || identity?.capabilities.includes('image_recognition') === true)
  const permitted = (workspace: Workspace) => workspace === 'inquiry' || workspace === 'orders' && canOrders || workspace === 'engineering' && canEngineering
  const visibleWorkspace = permitted(route.workspace) ? route.workspace : 'inquiry'

  useEffect(() => {
    const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 15000)
    let active = true
    setIdentityError('')
    void fetch(IDENTITY, { credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: abort.signal, headers: { 'X-Workbench-Request': '1' } }).then(async response => {
      if (!response.ok) throw new Error('暂未读取到工作台身份，请重新连接。')
      const value = await response.json() as Identity
      if (!value || !['customer', 'staff'].includes(value.role) || !Array.isArray(value.capabilities) || !value.capabilities.every(item => typeof item === 'string')) throw new Error('身份信息暂不可用，请重新连接。')
      if (!active || abort.signal.aborted) return
      const next = routeFromLocation(value.role)
      engineLocation.current = engineeringQuery(window.location.search)
      setLoginNext(canonicalUrl(next, engineLocation.current))
      setIdentity(value); setRoute(next)
      const allowed = next.workspace === 'inquiry' || value.role === 'staff' && (next.workspace === 'orders' ? value.capabilities.includes('staff_orders') : value.capabilities.includes('step_engineering') || value.capabilities.includes('image_recognition'))
      setVisited(new Set([allowed ? next.workspace : 'inquiry']))
      if (next.workspace === 'inquiry') setInquiryId(next.id)
      else if (next.workspace === 'orders' && value.role === 'staff') setOrder({ scope: next.scope, id: next.id })
    }).catch(failure => { if (!active) return; if (!abort.signal.aborted) setIdentityError(failure instanceof Error ? failure.message : '工作台连接暂未完成。'); else setIdentityError('身份连接超时，请重新连接。') }).finally(() => clearTimeout(timer))
    return () => { active = false; abort.abort(); clearTimeout(timer) }
  }, [reload])

  useEffect(() => {
    if (!identity) return
    const receive = () => {
      const next = routeFromLocation(identity.role)
      if (currentRoute.current.workspace === 'engineering') engineLocation.current = engineeringQuery(window.location.search)
      setRoute(next); setLoginNext(canonicalUrl(next, engineeringQuery(window.location.search)))
      if (next.workspace === 'inquiry') setInquiryId(next.id)
      if (next.workspace === 'orders' && canOrders) setOrder({ scope: next.scope, id: next.id })
      setVisited(previous => new Set([...previous, permitted(next.workspace) ? next.workspace : 'inquiry']))
    }
    window.addEventListener('popstate', receive)
    return () => window.removeEventListener('popstate', receive)
  }, [identity, canOrders, canEngineering])

  function navigate(next: Route, replace = false) {
    if (!permitted(next.workspace)) return
    if (currentRoute.current.workspace === 'engineering') engineLocation.current = engineeringQuery(window.location.search)
    const url = canonicalUrl(next, engineLocation.current)
    if (replace) window.history.replaceState({}, '', url); else window.history.pushState({}, '', url)
    currentRoute.current = next; setRoute(next); setLoginNext(url)
    setVisited(previous => new Set([...previous, next.workspace]))
    if (next.workspace === 'inquiry') setInquiryId(next.id)
    if (next.workspace === 'orders') setOrder({ scope: next.scope, id: next.id })
  }
  function newInquiry() { navigate({ workspace: 'inquiry', scope: 'customer', id: null }) }
  function selectInquiry(id: string | null) {
    setInquiryId(id)
    if (currentRoute.current.workspace === 'inquiry') navigate({ workspace: 'inquiry', scope: 'customer', id }, true)
  }
  function manageOrder(id: string) { if (canOrders && validBatch(id)) navigate({ workspace: 'orders', scope: 'customer', id }) }
  function selectBatch(id: string, scope: QuoteScope) {
    if (!canOrders || !validBatch(id)) return
    setOrder({ scope, id })
    if (currentRoute.current.workspace === 'orders') navigate({ workspace: 'orders', scope, id }, true)
  }

  return <main className="role-workbench" data-testid="role-workbench" data-role={identity?.role || 'loading'}>
    <header className="rw-header"><div className="rw-header-inner"><a href="/workbench/" className="rw-brand" onClick={event => { if (identity) { event.preventDefault(); navigate({ workspace: 'inquiry', scope: 'customer', id: inquiryId }) } }}><span><Layers3 size={22} /></span><div><strong>好运来</strong><small>拆件与报价工作台</small></div></a><div className="rw-header-actions"><span className={`rw-role${staff ? ' staff' : ''}`} data-testid="workbench-role"><ShieldCheck size={13} />{identity ? staff ? '工厂工作台' : '客户空间' : '正在连接'}</span>{identity?.role === 'customer' && <a className="rw-login" href={`${LOGIN}?next=${encodeURIComponent(loginNext)}`} data-testid="workbench-staff-login"><LogIn size={14} />工厂登录</a>}{staff && <button type="button" className="rw-new" onClick={newInquiry} data-testid="workbench-new-inquiry"><Plus size={14} />上传订单</button>}</div></div></header>
    {identity && <nav className="rw-nav" aria-label="工作台导航"><div><button type="button" onClick={() => navigate({ workspace: 'inquiry', scope: 'customer', id: inquiryId })} aria-current={visibleWorkspace === 'inquiry' ? 'page' : undefined} data-testid="workspace-inquiry"><Box size={16} />上传与拆件<span>提交资料 · 我的询价</span></button>{canOrders && <button type="button" onClick={() => navigate({ workspace: 'orders', ...order })} aria-current={visibleWorkspace === 'orders' ? 'page' : undefined} data-testid="workspace-orders"><FolderOpen size={16} />订单与核价<span>客户订单 · 内部核价</span></button>}{canEngineering && <button type="button" className="rw-engineering-tab" onClick={() => navigate({ workspace: 'engineering', scope: 'public' })} aria-current={visibleWorkspace === 'engineering' ? 'page' : undefined} data-testid="workspace-engineering"><Wrench size={15} />工程工具<span>STEP · 图片</span></button>}</div></nav>}
    {!identity && <section className="rw-loading" role="status" data-testid="workbench-identity-loading">{identityError ? <><ShieldCheck size={27} /><h1>暂未连接工作台</h1><p>{identityError}</p><button type="button" onClick={() => setReload(value => value + 1)}><RefreshCw size={14} />重新连接</button></> : <><LoaderCircle size={25} className="rw-spin" /><p>正在连接你的工作台…</p></>}</section>}
    {identityError && identity && <div className="rw-notice" role="status">{identityError}</div>}
    {identity && !permitted(route.workspace) && <div className="rw-notice" data-testid="workbench-staff-required">该工作区需要工厂身份。你仍可在下方提交资料并查看自己的拆件与报价。<a href={`${LOGIN}?next=${encodeURIComponent(loginNext)}`}>工厂登录<ArrowRight size={12} /></a></div>}
    {identity && visited.has('inquiry') && <div hidden={visibleWorkspace !== 'inquiry'} data-testid="workbench-inquiry-view"><CustomerUploadPortal embedded initialOrderId={inquiryId} onOrderSelected={selectInquiry} onManageOrder={canOrders ? manageOrder : undefined} /></div>}
    {canOrders && visited.has('orders') && <div hidden={visibleWorkspace !== 'orders'} data-testid="workbench-orders-view"><div className="rw-order-scope" role="group" aria-label="订单来源"><span>工厂内部 · </span><button type="button" aria-pressed={order.scope === 'customer'} onClick={() => navigate({ workspace: 'orders', scope: 'customer', id: null })} data-testid="workbench-order-scope-customer">客户询价</button><button type="button" aria-pressed={order.scope === 'public'} onClick={() => navigate({ workspace: 'orders', scope: 'public', id: null })} data-testid="workbench-order-scope-public">工程订单</button>{order.scope === 'poland' && <span className="rw-private-case">波兰案例</span>}</div><BatchQuotationWorkbench key={order.scope} embedded forcedScope={order.scope} initialBatchId={order.id} onNewOrder={newInquiry} onBatchSelected={selectBatch} /></div>}
    {canEngineering && visited.has('engineering') && <div hidden={visibleWorkspace !== 'engineering'} data-testid="workbench-engineering-view"><div className="rw-engineering-note"><Wrench size={15} /><span>单份 STEP 与图片的工程识别工具。订单询价可使用“上传与拆件”，复用已有资料进入核价。</span></div><UnifiedWorkbench embedded onOpenQuotation={id => navigate({ workspace: 'orders', scope: 'public', id: id || null })} /></div>}
  </main>
}
