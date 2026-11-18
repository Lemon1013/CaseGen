from datetime import datetime, time, timedelta, timezone

from sqlalchemy import and_, case, distinct, func
from sqlmodel import Session, select

from app.models.entities import (
    CaseDraft,
    DataPool,
    DataPoolRevision,
    Document,
    GenerationTask,
    IngestJob,
    PlatformArtifact,
    PlatformRenderRun,
    Project,
    Requirement,
    ReviewResult,
    TestCase,
    TestCaseOperationLog,
    User,
    WikiPageRow,
    WikiReviewItem,
    WikiSpace,
)
from app.schemas.dashboard import (
    CaseCountsOut,
    CaseOperationOut,
    CasePriorityCountOut,
    DashboardCountsOut,
    DashboardOverviewOut,
    DataPoolCountsOut,
    DocumentCountsOut,
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
    WikiCountsOut,
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


def _document_counts(session: Session) -> DocumentCountsOut:
    rows = _group_counts(session, Document.status)
    pending_ingest = _scalar_count(
        session,
        select(func.count(IngestJob.id)).where(
            IngestJob.status.in_(("queued", "running"))
        ),
    )
    return DocumentCountsOut(
        total=sum(count for _, count in rows),
        pending_ingest=pending_ingest,
        by_status=_status_counts(rows),
    )


def _wiki_counts(session: Session) -> WikiCountsOut:
    rows = _group_counts(session, WikiPageRow.status)
    pending_reviews = _scalar_count(
        session,
        select(func.count(WikiReviewItem.id)).where(
            WikiReviewItem.status == "pending"
        ),
    )
    return WikiCountsOut(
        pages=sum(count for _, count in rows),
        pending_reviews=pending_reviews,
        by_status=_status_counts(rows),
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
    status_counts = dict(_group_counts(session, TestCase.status))
    priority_counts = dict(_group_counts(session, TestCase.priority))
    return CaseCountsOut(
        total=sum(status_counts.values()),
        active=status_counts.get("active", 0),
        archived=status_counts.get("archived", 0),
        by_priority=[
            CasePriorityCountOut(
                priority=priority,
                count=priority_counts.get(priority, 0),
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
        documents=_document_counts(session),
        wiki=_wiki_counts(session),
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
    documents = _counts_by_date(session, Document, cutoff)
    points: list[TrendPointOut] = []
    for offset in range(14):
        date_value = (start_date + timedelta(days=offset)).isoformat()
        points.append(
            TrendPointOut(
                date=date_value,
                tasks=tasks.get(date_value, 0),
                cases=cases.get(date_value, 0),
                documents=documents.get(date_value, 0),
            )
        )
    return points


def _recent_activity(session: Session) -> list[RecentActivityOut]:
    candidates: list[dict] = []

    task_rows = session.exec(
        select(
            GenerationTask.id,
            Requirement.title,
            GenerationTask.status,
            GenerationTask.project_id,
            Requirement.project_id,
            GenerationTask.created_at,
        )
        .join(Requirement, GenerationTask.requirement_id == Requirement.id)
        .order_by(GenerationTask.created_at.desc(), GenerationTask.id.desc())
        .limit(6)
    ).all()
    for (
        task_id,
        title,
        status,
        project_id,
        requirement_project_id,
        created_at,
    ) in task_rows:
        candidates.append(
            {
                "type": "task",
                "id": int(task_id),
                "title": str(title or ""),
                "status": str(status or ""),
                "project_id": (
                    int(project_id)
                    if project_id is not None
                    else (
                        int(requirement_project_id)
                        if requirement_project_id is not None
                        else None
                    )
                ),
                "created_at": created_at,
            }
        )

    document_rows = session.exec(
        select(
            Document.id,
            Document.filename,
            Document.status,
            Document.space_id,
            Document.created_at,
        )
        .order_by(Document.created_at.desc(), Document.id.desc())
        .limit(6)
    ).all()
    for document_id, filename, status, space_id, created_at in document_rows:
        candidates.append(
            {
                "type": "document",
                "id": int(document_id),
                "title": str(filename or ""),
                "status": str(status or ""),
                "space_id": int(space_id) if space_id is not None else None,
                "created_at": created_at,
            }
        )

    review_rows = session.exec(
        select(
            WikiReviewItem.id,
            WikiReviewItem.reason,
            WikiPageRow.title,
            WikiReviewItem.status,
            WikiReviewItem.space_id,
            WikiReviewItem.created_at,
        )
        .outerjoin(WikiPageRow, WikiReviewItem.page_id == WikiPageRow.id)
        .order_by(WikiReviewItem.created_at.desc(), WikiReviewItem.id.desc())
        .limit(6)
    ).all()
    for review_id, reason, page_title, status, space_id, created_at in review_rows:
        candidates.append(
            {
                "type": "review",
                "id": int(review_id),
                "title": str(page_title or reason or ""),
                "status": str(status or ""),
                "space_id": int(space_id) if space_id is not None else None,
                "created_at": created_at,
            }
        )

    case_rows = session.exec(
        select(
            TestCase.id,
            TestCase.title,
            TestCase.status,
            Requirement.project_id,
            TestCase.created_at,
        )
        .join(Requirement, TestCase.requirement_id == Requirement.id)
        .order_by(TestCase.created_at.desc(), TestCase.id.desc())
        .limit(6)
    ).all()
    for case_id, title, status, project_id, created_at in case_rows:
        candidates.append(
            {
                "type": "case",
                "id": int(case_id),
                "title": str(title or ""),
                "status": str(status or ""),
                "project_id": (
                    int(project_id) if project_id is not None else None
                ),
                "created_at": created_at,
            }
        )

    space_ids = {
        item["space_id"]
        for item in candidates
        if item.get("space_id") is not None
    }
    space_projects: dict[int, int | None] = {}
    if space_ids:
        for space_id, project_id in session.exec(
            select(WikiSpace.id, WikiSpace.project_id).where(
                WikiSpace.id.in_(space_ids)
            )
        ).all():
            space_projects[int(space_id)] = (
                int(project_id) if project_id is not None else None
            )

    for item in candidates:
        if "space_id" in item:
            item["project_id"] = space_projects.get(item["space_id"])

    project_ids = {
        item["project_id"]
        for item in candidates
        if item.get("project_id") is not None
    }
    project_names: dict[int, str] = {}
    if project_ids:
        for project_id, name in session.exec(
            select(Project.id, Project.name).where(
                Project.id.in_(project_ids)
            )
        ).all():
            project_names[int(project_id)] = str(name or "")

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
            project_name=(
                project_names.get(item["project_id"], "")
                if item.get("project_id") is not None
                else ""
            ),
            created_at=item["created_at"],
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
