"""DSH session-plane gateway (line A: agent chat).

Single responsibility: speak HTTP to the local DSH web-service plugin and hand
back sanitized, filtered events.  Nothing here touches the database, the SSE
broker, or the browser contract; ``app.api.agent_chat`` owns those.

Design references:

- docs/superpowers/specs/2026-11-04-dsh-agent-integration-design.md §3.3/§6/§8
- backend/app/services/external_wiki_client.py (client build + error hygiene)

Hard rules implemented in this module:

1. Process data never leaves the backend.  ``reasoning`` is user-approved
   and forwarded as its own kind; a ``tool_call`` yields only a boundary
   event and, for whitelisted names, fixed anonymized copy — never its name,
   arguments or result.  ``tool_result`` frames are counted and dropped.
   No raw tool text reaches any return value, log record or database row.
2. Errors never interpolate raw exception text, DSH response bodies, URLs or
   proxy credentials.  Only whitelisted primitives (category, http_status,
   retryable, error_type) plus fixed Chinese copy leave this module.
3. A prompt is never retried automatically.  Re-sending ``prompt-stream``
   re-runs the whole agent turn, so every transport failure surfaces exactly
   once and the caller decides what to do.
4. Sync httpx only, matching ``llm.py`` and ``external_wiki_client.py``.
   Callers run inside a worker thread and must not hold a DB session.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import quote

import httpx

from app import config
from app.services.llm_http import select_environment_proxy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

_ERROR_CATEGORIES = frozenset({
    "unknown",
    "configuration",
    "unauthorized",
    "forbidden",
    "not_found",
    "conflict",
    "busy",
    "timeout",
    "network_error",
    "http_error",
    "stream_error",
    "bad_response",
    "permission_denied",
    "cancelled",
})

# Mirrors llm._SAFE_ERROR_TYPES: only these type names may be exported.
_SAFE_ERROR_TYPES = frozenset({
    "AgentGatewayError",
    "RuntimeError",
    "ValueError",
    "TypeError",
    "KeyError",
    "IndexError",
    "OSError",
    "TimeoutError",
    "Exception",
})

_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

_STATUS_CATEGORY: dict[int, str] = {
    400: "bad_response",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "bad_response",
    409: "conflict",
    422: "bad_response",
    501: "http_error",
}

# Fixed, user-presentable copy.  Only ``{status}`` (an int) is ever
# interpolated into these strings; no DSH text, URL or header is.
_CATEGORY_MESSAGE: dict[str, str] = {
    "configuration": "DSH 客户端配置无效，请检查 DSH_BASE_URL 与代理环境变量。",
    "unauthorized": "DSH 鉴权失败，请检查 DSH_API_TOKEN 与 DSH 服务端配置。",
    "forbidden": "DSH 拒绝了该操作，请检查会话权限设置。",
    "not_found": "DSH 会话不存在，请重新发起对话以创建新会话。",
    "conflict": "DSH 会话正忙，请等待本轮结束或先取消。",
    "busy": "DSH 会话正忙，请等待本轮结束或先取消。",
    "timeout": "DSH 响应超时，请稍后重试。",
    "network_error": "无法连接本地 DSH 服务，请确认其已启动并检查 DSH_BASE_URL。",
    "http_error": "DSH 服务返回错误状态，请稍后重试。",
    "stream_error": "DSH 流式响应中断，本轮回答可能不完整。",
    "bad_response": "DSH 返回的数据格式无法解析。",
    "permission_denied": "DSH 会话权限压降失败，已终止本次会话创建以避免越权。",
    "cancelled": "本轮对话已取消。",
    "unknown": "DSH 调用失败，请稍后重试。",
}


def _category_message(category: str, http_status: int | None = None) -> str:
    """Return fixed copy for a category, optionally suffixed with an int status."""

    base = _CATEGORY_MESSAGE.get(category, _CATEGORY_MESSAGE["unknown"])
    if http_status is not None and category in {"http_error", "bad_response"}:
        return f"{base}（HTTP {int(http_status)}）"
    return base


class AgentGatewayError(RuntimeError):
    """Safe diagnostics for a failed DSH call.

    ``message`` is always one of the fixed strings in ``_CATEGORY_MESSAGE`` so
    it can be shown to an end user verbatim.
    """

    def __init__(
        self,
        message: str = "",
        *,
        category: str = "unknown",
        http_status: int | None = None,
        retryable: bool = False,
    ) -> None:
        self.category = (
            category if type(category) is str and category in _ERROR_CATEGORIES else "unknown"
        )
        self.http_status = (
            http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        )
        self.retryable = retryable if type(retryable) is bool else False
        super().__init__(message or _category_message(self.category, self.http_status))


def safe_agent_error(exc: BaseException) -> dict[str, Any]:
    """Export only validated primitives, never exception text or payloads."""

    name = type(exc).__name__
    error_type = name if name in _SAFE_ERROR_TYPES else "Exception"
    if not isinstance(exc, AgentGatewayError):
        return {"category": "unexpected_error", "error_type": error_type}
    # Revalidate: a caller may have mutated the attributes after construction.
    safe = AgentGatewayError(
        category=exc.category, http_status=exc.http_status, retryable=exc.retryable
    )
    return {
        "category": safe.category,
        "http_status": safe.http_status,
        "retryable": safe.retryable,
        "error_type": error_type,
    }


def _http_client_error(exc: Exception) -> AgentGatewayError:
    # Keep the llm.py / external_wiki_client.py style: raw constructor errors
    # can include a proxy URL with credentials, so never interpolate them.
    if isinstance(exc, ImportError):
        logger.warning("DSH HTTP client initialization failed: missing proxy dependency.")
    else:
        logger.warning("DSH HTTP client configuration is invalid.")
    return AgentGatewayError(_category_message("configuration"), category="configuration")


# ---------------------------------------------------------------------------
# Auth circuit breaker
# ---------------------------------------------------------------------------


class AuthCircuitBreaker:
    """Trips after consecutive 401s so a misconfigured token cannot hot-loop.

    DSH ships ``authEnabled=false`` today; the breaker exists for the day an
    operator turns authentication on with a stale ``DSH_API_TOKEN``.
    """

    def __init__(self, threshold: int = 2, cooldown_sec: float = 300.0) -> None:
        self._threshold = max(1, int(threshold))
        self._cooldown_sec = max(1.0, float(cooldown_sec))
        self._lock = threading.Lock()
        self._consecutive = 0
        self._opened_at: float | None = None

    def record_failure(self) -> bool:
        """Record a 401 and return True only on the transition to open."""

        with self._lock:
            self._consecutive += 1
            if self._consecutive >= self._threshold and self._opened_at is None:
                self._opened_at = time.monotonic()
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._consecutive = 0
            self._opened_at = None

    def allow_request(self) -> bool:
        """Half-open after the cooldown so a fixed token recovers by itself."""

        with self._lock:
            if self._opened_at is None:
                return True
            if time.monotonic() - self._opened_at >= self._cooldown_sec:
                self._opened_at = None
                self._consecutive = 0
                return True
            return False

    @property
    def is_open(self) -> bool:
        """True while requests are suppressed (the cooldown has not elapsed)."""

        with self._lock:
            if self._opened_at is None:
                return False
            return time.monotonic() - self._opened_at < self._cooldown_sec

    def reset(self) -> None:
        with self._lock:
            self._consecutive = 0
            self._opened_at = None


auth_circuit = AuthCircuitBreaker()


def _guard_auth_circuit() -> None:
    if not auth_circuit.allow_request():
        raise AgentGatewayError(_category_message("unauthorized"), category="unauthorized")


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------

# Test seam: ``httpx.MockTransport`` installed here is used by every gateway
# call that does not receive an explicit ``transport=``.  Production leaves it
# None.  This is a transport seam only - it is not a chat/LLM hook and never
# changes pipeline execution mode.
_default_transport: httpx.BaseTransport | None = None


def set_default_transport(transport: httpx.BaseTransport | None) -> None:
    """Install (or clear) the process-wide mock transport used by tests."""

    global _default_transport
    _default_transport = transport


def default_transport() -> httpx.BaseTransport | None:
    return _default_transport


def _create_client(
    url: str,
    *,
    timeout: httpx.Timeout | float,
    transport: httpx.BaseTransport | None,
) -> httpx.Client:
    """Build a sync client exactly like external_wiki_client._create_client."""

    owned_transport: httpx.BaseTransport | None = None
    try:
        if transport is None:
            # Each call targets one absolute request URL; an explicit transport
            # avoids httpx's broken NO_PROXY mounts without disabling TLS env.
            owned_transport = httpx.HTTPTransport(
                proxy=select_environment_proxy(url), trust_env=True
            )
            transport = owned_transport
        return httpx.Client(timeout=timeout, transport=transport, trust_env=True)
    except (httpx.InvalidURL, httpx.HTTPError, ImportError, ValueError, OSError) as exc:
        if owned_transport is not None:
            try:
                owned_transport.close()
            except Exception:  # pragma: no cover - cleanup must not mask the error
                pass
        raise _http_client_error(exc) from None


# ---------------------------------------------------------------------------
# Response envelope
# ---------------------------------------------------------------------------


def _decode_json_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (ValueError, TypeError, json.JSONDecodeError):
        # Never interpolate the body: it may contain paths or provider text.
        raise AgentGatewayError(
            _category_message("bad_response", response.status_code),
            category="bad_response",
            http_status=response.status_code,
        ) from None


def _error_from_response(response: httpx.Response) -> AgentGatewayError:
    """Map a non-2xx response onto a sanitized error (status code only)."""

    status = int(response.status_code)
    category = _STATUS_CATEGORY.get(status)
    if category is None:
        category = "http_error" if status >= 500 else "bad_response"
    if status == 401 and auth_circuit.record_failure():
        logger.warning("DSH 连续鉴权失败，已临时熔断后续 agent 调用。")
    return AgentGatewayError(
        _category_message(category, status),
        category=category,
        http_status=status,
        retryable=status in _RETRYABLE_STATUS,
    )


def _unwrap_envelope(response: httpx.Response) -> Any:
    """Unwrap ``{ok, data, timestamp}`` (and ``{ok:false, error}``) payloads."""

    payload = _decode_json_body(response)
    if isinstance(payload, Mapping):
        if payload.get("ok") is False:
            raise _error_from_response(response)
        if "ok" in payload and "data" in payload:
            return payload.get("data")
    return payload



# ---------------------------------------------------------------------------
# Normalized downstream events + turn statistics
# ---------------------------------------------------------------------------

# Kinds the gateway may hand to its caller.  reasoning is surfaced as a
# dedicated downstream kind because the user approved showing the thinking
# process (m01118, "全量折叠"); it is never mixed into the answer delta.
# tool_call is an opaque turn boundary: its text is either "" or a fixed
# whitelisted notice, so real tool names (run_code, skill, ...) still delimit
# commentary from the final answer without leaking arguments or results.
# "ping" is a transport heartbeat used only to reset the caller's stall
# watchdog; it must never be forwarded to a browser.
AgentEventKind = Literal[
    "connected",
    "delta",
    "reasoning",
    "notice",
    "tool_call",
    "completed",
    "failed",
    "ping",
]


@dataclass(frozen=True)
class AgentEvent:
    """One sanitized, filtered event for the agent runner to consume."""

    kind: AgentEventKind
    text: str = ""
    session_id: str = ""
    reason: str = ""
    category: str = ""
    seq: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in ("", 0)}


@dataclass
class DshTurnStats:
    """Counts only.  Holds no reasoning/tool text, ever (bounded side channels)."""

    connected_frames: int = 0
    delta_frames: int = 0
    delta_chars: int = 0
    reasoning_frames: int = 0
    tool_call_frames: int = 0
    tool_result_frames: int = 0
    usage_frames: int = 0
    ping_frames: int = 0
    error_frames: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class SessionHandle:
    """Result of ensure_session: the ids the caller persists on the conversation."""

    session_id: str
    workspace_id: str
    cwd: str


# Anonymized liveness notices for long turns (the first token can take 5-8
# minutes while a model loads, and pure heartbeats look frozen in the UI).
# Lead-approved constraints: fixed literal copy only, zero dynamic content —
# no tool name, no arguments, no file path, no session id, no DSH text — and
# generic progress wording without the word "tool".  Keyed off the structured
# tool NAME; arguments are never read or forwarded.
_TOOL_NOTICE_WHITELIST: dict[str, str] = {
    "read": "正在查阅资料…",
    "grep": "正在查阅资料…",
    "glob": "正在查阅资料…",
    "ls": "正在查阅资料…",
    "web_search": "正在查阅资料…",
    "web_fetch": "正在查阅资料…",
    "bash": "正在处理…",
    "shell": "正在处理…",
    "edit": "正在处理…",
    "write": "正在处理…",
}

_TURN_CANCEL = frozenset({"cancelled", "canceled", "cancel", "aborted", "abort", "interrupted"})
_TURN_ERROR = frozenset({"error", "failed", "failure", "timeout"})


# ---------------------------------------------------------------------------
# Hand-written SSE parsing (no external SSE dependency)
# ---------------------------------------------------------------------------


def _safe_json(data: str) -> Any:
    """Parse a data payload; return None when it is not JSON (e.g. literal [DONE])."""

    text = (data or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _iter_raw_sse(response: httpx.Response, stats: DshTurnStats) -> Iterator[tuple[str, str]]:
    """Yield (event_name, data) pairs, consuming comment/ping frames.

    Framing is implemented by hand over httpx's byte-line iterator: an empty
    line dispatches the accumulated event, a leading colon is a comment frame
    (DSH sends ": ping" every 15s) which we count and drop, and "event:"/"data:"
    fields accumulate.  id:/retry: fields are ignored because the CaseGen SSE
    contract carries its own sequence numbering.
    """

    event_name = ""
    data_lines: list[str] = []
    for raw_line in response.iter_lines():
        line = raw_line if isinstance(raw_line, str) else str(raw_line)
        if line == "":
            if event_name or data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = ""
            data_lines = []
            continue
        if line.startswith(":"):
            # DSH sends ": ping" every 15s.  The comment body is dropped, but
            # the frame is surfaced as a liveness signal so the runner's stall
            # watchdog can tell "model is thinking" from "socket is dead".
            stats.ping_frames += 1
            yield "ping", ""
            continue
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            # Strip the single optional leading space per the SSE spec.
            value = line[len("data:"):]
            data_lines.append(value[1:] if value.startswith(" ") else value)
        # Any other field (id:, retry:) is intentionally ignored.
    if event_name or data_lines:
        yield event_name, "\n".join(data_lines)


def _turn_reason_kind(payload: Any) -> str:
    if isinstance(payload, Mapping):
        reason = payload.get("reason")
        if isinstance(reason, Mapping):
            return str(reason.get("kind") or "").strip().lower()
        if isinstance(reason, str):
            return reason.strip().lower()
    return ""


def _iter_prompt_events(
    response: httpx.Response,
    *,
    stats: DshTurnStats,
    session_id: str,
    should_stop: Optional[Any] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> Iterator[AgentEvent]:
    """Filter raw DSH frames into sanitized downstream AgentEvents.

    reasoning is forwarded as a dedicated reasoning event (user-approved
    "思考过程" display) and still counted.  tool_result is dropped silently
    (count only).  every tool_call is emitted as a tool_call boundary; its
    text is a fixed anonymized notice when the name is whitelisted, else ""
    (arguments are never read or forwarded).  usage is counted
    (tokens) and never forwarded.  delta becomes a downstream delta;
    turn_end/done become completed/failed; error becomes a sanitized failed
    event.
    """

    terminal_seen = False
    for name, data in _iter_raw_sse(response, stats):
        if should_stop is not None and should_stop():
            # Cooperative cancel: ask DSH to stop, then end the iteration.  The
            # runner emits the terminal frame so the browser never hangs.
            try:
                cancel(session_id, transport=transport)
            except AgentGatewayError:
                pass
            return
        event = (name or "").strip().lower()
        if event == "connected":
            stats.connected_frames += 1
            payload = _safe_json(data)
            sid = ""
            if isinstance(payload, Mapping):
                sid = str(payload.get("sessionId") or payload.get("session_id") or "")
            yield AgentEvent(kind="connected", session_id=sid or session_id)
            continue
        if event == "delta":
            payload = _safe_json(data)
            chunk = ""
            seq = 0
            if isinstance(payload, Mapping):
                raw = payload.get("delta")
                chunk = raw if isinstance(raw, str) else ""
                seq = int(payload.get("seq") or 0) if str(payload.get("seq", "")).isdigit() else 0
            elif isinstance(payload, str):
                chunk = payload
            stats.delta_frames += 1
            stats.delta_chars += len(chunk)
            if chunk:
                yield AgentEvent(kind="delta", text=chunk, seq=seq)
            continue
        if event == "reasoning":
            # User-approved thinking-process display: forward the chunk as a
            # dedicated reasoning event.  Real DSH frames use {"delta", "seq"};
            # the older {"text"} shape is kept for test/back-compat.
            stats.reasoning_frames += 1
            payload = _safe_json(data)
            chunk = ""
            seq = 0
            if isinstance(payload, Mapping):
                raw = payload.get("delta")
                if not isinstance(raw, str):
                    raw = payload.get("text")
                chunk = raw if isinstance(raw, str) else ""
                seq = (
                    int(payload.get("seq") or 0)
                    if str(payload.get("seq", "")).isdigit()
                    else 0
                )
            elif isinstance(payload, str):
                chunk = payload
            if chunk:
                yield AgentEvent(kind="reasoning", text=chunk, seq=seq)
            continue
        if event == "tool_call":
            stats.tool_call_frames += 1
            payload = _safe_json(data)
            tool_name = ""
            if isinstance(payload, Mapping):
                tool_name = str(payload.get("name") or "").strip().lower()
            # Every tool call is a turn boundary, even for names outside the
            # whitelist (run_code, skill, session_search, ...).  The runner
            # uses it to fold preceding commentary into the thinking process;
            # only the fixed whitelist copy is ever attached as text.
            notice = _TOOL_NOTICE_WHITELIST.get(tool_name) or ""
            yield AgentEvent(kind="tool_call", text=notice)
            continue
        if event == "tool_result":
            stats.tool_result_frames += 1
            continue
        if event == "usage":
            stats.usage_frames += 1
            payload = _safe_json(data)
            usage = payload.get("usage") if isinstance(payload, Mapping) else None
            if isinstance(usage, Mapping):
                stats.input_tokens += int(usage.get("inputTokens") or 0)
                stats.output_tokens += int(usage.get("outputTokens") or 0)
                stats.total_tokens += int(usage.get("totalTokens") or 0)
            continue
        if event == "turn_end":
            kind = _turn_reason_kind(_safe_json(data))
            if kind in _TURN_CANCEL:
                terminal_seen = True
                yield AgentEvent(kind="failed", reason=kind, category="cancelled",
                                 text=_category_message("cancelled"))
                return
            if kind in _TURN_ERROR:
                terminal_seen = True
                yield AgentEvent(kind="failed", reason=kind, category="stream_error",
                                 text=_category_message("stream_error"))
                return
            terminal_seen = True
            yield AgentEvent(kind="completed", reason=kind or "completed")
            return
        if event == "done":
            # data is the literal [DONE]; treat as clean end-of-stream.
            if not terminal_seen:
                terminal_seen = True
                yield AgentEvent(kind="completed", reason="done")
            return
        if event == "error":
            stats.error_frames += 1
            terminal_seen = True
            # The DSH error text is untrusted; never forward it.  The runner may
            # override the category to "cancelled" when it requested the stop.
            yield AgentEvent(kind="failed", category="stream_error",
                             text=_category_message("stream_error"))
            return
        if event == "ping":
            yield AgentEvent(kind="ping")
            continue
        # Unknown frame types are ignored (counted as neither process nor text).
    if not terminal_seen:
        # A clean TCP EOF without turn_end/done is not an application-level
        # completion signal (mirrors llm.py stream_incomplete handling).
        yield AgentEvent(kind="failed", category="stream_error",
                         text=_category_message("stream_error"))


# ---------------------------------------------------------------------------
# Gateway facade
# ---------------------------------------------------------------------------


def _auth_headers() -> dict[str, str]:
    """Injection point for a future DSH build with authEnabled=true.

    An empty token means no Authorization header at all.  The token value is
    never logged and never interpolated into an error message.
    """

    headers = {"Accept": "application/json"}
    token = (getattr(config, "DSH_API_TOKEN", "") or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def _stream_headers() -> dict[str, str]:
    headers = _auth_headers()
    headers["Accept"] = "text/event-stream"
    return headers


def _request_timeout() -> httpx.Timeout:
    # The first token can take 5-8 minutes while DSH loads a model, so the read
    # timeout must stay far above the LLM defaults.  Connect stays short.
    read = float(getattr(config, "AGENT_FIRST_TOKEN_TIMEOUT_SEC", 600))
    return httpx.Timeout(connect=10.0, read=max(30.0, read), write=30.0, pool=30.0)


def _url(path: str) -> str:
    base = (getattr(config, "DSH_BASE_URL", "") or "").rstrip("/")
    if not base:
        raise AgentGatewayError(_category_message("configuration"), category="configuration")
    return base + "/" + path.lstrip("/")


def conversation_workspace_dir(conversation_id: str) -> Path:
    """Resolve and guard the per-conversation workspace directory.

    The directory must stay strictly under the configured workspace root.  The
    id is our own uuid, so this is defense in depth: no caller-derived string
    may escape the root, and the root is a sibling of data/meta (which holds
    plaintext model API keys) rather than a parent of it.
    """

    conv_id = str(conversation_id or "").strip()
    if not conv_id:
        raise AgentGatewayError(_category_message("bad_response"), category="bad_response")
    root = Path(config.agent_workspace_root()).resolve()
    target = (root / conv_id).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise AgentGatewayError(
            _category_message("configuration"), category="configuration"
        ) from None
    return target


def ensure_workspace_dir(conversation_id: str) -> Path:
    """Create the conversation workspace directory and return its path."""

    target = conversation_workspace_dir(conversation_id)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _split_model(model: Optional[str]) -> tuple[str, str]:
    """Split a 'provider/model' string into (provider, model)."""

    text = (model or "").strip()
    if not text:
        return "", ""
    if "/" in text:
        provider, _, name = text.partition("/")
        return provider.strip(), name.strip()
    return "", text


def _extract_id(data: Any, keys: tuple[str, ...]) -> str:
    if isinstance(data, Mapping):
        for key in keys:
            value = data.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
        return ""
    if isinstance(data, (str, int)) and str(data).strip():
        return str(data).strip()
    return ""


class DshAgentGateway:
    """Thin sync wrapper over the DSH session plane.

    Every method accepts an optional transport so tests can install an
    httpx.MockTransport without touching the network.  No database access
    happens here; the caller persists whatever this returns.
    """

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Mapping[str, Any]] = None,
        timeout: Optional[httpx.Timeout] = None,
        headers: Optional[Mapping[str, str]] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> Any:
        _guard_auth_circuit()
        url = _url(path)
        client_transport = transport if transport is not None else _default_transport
        try:
            with _create_client(
                url, timeout=timeout or _request_timeout(), transport=client_transport
            ) as client:
                response = client.request(
                    method,
                    url,
                    json=dict(json_body) if json_body is not None else None,
                    headers=dict(headers or _auth_headers()),
                )
        except httpx.TimeoutException:
            logger.warning("DSH 请求超时。")
            raise AgentGatewayError(
                _category_message("timeout"), category="timeout", retryable=True
            ) from None
        except httpx.HTTPError:
            logger.warning("DSH 请求发生传输层错误。")
            raise AgentGatewayError(
                _category_message("network_error"), category="network_error", retryable=True
            ) from None
        if response.status_code >= 400:
            raise _error_from_response(response)
        auth_circuit.record_success()
        return _unwrap_envelope(response)

    def create_workspace(
        self,
        path: str,
        *,
        title: str = "",
        transport: Optional[httpx.BaseTransport] = None,
    ) -> str:
        """POST /workspaces -> workspace id."""

        payload: dict[str, Any] = {"path": str(path)}
        if title:
            payload["title"] = str(title)[:200]
        data = self._request("POST", "/workspaces", json_body=payload, transport=transport)
        workspace_id = _extract_id(data, ("id", "workspaceId", "workspace_id"))
        if not workspace_id:
            raise AgentGatewayError(
                _category_message("bad_response"), category="bad_response"
            )
        return workspace_id

    def create_session(
        self,
        *,
        workspace_id: str,
        title: str = "",
        model: Optional[str] = None,
        agent_preset: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> str:
        """POST /sessions {workspaceId, title, provider?, model?} -> session id."""

        payload: dict[str, Any] = {"workspaceId": str(workspace_id)}
        if title:
            payload["title"] = str(title)[:200]
        provider, model_name = _split_model(model)
        if provider:
            payload["provider"] = provider
        if model_name:
            payload["model"] = model_name
        if agent_preset:
            payload["agentPreset"] = str(agent_preset)
        data = self._request("POST", "/sessions", json_body=payload, transport=transport)
        session_id = _extract_id(data, ("sessionId", "session_id", "id"))
        if not session_id:
            raise AgentGatewayError(
                _category_message("bad_response"), category="bad_response"
            )
        return session_id

    def set_permission(
        self,
        session_id: str,
        preset: str = "workspace-write",
        *,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        """PUT /sessions/:id/permission.  A failure here is fatal for callers."""

        self._request(
            "PUT",
            "/sessions/" + quote(str(session_id), safe="") + "/permission",
            json_body={"preset": str(preset)},
            transport=transport,
        )

    def ensure_session(
        self,
        *,
        conversation_id: str,
        title: str = "",
        model: Optional[str] = None,
        workspace_id: Optional[str] = None,
        agent_preset: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> SessionHandle:
        """Three-step session provisioning (spec 3.3 erratum, lead-approved).

        DSH has no cwd field on POST /sessions, so the working directory comes
        from a workspace row: create the workspace (path guarded under the
        workspace root), create the session bound to it, then downgrade the
        permission preset from the danger-full-access default to
        workspace-write.  If the downgrade fails the whole call fails; the
        gateway never continues with full access.
        """

        workspace_dir = ensure_workspace_dir(conversation_id)
        resolved_workspace_id = (workspace_id or "").strip()
        if not resolved_workspace_id:
            resolved_workspace_id = self.create_workspace(
                str(workspace_dir),
                title=title or ("casegen-" + str(conversation_id)[:8]),
                transport=transport,
            )
        session_id = self.create_session(
            workspace_id=resolved_workspace_id,
            title=title,
            model=model,
            agent_preset=agent_preset,
            transport=transport,
        )
        try:
            self.set_permission(session_id, "workspace-write", transport=transport)
        except AgentGatewayError:
            logger.warning("DSH 会话权限压降失败，终止会话创建。")
            raise AgentGatewayError(
                _category_message("permission_denied"), category="permission_denied"
            ) from None
        return SessionHandle(
            session_id=session_id,
            workspace_id=resolved_workspace_id,
            cwd=str(workspace_dir),
        )

    def stream_prompt(
        self,
        session_id: str,
        prompt: str,
        *,
        should_stop: Optional[Any] = None,
        stats: Optional[DshTurnStats] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> Iterator[AgentEvent]:
        """POST /sessions/:id/prompt-stream and yield sanitized AgentEvents.

        NEVER auto-retries: repeating a prompt-stream call re-runs the whole
        agent turn, so a transport failure surfaces exactly once and the caller
        decides.  should_stop is polled between frames for cooperative cancel.
        """

        _guard_auth_circuit()
        url = _url("/sessions/" + quote(str(session_id), safe="") + "/prompt-stream")
        turn_stats = stats if stats is not None else DshTurnStats()
        client_transport = transport if transport is not None else _default_transport
        try:
            with _create_client(
                url, timeout=_request_timeout(), transport=client_transport
            ) as client:
                with client.stream(
                    "POST",
                    url,
                    json={"prompt": str(prompt), "mode": "normal"},
                    headers=_stream_headers(),
                ) as response:
                    if response.status_code >= 400:
                        response.read()
                        raise _error_from_response(response)
                    auth_circuit.record_success()
                    yield from _iter_prompt_events(
                        response,
                        stats=turn_stats,
                        session_id=str(session_id),
                        should_stop=should_stop,
                        transport=transport,
                    )
        except AgentGatewayError:
            raise
        except httpx.TimeoutException:
            logger.warning("DSH 流式响应超时。")
            yield AgentEvent(
                kind="failed", category="timeout", text=_category_message("timeout")
            )
        except httpx.HTTPError:
            logger.warning("DSH 流式响应发生传输层错误。")
            yield AgentEvent(
                kind="failed", category="stream_error", text=_category_message("stream_error")
            )

    def prompt_sync(
        self,
        session_id: str,
        prompt: str,
        *,
        timeout_ms: Optional[int] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> dict[str, Any]:
        """POST /sessions/:id/prompt -> {'content', 'tool_calls_count'}.

        Line-B entry point: only the final content is returned, never tool or
        reasoning payloads.
        """

        body: dict[str, Any] = {"prompt": str(prompt)}
        if timeout_ms is not None:
            body["timeout_ms"] = int(timeout_ms)
        data = self._request(
            "POST",
            "/sessions/" + quote(str(session_id), safe="") + "/prompt",
            json_body=body,
            transport=transport,
        )
        content = ""
        tool_calls_count = 0
        if isinstance(data, Mapping):
            raw = data.get("content")
            content = raw if isinstance(raw, str) else ""
            calls = data.get("toolCalls")
            if isinstance(calls, list):
                tool_calls_count = len(calls)
        elif isinstance(data, str):
            content = data
        return {"content": content, "tool_calls_count": tool_calls_count}

    def cancel(
        self,
        session_id: str,
        *,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> bool:
        """POST /sessions/:id/cancel.  Returns True on a clean 2xx response."""

        self._request(
            "POST",
            "/sessions/" + quote(str(session_id), safe="") + "/cancel",
            json_body={},
            transport=transport,
        )
        return True

    def delete_session(
        self,
        session_id: str,
        *,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> bool:
        """DELETE /sessions/:id - best-effort cleanup, never raises."""

        text = str(session_id or "").strip()
        if not text:
            return False
        try:
            self._request(
                "DELETE",
                "/sessions/" + quote(text, safe=""),
                transport=transport,
            )
        except AgentGatewayError as exc:
            logger.warning("DSH 会话清理失败：%s", getattr(exc, "category", "unknown"))
            return False
        return True

    def delete_workspace(
        self,
        workspace_id: str,
        *,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> bool:
        """DELETE /workspaces/:id - best-effort cleanup, never raises."""

        text = str(workspace_id or "").strip()
        if not text:
            return False
        try:
            self._request(
                "DELETE",
                "/workspaces/" + quote(text, safe=""),
                transport=transport,
            )
        except AgentGatewayError as exc:
            logger.warning("DSH 工作区清理失败：%s", getattr(exc, "category", "unknown"))
            return False
        return True


# ---------------------------------------------------------------------------
# Concurrency primitives (process-local, mirroring task_locks)
# ---------------------------------------------------------------------------


@dataclass
class _LockEntry:
    lock: threading.RLock = field(default_factory=threading.RLock)
    users: int = 0


class ConversationLockRegistry:
    """Per-conversation mutex with the TaskLockRegistry eviction discipline.

    Entries are removed when the holder and all waiters leave, so arbitrary
    conversation ids cannot grow the registry without bound.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[str, _LockEntry] = {}

    @contextmanager
    def hold(self, conversation_id: str) -> Iterator[None]:
        key = str(conversation_id)
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _LockEntry()
                self._entries[key] = entry
            entry.users += 1
        entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            with self._guard:
                entry.users -= 1
                if entry.users == 0 and self._entries.get(key) is entry:
                    self._entries.pop(key, None)

    def size(self) -> int:
        with self._guard:
            return len(self._entries)


