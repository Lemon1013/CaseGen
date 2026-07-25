import { api } from './client'

export interface ReviewResult {
  id: number
  task_id: number
  draft_id: number
  score: number
  verdict: string
  payload: {
    score?: number
    verdict?: string
    issues?: string[]
    missing_scenarios?: string[]
    prompt_improvement_hints?: string[]
    ready_for_final?: boolean
    raw?: string
    [key: string]: unknown
  }
  created_at: string
}

export interface TaskItem {
  id: number
  requirement_id: number
  status: string
  model_id: number | null
  prompt_template_id: number | null
  error_message: string | null
  title: string | null
  description: string | null
  focus_tags: string[]
  citation_count: number
  latest_draft_snippet: string | null
  latest_draft_version: number | null
  latest_review: ReviewResult | null
  created_at: string
  updated_at: string
}

export interface TaskCreate {
  title: string
  description: string
  focus_tags?: string[]
  model_id?: number | null
  prompt_template_id?: number | null
  auto_review?: boolean
  run_generate?: boolean
}

export interface CaseDraft {
  id: number
  task_id: number
  version: number
  content_md: string
  prompt_version_ref: string | null
  created_at: string
}

export interface TaskEvent {
  id: number
  task_id: number
  step: string
  message: string
  detail_json: string | null
  created_at: string
}

export interface PromptRevision {
  id: number
  task_id: number
  base_prompt_id: number | null
  new_content: string
  status: string
  created_at: string
}

export interface TaskCitation {
  id: number
  title: string
  path: string
  score: number
  snippet: string
  wiki_page_id: number | null
}

export type ApplyPromptMode = 'global' | 'task_temp'

export const IN_PROGRESS_STATUSES = new Set([
  'retrieving',
  'generating',
  'reviewing',
  'optimizing',
  'regenerating',
])

export function listTasks() {
  return api<TaskItem[]>('/api/tasks')
}

export function getTask(id: number) {
  return api<TaskItem>(`/api/tasks/${id}`)
}

export function createTask(body: TaskCreate) {
  return api<TaskItem>('/api/tasks', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function generateTask(id: number) {
  return api<TaskItem>(`/api/tasks/${id}/generate`, { method: 'POST' })
}

export function reviewTask(id: number) {
  return api<TaskItem>(`/api/tasks/${id}/review`, { method: 'POST' })
}

export function optimizePromptTask(id: number) {
  return api<TaskItem>(`/api/tasks/${id}/optimize-prompt`, { method: 'POST' })
}

export function regenerateTask(id: number) {
  return api<TaskItem>(`/api/tasks/${id}/regenerate`, { method: 'POST' })
}

export function finalizeTask(id: number) {
  return api<TaskItem>(`/api/tasks/${id}/finalize`, { method: 'POST' })
}

export function applyPrompt(
  id: number,
  body: { revision_id: number; mode: ApplyPromptMode },
) {
  return api<TaskItem>(`/api/tasks/${id}/apply-prompt`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function listDrafts(id: number) {
  return api<CaseDraft[]>(`/api/tasks/${id}/drafts`)
}

export function listEvents(id: number) {
  return api<TaskEvent[]>(`/api/tasks/${id}/events`)
}

export function listReviews(id: number) {
  return api<ReviewResult[]>(`/api/tasks/${id}/reviews`)
}

export function listRevisions(id: number) {
  return api<PromptRevision[]>(`/api/tasks/${id}/revisions`)
}

export function listCitations(id: number) {
  return api<TaskCitation[]>(`/api/tasks/${id}/citations`)
}

export function statusLabel(status: string): string {
  const map: Record<string, string> = {
    draft: '草稿',
    retrieving: '检索中',
    generating: '生成中',
    generated: '已生成',
    reviewing: '评审中',
    reviewed: '已评审',
    optimizing: '优化中',
    regenerating: '再生成中',
    finalized: '已终版',
    failed: '失败',
  }
  return map[status] || status
}

export function statusTagType(status: string): '' | 'success' | 'warning' | 'info' | 'danger' {
  switch (status) {
    case 'finalized':
      return 'success'
    case 'generated':
    case 'reviewed':
      return 'info'
    case 'retrieving':
    case 'generating':
    case 'reviewing':
    case 'optimizing':
    case 'regenerating':
      return 'warning'
    case 'failed':
      return 'danger'
    default:
      return ''
  }
}
