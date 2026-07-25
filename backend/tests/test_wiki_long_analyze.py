import json

from app.services.wiki_long_analyze import (
    split_analyze_windows,
    merge_analysis_partials,
    run_long_source_analyze,
)


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


def test_run_long_analyze_single_pass_under_budget(monkeypatch):
    monkeypatch.setattr("app.services.wiki_long_analyze.config.WIKI_ANALYZE_SINGLE_PASS_CHARS", 10000)
    monkeypatch.setattr("app.services.wiki_long_analyze.config.WIKI_ANALYZE_WINDOW_CHARS", 3000)

    calls: list[str] = []

    def chat(messages):
        calls.append(messages[-1]["content"])
        return json.dumps(
            {
                "summary_title": "短文",
                "key_rules": ["R1"],
                "api_points": [],
                "test_hints": [],
                "entities": [],
                "suggested_page_types": ["source_summary"],
            },
            ensure_ascii=False,
        )

    text = "短正文" * 10
    result = run_long_source_analyze(
        text,
        chat_fn=chat,
        analyze_system_prompt="输出 summary_title key_rules 仅 JSON",
        source_path="raw/x.md",
        filename="x.md",
    )
    assert result["mode"] == "single"
    assert result["analysis"]["key_rules"] == ["R1"]
    assert len(calls) == 1
    assert "短正文" in calls[0]


def test_run_long_analyze_multi_window_sees_tail(monkeypatch):
    monkeypatch.setattr("app.services.wiki_long_analyze.config.WIKI_ANALYZE_SINGLE_PASS_CHARS", 500)
    monkeypatch.setattr("app.services.wiki_long_analyze.config.WIKI_ANALYZE_WINDOW_CHARS", 200)
    monkeypatch.setattr("app.services.wiki_long_analyze.config.WIKI_ANALYZE_WINDOW_OVERLAP", 40)
    monkeypatch.setattr("app.services.wiki_long_analyze.config.WIKI_ANALYZE_WINDOW_RETRIES", 0)

    # Must exceed SINGLE_PASS (500) and yield >=2 windows at target 200.
    head = ("前部段落。\n\n" * 50) + ("填充内容。" * 80)
    tail = "\n\n尾部专属条款 TAIL_ONLY_RULE_9_9_9 必须被分析到。\n"
    text = head + tail
    assert len(text) > 500
    assert "TAIL_ONLY_RULE_9_9_9" in text

    call_n = {"n": 0}

    def chat(messages):
        call_n["n"] += 1
        user = messages[-1]["content"]
        rules = []
        if "TAIL_ONLY_RULE_9_9_9" in user:
            rules.append("TAIL_ONLY_RULE_9_9_9")
        else:
            rules.append(f"head_rule_{call_n['n']}")
        return json.dumps(
            {
                "summary_title": f"窗{call_n['n']}",
                "key_rules": rules,
                "api_points": [],
                "test_hints": [],
                "entities": [],
                "suggested_page_types": ["business"],
                "digest_update": f"digest-{call_n['n']}",
            },
            ensure_ascii=False,
        )

    steps: list[dict] = []

    def on_step(step: str, message: str, **extra):
        steps.append({"step": step, "message": message, **extra})

    result = run_long_source_analyze(
        text,
        chat_fn=chat,
        analyze_system_prompt="summary_title key_rules 仅 JSON",
        source_path="raw/long.md",
        filename="long.md",
        on_step=on_step,
    )
    assert result["mode"] == "multi"
    assert call_n["n"] >= 2
    assert "TAIL_ONLY_RULE_9_9_9" in result["analysis"]["key_rules"]
    assert any(s["step"] == "wiki_analyze_plan" for s in steps)
    assert any(s["step"] == "wiki_analyze_window" for s in steps)
    assert any(s["step"] == "wiki_analyze_consolidate" for s in steps)