class AgentSemaphore:
    """Global concurrency gate sized by AGENT_MAX_CONCURRENT (read lazily)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._semaphore: Optional[threading.Semaphore] = None
        self._limit = 0

    def _ensure(self) -> threading.Semaphore:
        limit = max(1, int(getattr(config, "AGENT_MAX_CONCURRENT", 4)))
        with self._lock:
            if self._semaphore is None or limit != self._limit:
                self._semaphore = threading.Semaphore(limit)
                self._limit = limit
            return self._semaphore

    @contextmanager
    def acquire(self) -> Iterator[None]:
        semaphore = self._ensure()
        semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()


conversation_locks = ConversationLockRegistry()
agent_semaphore = AgentSemaphore()
gateway = DshAgentGateway()


# Module-level wrappers so callers (and tests) can patch either the singleton
# attribute or the function itself.


def ensure_session(
    *,
    conversation_id: str,
    title: str = "",
    model: Optional[str] = None,
    workspace_id: Optional[str] = None,
    agent_preset: Optional[str] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> SessionHandle:
    return gateway.ensure_session(
        conversation_id=conversation_id,
        title=title,
        model=model,
        workspace_id=workspace_id,
        agent_preset=agent_preset,
        transport=transport,
    )


def stream_prompt(
    session_id: str,
    prompt: str,
    *,
    should_stop: Optional[Any] = None,
    stats: Optional[DshTurnStats] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> Iterator[AgentEvent]:
    return gateway.stream_prompt(
        session_id, prompt, should_stop=should_stop, stats=stats, transport=transport
    )


def prompt_sync(
    session_id: str,
    prompt: str,
    *,
    timeout_ms: Optional[int] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    return gateway.prompt_sync(session_id, prompt, timeout_ms=timeout_ms, transport=transport)


def cancel(
    session_id: str,
    *,
    transport: Optional[httpx.BaseTransport] = None,
) -> bool:
    return gateway.cancel(session_id, transport=transport)


def delete_session(
    session_id: str,
    *,
    transport: Optional[httpx.BaseTransport] = None,
) -> bool:
    return gateway.delete_session(session_id, transport=transport)


def delete_workspace(
    workspace_id: str,
    *,
    transport: Optional[httpx.BaseTransport] = None,
) -> bool:
    return gateway.delete_workspace(workspace_id, transport=transport)