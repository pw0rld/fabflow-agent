import type { Metadata } from 'next'
import CustomerTrial from '@/components/customer-trial'

export const metadata: Metadata = {
  title: 'FabFlow · 在线制造报价体验',
  description: '上传 STEP 模型，查看原生装配、零件与工程参数，获取客户报价草稿。',
  robots: { index: false, follow: false },
}

export default function ExperiencePage() { return <CustomerTrial /> }
