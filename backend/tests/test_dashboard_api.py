"""Global dashboard aggregation contract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlmodel import Session

from app import config
from app.db import get_engine
from app.main import create_app
from app.models.entities import (
    CaseDraft,
    DataPool,
    DataPoolRevision,
    Document,
    ExampleVariant,
    GenerationTask,
    IngestJob,
    PlatformArtifact,
    PlatformProfile,
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

ADMIN_PASSWORD = "CaseGen-Test1!"


def _origin() -> dict[str, str]:
    return {"Origin": "http://testserver"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _auth_client(tmp_app_data, monkeypatch) -> TestClient:
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    return TestClient(create_app())


def _setup_admin(client: TestClient) -> dict:
    response = client.post(
        "/api/auth/setup",
        headers=_origin(),
        json={
            "username": "rootadmin",
            "display_name": "Root",
            "password": ADMIN_PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]


def _project(
    session: Session,
    slug: str,
    *,
    status: str = "active",
    is_public: bool = True,
    created_at: datetime | None = None,
) -> Project:
    project = Project(
        name=slug,
        slug=slug,
        status=status,
        is_public=is_public,
    )
    if created_at is not None:
        project.created_at = created_at
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def _space(session: Session, project_id: int, slug: str) -> WikiSpace:
    space = WikiSpace(
        name=slug,
        slug=slug,
        scope="project",
        project_id=project_id,
    )
    session.add(space)
    session.commit()
    session.refresh(space)
    return space


def _task(
    session: Session,
    project_id: int,
    title: str,
    *,
    status: str = "completed",
    created_at: datetime | None = None,
) -> GenerationTask:
    requirement = Requirement(
        project_id=project_id,
        title=title,
        description=f"{title} description",
    )
    session.add(requirement)
    session.commit()
    session.refresh(requirement)
    task = GenerationTask(
        requirement_id=int(requirement.id),
        project_id=project_id,
        status=status,
    )
    if created_at is not None:
        task.created_at = created_at
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def _draft(session: Session, task_id: int, version: int) -> CaseDraft:
    draft = CaseDraft(
        task_id=task_id,
        version=version,
        content_md=f"draft v{version}",
    )
    session.add(draft)
    session.commit()
    session.refresh(draft)
    return draft


def _review(
    session: Session,
    task_id: int,
    draft_id: int,
    score: int,
    verdict: str,
) -> ReviewResult:
    review = ReviewResult(
        task_id=task_id,
        draft_id=draft_id,
        score=score,
        verdict=verdict,
    )
    session.add(review)
    session.commit()
    session.refresh(review)
    return review


def _document(
    session: Session,
    space_id: int,
    filename: str,
    *,
    status: str = "ready",
    created_at: datetime | None = None,
) -> Document:
    document = Document(
        filename=filename,
        stored_path=f"raw/{filename}",
        content_type="text/plain",
        sha256=f"sha-{filename}",
        status=status,
        space_id=space_id,
    )
    if created_at is not None:
        document.created_at = created_at
    session.add(document)
    session.commit()
    session.refresh(document)
    return document


def _ingest_job(
    session: Session,
    document_id: int,
    space_id: int,
    status: str,
) -> IngestJob:
    job = IngestJob(document_id=document_id, space_id=space_id, status=status)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _wiki_page(
    session: Session,
    space_id: int,
    title: str,
    *,
    status: str = "published",
) -> WikiPageRow:
    page = WikiPageRow(
        path=f"pages/{title}.md",
        title=title,
        page_type="concept",
        space_id=space_id,
        status=status,
    )
    session.add(page)
    session.commit()
    session.refresh(page)
    return page


def _wiki_review(
    session: Session,
    space_id: int,
    *,
    status: str,
    reason: str,
    page_id: int | None = None,
    created_at: datetime | None = None,
) -> WikiReviewItem:
    review = WikiReviewItem(
        page_id=page_id,
        space_id=space_id,
        status=status,
        reason=reason,
    )
    if created_at is not None:
        review.created_at = created_at
    session.add(review)
    session.commit()
    session.refresh(review)
    return review


def _case(
    session: Session,
    requirement_id: int,
    case_key: str,
    title: str,
    *,
    priority: str = "P1",
    status: str = "active",
    created_at: datetime | None = None,
) -> TestCase:
    case = TestCase(
        requirement_id=requirement_id,
        case_key=case_key,
        title=title,
        priority=priority,
        status=status,
    )
    if created_at is not None:
        case.created_at = created_at
    session.add(case)
    session.commit()
    session.refresh(case)
    return case


def _platform_run(
    session: Session,
    space_id: int,
    status: str,
) -> PlatformRenderRun:
    profile = PlatformProfile(
        wiki_space_id=space_id,
        name=f"platform-{status}",
    )
    session.add(profile)
    session.commit()
    session.refresh(profile)
    variant = ExampleVariant(platform_id=int(profile.id), name="default")
    session.add(variant)
    session.commit()
    session.refresh(variant)
    run = PlatformRenderRun(
        wiki_space_id=space_id,
        platform_id=int(profile.id),
        variant_id=int(variant.id),
        status=status,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _artifact(session: Session, run_id: int, filename: str) -> PlatformArtifact:
    artifact = PlatformArtifact(
        run_id=run_id,
        kind="case",
        filename=filename,
        content="content",
        content_hash=f"hash-{filename}",
    )
    session.add(artifact)
    session.commit()
    session.refresh(artifact)
    return artifact


def _data_pool(session: Session, space_id: int, name: str) -> DataPool:
    pool = DataPool(wiki_space_id=space_id, name=name)
    session.add(pool)
    session.commit()
    session.refresh(pool)
    return pool


def _data_pool_revision(
    session: Session,
    pool_id: int,
    revision: int,
) -> DataPoolRevision:
    row = DataPoolRevision(
        data_pool_id=pool_id,
        revision=revision,
        source_kind="csv",
        content_hash=f"pool-{pool_id}-{revision}",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _operation(
    session: Session,
    test_case_id: int,
    operation: str,
    created_at: datetime,
) -> TestCaseOperationLog:
    row = TestCaseOperationLog(
        test_case_id=test_case_id,
        operation=operation,
        created_at=created_at,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _bands(payload: dict) -> dict[str, int]:
    return {item["band"]: item["count"] for item in payload["score_bands"]}


def _status_map(items: list[dict]) -> dict[str, int]:
    return {item["status"]: item["count"] for item in items}


def test_dashboard_empty_database(tmp_app_data, monkeypatch):
    client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(client)

    response = client.get("/api/dashboard", headers=_origin())

    assert response.status_code == 200, response.text
    body = response.json()
    quality = body["review_quality"]
    assert quality["avg_score"] is None
    assert quality["reviewed_tasks"] == 0
    assert quality["generated_tasks"] == 0
    assert quality["coverage"] == 0
    assert [item["band"] for item in quality["score_bands"]] == [
        "0-59",
        "60-69",
        "70-79",
        "80-89",
        "90-100",
    ]
    assert all(item["count"] == 0 for item in quality["score_bands"])
    assert all(item["ratio"] == 0 for item in quality["score_bands"])

    counts = body["counts"]
    assert counts["users"] == {
        "total": 1,
        "active": 1,
        "admins": 1,
        "new_30d": 1,
    }
    assert counts["projects"]["total"] == 1
    assert counts["documents"] == {
        "total": 0,
        "pending_ingest": 0,
        "by_status": [],
    }
    assert counts["wiki"] == {
        "pages": 0,
        "pending_reviews": 0,
        "by_status": [],
    }
    assert counts["tasks"]["total"] == 0
    assert [item["group"] for item in counts["tasks"]["by_group"]] == [
        "draft",
        "waiting_confirmation",
        "in_progress",
        "completed",
        "failed",
    ]
    assert all(item["count"] == 0 for item in counts["tasks"]["by_group"])
    assert counts["cases"]["total"] == 0
    assert counts["cases"]["active"] == 0
    assert counts["cases"]["archived"] == 0
    assert [item["priority"] for item in counts["cases"]["by_priority"]] == [
        "P0",
        "P1",
        "P2",
    ]
    assert all(item["count"] == 0 for item in counts["cases"]["by_priority"])
    assert counts["platform"] == {
        "runs": 0,
        "artifacts": 0,
        "by_status": [],
    }
    assert counts["data_pools"] == {"total": 0, "revisions": 0}
    assert body["case_operations"] == []
    assert body["recent_activity"] == []
    assert len(body["trend"]) == 14
    assert all(
        point["tasks"] == 0
        and point["cases"] == 0
        and point["documents"] == 0
        for point in body["trend"]
    )
    assert body["generated_at"].endswith("Z")


def test_dashboard_requires_authentication_and_admin(tmp_app_data, monkeypatch):
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    anonymous = TestClient(create_app())
    assert anonymous.get("/api/dashboard", headers=_origin()).status_code == 401

    _setup_admin(admin_client)
    created = admin_client.post(
        "/api/users",
        headers=_origin(),
        json={
            "username": "member",
            "display_name": "Member",
            "password": ADMIN_PASSWORD,
            "role": "user",
        },
    )
    assert created.status_code == 201, created.text

    member_client = TestClient(create_app())
    login = member_client.post(
        "/api/auth/login",
        headers=_origin(),
        json={"username": "member", "password": ADMIN_PASSWORD},
    )
    assert login.status_code == 200, login.text
    assert member_client.get("/api/dashboard", headers=_origin()).status_code == 403
    assert admin_client.get("/api/dashboard", headers=_origin()).status_code == 200


def test_dashboard_counts_only_latest_valid_reviews(tmp_app_data, monkeypatch):
    client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(client)

    with Session(get_engine()) as session:
        project = _project(session, "dashboard-latest")

        task_a = _task(session, int(project.id), "Task A")
        draft_a1 = _draft(session, int(task_a.id), 1)
        _review(session, int(task_a.id), int(draft_a1.id), 60, "pass")
        draft_a2 = _draft(session, int(task_a.id), 2)
        _review(session, int(task_a.id), int(draft_a2.id), 90, "pass")

        task_b = _task(session, int(project.id), "Task B")
        draft_b1 = _draft(session, int(task_b.id), 1)
        _review(session, int(task_b.id), int(draft_b1.id), 50, "revise")
        _draft(session, int(task_b.id), 2)

        task_c = _task(session, int(project.id), "Task C")
        draft_c1 = _draft(session, int(task_c.id), 1)
        _review(session, int(task_c.id), int(draft_c1.id), 0, "unknown")

        task_d = _task(session, int(project.id), "Task D")
        draft_d1 = _draft(session, int(task_d.id), 1)
        _review(session, int(task_d.id), int(draft_d1.id), 70, "pass")

        task_e = _task(session, int(project.id), "Task E")
        _draft(session, int(task_e.id), 1)

    response = client.get("/api/dashboard", headers=_origin())

    assert response.status_code == 200, response.text
    quality = response.json()["review_quality"]
    assert quality["generated_tasks"] == 5
    assert quality["reviewed_tasks"] == 2
    assert quality["avg_score"] == 80.0
    assert quality["coverage"] == 0.4
    assert _bands(quality) == {
        "0-59": 0,
        "60-69": 0,
        "70-79": 1,
        "80-89": 0,
        "90-100": 1,
    }
    assert [item["ratio"] for item in quality["score_bands"]] == [
        0,
        0,
        0.5,
        0,
        0.5,
    ]


def test_dashboard_zero_score_fail_is_valid_but_unknown_is_not(
    tmp_app_data, monkeypatch
):
    client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(client)

    with Session(get_engine()) as session:
        project = _project(session, "dashboard-zero-score")

        failed_task = _task(session, int(project.id), "Failed review")
        failed_draft = _draft(session, int(failed_task.id), 1)
        _review(
            session,
            int(failed_task.id),
            int(failed_draft.id),
            0,
            "fail",
        )

        unknown_task = _task(session, int(project.id), "Unparsed review")
        unknown_draft = _draft(session, int(unknown_task.id), 1)
        _review(
            session,
            int(unknown_task.id),
            int(unknown_draft.id),
            0,
            "unknown",
        )

    response = client.get("/api/dashboard", headers=_origin())

    assert response.status_code == 200, response.text
    quality = response.json()["review_quality"]
    assert quality["generated_tasks"] == 2
    assert quality["reviewed_tasks"] == 1
    assert quality["avg_score"] == 0.0
    assert quality["coverage"] == 0.5
    assert _bands(quality) == {
        "0-59": 1,
        "60-69": 0,
        "70-79": 0,
        "80-89": 0,
        "90-100": 0,
    }


def test_dashboard_score_band_boundaries(tmp_app_data, monkeypatch):
    client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(client)

    scores = [0, 59, 60, 69, 70, 79, 80, 89, 90, 100]
    with Session(get_engine()) as session:
        project = _project(session, "dashboard-bands")
        for score in scores:
            task = _task(session, int(project.id), f"Task {score}")
            draft = _draft(session, int(task.id), 1)
            _review(
                session,
                int(task.id),
                int(draft.id),
                score,
                "fail" if score == 0 else "pass",
            )

    response = client.get("/api/dashboard", headers=_origin())

    assert response.status_code == 200, response.text
    quality = response.json()["review_quality"]
    assert quality["generated_tasks"] == len(scores)
    assert quality["reviewed_tasks"] == len(scores)
    assert quality["avg_score"] == 69.6
    assert quality["coverage"] == 1.0
    assert _bands(quality) == {
        "0-59": 2,
        "60-69": 2,
        "70-79": 2,
        "80-89": 2,
        "90-100": 2,
    }


def test_dashboard_aggregates_counts_across_projects(tmp_app_data, monkeypatch):
    client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(client)
    now = _now()

    with Session(get_engine()) as session:
        project_a = _project(session, "dashboard-project-a")
        project_b = _project(session, "dashboard-project-b")
        _project(
            session,
            "dashboard-project-old",
            status="archived",
            is_public=False,
            created_at=now - timedelta(days=40),
        )
        session.add(
            User(
                username="oldadmin",
                password_hash="unused",
                role="admin",
                is_active=False,
                created_at=now - timedelta(days=40),
            )
        )
        session.commit()

        space_a = _space(session, int(project_a.id), "dashboard-space-a")
        space_b = _space(session, int(project_b.id), "dashboard-space-b")

        statuses = [
            "draft",
            "awaiting_confirmation",
            "awaiting_test_point_confirmation",
            "retrieving",
            "generating_test_points",
            "generating",
            "reviewing",
            "optimizing",
            "regenerating",
            "generated",
            "reviewed",
            "finalized",
            "failed",
        ]
        task_a = None
        task_b = None
        for index, status in enumerate(statuses):
            project_id = int(project_a.id if index % 2 == 0 else project_b.id)
            task = _task(session, project_id, f"Status {status}", status=status)
            if status == "generated":
                task_a = task
            if status == "failed":
                task_b = task

        document_pending = _document(
            session, int(space_a.id), "pending.txt", status="pending"
        )
        _document(session, int(space_a.id), "ready.txt", status="ready")
        _document(session, int(space_b.id), "failed.txt", status="failed")
        _ingest_job(
            session, int(document_pending.id), int(space_a.id), "queued"
        )
        _ingest_job(
            session, int(document_pending.id), int(space_a.id), "running"
        )
        _ingest_job(
            session, int(document_pending.id), int(space_a.id), "completed"
        )

        page_one = _wiki_page(session, int(space_a.id), "Page One")
        _wiki_page(session, int(space_a.id), "Page Two")
        _wiki_page(
            session, int(space_b.id), "Old Page", status="archived"
        )
        _wiki_review(
            session,
            int(space_a.id),
            status="pending",
            reason="needs review",
            page_id=int(page_one.id),
        )
        _wiki_review(
            session, int(space_b.id), status="pending", reason="conflict"
        )
        _wiki_review(
            session, int(space_b.id), status="approved", reason="ok"
        )

        case_requirement_id = int(task_a.requirement_id)
        case_p0 = _case(
            session, case_requirement_id, "case-p0", "P0 case", priority="P0"
        )
        _case(
            session, case_requirement_id, "case-p1", "P1 case", priority="P1"
        )
        _case(
            session,
            case_requirement_id,
            "case-p2",
            "P2 case",
            priority="P2",
        )
        case_archived = _case(
            session,
            int(task_b.requirement_id),
            "case-archived",
            "Archived case",
            priority="P1",
            status="archived",
        )

        run_one = _platform_run(session, int(space_a.id), "completed")
        run_two = _platform_run(session, int(space_b.id), "completed")
        _platform_run(session, int(space_b.id), "failed")
        _artifact(session, int(run_one.id), "one.json")
        _artifact(session, int(run_two.id), "two.json")

        pool_one = _data_pool(session, int(space_a.id), "Pool One")
        pool_two = _data_pool(session, int(space_b.id), "Pool Two")
        _data_pool_revision(session, int(pool_one.id), 1)
        _data_pool_revision(session, int(pool_one.id), 2)
        _data_pool_revision(session, int(pool_two.id), 1)

        for _ in range(3):
            _operation(session, int(case_p0.id), "import", now - timedelta(hours=1))
        for _ in range(2):
            _operation(
                session,
                int(case_archived.id),
                "archive",
                now - timedelta(days=2),
            )
        _operation(session, int(case_p0.id), "edit", now - timedelta(days=3))
        _operation(session, int(case_p0.id), "import", now - timedelta(days=8))

    response = client.get("/api/dashboard", headers=_origin())

    assert response.status_code == 200, response.text
    counts = response.json()["counts"]
    assert counts["users"] == {
        "total": 2,
        "active": 1,
        "admins": 2,
        "new_30d": 1,
    }
    assert counts["projects"] == {
        "total": 4,
        "active": 3,
        "public": 3,
        "new_30d": 3,
    }
    assert counts["documents"]["total"] == 3
    assert counts["documents"]["pending_ingest"] == 2
    assert _status_map(counts["documents"]["by_status"]) == {
        "failed": 1,
        "pending": 1,
        "ready": 1,
    }
    assert counts["wiki"]["pages"] == 3
    assert counts["wiki"]["pending_reviews"] == 2
    assert _status_map(counts["wiki"]["by_status"]) == {
        "archived": 1,
        "published": 2,
    }
    assert counts["tasks"]["total"] == 13
    assert {
        item["group"]: item["count"] for item in counts["tasks"]["by_group"]
    } == {
        "draft": 1,
        "waiting_confirmation": 2,
        "in_progress": 6,
        "completed": 3,
        "failed": 1,
    }
    assert counts["cases"]["total"] == 4
    assert counts["cases"]["active"] == 3
    assert counts["cases"]["archived"] == 1
    assert {
        item["priority"]: item["count"]
        for item in counts["cases"]["by_priority"]
    } == {"P0": 1, "P1": 2, "P2": 1}
    assert counts["platform"]["runs"] == 3
    assert counts["platform"]["artifacts"] == 2
    assert _status_map(counts["platform"]["by_status"]) == {
        "completed": 2,
        "failed": 1,
    }
    assert counts["data_pools"] == {"total": 2, "revisions": 3}
    assert response.json()["case_operations"] == [
        {"operation": "import", "count": 3},
        {"operation": "archive", "count": 2},
        {"operation": "edit", "count": 1},
    ]


def test_dashboard_trend_zero_fills_fourteen_utc_days(tmp_app_data, monkeypatch):
    client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(client)
    now = _now()
    today = now.date()

    with Session(get_engine()) as session:
        project = _project(session, "dashboard-trend")
        space = _space(session, int(project.id), "dashboard-trend-space")

        _task(
            session,
            int(project.id),
            "Trend task one",
            created_at=now - timedelta(days=2),
        )
        _task(
            session,
            int(project.id),
            "Trend task two",
            created_at=now - timedelta(days=3),
        )
        _task(
            session,
            int(project.id),
            "Trend task three",
            created_at=now - timedelta(days=3),
        )
        trend_task = _task(
            session, int(project.id), "Trend case requirement"
        )
        _case(
            session,
            int(trend_task.requirement_id),
            "trend-case",
            "Trend case",
            created_at=now - timedelta(days=5),
        )
        _document(
            session,
            int(space.id),
            "trend.txt",
            created_at=now - timedelta(days=9),
        )

        _task(
            session,
            int(project.id),
            "Outside task",
            created_at=now - timedelta(days=15),
        )
        _case(
            session,
            int(trend_task.requirement_id),
            "outside-case",
            "Outside case",
            created_at=now - timedelta(days=15),
        )
        _document(
            session,
            int(space.id),
            "outside.txt",
            created_at=now - timedelta(days=15),
        )

    response = client.get("/api/dashboard", headers=_origin())

    assert response.status_code == 200, response.text
    trend = response.json()["trend"]
    assert len(trend) == 14
    assert [point["date"] for point in trend] == [
        (today - timedelta(days=offset)).isoformat()
        for offset in range(13, -1, -1)
    ]
    by_date = {point["date"]: point for point in trend}
    assert by_date[(today - timedelta(days=2)).isoformat()]["tasks"] == 1
    assert by_date[(today - timedelta(days=3)).isoformat()]["tasks"] == 2
    assert by_date[(today - timedelta(days=5)).isoformat()]["cases"] == 1
    assert by_date[(today - timedelta(days=9)).isoformat()]["documents"] == 1
    assert sum(point["tasks"] for point in trend) == 4
    assert sum(point["cases"] for point in trend) == 1
    assert sum(point["documents"] for point in trend) == 1


def test_dashboard_recent_activity_limit_order_and_resolution(
    tmp_app_data, monkeypatch
):
    client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(client)
    now = _now()

    with Session(get_engine()) as session:
        project = _project(session, "dashboard-recent")
        project_id = int(project.id)
        space = _space(session, int(project.id), "dashboard-recent-space")
        task_one = _task(
            session,
            int(project.id),
            "Recent task one",
            created_at=now - timedelta(minutes=1),
        )
        page = _wiki_page(session, int(space.id), "Review Page")
        _wiki_review(
            session,
            int(space.id),
            status="pending",
            reason="review-reason-page",
            page_id=int(page.id),
            created_at=now - timedelta(minutes=2),
        )
        _wiki_review(
            session,
            int(space.id),
            status="approved",
            reason="review-reason-only",
            created_at=now - timedelta(minutes=3),
        )
        _document(
            session,
            int(space.id),
            "recent-document.txt",
            created_at=now - timedelta(minutes=4),
        )
        case = _case(
            session,
            int(task_one.requirement_id),
            "recent-case",
            "Recent case",
            created_at=now - timedelta(minutes=5),
        )
        case_id = int(case.id)
        _task(
            session,
            int(project.id),
            "Recent task two",
            created_at=now - timedelta(minutes=6),
        )
        _document(
            session,
            int(space.id),
            "older-document.txt",
            created_at=now - timedelta(minutes=7),
        )
        _case(
            session,
            int(task_one.requirement_id),
            "older-case",
            "Older case",
            created_at=now - timedelta(minutes=8),
        )

    response = client.get("/api/dashboard", headers=_origin())

    assert response.status_code == 200, response.text
    activity = response.json()["recent_activity"]
    assert len(activity) == 6
    assert [item["type"] for item in activity] == [
        "task",
        "review",
        "review",
        "document",
        "case",
        "task",
    ]
    assert [item["created_at"] for item in activity] == sorted(
        [item["created_at"] for item in activity], reverse=True
    )
    assert {item["type"] for item in activity} == {
        "task",
        "document",
        "review",
        "case",
    }
    assert activity[0]["title"] == "Recent task one"
    assert activity[0]["project_id"] == project_id
    assert activity[0]["project_name"] == "dashboard-recent"
    assert activity[1]["title"] == "Review Page"
    assert activity[1]["project_id"] == project_id
    assert activity[2]["title"] == "review-reason-only"
    assert activity[3]["title"] == "recent-document.txt"
    assert activity[3]["project_id"] == project_id
    assert activity[4]["title"] == "Recent case"
    assert activity[4]["id"] == case_id
    assert activity[4]["project_id"] == project_id
