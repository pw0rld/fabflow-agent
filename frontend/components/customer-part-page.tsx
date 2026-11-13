'use client'

import { useEffect, useState } from 'react'
import { ArrowLeft, Box, FileText, LoaderCircle, ShieldCheck } from 'lucide-react'
import CustomerManufacturingPanel from '@/components/customer-manufacturing-panel'
import './customer-part-page.css'

type Selection = { order: string; run: string; part: string }
function selection(search: string): Selection {
  const params = new URLSearchParams(search)
  const keys = [...params.keys()]
  if (keys.length !== 3 || keys.some(key => !['order', 'run', 'part'].includes(key))) throw new Error('零件链接不完整，请从客户报价单重新打开。')
  const order = params.get('order') || '', run = params.get('run') || '', part = params.get('part') || ''
  if (!/^BATCH-[a-f0-9]{32}$/.test(order) || !/^RUN-[a-f0-9]{32}$/.test(run) || !/^L[0-9]{4,8}$/.test(part)) throw new Error('零件链接无效，请从客户报价单重新打开。')
  return { order, run, part }
}

export default function CustomerPartPage() {
  const [current, setCurrent] = useState<Selection | null>(null), [error, setError] = useState('')
  useEffect(() => {
    try { setCurrent(selection(window.location.search)) }
    catch (failure) { setError(failure instanceof Error ? failure.message : '零件链接暂不可用。') }
  }, [])
  const quote = current ? `/workbench/customer/api/orders/${current.order}/runs/${current.run}/customer-quote.html` : null
  return <main className="cpp-page" data-testid="customer-part-page">
    <header className="cpp-header"><div className="cpp-header-inner"><a href="/workbench/customer/" className="cpp-brand"><span><Box size={23} strokeWidth={1.6} /></span><strong>好运来</strong><small>客户询价</small></a><span className="cpp-private"><ShieldCheck size={15} />专属订单资料</span></div></header>
    <div className="cpp-shell"><nav className="cpp-navigation">{quote ? <a href={quote} data-testid="customer-part-backquote"><ArrowLeft size={15} />返回本次报价单</a> : <a href="/workbench/customer/"><ArrowLeft size={15} />返回我的询价</a>}<a href="/workbench/customer/">我的询价<FileText size={14} /></a></nav>
      <section className="cpp-title"><span>PARTS &amp; PROCESS</span><h1>零件与工艺</h1><p>查看本次报价对应的原生零件、工艺图和加工工程量。</p></section>
      {error ? <div className="cpp-message cpp-error" role="alert" data-testid="customer-part-page-error">{error}</div> : current ? <CustomerManufacturingPanel key={`${current.order}/${current.run}/${current.part}`} batchId={current.order} runId={current.run} revision={0} initialPartId={current.part} /> : <div className="cpp-message" role="status"><LoaderCircle size={18} />正在打开报价对应的零件</div>}
      <footer className="cpp-footer">好运来 · 原生拆件与工艺资料 · 待工程审核</footer>
    </div>
  </main>
}
