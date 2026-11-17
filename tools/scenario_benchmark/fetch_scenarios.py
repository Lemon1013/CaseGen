#!/usr/bin/env python3
"""Download an existing task's current test-point snapshot; standard library only."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
from http.cookies import CookieError, SimpleCookie
import json
import math
import os
from pathlib import Path
import re
import socket
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BODY_BYTES = 8 * 1024 * 1024
CATEGORIES = ("AUC", "AMT", "ACC", "OPN", "CLS", "CAN")
FILE_NAMES = ("api_response.json", "scenarios.json", "manifest.json")
SENSITIVE_KEYS = frozenset({
    "authorization", "cookie", "set-cookie", "password", "api_key",
    "access_token", "refresh_token", "session_token", "bearer_token",
    "resume_claim_token",
})


class FetchError(Exception):
    """Safe, fixed diagnostic without server bodies or credentials."""


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse can interpolate arbitrary input; do not echo it.
        self.exit(2, "error: invalid CLI arguments; use --help for accepted options\n")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def positive_integer(value: str) -> int:
    try:
        result = int(value) if re.fullmatch(r"[0-9]+", value) else 0
    except ValueError:
        raise FetchError("task/project id is too large to parse safely") from None
    if result < 1:
        raise FetchError("task/project id must be a positive decimal integer")
    return result


def positive_timeout(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise FetchError("timeout must be a finite positive number of seconds") from None
    if not math.isfinite(result) or result <= 0:
        raise FetchError("timeout must be a finite positive number of seconds")
    return result


def endpoint_url(base_url: str, task_id: int, project_id: int | None = None) -> str:
    if type(task_id) is not int or task_id < 1:
        raise FetchError("task id must be a positive integer")
    if project_id is not None and (type(project_id) is not int or project_id < 1):
        raise FetchError("project id must be a positive integer")
    try:
        if any(ord(char) <= 32 or ord(char) == 127 for char in base_url):
            raise ValueError
        parts = urlsplit(base_url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or "?" in base_url or "#" in base_url or "\\" in base_url):
            raise ValueError
        _ = parts.port  # Reject malformed/out-of-range ports.
        path = parts.path.rstrip("/")
        decoded_path = unquote(path)
        if decoded_path.endswith("/api") or any(
                segment in {".", ".."} for segment in decoded_path.split("/")):
            raise ValueError
        if any(ord(char) <= 32 or ord(char) == 127 for char in decoded_path):
            raise ValueError
    except ValueError:
        raise FetchError(
            "invalid base URL: use an http(s) service root (optional path prefix), "
            "without /api, userinfo, query, fragment, or dot segments"
        ) from None
    query = f"project_id={project_id}" if project_id is not None else ""
    return urlunsplit((parts.scheme, parts.netloc, f"{path}/api/tasks/{task_id}/test-points", query, ""))


def auth_headers(bearer_env: str | None, cookie_env: str | None) -> tuple[dict[str, str], list[str]]:
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    secrets: list[str] = []
    if bearer_env and cookie_env:
        raise FetchError("choose only one of bearer-token-env and cookie-env")
    name = bearer_env or cookie_env
    if name:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise FetchError("authentication environment variable name is invalid")
        value = os.environ.get(name)
        if not value or any(ord(char) < 32 or ord(char) >= 127 for char in value):
            raise FetchError("authentication environment variable is absent, empty, or not a safe ASCII header")
        secrets.append(value)
        if bearer_env:
            if any(char.isspace() for char in value):
                raise FetchError("bearer environment variable must contain a nonempty token without whitespace")
            headers["Authorization"] = "Bearer " + value
        else:
            cookies = SimpleCookie()
            try:
                cookies.load(value)
            except (ValueError, CookieError):
                raise FetchError("cookie environment variable must contain a valid existing Cookie header") from None
            if not cookies or any(not item.value for item in cookies.values()):
                raise FetchError("cookie environment variable must contain nonempty existing cookie values")
            secrets.extend(item.value for item in cookies.values())
            headers["Cookie"] = value
    return headers, secrets


def fetch_bytes(endpoint: str, headers: dict[str, str], timeout: float) -> bytes:
    opener = build_opener(NoRedirect())
    request = Request(endpoint, headers=headers, method="GET")
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            code = response.status
            if code != 200:
                raise FetchError(http_error_message(code))
            content_type = response.headers.get_content_type()
            if content_type != "application/json" and not content_type.endswith("+json"):
                raise FetchError("response is not JSON (check the service root; HTML/login pages are not accepted)")
            if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                raise FetchError("unsupported response content encoding; require uncompressed JSON")
            size = response.headers.get("Content-Length")
            if size is not None:
                try:
                    if int(size) < 0 or int(size) > MAX_BODY_BYTES:
                        raise FetchError("response exceeds the 8 MiB limit or has an invalid length")
                except ValueError:
                    raise FetchError("response has an invalid Content-Length") from None
            chunks: list[bytes] = []
            total = 0
            reader = getattr(response, "read1", response.read)
            while True:
                if time.monotonic() - started > timeout:
                    raise FetchError("network timeout while reading response")
                chunk = reader(min(65536, MAX_BODY_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_BODY_BYTES:
                    raise FetchError("response exceeds the 8 MiB limit")
            body = b"".join(chunks)
            if size is not None and len(body) != int(size):
                raise FetchError("response body is incomplete (Content-Length mismatch)")
            return body
    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise FetchError(http_error_message(code)) from None
    except (TimeoutError, socket.timeout):
        raise FetchError("network timeout; check address or increase --timeout") from None
    except (URLError, OSError, ValueError, HTTPException):
        raise FetchError("network/TLS request failed; check service address, TLS trust, and proxy environment") from None


def http_error_message(code: int) -> str:
    if 300 <= code < 400:
        return "HTTP redirect refused; supply the final correct service root (credentials were not forwarded)"
    messages = {
        401: "authentication required; supply an existing session cookie or configured proxy bearer via an explicit environment variable",
        403: "access forbidden; check existing credentials and proxy policy",
        404: "task or test-point checkpoint not found; verify ids/project or wait for generation (this CLI does not generate)",
        422: "request rejected; this API may require the task's explicit --project-id",
    }
    detail = messages.get(code, "service error" if code >= 500 else "request failed")
    return f"HTTP {code}: {detail}"


def decode_json(body: bytes, secrets: list[str]) -> dict[str, Any]:
    # Refuse echoed credentials rather than silently editing business text/raw bytes.
    if any(secret.encode("ascii") in body for secret in secrets):
        raise FetchError("response contains supplied authentication material; no files saved")

    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise FetchError("response JSON contains duplicate object keys")
            obj[key] = value
        return obj

    def invalid_constant(value):
        raise FetchError("response contains a nonstandard JSON number")

    try:
        payload = json.loads(body.decode("utf-8-sig"), object_pairs_hook=unique_object,
                             parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise FetchError("malformed JSON response; no files saved") from None
    if not isinstance(payload, dict):
        raise FetchError("response schema invalid: expected a checkpoint object")

    def check_sensitive(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in SENSITIVE_KEYS:
                    raise FetchError("response contains an unexpected sensitive key; no files saved (no redaction performed)")
                check_sensitive(item)
        elif isinstance(value, list):
            for item in value:
                check_sensitive(item)
        elif isinstance(value, str) and any(secret in value for secret in secrets):
            raise FetchError("response contains supplied authentication material; no files saved")

    check_sensitive(payload)
    return payload


def require_int(obj: dict, name: str, minimum: int = 1) -> int:
    value = obj.get(name)
    if type(value) is not int or value < minimum:
        raise FetchError(f"response schema invalid: {name} must be an integer >= {minimum}")
    return value


def require_string(obj: dict, name: str, nonempty: bool = False) -> str:
    value = obj.get(name)
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise FetchError(f"response schema invalid: {name} must be {'nonempty ' if nonempty else ''}text")
    return value


def map_scenarios(payload: dict, task_id: int, category: str | None) -> dict:
    checkpoint_id = require_int(payload, "id")
    if require_int(payload, "task_id") != task_id:
        raise FetchError("response task_id does not match requested task")
    require_int(payload, "attempt")
    require_int(payload, "version")
    require_string(payload, "status", True)
    for field in ("created_at", "updated_at"):
        require_string(payload, field, True)
    if payload.get("retrieval_checkpoint_id") is not None:
        require_int(payload, "retrieval_checkpoint_id")
    if payload.get("idempotency_key") is not None:
        require_string(payload, "idempotency_key")
    if "task_status" in payload and payload["task_status"] is not None:
        require_string(payload, "task_status", True)
    points = payload.get("points")
    if not isinstance(points, list):
        raise FetchError("response schema invalid: points must be an array")
    if not points:
        raise FetchError("no test-point artifacts: generation may be pending, or zero scenes need operator confirmation; nothing exported")
    seen: set[int] = set()
    scenarios = []
    for point in points:
        if not isinstance(point, dict):
            raise FetchError("response schema invalid: every point must be an object")
        point_id = require_int(point, "id")
        if point_id in seen:
            raise FetchError("response schema invalid: duplicate point id")
        seen.add(point_id)
        if require_int(point, "task_id") != task_id or require_int(point, "checkpoint_id") != checkpoint_id:
            raise FetchError("response point task/checkpoint association does not match")
        title = require_string(point, "title", True)
        goal = require_string(point, "verification_goal")
        for field in ("stable_key", "dimension", "priority", "created_at", "updated_at"):
            require_string(point, field, True)
        require_int(point, "sort_order", 0)
        for field in ("is_selected", "is_excluded"):
            if type(point.get(field)) is not bool:
                raise FetchError(f"response schema invalid: {field} must be boolean")
        citation_ids = point.get("citation_ids", [])
        if not isinstance(citation_ids, list) or any(type(item) is not int or item < 1 for item in citation_ids):
            raise FetchError("response schema invalid: citation_ids must be positive integers")
        # Optional forward-compatible business strings are never coerced or guessed.
        for field in ("module", "category", "description", "test_focus"):
            if field in point and point[field] is not None:
                require_string(point, field)
        scenarios.append({
            "id": f"TP-{task_id}-{point_id}",
            "source_task_id": task_id,
            "source_point_id": point_id,
            "source_checkpoint_id": checkpoint_id,
            "title": title,
            "description": goal,
            "test_focus": goal,
            "text": title + "\n" + goal,
            "text_source": "points[].title + newline + points[].verification_goal (verbatim; no LLM rewriting)",
            "category": category,
            "category_source": "user_cli_override" if category else None,
            "source_module": point.get("module"),
            "source_category": point.get("category"),
            "dimensions": [point["dimension"]],
            "sub_scenarios": [],
            "source_metadata": point,
        })
    return {"schema_version": "0.1", "source_task_id": task_id, "scenarios": scenarios}


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def check_output_dir(path: Path) -> None:
    if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
        raise FetchError("output directory must be new or empty; overwriting is refused")


def save_files(output_dir: Path, files: dict[str, bytes]) -> None:
    # Validation and all serialization precede any directory/file creation.
    check_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    check_output_dir(output_dir)
    resolved = output_dir.resolve()
    created: list[Path] = []
    try:
        for name, content in files.items():
            target = resolved / name
            with target.open("xb") as handle:
                created.append(target)
                handle.write(content)
    except OSError:
        for target in created:
            # Only unlink exact files this call created in the verified output directory.
            if target.parent == resolved and target.name in FILE_NAMES:
                target.unlink(missing_ok=True)
        raise


def run(args: argparse.Namespace) -> int:
    task_id = positive_integer(args.task_id)
    project_id = positive_integer(args.project_id) if args.project_id is not None else None
    timeout = positive_timeout(args.timeout)
    endpoint = endpoint_url(args.base_url, task_id, project_id)
    headers, secrets = auth_headers(args.bearer_token_env, args.cookie_env)
    if any(secret in endpoint for secret in secrets):
        raise FetchError("base URL contains authentication material; use a credential-free service root")
    output_dir = Path(args.output_dir)
    check_output_dir(output_dir)
    body = fetch_bytes(endpoint, headers, timeout)
    payload = decode_json(body, secrets)
    scenes = map_scenarios(payload, task_id, args.category)
    scenes_bytes = json_bytes(scenes)
    files = {"api_response.json": body, "scenarios.json": scenes_bytes}
    manifest = {
        "schema_version": "0.1",
        "fetched_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "endpoint": endpoint,
        "http_method": "GET",
        "task_id": task_id,
        "project_id": project_id,
        "attempt": payload["attempt"],
        "checkpoint_id": payload["id"],
        "checkpoint_status": payload["status"],
        "checkpoint_version": payload["version"],
        "retrieval_checkpoint_id": payload.get("retrieval_checkpoint_id"),
        "service_task_status": payload.get("task_status"),
        "service_task_status_source": "response.task_status" if "task_status" in payload else "not_returned_by_endpoint",
        "provenance": "current_service_snapshot",
        "human_edit_status": "unknown",
        "first_generation_unmodified": "unknown",
        "category": args.category,
        "category_source": "user_cli_override" if args.category else None,
        "counts": {
            "points": len(payload["points"]), "scenarios": len(scenes["scenarios"]),
            "selected": sum(point["is_selected"] for point in payload["points"]),
            "excluded": sum(point["is_excluded"] for point in payload["points"]),
        },
        "selection_policy": "all returned points retained, including unselected/excluded; no pagination",
        "raw_response": {
            "byte_preserved": True, "key_order_rearranged": False,
            "redaction": "none; sensitive-key or echoed-auth responses are refused",
            "received_byte_count": len(body), "received_sha256": hashlib.sha256(body).hexdigest(),
        },
        "files": {name: {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
                  for name, content in files.items()},
        "manifest_hash_note": "manifest.json is not self-hashed; hashes cover the other two exact saved files",
        "field_mapping": {
            "id": "TP-{task_id}-{points[].id}",
            "title": "points[].title verbatim",
            "description": "points[].verification_goal verbatim",
            "test_focus": "points[].verification_goal verbatim",
            "text": "points[].title + '\\n' + points[].verification_goal, verbatim fields; no LLM rewriting",
            "dimensions": "[points[].dimension] without taxonomy conversion",
            "sub_scenarios": "[]; not supplied by this API",
            "source_module/source_category": "optional original point.module/category; null when absent",
            "source_metadata": "complete original point object; unmapped fields retained",
            "category": "explicit --category only; otherwise null (no automatic service-dimension/category mapping)",
        },
    }
    files["manifest.json"] = json_bytes(manifest)
    # A final scan prevents credentials from reaching any artifact, including endpoint.
    if any(secret.encode("ascii") in content for secret in secrets for content in files.values()):
        raise FetchError("artifact contains authentication material; no files saved")
    save_files(output_dir, files)
    print(f"Saved {len(scenes['scenarios'])} scenarios (current service snapshot; edit history unknown).")
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = SafeParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Explicit http(s) service root, optionally with proxy prefix; exclude /api")
    parser.add_argument("--task-id", required=True, help="Existing task's positive integer id; does not create or generate")
    parser.add_argument("--output-dir", required=True, help="New or empty output directory; never overwrite nonempty directories")
    parser.add_argument("--category", choices=CATEGORIES, help="Optional user classification, not inferred from service fields")
    parser.add_argument("--timeout", default="30", help="Positive seconds for network timeout/read budget (default: 30)")
    parser.add_argument("--project-id", help="Explicit positive project id, required when service auth/project scoping is enabled")
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--bearer-token-env", help="Named environment variable with existing proxy bearer token; never logged")
    auth.add_argument("--cookie-env", help="Named environment variable with existing session Cookie header; never logged")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        return run(args)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, OverflowError, RecursionError, ValueError):
        print("error: file/network processing failed; no successful export", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
