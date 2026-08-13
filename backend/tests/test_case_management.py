from sqlmodel import SQLModel, Session, create_engine, select

from app.models.entities import (
    CaseDraft,
    GenerationTask,
    Requirement,
    TestCase as _TestCaseRow,
    TestCaseOperationLog as _TestCaseOperationLogRow,
)
from app.services.case_management import CaseDraftParseError, split_case_draft
from app.api.cases import _case_out
from app.services.task_pipeline import finalize_task


def _session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    return Session(engine)


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
