import { api } from './client'

export interface ExternalWikiConfig {
  id?: number | null
  project_id: number
  name: string
  base_url: string
  external_project_id: string
  external_project_name: string
  top_k: number
  timeout_sec: number
  weight: number
  use_synonyms: boolean
  enabled: boolean
  created_at?: string
  updated_at?: string
}

export interface ExternalWikiProjectItem {
  id: string
  name: string
  path?: string
  rawDir?: string
  exportDir?: string
}

export interface ExternalWikiTestResponse {
  success: boolean
  message: string
  projects: ExternalWikiProjectItem[]
}

export const getExternalWikiConfig = (projectId: number) =>
  api<ExternalWikiConfig>(`/api/projects/${projectId}/external-wiki?project_id=${projectId}`)

export const saveExternalWikiConfig = (projectId: number, body: Partial<ExternalWikiConfig>) =>
  api<ExternalWikiConfig>(`/api/projects/${projectId}/external-wiki?project_id=${projectId}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  })

export const testExternalWikiConnection = (
  projectId: number,
  body: { base_url: string; timeout_sec?: number },
) =>
  api<ExternalWikiTestResponse>(
    `/api/projects/${projectId}/external-wiki/test-connection?project_id=${projectId}`,
    {
      method: 'POST',
      body: JSON.stringify(body),
    },
  )
