from pathlib import Path

from app import config
from app.models.entities import WikiPageRow, WikiReviewItem
from app.services.wiki_index import rebuild_index
from app.services.wiki_lint import lint_wiki
from app.services.wiki_log import append_event, read_events
from app.services.wiki_overview import rebuild_overview
from app.services.wiki_repository import WikiRepository
from app.services.wiki_schema import WikiFrontmatter, WikiPage, WikiSource
from sqlmodel import Session


def _page(key, title, page_type="rule", domain="trade", sources=(), body="正文"):
    return WikiPage(
        frontmatter=WikiFrontmatter(
            page_key=key,
            title=title,
            type=page_type,
            domain=domain,
            status="published",
            sources=[WikiSource(document_id=item) for item in sources],
        ),
        body=body,
    )


def test_lint_reports_required_findings_without_writing_formal_pages(tmp_app_data):
    config.ensure_data_dirs()
    one = _page("trade.order", "下单规则", sources=(1,), body="参见 [[trade.missing]]")
    two = {"page_key": "trade.orphan", "title": "孤立规则", "page_type": "rule", "domain": "trade", "status": "published", "body": "孤立正文"}
    rebuild_index([one, two])
    before = (config.WIKI_DIR / "index.md").read_text(encoding="utf-8")
    report = lint_wiki([one, two], index_path=config.WIKI_DIR / "index.md")
    codes = {item["code"] for item in report.issues}
    assert "dead_link" in codes
    assert "rule_without_source" in codes
    assert "orphan_page" in codes
    assert report.candidate_diffs
    assert all(item["apply"] is False for item in report.candidate_diffs)
    assert (config.WIKI_DIR / "index.md").read_text(encoding="utf-8") == before


def test_lint_detects_missing_file_conflict_and_index_drift(tmp_app_data):
    config.ensure_data_dirs()
    row = WikiPageRow(id=1, page_key="trade.rule", title="规则", page_type="rule", path="rules/trade.rule.md", domain="trade", status="published")
    from app.db import get_engine, init_db

    init_db()
    with Session(get_engine()) as session:
        session.add(row)
        session.add(WikiReviewItem(page_id=1, kind="conflict", status="pending", reason="来源冲突"))
        session.commit()
        rebuild_index([row], session=session)
        (config.WIKI_DIR / "index.md").write_text("# stale\n", encoding="utf-8")
        report = lint_wiki([row], session=session)
    codes = {item["code"] for item in report.issues}
    assert {"missing_file", "conflict", "index_drift"}.issubset(codes)
    assert any(item["operation"] == "rebuild_index" and item["diff"] for item in report.candidate_diffs)


def test_index_overview_and_log_are_structured(tmp_app_data):
    page = _page("trade.rule", "交易规则", sources=(3,), body="成交规则摘要")
    index = rebuild_index([page])
    overview = rebuild_overview([page])
    assert "## trade" in index
    assert "来源数" in index and "成交规则摘要" in index
    assert "## 主要规则" in overview and "trade.rule" not in overview
    path = Path(tmp_app_data) / "wiki" / "log.md"
    append_event("ingest", {"job_id": 7}, log_path=path)
    append_event("lint", {"issues": 0}, log_path=path)
    assert [item["event"] for item in read_events(path)] == ["ingest", "lint"]
