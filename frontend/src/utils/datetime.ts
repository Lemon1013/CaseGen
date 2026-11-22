const DEFAULT_TIME_ZONE = 'Asia/Shanghai'

function toDate(value: string | number | Date | null | undefined): Date | null {
  if (value == null || value === '') return null
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  if (typeof value === 'number') return new Date(value)

  const raw = String(value).trim()
  if (!raw) return null
  // The backend persists naive UTC timestamps without a timezone marker.
  // Treat any offset-less ISO value as UTC before converting to the target zone.
  const hasOffset = /(?:Z|[+-]\d{2}:\d{2})$/i.test(raw)
  const parsed = hasOffset ? new Date(raw) : new Date(`${raw}Z`)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

function formatDateParts(
  value: string | number | Date | null | undefined,
  timeZone: string = DEFAULT_TIME_ZONE,
): Record<string, string> | null {
  const date = toDate(value)
  if (!date) return null
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date)
  const mapped: Record<string, string> = {}
  for (const part of parts) {
    mapped[part.type] = part.value
  }
  return mapped
}

export function formatDateTime(
  value: string | number | Date | null | undefined,
  timeZone: string = DEFAULT_TIME_ZONE,
): string {
  const parts = formatDateParts(value, timeZone)
  if (!parts) return '-'
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`
}

export function formatShortDateTime(
  value: string | number | Date | null | undefined,
  timeZone: string = DEFAULT_TIME_ZONE,
): string {
  const parts = formatDateParts(value, timeZone)
  if (!parts) return '—'
  return `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`
}

export function formatTimeOnly(
  value: string | number | Date | null | undefined,
  timeZone: string = DEFAULT_TIME_ZONE,
): string {
  const parts = formatDateParts(value, timeZone)
  if (!parts) return ''
  return `${parts.hour}:${parts.minute}:${parts.second}`
}
