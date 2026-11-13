export const PUBLIC_QUOTE_API = '/workbench/api/quote-batches' as const
export const POLAND_QUOTE_API = '/ortho2cad-test/quote-workbench/api/quote-batches' as const
export const CUSTOMER_ADMIN_QUOTE_API = '/ortho2cad-test/customer-upload-admin/api/quote-batches' as const
export type QuoteApiBase = typeof PUBLIC_QUOTE_API | typeof POLAND_QUOTE_API | typeof CUSTOMER_ADMIN_QUOTE_API
export type QuoteScope = 'public' | 'poland' | 'customer'

// API destinations are application constants, never arbitrary URL parameters.
export const quoteApiForScope = (scope: QuoteScope): QuoteApiBase => scope === 'customer' ? CUSTOMER_ADMIN_QUOTE_API : scope === 'poland' ? POLAND_QUOTE_API : PUBLIC_QUOTE_API
