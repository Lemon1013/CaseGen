import { api } from './client'

export interface WikiSpace {
  id: number
  name: string
  slug: string
  description: string
  status: 'active' | 'archived' | string
  scope: 'shared' | 'project'
  project_id: number | null
  namespace: string | null
  document_count: number
  page_count: number
  pending_review_count: number
  last_updated_at: string
  created_at: string
  updated_at: string
}

export interface WikiSpaceInput {
  name: string
  slug?: string
  description?: string
  scope?: 'shared' | 'project'
  project_id?: number
  namespace?: string
}

export type WikiSpaceStatus = 'active' | 'archived'

export function listWikiSpaces(includeAvailableShared = false) {
  return api<WikiSpace[]>(`/api/wiki-spaces${includeAvailableShared ? '?include_available_shared=true' : ''}`)
}

export function getWikiSpace(id: number) {
  return api<WikiSpace>(`/api/wiki-spaces/${id}`)
}

export function createWikiSpace(body: WikiSpaceInput) {
  return api<WikiSpace>('/api/wiki-spaces', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function updateWikiSpace(id: number, body: Partial<WikiSpaceInput>) {
  return api<WikiSpace>(`/api/wiki-spaces/${id}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

export function archiveWikiSpace(id: number) {
  return api<WikiSpace>(`/api/wiki-spaces/${id}/archive`, { method: 'POST' })
}

export function updateWikiSpaceStatus(id: number, status: WikiSpaceStatus) {
  return api<WikiSpace>(`/api/wiki-spaces/${id}/status`, {
    method: 'PATCH',
    body: JSON.stringify({ status }),
  })
}
