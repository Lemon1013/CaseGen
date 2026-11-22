import { api } from './client'

export interface ProviderFieldSpec {
  key: string
  label: string
  field_type: 'text' | 'password' | 'number' | 'boolean' | 'select' | 'textarea'
  required?: boolean
  default?: unknown
  placeholder?: string
  help_text?: string
  options?: Array<Record<string, string>>
}

export interface ProviderCapabilities {
  supports_remote_scopes: boolean
  supports_file_list: boolean
  supports_file_content: boolean
  supports_synonyms: boolean
  supported_auth_types: string[]
}

export interface ProviderTypeInfo {
  provider_type: string
  display_name: string
  description: string
  capabilities: ProviderCapabilities
  config_fields: ProviderFieldSpec[]
  credential_fields_by_auth: Record<string, ProviderFieldSpec[]>
}

export interface RemoteScope {
  id: string
  name: string
  description: string
  path: string
  raw: Record<string, unknown>
}

export interface RemoteFileItem {
  path: string
  name: string
  size?: number | null
  updated_at?: string | null
}

export interface RemoteFileContent {
  path: string
  content: string
  source_id?: number | null
  source_name?: string
}

export interface KnowledgeSourceOut {
  id: number
  project_id: number
  name: string
  provider_type: string
  base_url: string
  auth_type: string
  config: Record<string, unknown>
  has_credentials: boolean
  auth_username: string
  weight: number
  priority: number
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface KnowledgeSourceCreate {
  name?: string
  provider_type?: string
  base_url: string
  auth_type?: string
  config?: Record<string, unknown>
  credentials?: Record<string, unknown>
  weight?: number
  priority?: number
  enabled?: boolean
}

export interface KnowledgeSourceUpdate {
  name?: string
  provider_type?: string
  base_url?: string
  auth_type?: string
  config?: Record<string, unknown>
  credentials?: Record<string, unknown>
  weight?: number
  priority?: number
  enabled?: boolean
}

export interface KnowledgeSourceTestRequest {
  source_id?: number | null
  provider_type?: string
  base_url: string
  auth_type?: string
  config?: Record<string, unknown>
  credentials?: Record<string, unknown>
}

export interface KnowledgeSourceTestResponse {
  ok: boolean
  message: string
  scopes: RemoteScope[]
}

export function listKnowledgeProviderTypes() {
  return api<ProviderTypeInfo[]>('/api/knowledge-providers/types')
}

export function listKnowledgeSources(projectId: number) {
  return api<KnowledgeSourceOut[]>(
    `/api/projects/${projectId}/knowledge-sources?project_id=${projectId}`,
  )
}

export function createKnowledgeSource(projectId: number, payload: KnowledgeSourceCreate) {
  return api<KnowledgeSourceOut>(
    `/api/projects/${projectId}/knowledge-sources?project_id=${projectId}`,
    {
      method: 'POST',
      body: JSON.stringify(payload),
    },
  )
}

export function updateKnowledgeSource(
  projectId: number,
  sourceId: number,
  payload: KnowledgeSourceUpdate,
) {
  return api<KnowledgeSourceOut>(
    `/api/projects/${projectId}/knowledge-sources/${sourceId}?project_id=${projectId}`,
    {
      method: 'PUT',
      body: JSON.stringify(payload),
    },
  )
}

export function deleteKnowledgeSource(projectId: number, sourceId: number) {
  return api<void>(
    `/api/projects/${projectId}/knowledge-sources/${sourceId}?project_id=${projectId}`,
    {
      method: 'DELETE',
    },
  )
}

export function testKnowledgeSource(projectId: number, payload: KnowledgeSourceTestRequest) {
  return api<KnowledgeSourceTestResponse>(
    `/api/projects/${projectId}/knowledge-sources/test?project_id=${projectId}`,
    {
      method: 'POST',
      body: JSON.stringify(payload),
    },
  )
}

export function listKnowledgeSourceFiles(projectId: number, sourceId: number) {
  return api<RemoteFileItem[]>(
    `/api/projects/${projectId}/knowledge-sources/${sourceId}/files?project_id=${projectId}`,
  )
}

export function readKnowledgeSourceFile(
  projectId: number,
  sourceId: number,
  filePath: string,
) {
  return api<RemoteFileContent>(
    `/api/projects/${projectId}/knowledge-sources/${sourceId}/file-content?project_id=${projectId}&path=${encodeURIComponent(filePath)}`,
  )
}
