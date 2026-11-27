"""Tests for Batch 1: scenario-oriented prompts, task-global citation numbering, and scenario parser."""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session

from app.db import get_engine, init_db
from app.models.entities import (
    CaseDraft,
    GenerationTask,
    Requirement,
    TaskCitation,
    TaskTestPointCheckpoint,
    TestPoint,
    TestPointCitation,
)
from app.schemas.tasks import DraftCaseItemOut
from app.services.case_management import (
    aggregate_draft_cases_by_points,
    parse_case_detail,
    split_case_draft,
)
from app.services.pipeline.common import _LEAN_GENERATE_SYSTEM
from app.services.pipeline.context import (
    _build_point_generate_messages,
    _citations_for_point,
    _link_draft_to_test_points,
)
from app.services.pipeline.review import _build_review_messages


def test_parse_new_scenario_markdown_fields():
    md = """## TC-001-01 开盘集合竞价不可撤单时段提交撤单被拒
- 优先级：P0
- 类型：边界
- 关联测试点：TP-001
- 规则引用：[E1], [E2], [REQ]
- 验证目标：验证 9:20-9:25 不可撤单规则

### 前置状态
- 市场处于开盘集合竞价 9:20-9:25 不可撤单时段
- 已存在未成交的有效限价申报

### 触发动作
- 在 9:20-9:25 时段内针对该未成交申报提交撤单指令

### 预期业务结果
- 交易主机拒绝该撤单请求
- 原申报保持未成交有效状态，继续参与集合竞价撮合

### 待确认项
- 拒绝提示口径是否区分前端渠道
"""
    sections = split_case_draft(md)
    assert len(sections) == 1
    assert sections[0]["case_key"] == "TC-001-01"
    assert sections[0]["test_point_keys"] == ["TP-001"]

    detail = parse_case_detail(sections[0])
    assert detail["case_key"] == "TC-001-01"
    assert detail["priority"] == "P0"
    assert detail["type"] == "边界"
    assert detail["verification_goal"] == "验证 9:20-9:25 不可撤单规则"
    assert "9:20-9:25" in detail["preconditions"]
    assert "提交撤单指令" in detail["trigger_action"]
    assert "交易主机拒绝该撤单请求" in detail["expected_result"]
    assert detail["citation_ids"] == ["E1", "E2", "REQ"]
    assert detail["pending_items"] == ["拒绝提示口径是否区分前端渠道"]
    assert detail["test_data"] == ""
    assert detail["steps"] == []

    # Validate Pydantic schema compatibility
    item = DraftCaseItemOut.model_validate(detail)
    assert item.trigger_action == detail["trigger_action"]
    assert item.expected_result == detail["expected_result"]
    assert item.citation_ids == ["E1", "E2", "REQ"]


def test_parse_scenario_aliases_and_bare_citation_ids():
    md = """## TS-002-01 停牌期间限价申报暂存
- 优先级：P1
- 类型：异常
- 关联测试点：TP-002
- 规则引用：E2、e3、[S1]、[1]

### 前置条件
- 证券处于临时停牌状态

### 场景描述
- 投资者在停牌期间提交限价买入申报

### 预期结果
- 交易主机接受申报并暂存，待复牌集合竞价时参与撮合
"""
    sections = split_case_draft(md)
    assert len(sections) == 1
    assert sections[0]["case_key"] == "TS-002-01"

    detail = parse_case_detail(sections[0])
    assert "临时停牌" in detail["preconditions"]
    assert "限价买入申报" in detail["trigger_action"]
    assert "暂存" in detail["expected_result"]
    assert detail["citation_ids"] == ["E2", "E3", "S1", "1"]


def test_split_case_draft_supports_both_tc_and_ts_prefixes():
    md = """## TC-001-01 场景一
- 优先级：P0
- 关联测试点：TP-001
- 规则引用：[E1]

### 前置状态
- 正常交易时段

### 触发动作
- 提交有效申报

### 预期业务结果
- 申报被交易主机接收

## TS-001-02 场景二
- 优先级：P1
- 关联测试点：TP-001
- 规则引用：[E2]

### 前置状态
- 收盘集合竞价不可撤单时段

### 触发动作
- 提交撤单请求

### 预期业务结果
- 撤单请求被拒绝
"""
    sections = split_case_draft(md)
    assert [sec["case_key"] for sec in sections] == ["TC-001-01", "TS-001-02"]
    assert sections[0]["title"] == "场景一"
    assert sections[1]["title"] == "场景二"


