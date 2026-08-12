import json

import pytest

from app.services.wiki_candidates import recall_wiki_candidates
from app.services.wiki_long_analyze import run_long_source_analyze
from app.services.wiki_long_analyze import merge_analysis_partials, trim_digest
from app.services.wiki_plan import (
    PlanValidationError,
    coerce_step_a_plan,
    merge_step_a_plans,
    validate_step_a_plan,
)


def _anchor(window_index: int = 1, start: int = 0, end: int = 10) -> dict:
    return {
        "document_id": 1,
        "window_index": window_index,
        "start_char": start,
        "end_char": end,
        "clause_id": "3.5.2",
    }


def test_candidates_match_title_alias_clause_tag_and_keep_late_match():
    pages = [
        {"page_key": f"rule.order.noise-{index}", "title": "无关规则"}
        for index in range(90)
    ]
    pages.extend(
        [
            {
                "page_key": "rule.order.auction",
                "title": "集合竞价成交规则",
                "aliases": ["开盘竞价"],
                "tags": ["竞价"],
                "body": "第3.5.2条规定申报价格优先。",
            },
            {
                "page_key": "rule.order.tail-only",
                "title": "尾部规则",
                "tags": ["尾部专属"],
                "body": "TAIL_RULE_2026",
            },
        ]
    )
    result = recall_wiki_candidates(
        pages,
        text="第3.5.2条 尾部专属 TAIL_RULE_2026",
        title="集合竞价",
        limit=4,
    )
    keys = {item["page_key"] for item in result}
    assert "rule.order.auction" in keys
    assert "rule.order.tail-only" in keys
    assert any("title" in item["matched_fields"] for item in result if item["page_key"] == "rule.order.auction")


def test_strict_plan_accepts_create_update_noop_and_rejects_merge():
    plan = validate_step_a_plan(
        {
            "source_summary": {"title": "交易规则", "summary": "成交与申报"},
            "claims": [
                {
                    "claim_id": "c-1",
                    "statement": "申报价格按规则比较",
                    "clauses": ["3.5.2"],
                    "source_anchors": [_anchor()],
                }
            ],
            "entities": ["申报"],
            "related_pages": [{"page_key": "rule.order.auction", "matched_on": ["title"]}],
            "contradictions": [],
            "page_operations": [
                {"op": "create", "page_key": "rule.order.new-rule", "source_anchors": [_anchor()]},
                {"op": "update", "page_key": "rule.order.auction", "source_anchors": [_anchor()]},
                {"op": "noop", "page_key": "rule.order.existing"},
            ],
            "review_items": [],
        },
        existing_page_keys={"rule.order.auction", "rule.order.existing"},
        source_windows=[{"index": 1, "start": 0, "end": 100, "clause_ids": ["3.5.2"]}],
    )
    assert [item.op for item in plan.page_operations] == ["create", "update", "noop"]

    with pytest.raises(PlanValidationError):
        validate_step_a_plan(
            {"page_operations": [{"op": "merge", "page_key": "rule.order.auction"}]}
        )


def test_coerce_strips_full_page_fields_from_model_operation():
    plan = coerce_step_a_plan(
        {
            "source_summary": {"title": "交易规则", "summary": "摘要"},
            "claims": [],
            "page_operations": [
                {
                    "op": "create",
                    "page_key": "rule.order.auction",
                    "title": "集合竞价",
                    "type": "rule",
                    "domain": "交易",
                    "aliases": ["开盘竞价"],
                    "tags": ["竞价"],
                    "sources": [
                        {"document_id": 1, "chunk_ids": [7], "clauses": ["3.5.2"]}
                    ],
                    "status": "published",
                    "revision": 2,
                    "updated_at": "2026-08-09T00:00:00Z",
                }
            ],
        },
        existing_page_keys=set(),
        source_windows=[
            {"chunk_ids": [7], "clause_ids": ["3.5.2"], "start": 0, "end": 50}
        ],
    )
    operation = plan.page_operations[0]
    assert operation.op == "create"
    assert operation.page_type == "rule"
    assert operation.source_anchors[0].chunk_ids == [7]
    assert operation.source_anchors[0].clause_ids == ["3.5.2"]


