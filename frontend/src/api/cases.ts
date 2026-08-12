import { api } from './client'

export interface TestCaseItem {
  id: number
  requirement_id: number
  case_key: string
  source_case_key: string | null
  title: string
  content_md: string
  content?: string | null
  status: 'active' | 'archived' | string
  revision: number
  source_task_id: number | null
  source_draft_id: number | null
  archived_at: string | null
  created_at: string
  updated_at: string
}

export interface TestCaseOperationLog {
  id: number
  test_case_id: number
  operation: string
  changed_fields: string[]
  before_hash: string | null
  after_hash: string | null
  before_length: number | null
  after_length: number | null
  added_lines: number
  deleted_lines: number
  title_changed: boolean
  diff_summary: string
  reason: string | null
  operator: string | null
  source_task_id: number | null
  source_draft_id: number | null
  source_case_key: string | null
  created_at: string
}

export function listCases(opts?: {
  requirement_id?: number
  include_archived?: boolean
  keyword?: string
  status?: 'active' | 'archived' | ''
}) {
  const query = new URLSearchParams()
  if (opts?.requirement_id != null) query.set('requirement_id', String(opts.requirement_id))
  if (opts?.include_archived) query.set('include_archived', 'true')
  if (opts?.keyword?.trim()) query.set('keyword', opts.keyword.trim())
  if (opts?.status) query.set('status', opts.status)
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return api<TestCaseItem[]>(`/api/cases${suffix}`)
}

export function getCase(id: number) {
  return api<TestCaseItem>(`/api/cases/${id}`)
}

export function updateCase(
  id: number,
  body: {
    title?: string
    content_md?: string
    expected_revision?: number
    reason?: string
  },
) {
  return api<TestCaseItem>(`/api/cases/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

export function archiveCase(id: number, revision?: number) {
  const query = revision == null ? '' : `?expected_revision=${encodeURIComponent(revision)}`
  return api<TestCaseItem>(`/api/cases/${id}/archive${query}`, { method: 'POST' })
}

export function restoreCase(id: number, revision?: number) {
  const query = revision == null ? '' : `?expected_revision=${encodeURIComponent(revision)}`
  return api<TestCaseItem>(`/api/cases/${id}/restore${query}`, { method: 'POST' })
}

export function listCaseLogs(id: number) {
  return api<TestCaseOperationLog[]>(`/api/cases/${id}/logs`)
}

export function caseExportUrl(id: number) {
  return `/api/cases/${id}/export`
}

export function casesExportUrl(opts?: { ids?: number[]; requirement_id?: number }) {
  const query = new URLSearchParams()
  if (opts?.ids?.length) query.set('ids', opts.ids.join(','))
  if (opts?.requirement_id != null) query.set('requirement_id', String(opts.requirement_id))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return `/api/cases/export${suffix}`
}
