import type { Metadata } from 'next'
import TrialLeads from '@/components/trial-leads'

export const metadata: Metadata = {
  title: 'FabFlow · 体验线索',
  description: '内部体验申请与销售跟进工作台。',
  robots: { index: false, follow: false },
}

export default function LeadsPage() { return <TrialLeads /> }