def test_plan_rejects_invalid_key_missing_anchor_and_unknown_target():
    with pytest.raises(PlanValidationError):
        validate_step_a_plan(
            {"page_operations": [{"op": "create", "page_key": "../escape", "source_anchors": [_anchor()]}]}
        )
    with pytest.raises(PlanValidationError):
        validate_step_a_plan(
            {"page_operations": [{"op": "create", "page_key": "rule.order.no-anchor"}]}
        )
    with pytest.raises(PlanValidationError):
        validate_step_a_plan(
            {"page_operations": [{"op": "update", "page_key": "rule.order.missing", "source_anchors": [_anchor()]}]},
            existing_page_keys={"rule.order.present"},
        )
    with pytest.raises(PlanValidationError):
        validate_step_a_plan(
            {"page_operations": [{"op": "update", "page_key": "rule.order.present", "source_anchors": [{"window_index": 9}]}]},
            existing_page_keys={"rule.order.present"},
            source_windows=[{"index": 1, "start": 0, "end": 10}],
        )


def test_plan_allows_references_to_pages_planned_in_this_ingest():
    key = "rule.order.planned"
    current = validate_step_a_plan(
        {
            "related_pages": [{"page_key": key}],
            "page_operations": [
                {"op": "create", "page_key": key, "source_anchors": [_anchor()]}
            ],
        },
        existing_page_keys=set(),
    )
    assert current.related_pages[0].page_key == key

    later = validate_step_a_plan(
        {"related_pages": [{"page_key": key}]},
        existing_page_keys=set(),
        reference_page_keys={key},
    )
    assert later.related_pages[0].page_key == key

    with pytest.raises(PlanValidationError, match="update target page does not exist"):
        validate_step_a_plan(
            {
                "page_operations": [
                    {"op": "update", "page_key": key, "source_anchors": [_anchor()]}
                ]
            },
            existing_page_keys=set(),
            reference_page_keys={key},
        )


def test_plan_drops_unknown_auxiliary_page_references():
    plan = validate_step_a_plan(
        {
            "related_pages": [{"page_key": "rule.order.unknown"}],
            "contradictions": [
                {
                    "page_key": "rule.order.unknown",
                    "description": "可能冲突",
                }
            ],
            "review_items": [
                {
                    "page_key": "rule.order.unknown",
                    "reason": "需要复核",
                }
            ],
        },
        existing_page_keys={"rule.order.existing"},
    )
    assert plan.related_pages == []
    assert plan.contradictions[0].page_key is None
    assert plan.review_items[0].page_key is None


def test_plan_ignores_unknown_model_fields():
    plan = coerce_step_a_plan(
        {
            "claims": [],
            "page_operations": [],
            "unexpected": "provider explanation",
        }
    )
    assert plan.page_operations == []


def test_coerce_reconciles_operations_and_uses_real_window_anchors():
    plan = coerce_step_a_plan(
        {
            "claims": [{"statement": "余额不足时拒绝申报"}],
            "page_operations": [
                {"op": "create", "page_key": "RULE.ORDER.BALANCE"},
                {"op": "create", "page_key": "rule.order.balance"},
                {"op": "update", "page_key": "rule.order.new-topic"},
            ],
        },
        existing_page_keys={"rule.order.balance"},
        source_windows=[
            {
                "index": 1,
                "document_id": 9,
                "chunk_ids": [71],
                "clause_ids": ["3.5.2"],
                "start": 0,
                "end": 80,
            }
        ],
    )
    assert [(item.op, item.page_key) for item in plan.page_operations] == [
        ("update", "rule.order.balance"),
        ("create", "rule.order.new-topic"),
    ]
    assert plan.page_operations[0].source_anchors[0].chunk_ids == [71]
    assert plan.claims[0].source_anchors[0].document_id == 9


