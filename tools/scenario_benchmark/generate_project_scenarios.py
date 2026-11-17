#!/usr/bin/env python3
"""Fresh scenario-only CaseGen sampling. No gold/control inputs or case confirmation."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import getpass
import hashlib
from http.cookiejar import CookieJar
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_scenarios import NoRedirect, FetchError, decode_json, map_scenarios, json_bytes

VERSION = "project-scenarios-2"
SCOPES = ("集合竞价撮合成交", "单笔成交金额", "累计成交金额", "开盘价", "收盘价", "撤单订单类型验证")
MAX_BYTES = 8 * 1024 * 1024

class GenerationError(Exception):
    """Credential-safe error."""

class ApiError(GenerationError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"API HTTP {status}; inspect recorded endpoint/status")

def sha(data):
    return hashlib.sha256(data).hexdigest()

def safe_json(raw, secrets=()):
    # Strict duplicate-key/nonfinite/sensitive-key scanner also handles arrays.
    value = decode_json(b'{"business_response":' + raw + b'}', list(secrets))["business_response"]
    def check(item, depth=0):
        if depth > 40:
            raise FetchError("business JSON nesting exceeds safety limit")
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = key.lower().replace("-", "_")
                if normalized in {"password_hash", "token", "session", "session_id", "csrf_token", "apikey", "api_secret", "secret"}:
                    raise FetchError("unexpected sensitive field; raw bytes refused")
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)
        elif isinstance(item, str) and item.strip().startswith(("{", "[")):
            # Events can carry nested business JSON in detail_json strings.
            try:
                json.loads(item)
            except ValueError:
                return
            nested = decode_json(b'{"embedded":' + item.encode() + b'}', list(secrets))["embedded"]
            check(nested, depth + 1)
    check(value)
    return value

def exclusive(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())

def save_json(path, value):
    data = json_bytes(value)
    safe_json(data)
    exclusive(path, data)

@contextmanager
def output_lock(output, resume):
    if output.is_symlink() or any(parent.is_symlink() for parent in output.parents):
        raise GenerationError("symlink output or ancestor refused")
    if not resume and output.parent.exists() and any(output.parent.iterdir()):
        raise GenerationError("run root nonempty; choose a fresh run")
    if resume and not (output / "fingerprint.json").is_file():
        raise GenerationError("resume requires an existing bound fingerprint")
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".pid-lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GenerationError("another sampler owns this output") from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield

def metadata(db, project, space):
    c = sqlite3.connect(f"file:{Path(db).resolve()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    def rows(query, args=()):
        return [dict(r) for r in c.execute(query, args)]
    c.execute("BEGIN")
    result = {
        "project": rows("SELECT id,name,status,default_wiki_space_id FROM projects WHERE id=?", (project,)),
        "space": rows("SELECT id,name,status,scope,project_id FROM wiki_spaces WHERE id=?", (space,)),
        "documents": rows("SELECT id,filename,status,char_count,sha256 FROM documents WHERE space_id=? ORDER BY id", (space,)),
        "pages": rows("SELECT id,title,status,source_document_id,content_hash,revision FROM wiki_pages WHERE space_id=? ORDER BY id", (space,)),
        "chunks": rows("SELECT id,document_id,text FROM source_chunks WHERE space_id=? ORDER BY id", (space,)),
        "model": rows("SELECT id,name,model_name,base_url,is_default,protocol FROM models WHERE is_default=1"),
        "prompts": rows("SELECT id,name,type,version,is_active,content FROM prompt_templates WHERE is_active=1 AND type IN ('generate','test_points') ORDER BY id"),
        "external_bindings": rows("SELECT id,project_id,name,base_url,enabled FROM project_external_wikis WHERE project_id=? ORDER BY id", (project,)),
    }
    c.close()
    for item in result["chunks"]:
        text = item.pop("text")
        item.update(text_length=len(text), text_sha256=sha(text.encode()))
    for item in result["prompts"]:
        item["content_sha256"] = sha(item.pop("content").encode())
    validate_metadata(result, project, space)
    return result

def validate_metadata(m, project, space):
    if (len(m["project"]) != 1 or m["project"][0]["id"] != project
            or m["project"][0]["status"] != "active"
            or len(m["space"]) != 1 or m["space"][0]["id"] != space
            or m["space"][0]["project_id"] != project
            or m["space"][0]["scope"] != "project" or m["space"][0]["status"] != "active"):
        raise GenerationError("project/space metadata mismatch")
    docs = m["documents"]
    if not docs or any(d["status"] != "ready" or d["char_count"] <= 0 for d in docs):
        raise GenerationError("all scoped documents must be ready/nonempty")
    ids = {d["id"] for d in docs}
    if (not m["pages"] or not m["chunks"]
            or any(p["source_document_id"] not in ids for p in m["pages"])
            or any(ch["document_id"] not in ids for ch in m["chunks"])):
        raise GenerationError("source isolation/readiness failed")
    if any(b["enabled"] for b in m["external_bindings"]):
        raise GenerationError("external bindings enabled; do not mix local sampling sources")
    if len(m["model"]) != 1:
        raise GenerationError("exactly one current default model required")
    for kind in ("generate", "test_points"):
        if len([p for p in m["prompts"] if p["type"] == kind]) != 1:
            raise GenerationError("ambiguous active prompt")

def task_payload(scope, m, project, space):
    return {
        "title": f"纯场景采样：{scope}",
        "description": (
            f"业务范围：{scope}。请仅基于本项目已导入的交易规则资料，生成该范围内的纯测试场景与测试关注点。"
            "只描述要验证的业务行为，不生成可执行用例、操作步骤或执行断言。资料不足的部分应明确说明，不自行补充规则。"
            "\n\n"
            "# 测试点拆分框架（通用测试设计脚手架）\n"
            "把业务范围拆成一组测试点时，请按以下十个通用维度逐一检查该范围是否已有对应测试点；"
            "同一规则在不同取值、状态或时点下必须拆成互相独立的测试点，不得合并成一条规则描述。\n"
            "1. 取值形态：同一规则在典型值、极值、缺省值等不同取值形态下分别验证。\n"
            "2. 边界与阈值：指标达到、超过或未达到阈值时的行为差异。\n"
            "3. 对象类型与状态：规则涉及的不同对象类型，以及同一类型的不同状态。\n"
            "4. 时点与时段：交易前、交易中、交易后，以及不同时段/时间窗口下的行为差异。\n"
            "5. 竞争与优先次序：多条规则同时适用时的优先次序与竞争结果。\n"
            "6. 空态与无结果：无数据、无申报、无成交、无返回时的表现。\n"
            "7. 无效与异常输入：非法、越界或异常输入下的拒绝与提示行为。\n"
            "8. 下游与关联影响：规则触发后对下游数据与关联对象产生的影响。\n"
            "9. 兜底与替代路径：主路径不可用时的替代路径或兜底口径。\n"
            "10. 生效条件：规则生效与不生效的条件差异。\n"
            "\n"
            "测试点标题与验证目标必须说明「在哪个输入/状态下验证什么可观察行为」，不得把规则结论原样复述；"
            "同一规则的不同取值、状态或时点必须写成独立测试点，不得合并为一条规则描述。"
        ),
        "model_id": m["model"][0]["id"],
        "prompt_template_id": next(p["id"] for p in m["prompts"] if p["type"] == "generate"),
        "project_id": project, "wiki_space_id": space,
        "generation_granularity": "detailed", "test_dimensions": ["positive", "negative", "boundary"],
        "auto_review": False, "run_generate": False, "reference_case_ids": [], "reference_text": "",
    }

def select_citations(checkpoint, m):
    if checkpoint.get("conflict_groups"):
        raise GenerationError("unresolved source conflict; manual decision required")
    page_ids = {p["id"] for p in m["pages"] if p["status"] == "published"}
    chunk_ids = {ch["id"] for ch in m["chunks"]}
    selected = []
    for item in checkpoint["candidate_citations"]:
        if not item.get("available", True) or item.get("legacy", False):
            continue
        kind = item.get("citation_type")
        if ((kind == "wiki" and item.get("wiki_page_id") in page_ids)
                or (kind == "source" and item.get("source_chunk_id") in chunk_ids)):
            selected.append(item["id"])
    if not selected:
        raise GenerationError("no eligible project source citations; stopped")
    return selected

class Client:
    def __init__(self, root, origin, output, project, space, timeout=30):
        if root != "http://127.0.0.1:8001":
            raise GenerationError("only verified local CaseGen root8001 is allowed")
        self.root, self.origin, self.output = root, origin, output
        self.project, self.space, self.timeout = project, space, timeout
        self.jar = CookieJar()
        self.opener = build_opener(NoRedirect(), HTTPCookieProcessor(self.jar))
        self.secrets = []
        self.sequence = len(list((output / "calls").glob("*.started")))
        self.logged_in = False

    def request(self, method, path, data=None, archive=None, auth=False):
        query = urlencode({"project_id": self.project, "wiki_space_id": self.space, "space_id": self.space})
        endpoint = path + ("&" if "?" in path else "?") + query
        self.sequence += 1
        call_path = self.output / "calls" / f"{self.sequence:05d}.json"
        save_json(call_path.with_suffix(".started"), {"method": method, "endpoint": endpoint, "started_at": datetime.now(timezone.utc).isoformat()})
        request = Request(self.root + endpoint, data=json_bytes(data) if data is not None else None,
                          headers={"Origin": self.origin, "Content-Type": "application/json", "Accept": "application/json", "Accept-Encoding": "identity"}, method=method)
        started = time.monotonic()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                status = response.status
                chunks, total = [], 0
                reader = getattr(response, "read1", response.read)
                while True:
                    if time.monotonic() - started > self.timeout:
                        raise GenerationError("total API response read deadline exceeded")
                    chunk = reader(min(65536, MAX_BYTES + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise GenerationError("API response size limit exceeded")
                raw = b"".join(chunks)
                if status != 204 and response.headers.get_content_type() != "application/json":
                    raise GenerationError("expected JSON service API, not frontend HTML")
                if response.headers.get("Content-Encoding", "identity") not in ("identity", ""):
                    raise GenerationError("encoded response refused")
        except HTTPError as exc:
            status = exc.code
            exc.close()
            save_json(call_path, {"method": method, "endpoint": endpoint, "status": status})
            raise ApiError(status) from None
        except (URLError, OSError, TimeoutError):
            save_json(call_path, {"method": method, "endpoint": endpoint, "status": "network_error"})
            raise GenerationError("network request failed; uncertain writes will not be retried") from None
        self.secrets.extend(c.value for c in self.jar if c.value not in self.secrets)
        payload = safe_json(raw, self.secrets) if raw else None
        save_json(call_path, {"method": method, "endpoint": endpoint, "status": status,
                              "business_body_sha256": sha(raw) if not auth else None})
        if archive is not None and not auth:
            exclusive(archive, raw)
        return payload

    def login(self, username, password):
        self.secrets.append(password)
        try:
            self.request("POST", "/api/auth/login", {"username": username, "password": password}, auth=True)
            self.logged_in = True
        except (GenerationError, FetchError):
            # A successful HTTP login may issue a session before body validation fails.
            self.logged_in = any(self.jar)
            raise

    def logout(self):
        if self.logged_in:
            self.request("POST", "/api/auth/logout", auth=True)
            self.logged_in = False
        self.jar.clear()

def freeze(client, directory, task_id, scope):
    raw_path = directory / "proposal.raw.json"
    if raw_path.exists():
        proposal = safe_json(raw_path.read_bytes(), client.secrets)
    else:
        proposal = client.request("GET", f"/api/tasks/{task_id}/test-points", archive=raw_path)
    if proposal["status"] != "pending" or proposal["task_id"] != task_id:
        raise GenerationError("first observed proposal is not the expected pending checkpoint")
    mapped = map_scenarios(proposal, task_id, None)
    for item in mapped["scenarios"]:
        item["task_scope"] = scope
    if not (directory / "scenarios.json").exists():
        save_json(directory / "scenarios.json", mapped)
    for name in ("events", "citations"):
        path = directory / f"{name}.raw.json"
        if not path.exists():
            client.request("GET", f"/api/tasks/{task_id}/{name}", archive=path)
    events = safe_json((directory / "events.raw.json").read_bytes(), client.secrets)
    evidence = [e for e in events if any(word in json.dumps(e, ensure_ascii=False).lower() for word in ("fallback", "修复", "解析", "usage"))]
    manifest = {
        "source": "first_observed_service_proposal", "not_raw_llm": True,
        "fresh_task_created_by_this_run": True, "human_edit_status": "no_edit_observed_before_snapshot",
        "concurrent_edit_cannot_be_absolutely_excluded": True,
        "task_id": task_id, "task_scope": scope, "checkpoint_id": proposal["id"],
        "checkpoint_status": proposal["status"], "checkpoint_version": proposal["version"],
        "scenario_count": len(mapped["scenarios"]), "proposal_sha256": sha(raw_path.read_bytes()),
        "actual_response_model": "unknown", "usage": None, "cost": None,
        "usage_note": "remote usage/cost not exposed; unknown is not zero",
        "parsing_repair_fallback_event_evidence": evidence, "service_can_parse_repair_or_fallback": True,
    }
    if not (directory / "manifest.json").exists():
        save_json(directory / "manifest.json", manifest)
    return mapped["scenarios"]

def sample_scope(client, directory, scope, payload, m, deadline_seconds, poll_seconds):
    directory.mkdir(exist_ok=True)
    if (directory / "manifest.json").exists():
        return safe_json((directory / "scenarios.json").read_bytes())["scenarios"]
    created = directory / "created.raw.json"
    if not created.exists():
        if (directory / "create.started.json").exists():
            raise GenerationError("ambiguous create interruption; reconcile task ID manually, never recreate")
        save_json(directory / "create.started.json", payload)
        task = client.request("POST", "/api/tasks", payload, archive=created)
    else:
        task = safe_json(created.read_bytes(), client.secrets)
    task_id = task["id"]
    if task["project_id"] != client.project or task["wiki_space_id"] != client.space:
        raise GenerationError("created task project/space mismatch")
    generated = directory / "generate.started.json"
    if not generated.exists():
        save_json(generated, {"task_id": task_id})
        client.request("POST", f"/api/tasks/{task_id}/generate", archive=directory / "generate.raw.json")
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        task = client.request("GET", f"/api/tasks/{task_id}")
        status = task["status"]
        if status == "awaiting_test_point_confirmation":
            if not (directory / "observed_task.json").exists():
                save_json(directory / "observed_task.json", task)
            return freeze(client, directory, task_id, scope)
        if status == "awaiting_confirmation":
            retrieval_path = directory / "retrieval.raw.json"
            checkpoint = client.request("GET", f"/api/tasks/{task_id}/retrieval-checkpoint",
                                        archive=retrieval_path if not retrieval_path.exists() else None)
            if checkpoint["task_id"] != task_id or checkpoint["status"] != "pending":
                raise GenerationError("retrieval checkpoint mismatch or no longer pending")
            selected = select_citations(checkpoint, m)
            confirm = {"selected_citation_ids": selected, "supplemental_text": "", "expected_version": checkpoint["version"],
                       "idempotency_key": f"{client.output.parent.name}-{task_id}-{checkpoint['id']}-{checkpoint['version']}"}
            marker = directory / "retrieval-confirm.started.json"
            if marker.exists():
                raise GenerationError("retrieval confirmation already attempted; no replay")
            save_json(marker, confirm)
            client.request("POST", f"/api/tasks/{task_id}/retrieval-checkpoint/confirm", confirm,
                           archive=directory / "retrieval-confirm.raw.json")
        elif status in ("failed", "stopped", "generated", "finalized"):
            save_json(directory / "terminal_task.json", task)
            raise GenerationError(f"task {task_id} terminal status {status}; no regeneration")
        elif status == "draft":
            raise GenerationError("generate request uncertain/unstarted; never automatically resend")
        time.sleep(poll_seconds)
    raise GenerationError(f"task {task_id} polling deadline exceeded; resume observes existing ID")

def run(args):
    if not args.execute:
        print(json.dumps({"execute": False, "project_id": args.project_id, "space_id": args.space_id,
                          "scopes": SCOPES, "http_requests": 0}, ensure_ascii=False))
        return 0
    m = metadata(args.db, args.project_id, args.space_id)
    payloads = [task_payload(s, m, args.project_id, args.space_id) for s in SCOPES]
    bound = {"version": VERSION, "script_sha256": sha(Path(__file__).read_bytes()), "map_scenarios_source_sha256": sha(Path(__file__).with_name("fetch_scenarios.py").read_bytes()),
             "metadata": m, "payloads": payloads, "root": args.base_url}
    fingerprint = sha(json_bytes(bound))
    output = Path(args.output_dir).absolute()
    with output_lock(output, args.resume):
        fingerprint_path = output / "fingerprint.json"
        if args.resume:
            if safe_json(fingerprint_path.read_bytes())["sha256"] != fingerprint:
                raise GenerationError("resume fingerprint mismatch; source/model/prompt/parameters/code changed")
        else:
            save_json(fingerprint_path, {"sha256": fingerprint, "binding": bound})
        client = Client(args.base_url, args.origin, output, args.project_id, args.space_id, args.timeout)
        all_scenarios, outcomes = [], []
        try:
            if client.request("GET", "/api/health") != {"status": "ok"}:
                raise GenerationError("unrecognized health identity")
            password = os.environ.get(args.password_env) if args.password_env else (sys.stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass("Admin password: "))
            if not password:
                raise GenerationError("password absent or canceled")
            client.login(args.username, password)
            for path in (f"/api/projects/{args.project_id}", f"/api/wiki-spaces/{args.space_id}"):
                response = client.request("GET", path)
                if response["id"] != (args.project_id if "/projects/" in path else args.space_id):
                    raise GenerationError("API metadata identity mismatch")
            for doc in m["documents"]:
                response = client.request("GET", f"/api/documents/{doc['id']}")
                if response["status"] != "ready" or response["space_id"] != args.space_id:
                    raise GenerationError("API document readiness/source mismatch")
                preview_path = output / f"document-{doc['id']}-preview.raw.json"
                preview = client.request("GET", f"/api/documents/{doc['id']}/preview?max_chars=200000", archive=preview_path if not preview_path.exists() else None)
                if preview["truncated"] or preview["char_count"] != doc["char_count"] or not preview["quality_ok"]:
                    raise GenerationError("document parsed preview readiness failed")
                record_path = output / f"document-{doc['id']}-parsed-hash.json"
                if not record_path.exists():
                    save_json(record_path, {"document_id": doc["id"], "text_sha256": sha(preview["text"].encode()), "text_length": len(preview["text"])})
            for index, (scope, payload) in enumerate(zip(SCOPES, payloads)):
                if metadata(args.db, args.project_id, args.space_id) != m:
                    raise GenerationError("public source/model/prompt configuration changed during run")
                directory = output / f"scope-{index + 1:02d}"
                try:
                    items = sample_scope(client, directory, scope, payload, m, args.deadline, args.poll_interval)
                    all_scenarios.extend(items)
                    outcomes.append({"scope": scope, "status": "snapshot_saved", "task_id": items[0]["source_task_id"], "scenario_count": len(items)})
                    print(json.dumps(outcomes[-1], ensure_ascii=False), flush=True)
                except (GenerationError, FetchError) as exc:
                    failure = {"scope": scope, "status": "technical_failure", "safe_error": str(exc)}
                    if not (directory / "failure.json").exists():
                        save_json(directory / "failure.json", failure)
                    raise
            if not (output / "scenarios.json").exists():
                save_json(output / "scenarios.json", {"schema_version": "0.1", "scenarios": all_scenarios})
            if not (output / "source-manifest.json").exists():
                save_json(output / "source-manifest.json", {"fingerprint": fingerprint, "source_metadata": m, "outcomes": outcomes,
                          "scenario_count": len(all_scenarios), "workers": 1, "first_task_smoke": True,
                          "gold_or_controls_loaded": False, "actual_response_model": "unknown", "usage": None, "cost": None,
                          "provenance": "first_observed_service_proposal", "not_raw_llm": True,
                          "configured_model": m["model"][0], "active_test_points_prompt": next(p for p in m["prompts"] if p["type"] == "test_points"),
                          "credentials_read": "runtime memory only; normal administrator login; no credentials archived"})
            print(json.dumps({"output": str(output), "tasks": outcomes, "N": len(all_scenarios)}, ensure_ascii=False))
            return 0
        finally:
            try:
                client.logout()
            except (GenerationError, FetchError):
                print("warning: own-session logout failed; cookie jar discarded", file=sys.stderr)
            client.jar.clear()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--origin", default="http://127.0.0.1:5173")
    parser.add_argument("--project-id", type=int, default=3)
    parser.add_argument("--space-id", type=int, default=2)
    parser.add_argument("--db", default="data/meta/app.db")
    parser.add_argument("--output-dir", default="data/evaluation/scenario-benchmark-v0.1/runs/project3-space2-001/generation")
    parser.add_argument("--username", default="admin")
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--password-env")
    auth.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--deadline", type=float, default=1200)
    parser.add_argument("--poll-interval", type=float, default=3)
    args = parser.parse_args()
    if (not all(math.isfinite(v) for v in (args.timeout, args.deadline, args.poll_interval))
            or args.timeout <= 0 or args.deadline <= 0 or args.poll_interval < 1):
        parser.error("invalid timing bounds")
    try:
        return run(args)
    except (GenerationError, FetchError, KeyboardInterrupt):
        print("generation stopped safely; inspect per-scope failure/call status; uncertain writes are never resent", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