def test_parse_legacy_detailed_case_remains_backward_compatible():
    md = """## TC-101 余额不足买入失败
- 优先级：P0
- 类型：异常
- 验证目标：验证可用资金不足时拦截买入申报

### 前置条件
- 账户可用资金为 100 元

### 测试数据
- 证券代码：600000
- 委托价格：10.00
- 委托数量：100

### 测试步骤
1. 提交买入委托
   预期结果：系统返回资金不足错误并拒绝委托
2. 查询资金流水
   预期结果：可用资金仍为 100 元，冻结资金为 0

### 待确认项
- 错误码是否区分普通账户与信用账户
"""
    sections = split_case_draft(md)
    assert len(sections) == 1
    detail = parse_case_detail(sections[0])

    assert detail["preconditions"] == "- 账户可用资金为 100 元"
    assert "600000" in detail["test_data"]
    assert len(detail["steps"]) == 2
    assert detail["steps"][0]["action"] == "提交买入委托"
    assert detail["steps"][0]["expected"] == "系统返回资金不足错误并拒绝委托"
    assert detail["steps"][1]["action"] == "查询资金流水"
    assert detail["steps"][1]["expected"] == "可用资金仍为 100 元，冻结资金为 0"
    assert detail["pending_items"] == ["错误码是否区分普通账户与信用账户"]
    assert detail["trigger_action"] == ""
    assert detail["expected_result"] == ""
    assert detail["citation_ids"] == []


def test_parse_pending_items_filters_empty_placeholders():
    md = """## TC-003-01 正常申报无待确认项
- 优先级：P1
- 规则引用：None

### 前置状态
- 处于连续竞价时段

### 触发动作
- 提交有效限价买入申报

### 预期业务结果
- 申报被接收

### 待确认项
- 无
"""
    sections = split_case_draft(md)
    assert len(sections) == 1
    detail = parse_case_detail(sections[0])
    assert detail["pending_items"] == []
    assert detail["citation_ids"] == []


