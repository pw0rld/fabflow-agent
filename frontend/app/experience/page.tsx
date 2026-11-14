import type { Metadata } from 'next'
import ExperienceEntry from '@/components/experience-entry'

export const metadata: Metadata = {
  title: 'FabFlow · 从公开 CAD 体验拆件与报价',
  description: '查看公开 CAD 示例的原生模型、零件 BOM 与模拟报价，或上传自己的 STEP 体验。',
  robots: { index: false, follow: false },
}

export default function ExperiencePage() { return <ExperienceEntry /> }
