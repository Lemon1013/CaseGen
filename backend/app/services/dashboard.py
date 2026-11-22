from datetime import datetime, time, timedelta, timezone

from sqlalchemy import and_, case, distinct, func
from sqlmodel import Session, select

from app.models.entities import (
    AgentConversation,
    AgentMessage,
    CaseDraft,
    DataPool,
    DataPoolRevision,
    GenerationTask,
    PlatformArtifact,
    PlatformRenderRun,
    Project,
    Requirement,
    ReviewResult,
    TestCase,
    TestCaseOperationLog,
    User,
)
from app.schemas.dashboard import (
    AgentCountsOut,
    CaseCountsOut,
    CaseOperationOut,
    CasePriorityCountOut,
    DashboardCountsOut,
    DashboardOverviewOut,
    DataPoolCountsOut,
    PlatformCountsOut,
    ProjectCountsOut,
    RecentActivityOut,
    ReviewQualityOut,
    ReviewScoreBandOut,
    StatusCountOut,
    TaskCountsOut,
    TaskGroupCountOut,
    TrendPointOut,
    UserCountsOut,
)

_SCORE_BANDS = (
    ("0-59", 0, 59),
    ("60-69", 60, 69),
    ("70-79", 70, 79),
    ("80-89", 80, 89),
    ("90-100", 90, 100),
)

_TASK_GROUPS = (
    ("draft", ("draft",)),
    (
        "waiting_confirmation",
        ("awaiting_confirmation", "awaiting_test_point_confirmation"),
    ),
    (
        "in_progress",
        (
            "retrieving",
            "generating_test_points",
            "generating",
            "reviewing",
            "optimizing",
            "regenerating",
        ),
    ),
    ("completed", ("generated", "reviewed", "finalized")),
    ("failed", ("failed",)),
)

_CASE_PRIORITIES = ("P0", "P1", "P2")


def _latest_drafts_subquery():
    """One latest draft per task: max version, then max id on ties."""

    latest_versions = (
        select(
            CaseDraft.task_id.label("task_id"),
            func.max(CaseDraft.version).label("max_version"),
        )
        .group_by(CaseDraft.task_id)
        .subquery()
    )
    return (
        select(
            CaseDraft.task_id.label("task_id"),
            func.max(CaseDraft.id).label("draft_id"),
        )
        .join(
            latest_versions,
            CaseDraft.task_id == latest_versions.c.task_id,
        )
        .where(CaseDraft.version == latest_versions.c.max_version)
        .group_by(CaseDraft.task_id)
        .subquery()
    )


def _latest_reviews_subquery():
    """One latest review per task; ReviewResult is append-only by id."""

    return (
        select(
            ReviewResult.task_id.label("task_id"),
            func.max(ReviewResult.id).label("review_id"),
        )
        .group_by(ReviewResult.task_id)
        .subquery()
    )


def _scalar_count(session: Session, statement) -> int:
    return int(session.exec(statement).one() or 0)


def _group_counts(session: Session, column) -> list[tuple[str, int]]:
    rows = session.exec(
        select(column, func.count()).group_by(column).order_by(column)
    ).all()
    return [
        (str(value), int(count or 0))
        for value, count in rows
        if value is not None
    ]


def _status_counts(rows: list[tuple[str, int]]) -> list[StatusCountOut]:
    return [StatusCountOut(status=status, count=count) for status, count in rows]


