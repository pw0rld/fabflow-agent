'use client'

import { useEffect, useRef } from 'react'
import { ASSEMBLY_MARKUP } from '@/lib/assembly-markup'
import { mountAssembly } from '@/lib/assembly-controller'
import './assembly-workbench.css'
import './process-demo.css'

export default function AssemblyWorkbench() {
  const host = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const root = host.current?.querySelector<HTMLElement>('#hy-assembly-v3')
    if (!root) return
    return mountAssembly(root)
  }, [])

  // The markup is a fixed, reviewed build artifact. It contains no uploads or
  // model-generated content; event handlers live in the bundled controller.
  return <div ref={host} dangerouslySetInnerHTML={{ __html: ASSEMBLY_MARKUP }} />
}
