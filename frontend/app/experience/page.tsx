import type { Metadata, Viewport } from 'next'
import ExperienceEntry from '@/components/experience-entry'

export const metadata: Metadata = {
  title: '意志流动 WillFlow.AI · CAD 拆图与报价体验',
  description: '在意志流动 WillFlow.AI 体验公开 CAD 示例的原生模型、零件 BOM 与模拟报价，或上传自己的 STEP。',
  generator: 'WillFlow.AI',
  icons: { icon: '/workbench/willflow-mark.svg' },
  robots: { index: false, follow: false },
}

export const viewport: Viewport = { colorScheme: 'light', themeColor: '#070b17' }

export default function ExperiencePage() { return <ExperienceEntry /> }
