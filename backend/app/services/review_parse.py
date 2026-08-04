from __future__ import annotations

import json
import re
from typing import Any

from app import config


_DEFAULTS: dict[str, Any] = {
    "score": 0,
    "verdict": "unknown",
    "issues": [],
    "missing_scenarios": [],
    "prompt_improvement_hints": [],
    "ready_for_final": False,
}
_VERDICTS = {"pass", "revise", "fail", "unknown"}


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(_DEFAULTS)
    if "score" in data:
        try:
            out["score"] = max(0, min(100, int(data["score"])))
        except (TypeError, ValueError):
            out["score"] = 0
    if data.get("verdict") is not None:
        verdict = str(data["verdict"]).strip().lower()
        out["verdict"] = verdict if verdict in _VERDICTS else "unknown"
    for key in ("issues", "missing_scenarios", "prompt_improvement_hints"):
        val = data.get(key)
        if isinstance(val, list):
            out[key] = [str(x) for x in val]
    if "ready_for_final" in data:
        out["ready_for_final"] = data["ready_for_final"] is True
    out["ready_for_final"] = bool(
        out["ready_for_final"]
        and out["verdict"] == "pass"
        and out["score"] >= config.FINAL_SCORE_THRESHOLD
        and not any(str(item).startswith("[严重]") for item in out["issues"])
        and not out["missing_scenarios"]
    )
    return out


def parse_review_payload(text: str) -> dict[str, Any]:
    """Parse LLM review output into a structured dict.

    1. Try json.loads on the whole text.
    2. Else extract a ```json fenced block.
    3. Else return unknown defaults with raw text preserved.
    """
    raw = text if text is not None else ""

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return _normalize(data)
    except (json.JSONDecodeError, TypeError):
        pass

    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE)
    if fence:
        try:
            data = json.loads(fence.group(1).strip())
            if isinstance(data, dict):
                return _normalize(data)
        except (json.JSONDecodeError, TypeError):
            pass

    return {**_DEFAULTS, "raw": raw}
