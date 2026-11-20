import { api } from './client'
import type { WikiSpace } from './wikiSpaces'

export interface ProjectSpace extends Pick<WikiSpace, 'id' | 'name' | 'slug' | 'scope' | 'namespace' | 'status'> {
  priority?: number
  enabled: boolean
}

export interface Project {
  id: number
  name: string
  slug: string
  description: string
  status: string
  is_public: boolean
  default_wiki_space_id: number | null
  knowledge_source_count?: number
  private_spaces: ProjectSpace[]
  shared_spaces: ProjectSpace[]
  created_at: string
  updated_at: string
}

export const listProjects = () => api<Project[]>('/api/projects')
export const getProject = (projectId: number) => api<Project>(`/api/projects/${projectId}?project_id=${projectId}`)
export const createProject = (body: { name: string; slug?: string; description?: string }) =>
  api<Project>('/api/projects', { method: 'POST', body: JSON.stringify(body) })
export const updateProject = (projectId: number, body: { name?: string; description?: string; status?: string; is_public?: boolean }) =>
  api<Project>(`/api/projects/${projectId}?project_id=${projectId}`, { method: 'PUT', body: JSON.stringify(body) })
export const deleteProject = (projectId: number) =>
  api<void>(`/api/projects/${projectId}?project_id=${projectId}`, { method: 'DELETE' })
