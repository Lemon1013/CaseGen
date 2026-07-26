import { api } from './client'

export interface WikiPage {
  id: number
  path: string
  title: string
  page_type: string
  source_document_id: number | null
  tags: string[]
  content: string | null
  created_at: string
  updated_at: string
}

export interface WikiIndex {
  content: string
  path: string
}

export interface RetrieveHit {
  id: number | null
  title: string
  page_type: string
  path: string
  score: number
  snippet: string
  tags: string[]
  content: string | null
  source_document_id: number | null
  /** wiki | source */
  citation_type?: string
  source_chunk_id?: number | null
  start_char?: number | null
  end_char?: number | null
  clause_ids?: string[]
  anchor_clause?: string | null
}

export interface RetrieveResponse {
  query: string
  hits: RetrieveHit[]
  wiki_hit_count?: number
  source_hit_count?: number
  clause_ids?: string[]
  anchored_clause_ids?: string[]
}

export interface SourceChunk {
  id: number
  document_id: number
  chunk_index: number
  title: string
  text: string
  start_char: number
  end_char: number
}

export function listWikiPages() {
  return api<WikiPage[]>('/api/wiki/pages')
}

export function getWikiPage(id: number) {
  return api<WikiPage>(`/api/wiki/pages/${id}`)
}

export function getSourceChunk(id: number) {
  return api<SourceChunk>(`/api/source-chunks/${id}`)
}

export function getWikiIndex() {
  return api<WikiIndex>('/api/wiki/index')
}

export function retrieveWiki(query: string, top_k?: number) {
  return api<RetrieveResponse>('/api/wiki/retrieve', {
    method: 'POST',
    body: JSON.stringify({ query, top_k }),
  })
}
