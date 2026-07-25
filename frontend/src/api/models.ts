import { api } from './client'

export interface ModelConfig {
  id: number
  name: string
  base_url: string
  api_key: string
  model_name: string
  is_default: boolean
  created_at: string
  updated_at: string
}

export interface ModelCreate {
  name: string
  base_url: string
  api_key: string
  model_name: string
  is_default?: boolean
}

export interface ModelUpdate {
  name?: string
  base_url?: string
  api_key?: string
  model_name?: string
  is_default?: boolean
}

export interface ModelPingResult {
  ok: boolean
  content: string
}

export function listModels() {
  return api<ModelConfig[]>('/api/models')
}

export function createModel(body: ModelCreate) {
  return api<ModelConfig>('/api/models', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function updateModel(id: number, body: ModelUpdate) {
  return api<ModelConfig>(`/api/models/${id}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

export function deleteModel(id: number) {
  return api<{ ok: boolean }>(`/api/models/${id}`, { method: 'DELETE' })
}

export function pingModel(id: number) {
  return api<ModelPingResult>(`/api/models/${id}/ping`, { method: 'POST' })
}