def test_coerce_generates_stable_key_for_unusable_model_key():
    raw = {
        "page_operations": [
            {
                "op": "create",
                "page_key": "../不安全路径",
                "page_type": "rule",
                "reason": "余额校验规则",
            }
        ]
    }
    kwargs = {
        "source_windows": [{"document_id": 1, "chunk_ids": [7], "start": 0, "end": 20}]
    }
    first = coerce_step_a_plan(raw, **kwargs)
    second = coerce_step_a_plan(raw, **kwargs)
    assert first.page_operations[0].page_key == second.page_operations[0].page_key
    assert first.page_operations[0].page_key.startswith("rule.generated-")


def test_auxiliary_shape_errors_are_non_fatal():
    plan = coerce_step_a_plan(
        {
            "source_summary": "一份规则摘要",
            "related_pages": [
                {"page_key": "rule.order.limit", "score": "not-a-number"}
            ],
        },
        existing_page_keys=["rule.order.limit"],
    )

    assert plan.source_summary.summary == "一份规则摘要"
    assert plan.related_pages[0].score is None


def test_anchor_drops_unverified_clause_when_chunk_is_valid():
    plan = validate_step_a_plan(
        {
            "page_operations": [
                {
                    "op": "create",
                    "page_key": "rule.order.chunk-backed",
                    "source_anchors": [
                        {"chunk_ids": [7], "clause_ids": ["3.5.2", "9.9.9"]}
                    ],
                }
            ]
        },
        source_windows=[
            {"chunk_ids": [7], "clause_ids": ["3.5.2"], "start": 0, "end": 20}
        ],
    )
    anchor = plan.page_operations[0].source_anchors[0]
    assert anchor.chunk_ids == [7]
    assert anchor.clause_ids == ["3.5.2"]

    with pytest.raises(PlanValidationError, match="9.9.9"):
        validate_step_a_plan(
            {
                "claims": [
                    {
                        "statement": "条款结论",
                        "source_anchors": [{"clause_ids": ["9.9.9"]}],
                    }
                ]
            },
            source_windows=[{"clause_ids": ["3.5.2"], "start": 0, "end": 20}],
        )


def test_anchor_drops_invalid_auxiliary_locations_when_chunk_is_valid():
    plan = validate_step_a_plan(
        {
            "page_operations": [
                {
                    "op": "create",
                    "page_key": "rule.order.chunk-location",
                    "source_anchors": [
                        {
                            "chunk_ids": [7],
                            "window_index": 9,
                            "section": "不存在章节",
                            "page_start": 99,
                            "page_end": 100,
                            "start_char": 50,
                            "end_char": 60,
                        }
                    ],
                }
            ]
        },
        source_windows=[
            {
                "index": 1,
                "chunk_ids": [7],
                "section": "真实章节",
                "page_start": 1,
                "page_end": 2,
                "start": 0,
                "end": 20,
            }
        ],
    )
    anchor = plan.page_operations[0].source_anchors[0]
    assert anchor.chunk_ids == [7]
    assert anchor.window_index is None
    assert anchor.section is None
    assert anchor.page_start is None
    assert anchor.page_end is None
    assert anchor.start_char is None
    assert anchor.end_char is None
    with pytest.raises(PlanValidationError, match="chunk"):
        validate_step_a_plan(
            {
                "page_operations": [
                    {
                        "op": "create",
                        "page_key": "rule.order.invalid-anchor",
                        "source_anchors": [{"chunk_ids": [7, 999]}],
                    }
                ]
            },
            source_windows=[{"chunk_ids": [7], "start": 0, "end": 20}],
        )


