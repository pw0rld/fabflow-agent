import type { Metadata } from 'next'
import CustomerPartPage from '@/components/customer-part-page'

export const metadata: Metadata = {
  title: '好运来 · 零件与工艺',
  description: '查看本次客户报价对应的零件、原生三维及工艺图。',
  robots: { index: false, follow: false },
}

export default function CustomerPartRoute() { return <CustomerPartPage /> }
