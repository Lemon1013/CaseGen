"""Mock external LLM Wiki service for CaseGen end-to-end verification.

Implements the subset of docs/llm-wiki-reference/API_README.md consumed by
app/services/external_wiki_client.py using only the Python standard library:

  GET  /api/projects/                          -> {"projects": [...]}
  POST /api/projects/{project_id}/search/      -> {"results": [...], ...}
  GET  /health                                 -> {"status": "ok"}

Start with:
  python mock_external_wiki/server.py --port 8020 --host 127.0.0.1

Use ``--port 0`` to bind a random free port (the actual port is printed).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# Built-in data
# ---------------------------------------------------------------------------

MOCK_PROJECTS: list[dict[str, Any]] = [
    {
        "id": "proj-sse-rules",
        "name": "上交所交易规则知识库",
        "description": "上海证券交易所交易规则、竞价交易与大宗交易实施细则知识库（演示数据）。",
    },
    {
        "id": "proj-fund-rules",
        "name": "基金业务规则知识库",
        "description": "公开募集证券投资基金运作、申购赎回与估值规则知识库（演示数据）。",
    },
]

_SSE_DOC = "交易规则/上海证券交易所交易规则(2023修订).docx"
_SSE_BLOCK_DOC = "交易规则/上海证券交易所大宗交易实施细则.docx"
_FUND_DOC = "基金法规/公开募集证券投资基金运作管理办法.docx"
_ETF_DOC = "基金法规/交易型开放式指数基金业务实施细则.docx"

# ``score`` stores a static base prior (small tie-breaker bonus during search);
# the score actually returned to callers is recomputed from query matching.
# ``highlightTerms`` lists the slice's characteristic keywords; terms that also
# appear in the query get an extra bonus and join the returned highlight list.
MOCK_CHUNKS: dict[str, list[dict[str, Any]]] = {
    "proj-sse-rules": [
        {
            "chunkId": 101,
            "documentPath": _SSE_DOC,
            "headingPath": "第三章 证券买卖 > 第一节 竞价交易 > 集合竞价",
            "title": "集合竞价成交价格的确定",
            "score": 1.8,
            "snippet": (
                "集合竞价时，成交价格的确定原则为：可实现最大成交量；高于该价格的买入申报"
                "与低于该价格的卖出申报全部成交；与该价格相同的买方或卖方至少有一方全部成交。"
            ),
            "evidenceSnippet": (
                "第九十四条 集合竞价时，成交价格的确定原则为：（一）可实现最大成交量；"
                "（二）高于该价格的买入申报与低于该价格的卖出申报全部成交；"
                "（三）与该价格相同的买方或卖方至少有一方全部成交。"
                "两个以上价格符合上述条件的，取使未成交量最小的申报价格为成交价格。"
            ),
            "highlightTerms": ["集合竞价", "最大成交量", "成交价格"],
        },
        {
            "chunkId": 102,
            "documentPath": _SSE_DOC,
            "headingPath": "第三章 证券买卖 > 第二节 涨跌幅限制",
            "title": "股票交易涨跌幅限制",
            "score": 1.6,
            "snippet": (
                "本所对股票、基金交易实行价格涨跌幅限制，涨跌幅限制比例为10%。"
                "超过涨跌幅限制的申报为无效申报。"
            ),
            "evidenceSnippet": (
                "第六十条 本所对股票、基金交易实行价格涨跌幅限制，涨跌幅限制比例为10%，"
                "ST股票（含*ST股票）涨跌幅限制比例为5%。"
                "涨跌幅限制价格的计算公式为：涨跌幅限制价格=前收盘价×(1±涨跌幅限制比例)，"
                "计算结果按照四舍五入原则取至价格最小变动单位。"
            ),
            "highlightTerms": ["涨跌幅限制", "10%", "无效申报"],
        },
        {
            "chunkId": 103,
            "documentPath": _SSE_BLOCK_DOC,
            "headingPath": "第三章 证券买卖 > 第四节 大宗交易 > 申报",
            "title": "大宗交易申报要素与数量门槛",
            "score": 1.4,
            "snippet": (
                "大宗交易的申报价格须在当日涨跌幅限制价格范围内。"
                "A股单笔申报数量应当不低于30万股，或者交易金额不低于200万元人民币。"
            ),
            "evidenceSnippet": (
                "《上海证券交易所大宗交易实施细则》第五条：A股大宗交易单笔申报数量应当"
                "不低于30万股，或者交易金额不低于200万元人民币；基金或债券大宗交易的"
                "申报门槛由本所另行规定。大宗交易申报时间为每个交易日9:30至11:30、"
                "13:00至15:30。"
            ),
            "highlightTerms": ["大宗交易", "申报数量", "30万股"],
        },
        {
            "chunkId": 104,
            "documentPath": _SSE_DOC,
            "headingPath": "第三章 证券买卖 > 第一节 竞价交易 > 申报与撤单",
            "title": "撤销申报（撤单）的时间限制",
            "score": 1.5,
            "snippet": (
                "每个交易日9:20至9:25的开盘集合竞价阶段、14:57至15:00的收盘集合竞价阶段，"
                "交易主机不接受撤单申报；其他接受申报的时间内，未成交的申报可以撤销。"
            ),
            "evidenceSnippet": (
                "第八十九条 本所交易主机接受申报后，在9:20至9:25与14:57至15:00期间"
                "不接受撤单申报；其余时间内未成交的申报可以撤销，已成交的申报不得撤销。"
            ),
            "highlightTerms": ["撤单", "撤销申报", "集合竞价"],
        },
        {
            "chunkId": 105,
            "documentPath": _SSE_DOC,
            "headingPath": "第三章 证券买卖 > 第一节 竞价交易 > 开盘价与收盘价",
            "title": "开盘价与收盘价的确定",
            "score": 1.5,
            "snippet": (
                "证券的开盘价通过开盘集合竞价方式产生，不能产生开盘价的，"
                "以连续竞价的第一笔成交价为开盘价；收盘价通过收盘集合竞价方式产生。"
            ),
            "evidenceSnippet": (
                "第九十六条 证券的开盘价通过集合竞价方式产生，不能产生开盘价的，"
                "以连续竞价方式产生的第一笔成交价格为开盘价。"
                "证券的收盘价通过收盘集合竞价方式产生；收盘集合竞价未产生成交价格的，"
                "以当日该证券最后一笔交易前一分钟所有交易的成交量加权平均价为收盘价。"
            ),
            "highlightTerms": ["开盘价", "收盘价", "集合竞价"],
        },
        {
            "chunkId": 106,
            "documentPath": _SSE_DOC,
            "headingPath": "第三章 证券买卖 > 第一节 竞价交易 > 交易时间",
            "title": "竞价交易时间安排",
            "score": 1.5,
            "snippet": (
                "开盘集合竞价时间为9:15至9:25，连续竞价时间为9:30至11:30、13:00至14:57，"
                "收盘集合竞价时间为14:57至15:00。"
            ),
            "evidenceSnippet": (
                "第八十二条 每个交易日9:15至9:25为开盘集合竞价时间，9:30至11:30、"
                "13:00至14:57为连续竞价时间，14:57至15:00为收盘集合竞价时间，"
                "15:00至15:30为大宗交易时间。国家法定假日和本所公告的休市日，本所市场休市。"
            ),
            "highlightTerms": ["集合竞价", "连续竞价", "交易时间"],
        },
        {
            "chunkId": 107,
            "documentPath": _SSE_DOC,
            "headingPath": "第三章 证券买卖 > 第一节 竞价交易 > 连续竞价",
            "title": "连续竞价成交价格的确定",
            "score": 1.4,
            "snippet": (
                "连续竞价时，最高买入申报价格与最低卖出申报价格相同时，以该价格为成交价；"
                "买入申报价格高于即时揭示的最低卖出申报价格时，以该最低卖出申报价格为成交价。"
            ),
            "evidenceSnippet": (
                "第九十五条 连续竞价时，成交价格的确定原则为：（一）最高买入申报价格与"
                "最低卖出申报价格相同时，以该价格为成交价格；（二）买入申报价格高于"
                "即时揭示的最低卖出申报价格时，以即时揭示的最低卖出申报价格为成交价格；"
                "（三）卖出申报价格低于即时揭示的最高买入申报价格时，以即时揭示的最高"
                "买入申报价格为成交价格。"
            ),
            "highlightTerms": ["连续竞价", "成交价格", "申报价格"],
        },
        {
            "chunkId": 108,
            "documentPath": _SSE_DOC,
            "headingPath": "第三章 证券买卖 > 第一节 竞价交易 > 申报",
            "title": "市价申报与保护限价",
            "score": 1.2,
            "snippet": (
                "市价申报仅适用于有价格涨跌幅限制证券的连续竞价期间。"
                "市价申报应当输入保护限价，买入申报的保护限价不高于买入参考价的102%。"
            ),
            "evidenceSnippet": (
                "第八十八条 投资者可以采用限价申报和市价申报方式委托。市价申报类型包括"
                "最优五档即时成交剩余撤销、最优五档即时成交剩余转限价等。"
                "采用市价申报的，应当输入保护限价；保护限价用于限制成交价格的最大偏离。"
            ),
            "highlightTerms": ["市价申报", "保护限价", "连续竞价"],
        },
    ],
    "proj-fund-rules": [
        {
            "chunkId": 201,
            "documentPath": _FUND_DOC,
            "headingPath": "第二章 基金份额的申购与赎回 > 申购费用",
            "title": "申购费用与费率结构",
            "score": 1.5,
            "snippet": (
                "申购费用采用比例费率或固定金额。"
                "净申购金额=申购金额/(1+申购费率)，申购份额=净申购金额/申购当日基金份额净值。"
            ),
            "evidenceSnippet": (
                "第十五条 投资者申购基金份额时应当缴纳申购费用。申购费用在申购时收取，"
                "不得在基金合同约定之外收取其他费用。赎回费用在赎回时从赎回金额中扣除，"
                "不低于25%的部分归基金财产所有。"
            ),
            "highlightTerms": ["申购费用", "费率", "基金份额净值"],
        },
        {
            "chunkId": 202,
            "documentPath": _FUND_DOC,
            "headingPath": "第二章 基金份额的申购与赎回 > 赎回",
            "title": "赎回款项的支付时限",
            "score": 1.5,
            "snippet": (
                "投资者赎回申请成功后，基金管理人应当自接受赎回申请之日起"
                "不超过7个工作日内支付赎回款项。"
            ),
            "evidenceSnippet": (
                "第二十条 基金管理人应当自接受基金份额持有人有效赎回申请之日起7个工作日内，"
                "支付赎回款项。发生巨额赎回时，赎回款项的支付按照巨额赎回的约定办理。"
            ),
            "highlightTerms": ["赎回", "赎回款项", "工作日"],
        },
        {
            "chunkId": 203,
            "documentPath": _FUND_DOC,
            "headingPath": "第二章 基金份额的申购与赎回 > 巨额赎回",
            "title": "巨额赎回的认定与处理",
            "score": 1.6,
            "snippet": (
                "单个开放日基金净赎回申请超过基金总份额的10%时，为巨额赎回。"
                "基金管理人可以接受全部赎回申请，也可以延期办理部分赎回。"
            ),
            "evidenceSnippet": (
                "第二十三条 单个开放日基金净赎回申请超过基金总份额百分之十的，为巨额赎回。"
                "发生巨额赎回时，基金管理人对单个基金份额持有人的赎回申请，应当按照其申请"
                "赎回份额占当日申请赎回总份额的比例，确定该持有人当日受理的赎回份额。"
            ),
            "highlightTerms": ["巨额赎回", "净赎回", "10%"],
        },
        {
            "chunkId": 204,
            "documentPath": _FUND_DOC,
            "headingPath": "第三章 基金财产 > 估值",
            "title": "基金资产估值方法",
            "score": 1.4,
            "snippet": (
                "基金资产按照公允价值估值。上市交易的有价证券以估值日收盘价估值；"
                "估值日无交易的，采用最近交易日收盘价或指数收益法等估值技术确定公允价值。"
            ),
            "evidenceSnippet": (
                "第三十七条 基金估值应当遵循公允价值原则。对存在活跃市场的投资品种，"
                "估值日有市价的，应当采用市价确定公允价值；对不存在活跃市场的投资品种，"
                "应当采用市场参与者普遍认同的估值技术确定公允价值。"
            ),
            "highlightTerms": ["估值", "公允价值", "收盘价"],
        },
        {
            "chunkId": 205,
            "documentPath": _FUND_DOC,
            "headingPath": "第四章 基金收益与分配 > 收益分配",
            "title": "基金收益分配条件",
            "score": 1.3,
            "snippet": (
                "基金当年收益应当先弥补以前年度亏损后，方可进行收益分配。"
                "基金收益分配后基金份额净值不能低于面值。"
            ),
            "evidenceSnippet": (
                "第四十四条 基金收益分配应当符合下列规定：（一）基金当年收益先弥补以前年度"
                "亏损后，方可进行当年收益分配；（二）基金收益分配后，基金份额净值不能低于"
                "面值；（三）符合基金合同关于收益分配的约定。"
            ),
            "highlightTerms": ["收益分配", "净值", "面值"],
        },
        {
            "chunkId": 206,
            "documentPath": _ETF_DOC,
            "headingPath": "第五章 交易型开放式指数基金 > 申购赎回",
            "title": "ETF申购赎回清单与最小申赎单位",
            "score": 1.4,
            "snippet": (
                "ETF实行实物申赎机制，投资者申购、赎回必须以最小申购赎回单位或其整数倍进行。"
                "申购赎回清单中可以设置现金替代标志，替代金额于T+2日确认。"
            ),
            "evidenceSnippet": (
                "第五十二条 基金管理人在每个开放日前公布申购赎回清单，包括最小申购赎回单位、"
                "各成份证券名称及数量、现金替代标志、现金替代溢价比例、基金份额参考净值等要素。"
            ),
            "highlightTerms": ["ETF", "申购赎回清单", "最小申购赎回单位"],
        },
        {
            "chunkId": 207,
            "documentPath": _FUND_DOC,
            "headingPath": "第六章 销售与投资者服务 > 定期定额投资",
            "title": "定期定额投资扣款规则",
            "score": 1.2,
            "snippet": (
                "定期定额投资按照约定日期和金额自动发起申购申请。"
                "扣款日遇非开放日或非交易日的，顺延至下一开放日扣款。"
            ),
            "evidenceSnippet": (
                "第五十八条 投资者与销售机构约定定期定额投资的扣款日期和扣款金额后，"
                "销售机构按照约定自动发起申购申请。扣款日为非开放日的，顺延至下一个开放日，"
                "并按照顺延当日的基金份额净值确认申购份额。"
            ),
            "highlightTerms": ["定期定额", "扣款", "开放日"],
        },
    ],
}

# ---------------------------------------------------------------------------
# Deterministic scoring
# ---------------------------------------------------------------------------

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.%-]*")

# (response field, weight) — higher weight means a hit counts more.
FIELD_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("title", 3.0),
    ("headingPath", 2.0),
    ("snippet", 1.0),
    ("evidenceSnippet", 1.0),
)

SYNONYM_EXPANSION_FACTOR = 0.6
HIGHLIGHT_TERM_BONUS = 1.5
_BASE_SCORE_FACTOR = 0.1
_MAX_RUN_TERM_LEN = 8
_MAX_LIMIT = 50
_DEFAULT_LIMIT = 8

# Small synonym table mirroring the real service's synonym.yaml behaviour.
SYNONYM_TABLE: dict[str, list[str]] = {
    "竞价": ["撮合", "集中竞价"],
    "撮合": ["竞价", "集中竞价"],
    "集合竞价": ["开盘集合竞价", "集中竞价"],
    "涨跌停": ["涨跌幅限制"],
    "涨跌幅限制": ["涨跌停"],
    "撤单": ["撤销申报", "撤销委托"],
    "撤销申报": ["撤单"],
    "开盘价": ["开盘价格"],
    "收盘价": ["收盘价格"],
    "大宗交易": ["大宗买卖"],
    "大宗买卖": ["大宗交易"],
}


def _extract_query_terms(query: str) -> set[str]:
    """Split a query into CJK 2-grams/runs and latin tokens (deterministic)."""
    terms: set[str] = set()
    for run in _CJK_RUN_RE.findall(query):
        if len(run) == 1:
            terms.add(run)
            continue
        if len(run) <= _MAX_RUN_TERM_LEN:
            terms.add(run)
        for i in range(len(run) - 1):
            terms.add(run[i : i + 2])
    for token in _LATIN_TOKEN_RE.findall(query):
        if len(token) >= 2:
            terms.add(token)
    return terms


def _expand_terms(terms: set[str]) -> set[str]:
    """Expand query terms through the synonym table (skipping known terms)."""
    expanded: set[str] = set()
    for term in terms:
        for synonym in SYNONYM_TABLE.get(term, ()):  # noqa: B007 - small data
            if synonym not in terms:
                expanded.add(synonym)
    return expanded


def _score_chunk(
    chunk: dict[str, Any],
    query: str,
    primary: set[str],
    expanded: set[str],
) -> tuple[float, set[str]]:
    """Return (score, matched terms) for a chunk against the query."""
    matched: set[str] = set()
    score = 0.0
    for field, weight in FIELD_WEIGHTS:
        text = str(chunk.get(field) or "")
        if not text:
            continue
        for term in primary:
            if term in text:
                matched.add(term)
                score += weight * len(term)
        for term in expanded:
            if term in text:
                matched.add(term)
                score += weight * len(term) * SYNONYM_EXPANSION_FACTOR
    full_text = " ".join(str(chunk.get(field) or "") for field, _ in FIELD_WEIGHTS)
    for term in chunk.get("highlightTerms") or []:
        keyword = str(term)
        if keyword and keyword in query and keyword in full_text:
            matched.add(keyword)
            score += HIGHLIGHT_TERM_BONUS
    score += float(chunk.get("score") or 0.0) * _BASE_SCORE_FACTOR
    return score, matched


def _sort_highlight_terms(matched: set[str]) -> list[str]:
    return sorted(matched, key=lambda term: (-len(term), term))


def search_chunks(
    project_id: str,
    query: str,
    limit: int,
    use_synonyms: bool,
) -> tuple[list[dict[str, Any]], list[str]] | None:
    """Search one project's chunks. Returns (results, expanded_terms) or None
    when the project id is unknown."""
    chunks = MOCK_CHUNKS.get(project_id)
    if chunks is None:
        return None
    primary = _extract_query_terms(query)
    expanded = _expand_terms(primary) if use_synonyms else set()
    scored: list[tuple[float, int, dict[str, Any], set[str]]] = []
    for index, chunk in enumerate(chunks):
        score, matched = _score_chunk(chunk, query, primary, expanded)
        if not matched:
            continue
        scored.append((-score, index, chunk, matched))
    scored.sort(key=lambda item: (item[0], item[1]))
    results: list[dict[str, Any]] = []
    for _neg_score, _index, chunk, matched in scored[: max(0, limit)]:
        item = dict(chunk)
        item["score"] = round(-_neg_score, 2)
        item["highlightTerms"] = _sort_highlight_terms(matched)
        results.append(item)
    return results, _sort_highlight_terms(expanded)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()
_PROJECTS_SEARCH_RE = re.compile(r"^/api/projects/([^/]+)/search/?$")


def _log_line(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _LOG_LOCK:
        sys.stdout.write(f"[{timestamp}] {message}\n")
        sys.stdout.flush()


def _normalize_flag(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in {"false", "0", "no"}


class MockWikiHandler(BaseHTTPRequestHandler):
    server_version = "MockLLMWiki/1.0"
    protocol_version = "HTTP/1.1"

    def _log(self, message: str) -> None:
        _log_line(message)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _request_path(self) -> str:
        return urlsplit(self.path).path

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path = self._request_path()
        if path in ("/health", "/health/"):
            self._log(f"GET {path} -> 200")
            self._send_json(200, {"status": "ok"})
            return
        if path.rstrip("/") == "/api/projects":
            self._log(f"GET {path} -> 200 ({len(MOCK_PROJECTS)} projects)")
            self._send_json(200, {"projects": MOCK_PROJECTS})
            return
        self._log(f"GET {path} -> 404 (not found)")
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        path = self._request_path()
        match = _PROJECTS_SEARCH_RE.match(path)
        if match is None:
            if path.rstrip("/") == "/api/projects":
                self._log(f"POST {path} -> 405 (method not allowed)")
                self._send_json(405, {"error": "method not allowed"})
            else:
                self._log(f"POST {path} -> 404 (not found)")
                self._send_json(404, {"error": "not found"})
            return

        project_id = match.group(1)
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._log(f"POST {path} -> 400 (invalid JSON body)")
            self._send_json(400, {"error": "invalid JSON body"})
            return
        if not isinstance(payload, dict):
            self._log(f"POST {path} -> 400 (invalid JSON body)")
            self._send_json(400, {"error": "invalid JSON body"})
            return

        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            self._log(f"POST {path} -> 400 (query is required, got {query!r})")
            self._send_json(400, {"error": "query is required"})
            return
        try:
            limit = int(payload.get("limit", _DEFAULT_LIMIT))
        except (TypeError, ValueError):
            limit = _DEFAULT_LIMIT
        limit = max(1, min(_MAX_LIMIT, limit))
        use_synonyms = _normalize_flag(payload.get("useSynonyms"), default=True)

        started = time.perf_counter()
        outcome = search_chunks(project_id, query.strip(), limit, use_synonyms)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        if outcome is None:
            self._log(f'POST {path} query="{query}" limit={limit} -> 404 (project not found)')
            self._send_json(404, {"error": "project not found"})
            return
        results, expanded_terms = outcome
        response: dict[str, Any] = {
            "results": results,
            "searchContextId": f"mock-{uuid.uuid4().hex[:12]}",
            "elapsedMs": elapsed_ms,
        }
        if expanded_terms:
            response["synonymExpansion"] = {
                "query": query,
                "expandedTerms": expanded_terms,
            }
        self._log(
            f'POST {path} query="{query}" limit={limit} '
            f"useSynonyms={str(use_synonyms).lower()} -> 200 ({len(results)} results)"
        )
        self._send_json(200, response)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib API
        # Default stderr logging is silenced; access lines are written to stdout.
        return


def build_server(host: str = "127.0.0.1", port: int = 8020) -> ThreadingHTTPServer:
    """Create (and bind) the mock server without serving yet."""
    server = ThreadingHTTPServer((host, port), MockWikiHandler)
    server.daemon_threads = True
    return server


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mock external LLM Wiki service for CaseGen end-to-end verification."
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    parser.add_argument(
        "--port",
        type=int,
        default=8020,
        help="bind port; 0 selects a random free port (default: 8020)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    server = build_server(args.host, args.port)
    bound_host, bound_port = server.server_address[:2]
    print(f"Mock external LLM Wiki listening on http://{bound_host}:{bound_port}", flush=True)
    print(
        "Projects: " + ", ".join(f"{p['id']} ({p['name']})" for p in MOCK_PROJECTS),
        flush=True,
    )
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down mock external wiki server...", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
