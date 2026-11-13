/** Display-only formatting. Saved measurements retain their original precision. */
export function quotationCount(value: string | number | null | undefined, missing = '未提供'): string {
  if (typeof value === 'number') return Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString('zh-CN') : missing
  if (typeof value !== 'string' || value.length > 128 || !/^(?:0|[1-9]\d*)(?:\.0+)?$/.test(value)) return missing
  return value.split('.')[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

export function quotationNumber(value: string | number | null | undefined, missing = '未提供'): string {
  const text = typeof value === 'number' ? String(value) : value
  if (typeof text !== 'string' || text.length > 128 || !/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(text)) return missing
  const [whole, fraction = ''] = text.split('.')
  const cents = BigInt(whole) * BigInt(100) + BigInt(fraction.padEnd(2, '0').slice(0, 2)) + (fraction[2] >= '5' ? BigInt(1) : BigInt(0))
  return `${(cents / BigInt(100)).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',')}.${(cents % BigInt(100)).toString().padStart(2, '0')}`
}

export function estimatedWeight(value: string | null | undefined, missing = '暂无法估算'): string {
  if (typeof value !== 'string' || !/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value)) return missing
  const kilograms = Number(value)
  if (!Number.isFinite(kilograms) || kilograms < 0) return missing
  // A positive decimal can underflow Number; never display that value as zero.
  const positive = /[1-9]/.test(value)
  if (kilograms < 1 && positive) {
    const grams = kilograms * 1000
    return grams < .01 ? '小于 0.01 g' : `约 ${grams.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} g`
  }
  return `约 ${quotationNumber(value)} kg`
}
