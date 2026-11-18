import json

from sqlmodel import SQLModel, Session, create_engine, select

from app.models.entities import (
    CaseDraft,
    GenerationTask,
    Requirement,
    TestCase as _TestCaseRow,
    TestCaseOperationLog as _TestCaseOperationLogRow,
    TaskEvent,
)
from app.services.case_management import (
    CaseDraftParseError,
    CaseKeyConflictError,
    find_finalize_conflicts,
    split_case_draft,
    utcnow,
)
from app.api.cases import _case_out
from app.services.task_pipeline import finalize_task


def _session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _task_with_draft(session, requirement_id, content_md, version=1):
    task = GenerationTask(requirement_id=requirement_id, status="generated")
    session.add(task)
    session.flush()
    draft = CaseDraft(task_id=task.id, version=version, content_md=content_md)
    session.add(draft)
    session.commit()
    return task, draft


def test_split_case_draft_preserves_preamble_and_rejects_duplicate_keys():
    sections = split_case_draft("# 说明\n\n## TC-002 - B\nB\n\n## TC-001: A\nA")
    assert [item["case_key"] for item in sections] == ["TC-002", "TC-001"]
    assert sections[0]["content_md"].startswith("# 说明")
    try:
        split_case_draft("## TC-001\na\n## TC-001\nb")
    except CaseDraftParseError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate case keys must fail before import")


def test_split_case_draft_accepts_model_heading_without_separator():
    sections = split_case_draft(
        "## TC-001 登录必填校验\n内容一\n\n## TC-002 重复提交防护\n内容二"
    )
    assert [item["case_key"] for item in sections] == ["TC-001", "TC-002"]
    assert [item["title"] for item in sections] == ["登录必填校验", "重复提交防护"]


def test_finalize_writes_empty_legacy_diff_placeholders():
    with _session() as session:
        requirement = Requirement(title="需求", description="描述")
        session.add(requirement)
        session.flush()
        task = GenerationTask(requirement_id=requirement.id, status="generated")
        session.add(task)
        session.flush()
        draft = CaseDraft(
            task_id=task.id,
            version=1,
            content_md="## TC-001 登录必填校验\n内容",
        )
        session.add(draft)
        session.commit()

        finalize_task(session, task.id, draft.id)

        log = session.exec(select(_TestCaseOperationLogRow)).one()
        assert log.diff_text == ""
        assert log.diff_json == "{}"


def test_case_out_exposes_draft_version_not_database_id():
    with _session() as session:
        requirement = Requirement(title="需求", description="描述")
        session.add(requirement)
        session.flush()
        task = GenerationTask(requirement_id=requirement.id, status="generated")
        session.add(task)
        session.flush()
        draft = CaseDraft(task_id=task.id, version=2, content_md="## TC-001 登录\n内容")
        session.add(draft)
        session.flush()
        case = _TestCaseRow(
            requirement_id=requirement.id,
            case_key="TC-001",
            title="登录",
            content_md="内容",
            source_task_id=task.id,
            source_draft_id=draft.id,
            source_case_key="TC-001",
        )
        session.add(case)
        session.commit()

        response = _case_out(session, case)
        assert response.source_draft_id == draft.id
        assert response.source_draft_version == 2


def test_finalize_exact_draft_is_idempotent_and_does_not_overwrite_manual_edit():
    with _session() as session:
        requirement = Requirement(title="需求", description="描述")
        session.add(requirement)
        session.flush()
        task = GenerationTask(requirement_id=requirement.id, status="generated")
        session.add(task)
        session.flush()
        draft = CaseDraft(
            task_id=task.id,
            version=1,
            content_md="## TC-001 - 登录\n原始内容\n\n## TC-002 - 退出\n退出内容",
        )
        session.add(draft)
        session.commit()

        finalized = finalize_task(session, task.id, draft.id)
        assert finalized.status == "finalized"
        assert finalized.finalized_draft_id == draft.id
        assert finalized.finalized_at is not None
        cases = session.exec(select(_TestCaseRow).order_by(_TestCaseRow.case_key)).all()
        assert [item.case_key for item in cases] == ["TC-001", "TC-002"]
        logs = session.exec(select(_TestCaseOperationLogRow)).all()
        assert len(logs) == 2

        cases[0].content_md = "人工修改"
        cases[0].revision = 2
        session.add(cases[0])
        session.commit()
        finalize_task(session, task.id, draft.id)
        assert session.exec(select(_TestCaseRow)).all()[0].content_md == "人工修改"
        assert len(session.exec(select(_TestCaseOperationLogRow)).all()) == 2


