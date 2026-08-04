from app.services.review_parse import parse_review_payload


def test_parse_pure_json():
    text = """{
      "score": 88,
      "verdict": "pass",
      "issues": ["步骤略简"],
      "missing_scenarios": [],
      "prompt_improvement_hints": ["强调资金精度"],
      "ready_for_final": true
    }"""
    data = parse_review_payload(text)
    assert data["score"] == 88
    assert data["verdict"] == "pass"
    assert data["issues"] == ["步骤略简"]
    assert data["missing_scenarios"] == []
    assert data["prompt_improvement_hints"] == ["强调资金精度"]
    assert data["ready_for_final"] is True
    assert "raw" not in data


def test_parse_fenced_json():
    text = """以下是评审结果：

```json
{
  "score": 72,
  "verdict": "revise",
  "issues": ["缺异常路径"],
  "missing_scenarios": ["余额不足"],
  "prompt_improvement_hints": ["强制覆盖拒单"],
  "ready_for_final": false
}
```

请参考。
"""
    data = parse_review_payload(text)
    assert data["score"] == 72
    assert data["verdict"] == "revise"
    assert data["issues"] == ["缺异常路径"]
    assert data["ready_for_final"] is False


def test_parse_garbage_fallback():
    text = "这不是 JSON，也没有代码围栏。"
    data = parse_review_payload(text)
    assert data["score"] == 0
    assert data["verdict"] == "unknown"
    assert data["issues"] == []
    assert data["missing_scenarios"] == []
    assert data["prompt_improvement_hints"] == []
    assert data["ready_for_final"] is False
    assert data["raw"] == text


def test_normalize_invalid_score_verdict_and_boolean():
    data = parse_review_payload(
        '{"score": 130, "verdict": "maybe", "issues": [], '
        '"missing_scenarios": [], "prompt_improvement_hints": [], '
        '"ready_for_final": "false"}'
    )
    assert data["score"] == 100
    assert data["verdict"] == "unknown"
    assert data["ready_for_final"] is False


def test_serious_issue_blocks_ready_for_final():
    data = parse_review_payload(
        '{"score": 95, "verdict": "pass", '
        '"issues": ["[严重] 引用与原文冲突"], '
        '"missing_scenarios": [], "prompt_improvement_hints": [], '
        '"ready_for_final": true}'
    )
    assert data["ready_for_final"] is False
