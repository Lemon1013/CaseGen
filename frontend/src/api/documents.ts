import { api } from './client'

export interface DocumentItem {
  id: number
  filename: string
  stored_path: string
  content_type: string
  sha256: string
  status: string
  char_count: number
  error_message: string | null
  created_at: string
  updated_at: string
}

export interface IngestJob {
  id: number
  document_id: number
  status: string
  step_log_json: string
  error_message: string | null
  created_at: string
  updated_at: string
}

export function listDocuments() {
  return api<DocumentItem[]>('/api/documents')
}

export function uploadDocument(file: File) {
  const form = new FormData()
  form.append('file', file)
  return api<DocumentItem>('/api/documents', {
    method: 'POST',
    body: form,
  })
}

export function ingestDocument(id: number) {
  return api<IngestJob>(`/api/documents/${id}/ingest`, { method: 'POST' })
}

export function getIngestJob(id: number) {
  return api<IngestJob>(`/api/ingest-jobs/${id}`)
}
