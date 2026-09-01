import { api } from './client'

export interface DataPoolRevision {
  id: number
  data_pool_id: number
  revision: number
  source_kind: string
  schema: Record<string, unknown>
  records: unknown[]
  record_count: number
  content_hash: string
  status: string
  error_message: string | null
  created_at: string
}

export interface DataPool {
  id: number
  wiki_space_id: number
  name: string
  description: string
  attributes: Record<string, unknown>
  status: string
  latest_revision: DataPoolRevision | null
  created_at: string
  updated_at: string
}

export interface PlatformExample {
  id: number
  variant_id: number
  kind: string
  name: string
  media_type: string
  content: string
  content_hash: string
}

export interface ExampleVariant {
  id: number
  platform_id: number
  name: string
  applicability: string
  status: string
  examples: PlatformExample[]
}

export interface PlatformProfile {
  id: number
  wiki_space_id: number
  name: string
  description: string
  artifact_topology: 'separated' | 'combined' | 'hybrid'
  status: string
  variants: ExampleVariant[]
}

export interface RenderRun {
  id: number
  wiki_space_id: number
  platform_id: number
  variant_id: number
  platform_name: string
  variant_name: string
  status: string
  generation_mode: string
  input_hash: string
  model_ref: string | null
  prompt_ref: string | null
  warnings: string[]
  error_message: string | null
  artifacts: Array<{ id: number; kind: string; filename: string; media_type: string; content: string; content_hash: string }>
  created_at: string
  updated_at: string
}

export interface SemanticCase {
  id: number
  case_key: string
  title: string
  priority: string
  content_md: string
}

export interface PlatformCaseSummary {
  id: number
  wiki_space_id: number
  platform_id: number
  variant_id: number | null
  platform_name: string
  variant_name: string
  source_run_id: number | null
  source_artifact_id: number | null
  filename: string
  name: string
  kind: string
  media_type: string
  content_hash: string
  content_length: number
  status: 'active' | 'archived'
  created_at: string
  updated_at: string
}

export interface PlatformCaseDetail extends PlatformCaseSummary {
  content: string
  render_mode: 'json' | 'csv' | 'markdown' | 'text'
  json_value: unknown
  json_truncated: boolean
  csv_headers: string[]
  csv_rows: string[][]
  csv_total_rows: number
  csv_truncated: boolean
}

export interface PlatformCasePage { items: PlatformCaseSummary[]; total: number; limit: number; offset: number }

export interface PlatformCaseInput {
  wiki_space_id?: number
  platform_id?: number
  variant_id?: number | null
  filename: string
  name?: string
  kind: string
  media_type: string
  content: string
}

export function listDataPools(spaceId: number, includeArchived = false) {
  return api<DataPool[]>(`/api/data-pools?wiki_space_id=${spaceId}&include_archived=${includeArchived}`)
}

export function createDataPool(body: {
  wiki_space_id: number; name: string; description?: string; attributes?: Record<string, unknown>
  source_kind: 'csv' | 'json' | 'description'; content: string; parse_with_llm?: boolean
}) {
  return api<DataPool>('/api/data-pools', { method: 'POST', body: JSON.stringify(body) })
}

export function archiveDataPool(id: number, spaceId: number) {
  return api<DataPool>(`/api/data-pools/${id}/archive?wiki_space_id=${spaceId}`, { method: 'POST' })
}

export function listPlatforms(spaceId: number) {
  return api<PlatformProfile[]>(`/api/platforms?wiki_space_id=${spaceId}`)
}

export function listPlatformSemanticCases(spaceId: number) {
  return api<SemanticCase[]>(`/api/platform-semantic-cases?wiki_space_id=${spaceId}`)
}

export function createPlatform(body: { wiki_space_id: number; name: string; description?: string; artifact_topology: string }) {
  return api<PlatformProfile>('/api/platforms', { method: 'POST', body: JSON.stringify(body) })
}

export function updatePlatform(id: number, spaceId: number, body: { name?: string; description?: string; artifact_topology?: string }) {
  return api<PlatformProfile>(`/api/platforms/${id}?wiki_space_id=${spaceId}`, { method: 'PATCH', body: JSON.stringify(body) })
}

