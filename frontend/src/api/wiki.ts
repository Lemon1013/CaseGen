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
}

export interface RetrieveResponse {
  query: string
  hits: RetrieveHit[]
}

export function listWikiPages() {
  return api<WikiPage[]>('/api/wiki/pages')
}

export function getWikiPage(id: number) {
  return api<WikiPage>(`/api/wiki/pages/${id}`)
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
