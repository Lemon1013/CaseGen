from datetime import date

import pytest
from pydantic import ValidationError

from app import config
from app.services.wiki_schema import (
    WikiFrontmatter,
    WikiPage,
    normalize_frontmatter,
    parse_frontmatter,
    parse_wiki_page,
    serialize_frontmatter,
    serialize_wiki_page,
    validate_page_key,
)


def _rule_frontmatter() -> WikiFrontmatter:
    return WikiFrontmatter(
        page_key="rule.order.insufficient-balance",
        title="余额不足时的下单处理",
        type="rule",
        domain="spot-order",
        aliases=["余额不足下单", "余额不足下单"],
        tags=["余额", "下单"],
        sources=[
            {
                "document_id": 12,
                "chunk_ids": [81, 82],
                "clauses": ["3.5.2"],
            }
        ],
        status="published",
        revision=3,
        updated_at=date(2026, 8, 2),
    )


def test_frontmatter_and_page_round_trip():
    metadata = _rule_frontmatter()
    page = WikiPage(frontmatter=metadata, body="# 规则\n\n余额不足时拒绝下单。")

    serialized = serialize_wiki_page(page)
    reparsed = parse_wiki_page(serialized)

    assert reparsed.frontmatter == page.frontmatter
    assert reparsed.body == page.body
    assert parse_frontmatter(serialized)[0] == normalize_frontmatter(metadata)

    frontmatter_only = serialize_frontmatter(metadata)
    assert frontmatter_only.startswith("---\n")
    assert frontmatter_only.endswith("\n---")


@pytest.mark.parametrize("page_type", ["business", "api_rule", "source_summary", "unknown"])
def test_invalid_page_type_is_rejected(page_type):
    with pytest.raises(ValidationError):
        WikiFrontmatter(
            page_key="rule.order.invalid-type",
            title="非法类型",
            type=page_type,
        )


@pytest.mark.parametrize(
    "page_key",
    [
        "Rule.order.balance",
        "rule/order/balance",
        r"rule\order\balance",
        "../rule.order.balance",
        "rule..order.balance",
        "/absolute.page",
        "C:drive.page",
        "rule.order.balance ",
        ".rule.order",
    ],
)
def test_invalid_and_path_traversal_page_keys_are_rejected(page_key):
    with pytest.raises(ValueError):
        validate_page_key(page_key)


def test_rule_page_requires_sources():
    with pytest.raises(ValidationError):
        WikiFrontmatter(
            page_key="rule.order.no-source",
            title="没有来源的规则",
            type="rule",
            sources=[],
        )

    with pytest.raises(ValidationError):
        WikiFrontmatter(
            page_key="rule.order.invalid-source",
            title="无效来源的规则",
            type="rule",
            sources=[{"document_id": 0}],
        )


def test_startup_copies_defaults_without_overwriting_user_files(tmp_app_data):
    config.ensure_data_dirs()
    purpose_path = tmp_app_data / "wiki" / "purpose.md"
    schema_path = tmp_app_data / "wiki" / "schema.md"
    assert purpose_path.exists()
    assert schema_path.exists()
    assert "CaseGen Wiki" in purpose_path.read_text(encoding="utf-8")
    assert "页面类型" in schema_path.read_text(encoding="utf-8")

    purpose_path.write_text("# 用户定制的目标\n", encoding="utf-8")
    schema_path.write_text("# 用户定制的 Schema\n", encoding="utf-8")
    config.ensure_data_dirs()

    assert purpose_path.read_text(encoding="utf-8") == "# 用户定制的目标\n"
    assert schema_path.read_text(encoding="utf-8") == "# 用户定制的 Schema\n"

