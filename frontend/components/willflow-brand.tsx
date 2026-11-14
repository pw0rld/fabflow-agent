'use client'

import { useId } from 'react'
import './willflow-brand.css'

export default function WillFlowBrand({ className = '', href = 'https://willflowai.com/' }: { className?: string; href?: string }) {
  const gradient = 'wf-brand-' + useId().replace(/[^A-Za-z0-9_-]/g, '')
  return <a className={`wf-brand ${className}`} href={href} aria-label="意志流动 WillFlow.AI 官网" data-testid="willflow-brand">
    <svg className="wf-brand-mark" viewBox="0 0 64 64" aria-hidden="true"><defs><linearGradient id={gradient} x2="1" y2="1"><stop stopColor="#22d3ee"/><stop offset="1" stopColor="#8b5cf6"/></linearGradient></defs><rect width="64" height="64" rx="16" fill="#0b1020" stroke={`url(#${gradient})`} strokeOpacity=".5"/><path d="m12 22 9 22 11-22 10 22 10-22" fill="none" stroke={`url(#${gradient})`} strokeWidth="5" strokeLinejoin="round" strokeLinecap="round"/></svg>
    <strong className="wf-brand-text">意志流动<small>WILLFLOW.AI</small></strong>
  </a>
}
