from __future__ import annotations

import json
import re
from pathlib import Path

from sqlmodel import Session

from app import config
from app.db import get_engine, init_db
from app.models.entities import Document, SourceChunk, WikiPageRow, WikiSpace
from app.services.hybrid_retrieve import _one_hop_expand, hybrid_retrieve
from app.services.source_chunks_store import replace_chunks_for_document
from app.services.task_pipeline import assemble_task_context
from app.services.wiki_fts import index_counts, rebuild_fts, search_wiki, upsert_wiki_page
from app.services.wiki_repository import WikiRepository
from app.services.wiki_schema import WikiFrontmatter, WikiPage
from app.services.wiki_spaces import get_default_space, resolve_space_id


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "wiki_retrieval_eval.json"


def _seed_eval_corpus(session: Session) -> dict:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    config.WIKI_PAGES_DIR.mkdir(parents=True, exist_ok=True)
    for index, page in enumerate(data["pages"], 1):
        relative = f"pages/rules/{page['page_key']}.md"
        target = config.WIKI_DIR / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page["body"], encoding="utf-8")
        session.add(
            WikiPageRow(
                path=relative,
                title=page["title"],
                page_type="rule",
                source_document_id=index,
                tags_json=json.dumps(page["tags"], ensure_ascii=False),
                aliases_json=json.dumps(page["aliases"], ensure_ascii=False),
                page_key=page["page_key"],
                domain=page["page_key"].split(".", 1)[0],
                status="published",
            )
        )
        session.add(
            SourceChunk(
                document_id=index,
                chunk_index=0,
                title=f"{page['clause']} {page['title']}",
                text=page["body"],
                start_char=0,
                end_char=len(page["body"]),
                section=page["title"],
                clause_ids_json=json.dumps([page["clause"]], ensure_ascii=False),
            )
        )
    session.commit()
    return data


def _has_query_overlap(query: str, snippet: str) -> bool:
    clean = re.sub(r"</?mark>", "", snippet)
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", query))
    terms = {cjk[i : i + 2] for i in range(max(0, len(cjk) - 1))}
    if terms and any(term in clean for term in terms):
        return True
    # Synonym queries such as “撤单” → “撤销申报” may only share a root
    # character; punctuation-normalized numbers are also valid hit evidence.
    signal_chars = set(cjk) - set("的了是在到可以怎么什么哪会后个一")
    query_digits = set(re.findall(r"\d+", query))
    return any(char in clean for char in signal_chars) or any(number in clean for number in query_digits)