def test_point_citations_use_task_global_labels_matching_review(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        req = Requirement(title="集合竞价撤单规则", description="验证开盘集合竞价撤单限制")
        session.add(req)
        session.flush()

        task = GenerationTask(requirement_id=int(req.id), status="generated")
        session.add(task)
        session.flush()

        # Create task-level citations in deterministic ID order:
        # E1, E2, E3, S1, [1]
        c_e1 = TaskCitation(
            task_id=int(task.id),
            citation_type="external",
            title="规则一：开盘申报时段",
            path="rules/auction.md",
            content_excerpt="9:15-9:20 可以申报和撤单。",
            snippet="9:15-9:20 可以申报和撤单。",
        )
        c_e2 = TaskCitation(
            task_id=int(task.id),
            citation_type="external",
            title="规则二：不可撤单时段",
            path="rules/auction.md",
            content_excerpt="9:20-9:25 不接受撤单申报。",
            snippet="9:20-9:25 不接受撤单申报。",
        )
        c_e3 = TaskCitation(
            task_id=int(task.id),
            citation_type="external",
            title="规则三：连续竞价",
            path="rules/continuous.md",
            content_excerpt="9:30 起进入连续竞价。",
            snippet="9:30 起进入连续竞价。",
        )
        c_s1 = TaskCitation(
            task_id=int(task.id),
            citation_type="source",
            title="交易所实施细则原文",
            path="sources/rule.pdf",
            content_excerpt="第 3.3.5 条 集合竞价不可撤单。",
            snippet="第 3.3.5 条",
        )
        session.add_all([c_e1, c_e2, c_e3, c_s1])
        session.flush()

        cp = TaskTestPointCheckpoint(task_id=int(task.id), attempt=1, version=1, status="confirmed")
        session.add(cp)
        session.flush()

        pt = TestPoint(
            task_id=int(task.id),
            checkpoint_id=int(cp.id),
            stable_key="TP-002",
            title="9:20-9:25 不可撤单",
            verification_goal="验证不可撤单时段撤单被拒",
            dimension="boundary",
            priority="P0",
            sort_order=1,
            is_selected=True,
            is_excluded=False,
        )
        session.add(pt)
        session.flush()

        # Bind ONLY the 2nd external citation (c_e2) and the 1st source citation (c_s1)
        session.add(TestPointCitation(test_point_id=int(pt.id), citation_id=int(c_e2.id)))
        session.add(TestPointCitation(test_point_id=int(pt.id), citation_id=int(c_s1.id)))
        session.commit()

        point_context = _citations_for_point(session, int(task.id), pt)
        assert "[E2] 规则二：不可撤单时段" in point_context
        assert "[E1]" not in point_context
        assert "[S1] 交易所实施细则原文" in point_context

        # Verify review stage uses the exact same global labels
        draft = CaseDraft(
            task_id=int(task.id),
            version=1,
            content_md="## TC-002-01 不可撤单\n- 规则引用：[E2], [S1]\n",
        )
        session.add(draft)
        session.commit()

        review_messages = _build_review_messages(
            "review system",
            req,
            draft,
            [c_e1, c_e2, c_e3, c_s1],
        )
        review_user = review_messages[1]["content"]
        assert "[E2] 规则二：不可撤单时段" in review_user
        assert "[S1] 交易所实施细则原文" in review_user


def test_aggregate_draft_cases_populates_new_scenario_fields_and_ts_links(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        req = Requirement(title="停牌申报", description="停牌期间申报处理")
        session.add(req)
        session.flush()

        task = GenerationTask(requirement_id=int(req.id), status="generated")
        session.add(task)
        session.flush()

        cp = TaskTestPointCheckpoint(task_id=int(task.id), attempt=1, version=1, status="confirmed")
        session.add(cp)
        session.flush()

        pt = TestPoint(
            task_id=int(task.id),
            checkpoint_id=int(cp.id),
            stable_key="TP-001",
            title="停牌期间申报",
            verification_goal="验证停牌期间申报规则",
            dimension="positive",
            priority="P0",
            sort_order=1,
            is_selected=True,
            is_excluded=False,
        )
        session.add(pt)
        session.flush()

        draft = CaseDraft(
            task_id=int(task.id),
            version=1,
            content_md=(
                "## TS-001-01 停牌期间提交限价申报\n"
                "- 优先级：P0\n"
                "- 类型：主流程\n"
                "- 规则引用：[E2]\n"
                "- 验证目标：验证停牌期间申报处理\n\n"
                "### 前置状态\n"
                "- 标的处于临时停牌状态\n\n"
                "### 触发动作\n"
                "- 提交限价买入申报\n\n"
                "### 预期业务结果\n"
                "- 交易主机暂存申报并在复牌集合竞价参与撮合\n"
            ),
        )
        session.add(draft)
        session.flush()

        _link_draft_to_test_points(session, draft, [pt])
        groups = aggregate_draft_cases_by_points(session, int(task.id), draft)
        assert len(groups) == 1
        assert groups[0]["stable_key"] == "TP-001"
        assert len(groups[0]["cases"]) == 1

        case_item = DraftCaseItemOut.model_validate(groups[0]["cases"][0])
        assert case_item.case_key == "TS-001-01"
        assert case_item.trigger_action == "- 提交限价买入申报"
        assert "复牌集合竞价" in case_item.expected_result
        assert case_item.citation_ids == ["E2"]


def test_point_generate_messages_and_default_prompts_align_with_scenario_contract():
    req = Requirement(title="集合竞价", description="9:15-9:25 集合竞价规则")
    pt = TestPoint(
        task_id=1,
        checkpoint_id=1,
        stable_key="TP-001",
        title="9:20-9:25 不可撤单",
        verification_goal="验证不可撤单",
        dimension="boundary",
        priority="P0",
    )
    messages = _build_point_generate_messages(
        "sys",
        req,
        pt,
        1,
        2,
        granularity="standard",
        citations_text="[E2] 不可撤单条款 (rules/a.md)\n9:20-9:25 不可撤单。",
    )
    user_content = messages[1]["content"]
    assert "### 前置状态" in user_content
    assert "### 触发动作" in user_content
    assert "### 预期业务结果" in user_content
    assert "### 待确认项" in user_content
    assert "- 规则引用：" in user_content
    assert "TC-001-01" in user_content
    assert "### 测试数据（若有）" not in user_content
    assert "### 测试步骤\n" not in user_content

    assert "不涉及具体测试数据" in _LEAN_GENERATE_SYSTEM
    assert "预期业务结果" in _LEAN_GENERATE_SYSTEM

    prompts_dir = Path(__file__).resolve().parents[1] / "app" / "default_prompts"
    gen_md = (prompts_dir / "generate.md").read_text(encoding="utf-8")
    rev_md = (prompts_dir / "review.md").read_text(encoding="utf-8")
    opt_md = (prompts_dir / "optimize.md").read_text(encoding="utf-8")

    assert "### 前置状态" in gen_md
    assert "### 触发动作" in gen_md
    assert "### 预期业务结果" in gen_md
    assert "### 待确认项" in gen_md
    assert "规则引用" in gen_md
    assert "场景质量与数据边界（25 分）" in rev_md
    assert "测试场景" in opt_md
