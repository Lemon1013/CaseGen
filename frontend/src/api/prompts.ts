import { api } from './client'

export type PromptType =
  | 'generate'
  | 'test_points'
  | 'review'
  | 'optimize'
  | 'requirement_optimize'
  | 'platform_example_render'
  | 'data_description_parse'

export type PromptVisibility = 'system' | 'shared' | 'private'

export interface PromptTemplate {
  id: number
  name: string
  type: string
  content: string
  version: number
  is_active: boolean
  owner_user_id?: number | null
  owner_username?: string | null
  visibility: PromptVisibility
  can_edit?: boolean
  can_delete?: boolean
  can_fork?: boolean
  created_at: string
  updated_at: string
}

export interface PromptCreate {
  name: string
  type: string
  content: string
  is_active?: boolean
  visibility?: PromptVisibility
}

export interface PromptUpdate {
  name?: string
  content?: string
  is_active?: boolean
  visibility?: PromptVisibility
}

export interface PromptForkBody {
  name?: string
  visibility?: 'private' | 'shared'
}

export function listPrompts(type?: string, visibility?: PromptVisibility) {
  const params = new URLSearchParams()
  if (type) params.set('type', type)
  if (visibility) params.set('visibility', visibility)
  const qs = params.toString()
  return api<PromptTemplate[]>(`/api/prompts${qs ? `?${qs}` : ''}`)
}

export function createPrompt(body: PromptCreate) {
  return api<PromptTemplate>('/api/prompts', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function updatePrompt(id: number, body: PromptUpdate) {
  return api<PromptTemplate>(`/api/prompts/${id}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

export function deletePrompt(id: number) {
  return api<{ ok: boolean; id: number }>(`/api/prompts/${id}`, {
    method: 'DELETE',
  })
}

export function forkPrompt(
  id: number,
  body?: PromptForkBody,
): Promise<PromptTemplate> {
  return api<PromptTemplate>(`/api/prompts/${id}/fork`, {
    method: 'POST',
    ...(body ? { body: JSON.stringify(body) } : {}),
  })
}

export function isEnginePromptType(type: string): boolean {
  return type !== 'generate'
}

export const PROMPT_TYPE_OPTIONS: { value: PromptType; label: string }[] = [
  { value: 'generate', label: '用例生成 (generate)' },
  { value: 'test_points', label: '测试点生成 (test_points)' },
  { value: 'review', label: '用例评审 (review)' },
  { value: 'optimize', label: 'Prompt 优化 (optimize)' },
  { value: 'requirement_optimize', label: '需求分析优化 (requirement_optimize)' },
  { value: 'platform_example_render', label: '平台用例渲染 (platform_example_render)' },
  { value: 'data_description_parse', label: '数据描述解析 (data_description_parse)' },
]