def test_retrieval_eval_recall_at_five_and_explain(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        data = _seed_eval_corpus(session)
        matched = 0
        for case in data["queries"]:
            result = hybrid_retrieve(session, case["query"], top_k=8, wiki_k=5, source_k=3)
            keys = [hit.get("page_key") for hit in result["wiki_hits"][:5]]
            matched += case["expected"] in keys
            assert result["hits"], case["query"]
            assert result["hits"][0].get("explain"), case["query"]
            assert result["hits"][0].get("snippet"), case["query"]
            assert _has_query_overlap(case["query"], result["hits"][0]["snippet"]), case["query"]

        recall_at_five = matched / len(data["queries"])
        assert len(data["queries"]) >= 30
        assert recall_at_five >= 0.85, f"Recall@5={recall_at_five:.1%}"

        counts = index_counts(session)
        assert counts["available"] is True
        assert counts["wiki_pages"] == len(data["pages"])
        assert counts["source_chunks"] == len(data["pages"])


def test_only_explicit_query_clause_creates_strong_anchor(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        _seed_eval_corpus(session)
        inferred = hybrid_retrieve(session, "集合竞价成交价格", wiki_k=3, source_k=3)
        assert "3.5.2" in inferred["clause_ids"]
        assert inferred["anchored_clause_ids"] == []

        explicit = hybrid_retrieve(session, "第3.5.2条集合竞价成交价格", wiki_k=3, source_k=3)
        assert "3.5.2" in explicit["anchored_clause_ids"]
        anchored = [hit for hit in explicit["source_hits"] if hit.get("anchor_clause") == "3.5.2"]
        assert anchored and all(hit.get("strong_anchor") for hit in anchored)


def test_fts_projection_updates_incrementally(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        document = Document(
            filename="rules.md",
            stored_path="raw/sources/rules.md",
            content_type="text/markdown",
            sha256="f" * 64,
            status="ready",
        )
        session.add(document)
        session.flush()
        replace_chunks_for_document(session, document.id, "3.5.2 集合竞价按最大成交量确定价格。")
        repository = WikiRepository(session)
        repository.create(
            WikiPage(
                frontmatter=WikiFrontmatter(
                    page_key="trading.incremental-index",
                    title="增量索引规则",
                    type="rule",
                    domain="trading",
                    aliases=["索引即时更新"],
                    tags=["FTS"],
                    sources=[{"document_id": document.id, "clauses": ["3.5.2"]}],
                    status="published",
                ),
                body="3.5.2 集合竞价按最大成交量确定价格。",
            )
        )
        counts = index_counts(session)
        assert counts["wiki_pages"] == 1
        assert counts["source_chunks"] == 1

        replace_chunks_for_document(session, document.id, "3.5.2 更新后的集合竞价原文。")
        session.commit()
        counts = index_counts(session)
        assert counts["wiki_pages"] == 1
        assert counts["source_chunks"] == 1


def test_fts_default_status_filter_excludes_archived_pages(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        space_id = resolve_space_id(session)
        config.WIKI_PAGES_DIR.mkdir(parents=True, exist_ok=True)
        for page_id, key, status in (
            (1, "rule.visible", "published"),
            (2, "rule.historical", "archived"),
        ):
            relative = f"pages/rules/{key}.md"
            target = config.WIKI_DIR / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("集合竞价历史规则", encoding="utf-8")
            session.add(
                WikiPageRow(
                    id=page_id,
                    path=relative,
                    title=key,
                    page_type="rule",
                    page_key=key,
                    domain="trading",
                    status=status,
                    space_id=space_id,
                )
            )
        session.commit()
        rebuild_fts(session, space_id=space_id)

        default_hits = search_wiki(session, "集合竞价", status=None, space_id=space_id)
        assert {hit["page_key"] for hit in default_hits} == {"rule.visible"}
        assert not search_wiki(
            session,
            "集合竞价",
            status="archived",
            space_id=space_id,
        )


def test_fts_archive_upsert_removes_existing_projection(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        space_id = resolve_space_id(session)
        page = WikiPageRow(
            path="pages/rules/archive-upsert.md",
            title="增量归档规则",
            page_type="rule",
            page_key="rule.archive-upsert",
            domain="trading",
            status="published",
            space_id=space_id,
        )
        session.add(page)
        session.commit()
        rebuild_fts(session, [page], [], space_id=space_id)
        assert index_counts(session, space_id=space_id)["wiki_pages"] == 1

        page.status = "archived"
        session.add(page)
        session.commit()
        result = upsert_wiki_page(session, page, "增量归档规则正文")
        assert result["action"] == "delete"
        assert index_counts(session, space_id=space_id)["wiki_pages"] == 0
        assert not search_wiki(session, "增量归档", space_id=space_id)


def test_rebuild_fts_explicit_rows_respects_space_scope(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        default = get_default_space(session)
        assert default is not None and default.id is not None
        project = WikiSpace(name="重建项目", slug="rebuild-project", status="active")
        session.add(project)
        session.flush()
        assert project.id is not None
        default_page = WikiPageRow(
            path="pages/rules/default-rebuild.md",
            title="默认重建规则",
            page_type="rule",
            page_key="rule.default-rebuild",
            status="published",
            space_id=default.id,
        )
        project_page = WikiPageRow(
            path="pages/rules/project-rebuild.md",
            title="项目重建规则",
            page_type="rule",
            page_key="rule.project-rebuild",
            status="published",
            space_id=project.id,
        )
        session.add(default_page)
        session.add(project_page)
        session.commit()

        rebuild_fts(session, [default_page, project_page], [], space_id=project.id)
        assert index_counts(session, space_id=project.id)["wiki_pages"] == 1
        assert index_counts(session, space_id=default.id)["wiki_pages"] == 0

        rebuild_fts(session, [default_page, project_page], [], space_id=default.id)
        assert index_counts(session, space_id=default.id)["wiki_pages"] == 1
        assert search_wiki(session, "默认重建", space_id=default.id)
        assert not search_wiki(session, "项目重建", space_id=default.id)


def test_rebuild_fts_default_clears_empty_space_legacy_rows(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        default = get_default_space(session)
        assert default is not None and default.id is not None
        page = WikiPageRow(
            path="pages/rules/default-empty-space.md",
            title="默认空空间规则",
            page_type="rule",
            page_key="rule.default-empty-space",
            status="published",
            space_id=default.id,
        )
        session.add(page)
        session.commit()
        upsert_wiki_page(
            session,
            {
                "id": 999,
                "space_id": "",
                "page_key": "rule.legacy-empty-space",
                "title": "空空间旧规则",
                "page_type": "rule",
                "status": "published",
                "body": "空空间旧规则正文",
            },
        )
        assert index_counts(session, space_id=default.id)["wiki_pages"] == 1

        rebuild_fts(session, [page], [], space_id=default.id)
        assert index_counts(session, space_id=default.id)["wiki_pages"] == 1
        assert all(
            hit["page_key"] != "rule.legacy-empty-space"
            for hit in search_wiki(session, "空空间旧规则", space_id=default.id)
        )


def test_wiki_results_expand_only_one_hop_and_deduplicate():
    pages = [
        {
            "id": 1,
            "page_key": "rule.seed",
            "page_type": "rule",
            "content": "相关规则见 [[rule.linked]]。",
            "source_document_ids": [7],
        },
        {
            "id": 2,
            "page_key": "rule.linked",
            "page_type": "rule",
            "content": "被链接规则。",
            "source_document_ids": [8],
        },
        {
            "id": 3,
            "page_key": "rule.shared",
            "page_type": "rule",
            "content": "共享来源规则。",
            "source_document_ids": [7],
        },
    ]
    result = _one_hop_expand([{**pages[0], "score": 10.0}], pages, limit=5, types=None)
    assert [item["id"] for item in result] == [1, 2, 3]
    assert len({item["id"] for item in result}) == len(result)
    assert result[1]["explain"]["algorithm"] == "one_hop_expansion"


def test_context_budget_is_fair_deduplicated_and_traceable():
    wiki_hits = [
        {"id": 1, "page_key": "rule.a", "title": "规则A", "path": "rules/a.md", "content": "集合竞价" * 300, "score": 10},
        {"id": 2, "page_key": "rule.b", "title": "规则B", "path": "rules/b.md", "content": "撤销申报" * 300, "score": 9},
    ]
    source_hits = [
        {"id": 11, "document_id": 7, "title": "3.5.2 原文", "text": "3.5.2 最大成交量" * 200, "clause_ids": ["3.5.2"], "score": 8},
        {"id": 11, "document_id": 7, "title": "重复原文", "text": "重复", "clause_ids": ["3.5.2"], "score": 7},
        {"id": 12, "document_id": 8, "title": "4.2.4 原文", "text": "4.2.4 临时停牌" * 200, "clause_ids": ["4.2.4"], "anchor_clause": "4.2.4", "score": 6},
    ]
    result = assemble_task_context(
        wiki_hits,
        source_hits,
        query="验证3.5.2集合竞价",
        max_chars=1200,
        include_explain=True,
    )

    assert len(result["wiki_context"]) <= result["budgets"]["wiki_chars"]
    assert len(result["source_context"]) <= result["budgets"]["source_chars"]
    assert "规则A" in result["wiki_context"] and "规则B" in result["wiki_context"]
    assert len(result["source_hits"]) == 2
    assert {citation["source_chunk_id"] for citation in result["citations"] if citation["citation_type"] == "source"} == {11, 12}
    assert result["explicit_anchor_clause_ids"] == ["3.5.2"]
    assert next(hit for hit in result["source_hits"] if hit["id"] == 12)["anchor_clause"] is None
