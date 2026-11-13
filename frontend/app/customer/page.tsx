import type { Metadata } from 'next'
import CustomerUploadPortal from '@/components/customer-upload-portal'

export const metadata: Metadata = {
  title: '好运来 · 客户询价',
  description: '上传 STEP 模型或订单资料包，查看处理进度与客户报价。',
  robots: { index: false, follow: false },
}

export default function CustomerPage() { return <CustomerUploadPortal /> }