def test_finalize_parse_error_does_not_change_task_or_write_cases():
    with _session() as session:
        requirement = Requirement(title="需求", description="描述")
        session.add(requirement)
        session.flush()
        task = GenerationTask(requirement_id=requirement.id, status="generated")
        session.add(task)
        session.flush()
        draft = CaseDraft(task_id=task.id, version=1, content_md="")
        session.add(draft)
        session.commit()
        try:
            finalize_task(session, task.id, draft.id)
        except ValueError:
            pass
        else:
            raise AssertionError("empty drafts must not finalize")
        session.rollback()
        assert session.get(GenerationTask, task.id).status == "generated"
        assert session.exec(select(_TestCaseRow)).all() == []


def test_finalize_preview_and_overwrite_existing_case():
    with _session() as session:
        requirement = Requirement(title="需求", description="描述")
        session.add(requirement)
        session.flush()
        requirement_id = int(requirement.id)
        first_task, first_draft = _task_with_draft(
            session,
            requirement_id,
            "## TC-001 - 登录\n原始步骤\n- 优先级：P1",
        )
        finalize_task(session, first_task.id, first_draft.id)
        original = session.exec(select(_TestCaseRow)).one()
        original_id = int(original.id)
        original_length = len(original.content_md)

        second_task, second_draft = _task_with_draft(
            session,
            requirement_id,
            "## TC-001 - 再次登录\n新步骤\n- 优先级：P0",
        )
        assert find_finalize_conflicts(session, second_task, second_draft) == [{
            "case_key": "TC-001",
            "existing_case_id": original_id,
            "existing_title": "登录",
            "existing_status": "active",
            "new_title": "再次登录",
        }]

        try:
            finalize_task(session, second_task.id, second_draft.id)
        except CaseKeyConflictError as exc:
            assert [item["case_key"] for item in exc.conflicts] == ["TC-001"]
        else:
            raise AssertionError("existing requirement cases require explicit overwrite")

        unchanged = session.get(_TestCaseRow, original_id)
        assert unchanged.title == "登录"
        assert unchanged.revision == 1
        assert session.get(GenerationTask, second_task.id).status == "generated"

        finalize_task(
            session,
            second_task.id,
            second_draft.id,
            overwrite_existing=True,
            operator="alice",
        )
        session.expire_all()
        updated = session.get(_TestCaseRow, original_id)
        assert updated is not None
        assert int(updated.id) == original_id
        assert updated.title == "再次登录"
        assert updated.content_md.startswith("## TC-001 - 再次登录")
        assert "新步骤" in updated.content_md
        assert updated.priority == "P0"
        assert updated.revision == 2
        assert updated.status == "active"
        assert updated.source_task_id == second_task.id
        assert updated.source_draft_id == second_draft.id
        assert updated.source_case_key == "TC-001"

        logs = session.exec(
            select(_TestCaseOperationLogRow)
            .where(_TestCaseOperationLogRow.test_case_id == original_id)
            .order_by(_TestCaseOperationLogRow.id.asc())
        ).all()
        assert [row.operation for row in logs] == ["import", "update"]
        update_log = logs[-1]
        assert update_log.reason == "终版更新已有用例"
        assert update_log.operator == "alice"
        assert update_log.diff_text == ""
        assert update_log.diff_json == "{}"
        assert update_log.title_changed is True
        assert update_log.before_length == original_length
        assert {"title", "content_md", "priority"} <= set(
            json.loads(update_log.changed_fields_json)
        )

        events = session.exec(
            select(TaskEvent).where(TaskEvent.task_id == second_task.id)
        ).all()
        finalize_events = [row for row in events if row.step == "finalize"]
        assert len(finalize_events) == 1
        assert json.loads(finalize_events[0].detail_json)["imported_case_count"] == 1


