import { api } from './client'

export interface RequirementItem {
  id: number
  title: string
  description: string
  focus_tags: string[]
  created_at: string
  updated_at: string
}

export function listRequirements() {
  return api<RequirementItem[]>('/api/requirements')
}

export function getRequirement(id: number) {
  return api<RequirementItem>(`/api/requirements/${id}`)
}
