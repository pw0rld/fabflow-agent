const demoBasePath = process.env.HYL_DEMO_BASE_PATH || '/workbench'
if (!/^\/[A-Za-z0-9][A-Za-z0-9/_-]*$/.test(demoBasePath) || demoBasePath.endsWith('/') || demoBasePath.includes('//')) {
  throw new Error('HYL_DEMO_BASE_PATH must be an absolute path without a trailing slash')
}
/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'export',
  basePath: demoBasePath,
  trailingSlash: true,
  poweredByHeader: false,
  experimental: {
    cpus: 1,
  },
  images: {
    unoptimized: true,
  },
}

export default nextConfig
