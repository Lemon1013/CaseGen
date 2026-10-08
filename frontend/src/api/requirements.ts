import { api } from './client'

export interface RequirementItem {
  id: number
  title: string
  description: string
  focus_tags: string[]
  source_filename?: string | null
  created_at: string
  updated_at: string
}

export interface RequirementDocImport {
  title: string
  text: string
  char_count: number
  stored_path: string
  sha256: string
}

export function listRequirements() {
  return api<RequirementItem[]>('/api/requirements')
}

export function getRequirement(id: number) {
  return api<RequirementItem>(`/api/requirements/${id}`)
}

export function importRequirementDoc(file: File, projectId?: number | null) {
  const form = new FormData()
  form.append('file', file)
  return api<RequirementDocImport>(
    `/api/requirements/import-doc${projectId ? `?project_id=${projectId}` : ''}`,
    { method: 'POST', body: form },
  )
}
