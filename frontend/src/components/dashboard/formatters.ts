export interface DistributionBarItem {
  label: string
  count: number
  percent?: string
  width: string
}

export function finite(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

export function formatCount(value: unknown): string {
  return Math.max(0, finite(value)).toLocaleString('zh-CN')
}

export function percentRatio(count: number, total: number): number {
  if (!Number.isFinite(total) || total <= 0) return 0
  return Math.min(Math.max(count / total, 0), 1)
}

export function ratioLabel(ratio: number): string {
  if (!Number.isFinite(ratio) || ratio <= 0) return '0%'
  return `${(Math.min(ratio, 1) * 100).toFixed(1)}%`
}

export function ratioWidth(ratio: number): string {
  if (!Number.isFinite(ratio) || ratio <= 0) return '0%'
  return `${(Math.min(ratio, 1) * 100).toFixed(2)}%`
}

export function countWidth(count: number, max: number): string {
  if (!Number.isFinite(max) || max <= 0 || count <= 0) return '0%'
  return `${Math.max((count / max) * 100, 2).toFixed(2)}%`
}

export function withRatios(items: Array<{ label: string; count: number }>): DistributionBarItem[] {
  const total = items.reduce((sum, item) => sum + item.count, 0)
  return items.map((item) => {
    const ratio = percentRatio(item.count, total)
    return {
      ...item,
      percent: ratioLabel(ratio),
      width: ratioWidth(ratio),
    }
  })
}
