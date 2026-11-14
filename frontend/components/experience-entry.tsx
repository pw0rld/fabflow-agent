'use client'

import { useEffect, useState } from 'react'
import CustomerTrial from '@/components/customer-trial'
import PublicCadExamples from '@/components/public-cad-examples'

// Public visits never initialize a customer session or restore a saved order.
// Private browser-owned results are opened only through the explicit upload link.
export default function ExperienceEntry() {
  const [mode, setMode] = useState<'checking' | 'public' | 'upload'>('checking')
  useEffect(() => {
    const query = new URLSearchParams(window.location.search)
    setMode(query.getAll('mode').length === 1 && query.get('mode') === 'upload' ? 'upload' : 'public')
  }, [])
  if (mode === 'checking') return <div className="pce-entry-loading" role="status">正在打开 WillFlow.AI 体验</div>
  return mode === 'upload' ? <CustomerTrial /> : <PublicCadExamples />
}
