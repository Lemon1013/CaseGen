from typing import Literal, Optional

from pydantic import BaseModel


class ReviewScoreBandOut(BaseModel):
    band: str
    count: int
    ratio: float


class ReviewQualityOut(BaseModel):
    avg_score: Optional[float]
    reviewed_tasks: int
    generated_tasks: int
    coverage: float
    score_bands: list[ReviewScoreBandOut]


class StatusCountOut(BaseModel):
    status: str
    count: int


class TaskGroupCountOut(BaseModel):
    group: str
    count: int


class CasePriorityCountOut(BaseModel):
    priority: str
    count: int


class UserCountsOut(BaseModel):
    total: int
    active: int
    admins: int
    new_30d: int


class ProjectCountsOut(BaseModel):
    total: int
    active: int
    public: int
    new_30d: int


class AgentCountsOut(BaseModel):
    conversations: int
    active_conversations: int
    messages: int
    new_30d: int


class TaskCountsOut(BaseModel):
    total: int
    by_group: list[TaskGroupCountOut]


class CaseCountsOut(BaseModel):
    total: int
    active: int
    archived: int
    by_priority: list[CasePriorityCountOut]


class PlatformCountsOut(BaseModel):
    runs: int
    artifacts: int
    by_status: list[StatusCountOut]


class DataPoolCountsOut(BaseModel):
    total: int
    revisions: int


class DashboardCountsOut(BaseModel):
    users: UserCountsOut
    projects: ProjectCountsOut
    agent: AgentCountsOut
    tasks: TaskCountsOut
    cases: CaseCountsOut
    platform: PlatformCountsOut
    data_pools: DataPoolCountsOut


class CaseOperationOut(BaseModel):
    operation: str
    count: int


class TrendPointOut(BaseModel):
    date: str
    tasks: int
    cases: int
    agent_conversations: int


class RecentActivityOut(BaseModel):
    type: Literal["task", "case"]
    id: int
    title: str
    status: str
    project_id: Optional[int]
    project_name: str
    created_at: str


class DashboardOverviewOut(BaseModel):
    review_quality: ReviewQualityOut
    counts: DashboardCountsOut
    case_operations: list[CaseOperationOut]
    trend: list[TrendPointOut]
    recent_activity: list[RecentActivityOut]
    generated_at: str