def _user_counts(session: Session, now: datetime) -> UserCountsOut:
    cutoff = now - timedelta(days=30)
    row = session.exec(
        select(
            func.count(User.id),
            func.coalesce(
                func.sum(case((User.is_active.is_(True), 1), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((User.role == "admin", 1), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((User.created_at >= cutoff, 1), else_=0)), 0
            ),
        )
    ).one()
    return UserCountsOut(
        total=int(row[0] or 0),
        active=int(row[1] or 0),
        admins=int(row[2] or 0),
        new_30d=int(row[3] or 0),
    )


def _project_counts(session: Session, now: datetime) -> ProjectCountsOut:
    cutoff = now - timedelta(days=30)
    row = session.exec(
        select(
            func.count(Project.id),
            func.coalesce(
                func.sum(case((Project.status == "active", 1), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((Project.is_public.is_(True), 1), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((Project.created_at >= cutoff, 1), else_=0)), 0
            ),
        )
    ).one()
    return ProjectCountsOut(
        total=int(row[0] or 0),
        active=int(row[1] or 0),
        public=int(row[2] or 0),
        new_30d=int(row[3] or 0),
    )


def _format_utc_iso(value: datetime) -> str:
    """将数据库存储的 UTC datetime 统一格式化为带 Z 后缀的 ISO-8601 字符串。"""

    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _agent_counts(session: Session, now: datetime) -> AgentCountsOut:
    cutoff = now - timedelta(days=30)
    conv_row = session.exec(
        select(
            func.count(AgentConversation.id),
            func.coalesce(
                func.sum(
                    case(
                        (AgentConversation.status.in_(("active", "busy")), 1),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case((AgentConversation.created_at >= cutoff, 1), else_=0)
                ),
                0,
            ),
        )
    ).one()
    message_total = _scalar_count(session, select(func.count(AgentMessage.id)))
    return AgentCountsOut(
        conversations=int(conv_row[0] or 0),
        active_conversations=int(conv_row[1] or 0),
        messages=message_total,
        new_30d=int(conv_row[2] or 0),
    )


def _task_counts(session: Session) -> TaskCountsOut:
    status_counts = dict(_group_counts(session, GenerationTask.status))
    by_group = [
        TaskGroupCountOut(
            group=group,
            count=sum(status_counts.get(status, 0) for status in statuses),
        )
        for group, statuses in _TASK_GROUPS
    ]
    return TaskCountsOut(
        total=sum(status_counts.values()),
        by_group=by_group,
    )


def _case_counts(session: Session) -> CaseCountsOut:
    row = session.exec(
        select(
            func.count(TestCase.id),
            func.coalesce(
                func.sum(case((TestCase.status == "active", 1), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((TestCase.status == "archived", 1), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((TestCase.priority == "P0", 1), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((TestCase.priority == "P1", 1), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((TestCase.priority == "P2", 1), else_=0)), 0
            ),
        )
    ).one()
    priority_map = {
        "P0": int(row[3] or 0),
        "P1": int(row[4] or 0),
        "P2": int(row[5] or 0),
    }
    return CaseCountsOut(
        total=int(row[0] or 0),
        active=int(row[1] or 0),
        archived=int(row[2] or 0),
        by_priority=[
            CasePriorityCountOut(
                priority=priority,
                count=priority_map.get(priority, 0),
            )
            for priority in _CASE_PRIORITIES
        ],
    )


def _platform_counts(session: Session) -> PlatformCountsOut:
    rows = _group_counts(session, PlatformRenderRun.status)
    artifacts = _scalar_count(
        session, select(func.count(PlatformArtifact.id))
    )
    return PlatformCountsOut(
        runs=sum(count for _, count in rows),
        artifacts=artifacts,
        by_status=_status_counts(rows),
    )


def _data_pool_counts(session: Session) -> DataPoolCountsOut:
    return DataPoolCountsOut(
        total=_scalar_count(session, select(func.count(DataPool.id))),
        revisions=_scalar_count(
            session, select(func.count(DataPoolRevision.id))
        ),
    )


def _dashboard_counts(
    session: Session, now: datetime
) -> DashboardCountsOut:
    return DashboardCountsOut(
        users=_user_counts(session, now),
        projects=_project_counts(session, now),
        agent=_agent_counts(session, now),
        tasks=_task_counts(session),
        cases=_case_counts(session),
        platform=_platform_counts(session),
        data_pools=_data_pool_counts(session),
    )


def _case_operations(session: Session, now: datetime) -> list[CaseOperationOut]:
    cutoff = now - timedelta(days=7)
    total = func.count(TestCaseOperationLog.id)
    rows = session.exec(
        select(TestCaseOperationLog.operation, total)
        .where(TestCaseOperationLog.created_at >= cutoff)
        .group_by(TestCaseOperationLog.operation)
        .order_by(total.desc(), TestCaseOperationLog.operation.asc())
    ).all()
    return [
        CaseOperationOut(operation=str(operation), count=int(count or 0))
        for operation, count in rows
    ]


def _counts_by_date(session: Session, model, cutoff: datetime) -> dict[str, int]:
    day = func.date(model.created_at)
    rows = session.exec(
        select(day, func.count(model.id))
        .where(model.created_at >= cutoff)
        .group_by(day)
    ).all()
    return {str(value): int(count or 0) for value, count in rows}


def _trend(session: Session, now: datetime) -> list[TrendPointOut]:
    today = now.date()
    start_date = today - timedelta(days=13)
    cutoff = datetime.combine(start_date, time.min)
    tasks = _counts_by_date(session, GenerationTask, cutoff)
    cases = _counts_by_date(session, TestCase, cutoff)
    conversations = _counts_by_date(session, AgentConversation, cutoff)
    points: list[TrendPointOut] = []
    for offset in range(14):
        date_value = (start_date + timedelta(days=offset)).isoformat()
        points.append(
            TrendPointOut(
                date=date_value,
                tasks=tasks.get(date_value, 0),
                cases=cases.get(date_value, 0),
                agent_conversations=conversations.get(date_value, 0),
            )
        )
    return points


def _recent_activity(session: Session) -> list[RecentActivityOut]:
    # 严守隐私红线：管理员看板最近活动仅展示任务与用例，绝不暴露任何用户私密 Agent 会话。
    candidates: list[dict] = []

    resolved_task_project = func.coalesce(
        GenerationTask.project_id, Requirement.project_id
    )
    task_rows = session.exec(
        select(
            GenerationTask.id,
            Requirement.title,
            GenerationTask.status,
            resolved_task_project,
            Project.name,
            GenerationTask.created_at,
        )
        .join(Requirement, GenerationTask.requirement_id == Requirement.id)
        .outerjoin(Project, resolved_task_project == Project.id)
        .order_by(GenerationTask.created_at.desc(), GenerationTask.id.desc())
        .limit(6)
    ).all()
    for (
        task_id,
        title,
        status,
        project_id,
        project_name,
        created_at,
    ) in task_rows:
        candidates.append(
            {
                "type": "task",
                "id": int(task_id),
                "title": str(title or ""),
                "status": str(status or ""),
                "project_id": int(project_id) if project_id is not None else None,
                "project_name": str(project_name or "") if project_id is not None else "",
                "created_at": created_at,
            }
        )

    case_rows = session.exec(
        select(
            TestCase.id,
            TestCase.title,
            TestCase.status,
            Requirement.project_id,
            Project.name,
            TestCase.created_at,
        )
        .join(Requirement, TestCase.requirement_id == Requirement.id)
        .outerjoin(Project, Requirement.project_id == Project.id)
        .order_by(TestCase.created_at.desc(), TestCase.id.desc())
        .limit(6)
    ).all()
    for (
        case_id,
        title,
        status,
        project_id,
        project_name,
        created_at,
    ) in case_rows:
        candidates.append(
            {
                "type": "case",
                "id": int(case_id),
                "title": str(title or ""),
                "status": str(status or ""),
                "project_id": int(project_id) if project_id is not None else None,
                "project_name": str(project_name or "") if project_id is not None else "",
                "created_at": created_at,
            }
        )

    candidates.sort(
        key=lambda item: (item["created_at"], item["id"]),
        reverse=True,
    )
    return [
        RecentActivityOut(
            type=item["type"],
            id=item["id"],
            title=item["title"],
            status=item["status"],
            project_id=item.get("project_id"),
            project_name=item.get("project_name", ""),
            created_at=_format_utc_iso(item["created_at"]),
        )
        for item in candidates[:6]
    ]


def get_review_quality(session: Session) -> ReviewQualityOut:
    latest_drafts = _latest_drafts_subquery()
    latest_reviews = _latest_reviews_subquery()
    valid_reviews = (
        select(ReviewResult.score.label("score"))
        .join(latest_reviews, ReviewResult.id == latest_reviews.c.review_id)
        .join(
            latest_drafts,
            and_(
                ReviewResult.task_id == latest_drafts.c.task_id,
                ReviewResult.draft_id == latest_drafts.c.draft_id,
            ),
        )
        .where(ReviewResult.verdict != "unknown")
        .subquery()
    )

    band_columns = [
        func.coalesce(
            func.sum(
                case(
                    (
                        and_(
                            valid_reviews.c.score >= lower,
                            valid_reviews.c.score <= upper,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label(f"band_{index}")
        for index, (_, lower, upper) in enumerate(_SCORE_BANDS)
    ]
    aggregate = session.exec(
        select(
            func.count(valid_reviews.c.score).label("reviewed_tasks"),
            func.avg(valid_reviews.c.score).label("avg_score"),
            *band_columns,
        ).select_from(valid_reviews)
    ).one()
    reviewed_tasks = int(aggregate[0] or 0)
    avg_raw = aggregate[1]
    band_counts = [int(value or 0) for value in aggregate[2:]]

    generated_tasks = _scalar_count(
        session, select(func.count(distinct(CaseDraft.task_id)))
    )

    score_bands = [
        ReviewScoreBandOut(
            band=label,
            count=count,
            ratio=round(count / reviewed_tasks, 4) if reviewed_tasks else 0.0,
        )
        for (label, _, _), count in zip(_SCORE_BANDS, band_counts)
    ]
    return ReviewQualityOut(
        avg_score=round(float(avg_raw), 1) if avg_raw is not None else None,
        reviewed_tasks=reviewed_tasks,
        generated_tasks=generated_tasks,
        coverage=(
            round(reviewed_tasks / generated_tasks, 4)
            if generated_tasks
            else 0.0
        ),
        score_bands=score_bands,
    )


def get_dashboard_overview(session: Session) -> DashboardOverviewOut:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return DashboardOverviewOut(
        review_quality=get_review_quality(session),
        counts=_dashboard_counts(session, now),
        case_operations=_case_operations(session, now),
        trend=_trend(session, now),
        recent_activity=_recent_activity(session),
        generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