export function deletePlatform(id: number, spaceId: number) {
  return api<{ ok: boolean }>(`/api/platforms/${id}?wiki_space_id=${spaceId}`, { method: 'DELETE' })
}

export function createVariant(platformId: number, spaceId: number, body: { name: string; applicability?: string }) {
  return api<ExampleVariant>(`/api/platforms/${platformId}/variants?wiki_space_id=${spaceId}`, { method: 'POST', body: JSON.stringify(body) })
}

export function updateVariant(platformId: number, variantId: number, spaceId: number, body: { name?: string; applicability?: string }) {
  return api<ExampleVariant>(`/api/platforms/${platformId}/variants/${variantId}?wiki_space_id=${spaceId}`, { method: 'PATCH', body: JSON.stringify(body) })
}

export function deleteVariant(platformId: number, variantId: number, spaceId: number) {
  return api<{ ok: boolean }>(`/api/platforms/${platformId}/variants/${variantId}?wiki_space_id=${spaceId}`, { method: 'DELETE' })
}

export function createExample(platformId: number, variantId: number, spaceId: number, body: { kind: string; name?: string; media_type?: string; content: string }) {
  return api<PlatformExample>(`/api/platforms/${platformId}/variants/${variantId}/examples?wiki_space_id=${spaceId}`, { method: 'POST', body: JSON.stringify(body) })
}

export function updateExample(platformId: number, variantId: number, exampleId: number, spaceId: number, body: { kind?: string; name?: string; media_type?: string; content?: string }) {
  return api<PlatformExample>(`/api/platforms/${platformId}/variants/${variantId}/examples/${exampleId}?wiki_space_id=${spaceId}`, { method: 'PATCH', body: JSON.stringify(body) })
}

export function deleteExample(platformId: number, variantId: number, exampleId: number, spaceId: number) {
  return api<{ ok: boolean }>(`/api/platforms/${platformId}/variants/${variantId}/examples/${exampleId}?wiki_space_id=${spaceId}`, { method: 'DELETE' })
}

export function renderExampleDirect(body: { wiki_space_id: number; platform_id: number; variant_id: number; test_case_ids?: number[]; external_case_markdown?: string; keywords?: string[]; data_pool_revision_ids: number[]; include_data_values?: boolean; data_sample_limit?: number }) {
  return api<RenderRun>('/api/platform-renders/example-direct', { method: 'POST', body: JSON.stringify(body) })
}

export function listRenderRuns(spaceId: number, platformId?: number) {
  const extra = platformId ? `&platform_id=${platformId}` : ''
  return api<RenderRun[]>(`/api/platform-renders?wiki_space_id=${spaceId}${extra}`)
}

export function listPlatformCases(spaceId: number, filters: { platform_id?: number; variant_id?: number; media_type?: string; search?: string; include_archived?: boolean; limit?: number; offset?: number } = {}) {
  const query = new URLSearchParams({ wiki_space_id: String(spaceId) })
  Object.entries(filters).forEach(([key, value]) => { if (value !== undefined && value !== '' && value !== false) query.set(key, String(value)) })
  return api<PlatformCasePage>(`/api/platform-cases?${query}`)
}

export function getPlatformCase(id: number, spaceId: number) {
  return api<PlatformCaseDetail>(`/api/platform-cases/${id}?wiki_space_id=${spaceId}`)
}

export function updatePlatformCase(id: number, spaceId: number, body: Partial<PlatformCaseInput>) {
  return api<PlatformCaseDetail>(`/api/platform-cases/${id}?wiki_space_id=${spaceId}`, { method: 'PATCH', body: JSON.stringify(body) })
}

export function archivePlatformCase(id: number, spaceId: number) {
  return api<PlatformCaseDetail>(`/api/platform-cases/${id}?wiki_space_id=${spaceId}`, { method: 'DELETE' })
}

export function restorePlatformCase(id: number, spaceId: number) {
  return api<PlatformCaseDetail>(`/api/platform-cases/${id}/restore?wiki_space_id=${spaceId}`, { method: 'POST' })
}
