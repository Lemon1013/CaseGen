import { api } from './client'

export interface WikiPage {
  id: number
  path: string
  title: string
  page_type: string
  page_key?: string | null
  domain?: string | null
  status?: string | null
  revision?: number | null
  aliases?: string[]
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
  page_key?: string | null
  domain?: string | null
  status?: string | null
  revision?: number | null
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
  explain?: Record<string, unknown> | null
  aliases?: string[]
  source_document_ids?: number[]
}

export interface RetrieveResponse {
  query: string
  hits: RetrieveHit[]
  wiki_hit_count?: number
  source_hit_count?: number
  clause_ids?: string[]
  anchored_clause_ids?: string[]
  retrieval_mode?: string | null
  explain?: Record<string, unknown> | null
}

export type WikiReviewStatus = 'pending' | 'approved' | 'rejected' | 'acknowledged'

export interface WikiReview {
  id: number
  page_id: number | null
  job_id: number | null
  kind: string
  status: WikiReviewStatus | string
  reason: string
  candidate_available: boolean
  reviewed_at: string | null
  reviewed_by: string | null
  decision_reason: string | null
  created_at: string
  updated_at: string
}

export interface WikiSourceEvidence {
  document_id: number
  chunk_ids: number[]
  clauses: string[]
}

export interface WikiRevision {
  id: number
  page_id: number
  revision: number
  frontmatter: Record<string, unknown>
  frontmatter_json: string
  content_md: string
  operation: string
  job_id: number | null
  reason: string
  created_at: string
}

export interface WikiCandidate {
  page_key: string | null
  title: string | null
  type: string | null
  domain: string | null
  aliases: string[]
  tags: string[]
  status: string | null
  sources: WikiSourceEvidence[]
  content_md: string | null
  raw: Record<string, unknown>
}

export interface WikiReviewReason {
  summary: string
  kind: string
  operation: string | null
  page_key: string | null
  risk_flags: string[]
}

export interface WikiDiff {
  from_revision: number | null
  to_revision: number | null
  unified: string
  text: string
  changed: boolean
  available: boolean
  reason: string
}

export interface WikiReviewDetail extends WikiReview {
  old_version: WikiRevision | null
  new_candidate: WikiCandidate
  reason_detail: WikiReviewReason
  payload: Record<string, unknown>
  source_evidence: WikiSourceEvidence[]
  diff: WikiDiff
}

export interface WikiReviewDecision {
  reviewed_by?: string
  reason?: string
  decision_reason?: string
}

export interface WikiRollbackRequest {
  revision_id?: number
  revision?: number
  job_id?: number
  reason: string
  reviewed_by?: string
}

export type WikiRollbackResponse = WikiRevision

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

export interface WikiReviewQuery {
  status?: WikiReviewStatus | string
  kind?: string
  page_id?: number
  job_id?: number
}

function withQuery(path: string, params: Record<string, string | number | undefined>) {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      search.set(key, String(value))
    }
  })
  const query = search.toString()
  return query ? `${path}?${query}` : path
}

export function listWikiReviews(params: WikiReviewQuery = {}) {
  return api<WikiReview[]>(
    withQuery('/api/wiki/reviews', {
      status: params.status,
      kind: params.kind,
      page_id: params.page_id,
      job_id: params.job_id,
    }),
  )
}

export function getWikiReview(id: number) {
  return api<WikiReviewDetail>(`/api/wiki/reviews/${id}`)
}

export function approveWikiReview(id: number, body: WikiReviewDecision = {}) {
  return api<WikiReviewDetail>(`/api/wiki/reviews/${id}/approve`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function rejectWikiReview(id: number, body: WikiReviewDecision = {}) {
  return api<WikiReviewDetail>(`/api/wiki/reviews/${id}/reject`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function acknowledgeWikiReview(id: number, body: WikiReviewDecision = {}) {
  return api<WikiReviewDetail>(`/api/wiki/reviews/${id}/acknowledge`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function listWikiRevisions(pageId: number) {
  return api<WikiRevision[]>(`/api/wiki/pages/${pageId}/revisions`)
}

export function getWikiRevision(pageId: number, revisionId: number) {
  return api<WikiRevision>(`/api/wiki/pages/${pageId}/revisions/${revisionId}`)
}

export interface WikiDiffQuery {
  from_revision?: number
  to_revision?: number
}

export function getWikiDiff(pageId: number, params: WikiDiffQuery = {}) {
  return api<WikiDiff>(
    withQuery(`/api/wiki/pages/${pageId}/diff`, {
      from_revision: params.from_revision,
      to_revision: params.to_revision,
    }),
  )
}

export function rollbackWikiPage(pageId: number, body: WikiRollbackRequest) {
  return api<WikiRollbackResponse>(`/api/wiki/pages/${pageId}/rollback`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