def test_finalize_repeat_with_overwrite_is_still_idempotent():
    with _session() as session:
        requirement = Requirement(title="需求", description="描述")
        session.add(requirement)
        session.flush()
        task, draft = _task_with_draft(
            session,
            int(requirement.id),
            "## TC-001 - 登录\n内容",
        )
        finalize_task(session, task.id, draft.id)
        finalize_task(
            session,
            task.id,
            draft.id,
            overwrite_existing=True,
            operator="alice",
        )
        session.expire_all()
        case = session.exec(select(_TestCaseRow)).one()
        assert case.revision == 1
        logs = session.exec(select(_TestCaseOperationLogRow)).all()
        assert [row.operation for row in logs] == ["import"]


def test_finalize_without_conflicts_creates_new_cases():
    with _session() as session:
        requirement = Requirement(title="需求", description="描述")
        session.add(requirement)
        session.flush()
        task, draft = _task_with_draft(
            session,
            int(requirement.id),
            "## TC-001 - 登录\n内容",
        )
        finalize_task(session, task.id, draft.id)
        case = session.exec(select(_TestCaseRow)).one()
        assert case.title == "登录"
        assert case.revision == 1
        assert case.status == "active"
        logs = session.exec(select(_TestCaseOperationLogRow)).all()
        assert [row.operation for row in logs] == ["import"]


def test_finalize_overwrite_keeps_archived_status():
    with _session() as session:
        requirement = Requirement(title="需求", description="描述")
        session.add(requirement)
        session.flush()
        requirement_id = int(requirement.id)
        first_task, first_draft = _task_with_draft(
            session,
            requirement_id,
            "## TC-001 - 登录\n旧内容",
        )
        finalize_task(session, first_task.id, first_draft.id)
        archived = session.exec(select(_TestCaseRow)).one()
        archived.status = "archived"
        archived.archived_at = utcnow()
        session.add(archived)
        session.commit()
        archived_at = archived.archived_at

        second_task, second_draft = _task_with_draft(
            session,
            requirement_id,
            "## TC-001 - 再次登录\n新内容",
        )
        finalize_task(
            session,
            second_task.id,
            second_draft.id,
            overwrite_existing=True,
        )
        session.expire_all()
        updated = session.get(_TestCaseRow, int(archived.id))
        assert updated.status == "archived"
        assert updated.archived_at == archived_at
        assert updated.revision == 2


def test_finalize_multiple_conflicts_updates_all_after_validation():
    with _session() as session:
        requirement = Requirement(title="需求", description="描述")
        session.add(requirement)
        session.flush()
        requirement_id = int(requirement.id)
        first_task, first_draft = _task_with_draft(
            session,
            requirement_id,
            "## TC-001 - A\nA\n\n## TC-002 - B\nB",
        )
        finalize_task(session, first_task.id, first_draft.id)

        second_task, second_draft = _task_with_draft(
            session,
            requirement_id,
            "## TC-001 - A2\nA2\n\n## TC-002 - B2\nB2",
        )
        conflicts = find_finalize_conflicts(session, second_task, second_draft)
        assert [item["case_key"] for item in conflicts] == ["TC-001", "TC-002"]

        try:
            finalize_task(session, second_task.id, second_draft.id)
        except CaseKeyConflictError as exc:
            assert len(exc.conflicts) == 2
        else:
            raise AssertionError("all conflicts must block the whole import")

        unchanged = session.exec(select(_TestCaseRow).order_by(_TestCaseRow.case_key)).all()
        assert [row.title for row in unchanged] == ["A", "B"]
        assert all(row.revision == 1 for row in unchanged)
        assert session.get(GenerationTask, second_task.id).status == "generated"

        finalize_task(
            session,
            second_task.id,
            second_draft.id,
            overwrite_existing=True,
        )
        session.expire_all()
        updated = session.exec(select(_TestCaseRow).order_by(_TestCaseRow.case_key)).all()
        assert [row.title for row in updated] == ["A2", "B2"]
        assert all(row.revision == 2 for row in updated)
        logs = session.exec(select(_TestCaseOperationLogRow)).all()
        assert [row.operation for row in logs].count("update") == 2
