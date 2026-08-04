import { api } from './client'

export type PromptType =
  | 'generate'
  | 'review'
  | 'optimize'
  | 'wiki_analyze'
  | 'wiki_write'

export interface PromptTemplate {
  id: number
  name: string
  type: string
  content: string
  version: number
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface PromptCreate {
  name: string
  type: string
  content: string
  is_active?: boolean
}

export interface PromptUpdate {
  name?: string
  content?: string
  is_active?: boolean
}

export function listPrompts(type?: string) {
  const qs = type ? `?type=${encodeURIComponent(type)}` : ''
  return api<PromptTemplate[]>(`/api/prompts${qs}`)
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

export const PROMPT_TYPE_OPTIONS: { value: PromptType; label: string }[] = [
  { value: 'generate', label: '生成 (generate)' },
  { value: 'review', label: '评审 (review)' },
  { value: 'optimize', label: '优化 (optimize)' },
  { value: 'wiki_analyze', label: 'Wiki 分析 (wiki_analyze)' },
  { value: 'wiki_write', label: 'Wiki 写入 (wiki_write)' },
]
