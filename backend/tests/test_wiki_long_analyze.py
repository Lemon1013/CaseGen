from app.services.wiki_long_analyze import split_analyze_windows, merge_analysis_partials


def test_split_covers_full_text_without_gaps():
    parts = [f"段落{i}。" + ("内容" * 50) for i in range(30)]
    text = "\n\n".join(parts)
    windows = split_analyze_windows(text, target_chars=800, overlap_chars=100)
    assert len(windows) >= 2
    assert windows[0]["start"] == 0
    assert windows[-1]["end"] == len(text)
    covered = 0
    for w in windows:
        assert w["start"] <= covered
        assert w["end"] > w["start"]
        assert w["main"] == text[w["start"] : w["end"]]
        covered = max(covered, w["end"])
    assert covered == len(text)
    assert windows[1]["overlap_before"]
    assert windows[1]["overlap_before"] in windows[0]["main"]


def test_split_short_text_single_window():
    text = "短文档仅一窗。"
    windows = split_analyze_windows(text, target_chars=12000, overlap_chars=1000)
    assert len(windows) == 1
    assert windows[0]["main"] == text
    assert windows[0]["overlap_before"] == ""


def test_merge_analysis_partials_dedupes_and_keeps_tail_rules():
    partials = [
        {
            "summary_title": "文档前部",
            "key_rules": ["规则A", "规则B"],
            "api_points": [],
            "test_hints": ["提示1"],
            "entities": ["实体1"],
            "suggested_page_types": ["source_summary"],
            "digest_update": "前部摘要",
        },
        {
            "summary_title": "文档后部",
            "key_rules": ["规则B", "尾部独有规则TAIL_MARKER"],
            "api_points": ["接口X"],
            "test_hints": [],
            "entities": ["实体1", "实体2"],
            "suggested_page_types": ["business"],
            "digest_update": "全文摘要含尾部",
        },
    ]
    merged = merge_analysis_partials(partials, digest="全文摘要含尾部", source_chars=9999)
    assert merged["summary_title"] == "文档前部"
    assert merged["key_rules"] == ["规则A", "规则B", "尾部独有规则TAIL_MARKER"]
    assert "接口X" in merged["api_points"]
    assert merged["entities"] == ["实体1", "实体2"]
    assert set(merged["suggested_page_types"]) >= {"source_summary", "business"}
    assert merged["global_digest"] == "全文摘要含尾部"
    assert merged["window_count"] == 2
    assert merged["coverage"]["chars"] == 9999
    assert merged["coverage"]["windows"] == 2
