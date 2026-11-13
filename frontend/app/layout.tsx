import type { Metadata, Viewport } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: '好运来 · 图纸工作台',
  description: '上传 STEP 进入原生拆件，上传图片自动分图与提取参数，统一核对和工程资料导出。',
  generator: 'v0.app',
  icons: {
    icon: [
      {
        url: '/workbench/icon-light-32x32.png',
        media: '(prefers-color-scheme: light)',
      },
      {
        url: '/workbench/icon-dark-32x32.png',
        media: '(prefers-color-scheme: dark)',
      },
      {
        url: '/workbench/icon.svg',
        type: 'image/svg+xml',
      },
    ],
    apple: '/workbench/apple-icon.png',
  },
}

export const viewport: Viewport = {
  colorScheme: 'light',
  themeColor: '#f4f7fa',
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html lang="zh-CN">
      <body className="antialiased">
        {children}
      </body>
    </html>
  )
}
