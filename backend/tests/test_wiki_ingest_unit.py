from app.services.wiki_pages_parse import (
    pages_from_analysis,
    parse_json_flexible,
    split_wiki_pages,
)


def test_split_markdown_pages():
    raw = """---
title: T1
type: source_summary
sources: ["raw/sources/a.md"]
tags: ["余额"]
---
body1
---
title: T2
type: business
sources: ["raw/sources/a.md"]
tags: []
---
body2
"""
    pages = split_wiki_pages(raw)
    assert len(pages) == 2
    assert pages[0]["title"] == "T1"
    assert pages[0]["type"] == "source_summary"
    assert pages[0]["page_type"] == "source_summary"
    assert pages[0]["tags"] == ["余额"]
    assert pages[0]["sources"] == ["raw/sources/a.md"]
    assert pages[0]["body"] == "body1"
    assert pages[1]["title"] == "T2"
    assert pages[1]["type"] == "business"
    assert pages[1]["body"] == "body2"


def test_split_pages_inside_code_fence():
    raw = """```markdown
---
title: 摘要
type: source_summary
sources: ["x"]
tags: ["余额"]
---
内容A
---
title: 规则
type: business
sources: ["x"]
tags: []
---
内容B
```"""
    pages = split_wiki_pages(raw)
    assert len(pages) == 2
    assert pages[0]["title"] == "摘要"
    assert "内容A" in pages[0]["body"]


def test_parse_json_flexible_fenced():
    raw = """Here is analysis:
```json
{"summary_title": "余额", "key_rules": ["a"]}
```
"""
    data = parse_json_flexible(raw)
    assert data["summary_title"] == "余额"
    assert data["key_rules"] == ["a"]


def test_pages_from_analysis_fallback():
    analysis = {
        "summary_title": "上交所交易规则摘要",
        "key_rules": ["集合竞价规则", "连续竞价规则"],
        "api_points": ["下单接口需校验证券代码"],
        "test_hints": ["覆盖涨跌幅边界"],
        "entities": ["集合竞价", "连续竞价"],
        "suggested_page_types": ["source_summary", "business"],
    }
    pages = pages_from_analysis(
        analysis,
        source_path="raw/sources/sse.docx",
        filename="sse.docx",
    )
    assert len(pages) >= 3
    assert pages[0]["type"] == "source_summary"
    assert "集合竞价规则" in pages[0]["body"]
    types = {p["type"] for p in pages}
    assert "business" in types
    assert "test_hint" in types
    assert pages[0]["sources"] == ["raw/sources/sse.docx"]
