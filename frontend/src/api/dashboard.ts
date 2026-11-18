import { api } from './client'

export interface ReviewScoreBand {
  band: string
  count: number
  ratio: number
}

export interface ReviewQuality {
  avg_score: number | null
  reviewed_tasks: number
  generated_tasks: number
  coverage: number
  score_bands: ReviewScoreBand[]
}

export interface DashboardUserCounts {
  total: number
  active: number
  admins: number
  new_30d: number
}

export interface DashboardProjectCounts {
  total: number
  active: number
  public: number
  new_30d: number
}

export interface DashboardStatusCount {
  status: string
  count: number
}

export interface DashboardDocumentCounts {
  total: number
  pending_ingest: number
  by_status: DashboardStatusCount[]
}

export interface DashboardWikiCounts {
  pages: number
  pending_reviews: number
  by_status: DashboardStatusCount[]
}

export interface DashboardGroupCount {
  group: string
  count: number
}

export interface DashboardTaskCounts {
  total: number
  by_group: DashboardGroupCount[]
}

export interface DashboardPriorityCount {
  priority: string
  count: number
}

export interface DashboardCaseCounts {
  total: number
  active: number
  archived: number
  by_priority: DashboardPriorityCount[]
}

export interface DashboardPlatformCounts {
  runs: number
  artifacts: number
  by_status: DashboardStatusCount[]
}

export interface DashboardDataPoolCounts {
  total: number
  revisions: number
}

export interface DashboardCounts {
  users: DashboardUserCounts
  projects: DashboardProjectCounts
  documents: DashboardDocumentCounts
  wiki: DashboardWikiCounts
  tasks: DashboardTaskCounts
  cases: DashboardCaseCounts
  platform: DashboardPlatformCounts
  data_pools: DashboardDataPoolCounts
}

export interface CaseOperationCount {
  operation: string
  count: number
}

export interface TrendPoint {
  date: string
  tasks: number
  cases: number
  documents: number
}

export interface RecentActivity {
  type: string
  id: number | string
  title: string
  status: string
  project_id: number | null
  project_name: string | null
  created_at: string
}

export interface DashboardOverview {
  counts: DashboardCounts
  case_operations: CaseOperationCount[]
  trend: TrendPoint[]
  recent_activity: RecentActivity[]
  review_quality: ReviewQuality
  generated_at: string
}

export function getDashboardOverview() {
  return api<DashboardOverview>('/api/dashboard')
}
