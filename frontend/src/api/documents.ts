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

export interface DocumentDiagnostics {
  is_empty?: boolean
  replacement_char_count?: number
  replacement_char_rate?: number
  garbled_char_count?: number
  garbled_char_rate?: number
  suspicious_scanned_pdf?: boolean
  page_count?: number
  pages_with_text?: number
  warnings?: string[]
  errors?: string[]
  [key: string]: unknown
}

export interface DocumentPreview {
  document_id: number
  filename: string
  text: string
  char_count: number
  returned_chars: number
  truncated: boolean
  quality_ok: boolean
  diagnostics: DocumentDiagnostics
}

export interface IngestJob {
  id: number
  document_id: number
  status: string
  stage: string
  progress: number
  plan_json: string
  cancel_requested: boolean
  step_log_json: string
  error_message: string | null
  created_at: string
  updated_at: string
}

export interface IngestStep {
  step?: string
  message?: string
  at?: string
  progress?: number
  [key: string]: unknown
}

export interface SourceChunk {
  id: number
  document_id: number
  chunk_index: number
  title: string
  text: string
  start_char: number
  end_char: number
  page_start: number | null
  page_end: number | null
  section: string
  clause_ids_json: string
  parent_index: number | null
  created_at: string
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

export function getDocumentPreview(id: number, maxChars = 50000) {
  return api<DocumentPreview>(`/api/documents/${id}/preview?max_chars=${maxChars}`)
}

export function listDocumentChunks(id: number) {
  return api<SourceChunk[]>(`/api/documents/${id}/chunks`)
}

export function ingestDocument(id: number, force = false) {
  const query = force ? '?force=true' : ''
  return api<IngestJob>(`/api/documents/${id}/ingest${query}`, { method: 'POST' })
}

export function listIngestJobs(status?: string) {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  return api<IngestJob[]>(`/api/ingest-jobs${query}`)
}

export function getIngestJob(id: number) {
  return api<IngestJob>(`/api/ingest-jobs/${id}`)
}

export function cancelIngestJob(id: number) {
  return api<IngestJob>(`/api/ingest-jobs/${id}/cancel`, { method: 'POST' })
}

export function retryFailedWindows(id: number) {
  return api<IngestJob>(`/api/ingest-jobs/${id}/retry-failed-windows`, { method: 'POST' })
}
