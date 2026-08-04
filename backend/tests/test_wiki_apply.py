from __future__ import annotations

import json

from sqlmodel import Session, select

from app.db import get_engine, init_db
from app.models.entities import (
    Document,
    IngestJob,
    WikiPageRevision,
    WikiReviewItem,
)
from app.services.wiki_apply import apply_wiki_plan, queue_merge_review
from app.services.wiki_pages_parse import WikiPageParseError, parse_wiki_write_output
from app.services.wiki_repository import WikiRepository
from app.services.wiki_schema import WikiFrontmatter, WikiPage, WikiSource


def _seed_job(session: Session) -> tuple[int, int]:
    document = Document(
        filename="rules.md",
        stored_path="raw/sources/rules.md",
        content_type="text/markdown",
        sha256="rules",
        status="ingesting",
    )
    session.add(document)
    session.flush()
    job = IngestJob(document_id=int(document.id), status="running")
    session.add(job)
    session.commit()
    return int(document.id), int(job.id)


def _anchor() -> dict:
    return {"window_index": 1, "start_char": 0, "end_char": 20, "clause_id": "3.5.2"}


def test_create_applies_revisioned_rule_and_automatic_source_summary(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        document_id, job_id = _seed_job(session)
        plan = {
            "source_summary": {"title": "交易规则", "summary": "集合竞价规则"},
            "claims": [{"claim_id": "c1", "statement": "按最大成交量确定价格"}],
            "entities": ["集合竞价"],
            "page_operations": [
                {
                    "op": "create",
                    "page_key": "rule.order.auction",
                    "page_type": "rule",
                    "reason": "新增规则",
                    "source_anchors": [_anchor()],
                }
            ],
        }
        candidates = [
            {
                "page_key": "rule.order.auction",
                "title": "集合竞价成交规则",
                "type": "rule",
                "aliases": ["开盘竞价"],
                "tags": ["竞价"],
                "body": "3.5.2 集合竞价按最大成交量确定成交价格。",
            }
        ]

        result = apply_wiki_plan(
            session,
            plan,
            candidates,
            document_id=document_id,
            job_id=job_id,
        )

        assert set(result.applied_page_keys) == {
            f"source.document.{document_id}",
            "rule.order.auction",
        }
        rule = WikiRepository(session).read("rule.order.auction")
        assert rule.frontmatter.sources[0].document_id == document_id
        assert "3.5.2" in rule.frontmatter.sources[0].clauses
        revisions = session.exec(select(WikiPageRevision)).all()
        assert len(revisions) == 2
        assert all(item.job_id == job_id for item in revisions)


def test_safe_update_preserves_old_body_aliases_and_sources(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        old_document_id, _ = _seed_job(session)
        repository = WikiRepository(session)
        repository.create(
            WikiPage(
                frontmatter=WikiFrontmatter(
                    page_key="rule.order.auction",
                    title="集合竞价",
                    type="rule",
                    aliases=["旧别名"],
                    sources=[WikiSource(document_id=old_document_id, clauses=["3.5.1"])],
                ),
                body="旧规则：价格优先、时间优先。",
            )
        )
        new_document_id, job_id = _seed_job(session)
        result = apply_wiki_plan(
            session,
            {
                "claims": [{"statement": "新增最大成交量原则"}],
                "page_operations": [
                    {
                        "op": "update",
                        "page_key": "rule.order.auction",
                        "reason": "补充规则",
                        "source_anchors": [_anchor()],
                    }
                ],
            },
            [
                {
                    "page_key": "rule.order.auction",
                    "title": "集合竞价",
                    "type": "rule",
                    "aliases": ["新别名"],
                    "body": "新增规则：按最大成交量确定价格。",
                }
            ],
            document_id=new_document_id,
            job_id=job_id,
        )

        assert "rule.order.auction" in result.applied_page_keys
        updated = repository.read("rule.order.auction")
        assert "旧规则" in updated.body and "新增规则" in updated.body
        assert set(updated.frontmatter.aliases) == {"旧别名", "新别名"}
        assert {source.document_id for source in updated.frontmatter.sources} == {
            old_document_id,
            new_document_id,
        }


def test_numeric_change_is_queued_for_review_without_overwriting_page(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        old_document_id, _ = _seed_job(session)
        repository = WikiRepository(session)
        repository.create(
            WikiPage(
                frontmatter=WikiFrontmatter(
                    page_key="rule.order.limit",
                    title="申报限额",
                    type="rule",
                    sources=[WikiSource(document_id=old_document_id)],
                ),
                body="单笔申报上限为100万股。",
            )
        )
        revision = repository.read("rule.order.limit").revision
        new_document_id, job_id = _seed_job(session)

        result = apply_wiki_plan(
            session,
            {
                "page_operations": [
                    {
                        "op": "update",
                        "page_key": "rule.order.limit",
                        "reason": "限额变化",
                        "source_anchors": [_anchor()],
                    }
                ]
            },
            [
                {
                    "page_key": "rule.order.limit",
                    "title": "申报限额",
                    "type": "rule",
                    "body": "单笔申报上限为200万股。",
                }
            ],
            document_id=new_document_id,
            job_id=job_id,
        )

        assert "rule.order.limit" not in result.applied_page_keys
        assert result.review_item_ids
        assert repository.read("rule.order.limit").revision == revision
        review = session.get(WikiReviewItem, result.review_item_ids[0])
        assert review is not None and review.kind == "numeric_change"


def test_invalid_wikilink_fails_before_any_page_is_applied(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        document_id, job_id = _seed_job(session)
        try:
            apply_wiki_plan(
                session,
                {
                    "page_operations": [
                        {
                            "op": "create",
                            "page_key": "rule.order.link",
                            "page_type": "rule",
                            "source_anchors": [_anchor()],
                        }
                    ]
                },
                [
                    {
                        "page_key": "rule.order.link",
                        "title": "链接规则",
                        "type": "rule",
                        "body": "参见 [[rule.missing]]。",
                    }
                ],
                document_id=document_id,
                job_id=job_id,
            )
        except ValueError as exc:
            assert "wikilink target" in str(exc)
        else:
            raise AssertionError("invalid wikilink should fail")
        assert WikiRepository(session).list_rows() == []


def test_merge_only_creates_review_item(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        _document_id, job_id = _seed_job(session)
        review_id = queue_merge_review(
            session,
            page_key="rule.order.a",
            target_page_key="rule.order.b",
            job_id=job_id,
        )
        review = session.get(WikiReviewItem, review_id)
        assert review is not None
        assert review.kind == "merge"
        assert review.status == "pending"


def test_structured_writer_rejects_paths_unknown_fields_and_unknown_operations():
    base = {
        "pages": [
            {
                "operation": "create",
                "page_key": "rule.order.valid",
                "title": "有效规则",
                "type": "rule",
                "body": "规则正文",
            }
        ]
    }
    parsed = parse_wiki_write_output(json.dumps(base), allow_legacy_markdown=False)
    assert parsed[0]["page_key"] == "rule.order.valid"

    for field, value in (
        ("path", "C:/escape.md"),
        ("unknown", True),
        ("operation", "merge"),
    ):
        invalid = json.loads(json.dumps(base))
        invalid["pages"][0][field] = value
        try:
            parse_wiki_write_output(json.dumps(invalid), allow_legacy_markdown=False)
        except WikiPageParseError:
            pass
        else:
            raise AssertionError(f"invalid writer field should be rejected: {field}")