def test_legacy_analysis_is_coerced_without_breaking_old_json():
    plan = coerce_step_a_plan(
        {
            "summary_title": "旧格式摘要",
            "key_rules": ["前部规则", "尾部规则"],
            "entities": ["订单"],
            "suggested_page_types": ["source_summary"],
        },
        source_path="raw/sources/rules.md",
    )
    assert plan.source_summary.title == "旧格式摘要"
    assert [claim.statement for claim in plan.claims] == ["前部规则", "尾部规则"]


def test_merge_uses_semantic_dedupe_and_tail_quota():
    plans = [
        {"claims": [{"statement": f"前部规则 {index}"} for index in range(70)]},
        {"claims": [{"statement": f"尾部规则 {index}"} for index in range(70)]},
    ]
    merged = merge_step_a_plans(plans, max_claims=80)
    statements = [claim.statement for claim in merged.claims]
    assert len(statements) == 80
    assert any(statement == "尾部规则 69" for statement in statements)


def test_merge_bounds_operations_and_removes_dangling_related_pages():
    plans = []
    all_keys = {f"rule.order.topic-{index}" for index in range(10)}
    for index in range(10):
        key = f"rule.order.topic-{index}"
        plans.append(
            {
                "related_pages": [{"page_key": key}],
                "page_operations": [
                    {
                        "op": "create",
                        "page_key": key,
                        "source_anchors": [_anchor()],
                    }
                ],
            }
        )

    merged = merge_step_a_plans(
        plans,
        existing_page_keys=set(),
        reference_page_keys=all_keys,
        max_operations=4,
    )
    kept_keys = {item.page_key for item in merged.page_operations}
    assert len(kept_keys) == 4
    assert {item.page_key for item in merged.related_pages}.issubset(kept_keys)
    assert "rule.order.topic-0" in kept_keys
    assert "rule.order.topic-9" in kept_keys


def test_legacy_merge_and_digest_keep_tail_content():
    partials = [
        {"summary_title": f"窗{index}", "key_rules": [f"规则{index}"]}
        for index in range(100)
    ]
    merged = merge_analysis_partials(partials, max_rules=10)
    assert "规则99" in merged["key_rules"]

    digest = "HEAD_MARKER\n" + ("中间" * 100) + "\nTAIL_MARKER"
    trimmed = trim_digest(digest, max_chars=80)
    assert "HEAD_MARKER" in trimmed
    assert "TAIL_MARKER" in trimmed


def test_long_analyze_passes_wiki_context_and_returns_plan(monkeypatch):
    monkeypatch.setattr("app.services.wiki_long_analyze.config.WIKI_ANALYZE_SINGLE_PASS_CHARS", 10000)
    seen: list[str] = []

    def chat(messages):
        seen.append(messages[-1]["content"])
        return json.dumps(
            {
                "source_summary": {"title": "新规则", "summary": "窗口摘要"},
                "claims": [{"statement": "规则结论", "source_anchors": [_anchor()]}],
                "entities": ["订单"],
                "related_pages": [{"page_key": "rule.order.existing", "reason": "同一实体"}],
                "contradictions": [],
                "page_operations": [{"op": "update", "page_key": "rule.order.existing", "source_anchors": [_anchor()]}],
                "review_items": [],
            },
            ensure_ascii=False,
        )

    result = run_long_source_analyze(
        "订单 第3.5.2条 规则正文",
        chat_fn=chat,
        analyze_system_prompt="Step A JSON",
        source_path="raw/sources/rules.md",
        purpose="Wiki 目标",
        schema="Wiki Schema",
        candidate_pages=[
            {"page_key": "rule.order.existing", "title": "现有订单规则", "tags": ["订单"]}
        ],
        existing_page_keys={"rule.order.existing"},
        source_windows=[{"index": 1, "start": 0, "end": 100, "clause_ids": ["3.5.2"]}],
    )
    assert result["plan"]["page_operations"][0]["op"] == "update"
    assert "Wiki 目标" in seen[0]
    assert "rule.order.existing" in seen[0]
    assert "3.5.2" in seen[0]
