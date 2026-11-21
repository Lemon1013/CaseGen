export type TaskStatusTagType = '' | 'success' | 'warning' | 'info' | 'danger'
export type PriorityTagType = 'danger' | 'warning' | 'primary' | 'info'
export type DimensionTagType = 'primary' | 'success' | 'warning' | 'danger' | 'info'
export type CaseStatusTagType = 'success' | 'info'
export type CasePriority = 'P0' | 'P1' | 'P2'

export const TASK_STATUS_LABELS: Record<string, string> = {
  draft: '草稿',
  retrieving: '检索中',
  awaiting_confirmation: '等待确认',
  generating_test_points: '生成测试点中',
  awaiting_test_point_confirmation: '等待测试点确认',
  generating: '生成中',
  generated: '已生成',
  reviewing: '评审中',
  reviewed: '已评审',
  optimizing: '优化中',
  regenerating: '再生成中',
  finalized: '已终版',
  failed: '失败',
}

export const TASK_STATUS_TAG_TYPES: Record<string, TaskStatusTagType> = {
  draft: '',
  retrieving: 'warning',
  awaiting_confirmation: 'warning',
  generating_test_points: 'warning',
  awaiting_test_point_confirmation: 'warning',
  generating: 'warning',
  generated: 'info',
  reviewing: 'warning',
  reviewed: 'info',
  optimizing: 'warning',
  regenerating: 'warning',
  finalized: 'success',
  failed: 'danger',
}

export function taskStatusLabel(status?: string | null): string {
  if (!status) return ''
  return TASK_STATUS_LABELS[status] || status
}

export function taskStatusTagType(status?: string | null): TaskStatusTagType {
  if (!status) return ''
  return TASK_STATUS_TAG_TYPES[status] ?? ''
}

export const PRIORITY_TAG_TYPES: Record<CasePriority, PriorityTagType> = {
  P0: 'danger',
  P1: 'warning',
  P2: 'info',
}

export function normalizePriority(
  priority?: string | null,
  fallback: CasePriority = 'P1',
): CasePriority {
  const normalized = (priority || '').trim().toUpperCase()
  if (normalized === 'P0' || normalized === 'P1' || normalized === 'P2') {
    return normalized
  }
  return fallback
}

export function priorityTagType(priority?: string | null): PriorityTagType {
  const normalized = (priority || '').trim().toUpperCase()
  if (normalized === 'P0') return PRIORITY_TAG_TYPES.P0
  if (normalized === 'P1') return PRIORITY_TAG_TYPES.P1
  if (normalized === 'P2') return PRIORITY_TAG_TYPES.P2
  return 'info'
}

export const DIMENSION_LABELS: Record<string, string> = {
  positive: '正向',
  negative: '反向',
  boundary: '边界',
  permission: '权限',
  security: '安全',
  compatibility: '兼容性',
  performance: '性能',
  recovery: '恢复',
  usability: '可用性',
}

export const DIMENSION_TAG_TYPES: Record<string, DimensionTagType> = {
  positive: 'primary',
  boundary: 'warning',
  negative: 'danger',
  permission: 'info',
  security: 'info',
  compatibility: 'info',
  performance: 'info',
  recovery: 'info',
  usability: 'info',
}

export function dimensionLabel(dimension?: string | null): string {
  if (!dimension) return ''
  return DIMENSION_LABELS[dimension] || DIMENSION_LABELS[dimension.toLowerCase()] || dimension
}

export function dimensionTagType(dimension?: string | null): DimensionTagType {
  const normalized = (dimension || '').trim().toLowerCase()
  if (!normalized) return 'info'
  if (normalized in DIMENSION_TAG_TYPES) {
    return DIMENSION_TAG_TYPES[normalized]
  }
  if (normalized.includes('positive') || normalized.includes('正向') || normalized.includes('主流程')) {
    return 'primary'
  }
  if (normalized.includes('boundary') || normalized.includes('边界')) {
    return 'warning'
  }
  if (
    normalized.includes('negative')
    || normalized.includes('反向')
    || normalized.includes('异常')
    || normalized.includes('容错')
  ) {
    return 'danger'
  }
  if (normalized.includes('permission') || normalized.includes('auth') || normalized.includes('权限')) {
    return 'info'
  }
  return 'info'
}

export const CASE_STATUS_LABELS: Record<string, string> = {
  active: '当前',
  archived: '已归档',
}

export const CASE_STATUS_TAG_TYPES: Record<string, CaseStatusTagType> = {
  active: 'success',
  archived: 'info',
}

export function caseStatusLabel(status?: string | null): string {
  if (!status) return ''
  return CASE_STATUS_LABELS[status] || status
}

export function caseStatusTagType(status?: string | null): CaseStatusTagType {
  if (!status) return 'info'
  return CASE_STATUS_TAG_TYPES[status] ?? 'info'
}
