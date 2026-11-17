"""Pure unittest; local temporary HTTP mocks only, no backend/DB/model imports."""
from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

SCRIPT = Path(__file__).resolve().parents[1] / "fetch_scenarios.py"
SPEC = importlib.util.spec_from_file_location("fetch_scenarios", SCRIPT)
FETCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FETCH)


def fixture() -> dict:
    return {
        "id": 17, "task_id": 123, "retrieval_checkpoint_id": 9,
        "attempt": 3, "version": 4, "status": "pending",
        "idempotency_key": None,
        "created_at": "2026-01-02T03:04:05", "updated_at": "2026-01-03T04:05:06",
        "points": [
            {
                "id": 201, "task_id": 123, "checkpoint_id": 17,
                "stable_key": "TP-001", "title": "  场景①：按钮\n原样  ",
                "verification_goal": " 验证中文/emoji 🚀，不改写\n第二行。 ",
                "dimension": "boundary", "priority": "P0", "sort_order": 0,
                "is_selected": True, "is_excluded": False, "citation_ids": [2, 6],
                "created_at": "2026-01-02T03:04:05", "updated_at": "2026-01-03T04:05:06",
            },
            {
                "id": 202, "task_id": 123, "checkpoint_id": 17,
                "stable_key": "TP-002", "title": "未选中也保存",
                "verification_goal": "保留当前返回的点", "dimension": "positive",
                "priority": "P2", "sort_order": 1, "is_selected": False,
                "is_excluded": True, "citation_ids": [],
                "created_at": "2026-01-02T03:04:05", "updated_at": "2026-01-03T04:05:06",
            },
        ],
    }


@contextmanager
def mock_server(body: bytes, status: int = 200, content_type: str = "application/json", extra_headers=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers)})
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            requests.append({"method": self.command, "path": self.path})
            self.send_error(405)

        do_PUT = do_POST
        do_DELETE = do_POST

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class FetchScenariosTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scenario-cli-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "export"
        self.env = patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def invoke(self, base_url, *extra, output=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["--base-url", base_url, "--task-id", "123", "--output-dir", str(output or self.out), *extra]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = FETCH.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def response_call(self, payload, *extra, **server_options):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
        with mock_server(body, **server_options) as (base_url, requests):
            result = self.invoke(base_url, *extra)
        return result, requests, body

    def read_outputs(self):
        return {name: json.loads((self.out / name).read_bytes()) for name in FETCH.FILE_NAMES}

    def assert_schema_failure(self, payload, expected):
        result, requests, _ = self.response_call(payload)
        self.assertEqual(result[0], 1)
        self.assertIn(expected, result[2])
        self.assertEqual(len(requests), 1)
        self.assertFalse(self.out.exists())

    def test_success_verbatim_unicode_mapping_and_hashes(self):
        payload = fixture()
        (code, stdout, stderr), requests, body = self.response_call(payload)
        self.assertEqual(code, 0, stderr)
        self.assertIn("Saved 2 scenarios", stdout)
        self.assertEqual(stderr, "")
        self.assertEqual([(r["method"], r["path"]) for r in requests], [("GET", "/api/tasks/123/test-points")])
        self.assertEqual((self.out / "api_response.json").read_bytes(), body)
        outputs = self.read_outputs()
        self.assertEqual(set(p.name for p in self.out.iterdir()), set(FETCH.FILE_NAMES))
        scenes = outputs["scenarios.json"]["scenarios"]
        for point, scene in zip(payload["points"], scenes):
            self.assertEqual(scene["id"], f"TP-123-{point['id']}")
            self.assertEqual(scene["title"], point["title"])
            self.assertEqual(scene["description"], point["verification_goal"])
            self.assertEqual(scene["test_focus"], point["verification_goal"])
            self.assertEqual(scene["text"], point["title"] + "\n" + point["verification_goal"])
            self.assertEqual(scene["source_metadata"], point)
            self.assertEqual(scene["source_task_id"], 123)
            self.assertEqual(scene["source_checkpoint_id"], 17)
            self.assertEqual(scene["source_point_id"], point["id"])
            self.assertIsNone(scene["category"])
            self.assertIsNone(scene["category_source"])
            self.assertIsNone(scene["source_module"])
            self.assertIsNone(scene["source_category"])
            self.assertEqual(scene["dimensions"], [point["dimension"]])
            self.assertEqual(scene["sub_scenarios"], [])
            for forbidden in ("steps", "expected_result", "preconditions", "cases"):
                self.assertNotIn(forbidden, scene)
        manifest = outputs["manifest.json"]
        self.assertEqual(manifest["attempt"], 3)
        self.assertEqual(manifest["checkpoint_id"], 17)
        self.assertEqual(manifest["checkpoint_version"], 4)
        self.assertEqual(manifest["checkpoint_status"], "pending")
        self.assertEqual(manifest["retrieval_checkpoint_id"], 9)
        self.assertEqual(manifest["counts"], {"points": 2, "scenarios": 2, "selected": 1, "excluded": 1})
        self.assertEqual(manifest["provenance"], "current_service_snapshot")
        self.assertEqual(manifest["first_generation_unmodified"], "unknown")
        self.assertEqual(manifest["human_edit_status"], "unknown")
        self.assertIsNone(manifest["service_task_status"])
        self.assertEqual(manifest["service_task_status_source"], "not_returned_by_endpoint")
        self.assertTrue(manifest["fetched_at_utc"].endswith("Z"))
        self.assertTrue(manifest["raw_response"]["byte_preserved"])
        self.assertFalse(manifest["raw_response"]["key_order_rearranged"])
        self.assertEqual(manifest["raw_response"]["received_sha256"], hashlib.sha256(body).hexdigest())
        for name, record in manifest["files"].items():
            saved = (self.out / name).read_bytes()
            self.assertEqual(record["sha256"], hashlib.sha256(saved).hexdigest())
            self.assertEqual(record["bytes"], len(saved))
        self.assertEqual(set(manifest["files"]), {"api_response.json", "scenarios.json"})

    def test_confirmed_version_one_does_not_claim_unmodified(self):
        payload = fixture()
        payload.update(status="confirmed", version=1, attempt=8, idempotency_key="confirmation-request-id")
        result, _, _ = self.response_call(payload)
        self.assertEqual(result[0], 0, result[2])
        manifest = self.read_outputs()["manifest.json"]
        self.assertEqual((manifest["checkpoint_status"], manifest["checkpoint_version"], manifest["attempt"]), ("confirmed", 1, 8))
        self.assertEqual(manifest["human_edit_status"], "unknown")
        self.assertEqual(manifest["first_generation_unmodified"], "unknown")

    def test_original_category_module_unknown_metadata_and_explicit_category(self):
        payload = fixture()
        payload["points"][0].update(module="模块 原名", category="normal", extension={"not_mapped": [1, "原文"]})
        payload["task_status"] = "awaiting_test_point_confirmation"
        result, _, _ = self.response_call(payload, "--category", "CAN")
        self.assertEqual(result[0], 0, result[2])
        outputs = self.read_outputs()
        scene = outputs["scenarios.json"]["scenarios"][0]
        self.assertEqual(scene["source_category"], "normal")
        self.assertEqual(scene["source_module"], "模块 原名")
        self.assertEqual(scene["source_metadata"]["extension"], {"not_mapped": [1, "原文"]})
        self.assertEqual((scene["category"], scene["category_source"]), ("CAN", "user_cli_override"))
        self.assertEqual(outputs["manifest.json"]["service_task_status"], payload["task_status"])
        self.assertEqual(outputs["manifest.json"]["service_task_status_source"], "response.task_status")

    def test_no_automatic_service_category_mapping(self):
        payload = fixture()
        payload["points"][0]["category"] = "edge"
        result, _, _ = self.response_call(payload)
        self.assertEqual(result[0], 0)
        scene = self.read_outputs()["scenarios.json"]["scenarios"][0]
        self.assertEqual(scene["source_category"], "edge")
        self.assertIsNone(scene["category"])

    def test_prefix_and_project_query(self):
        with mock_server(json.dumps(fixture()).encode()) as (base_url, requests):
            result = self.invoke(base_url + "/proxy/casegen/", "--project-id", "456")
            expected = base_url + "/proxy/casegen/api/tasks/123/test-points?project_id=456"
        self.assertEqual(result[0], 0, result[2])
        self.assertEqual(requests[0]["path"], "/proxy/casegen/api/tasks/123/test-points?project_id=456")
        self.assertEqual(self.read_outputs()["manifest.json"]["endpoint"], expected)

    def test_missing_checkpoint_and_wrapper_rejected(self):
        for payload in ({}, {"checkpoint": fixture(), "points": fixture()["points"]}):
            with self.subTest(payload_type=list(payload)):
                self.assert_schema_failure(payload, "id must")

    def test_empty_points_no_success(self):
        payload = fixture()
        payload["points"] = []
        self.assert_schema_failure(payload, "no test-point artifacts")

    def test_missing_required_fields(self):
        for field in ("id", "task_id", "attempt", "version", "status", "points", "created_at", "updated_at"):
            payload = fixture()
            del payload[field]
            with self.subTest(field=field):
                self.assert_schema_failure(payload, "schema invalid")

    def test_missing_point_fields(self):
        for field in ("id", "task_id", "checkpoint_id", "title", "verification_goal", "dimension", "priority", "stable_key", "sort_order", "is_selected", "is_excluded", "created_at", "updated_at"):
            payload = fixture()
            del payload["points"][0][field]
            with self.subTest(field=field):
                self.assert_schema_failure(payload, "schema invalid")

    def test_duplicate_point_ids(self):
        payload = fixture()
        payload["points"][1]["id"] = 201
        self.assert_schema_failure(payload, "duplicate point id")

    def test_nonstring_business_fields(self):
        for field, value in (("title", ["bad"]), ("verification_goal", {}), ("verification_goal", None), ("verification_goal", 0), ("dimension", 1), ("priority", False), ("stable_key", None), ("module", []), ("category", 2), ("description", {}), ("test_focus", ["bad"])):
            payload = fixture()
            payload["points"][0][field] = value
            with self.subTest(field=field):
                self.assert_schema_failure(payload, "must be")

    def test_empty_title_rejected(self):
        for value in ("", " \n\t"):
            payload = fixture()
            payload["points"][0]["title"] = value
            with self.subTest(value=value):
                self.assert_schema_failure(payload, "nonempty text")

    def test_empty_and_whitespace_goal_verbatim_cli_mock_export(self):
        for index, goal in enumerate(("", " \n\t")):
            payload = fixture()
            payload["points"][0].update(
                title="验证限价全成交订单的撤单处理。", verification_goal=goal)
            output = self.root / f"empty-goal-{index}"
            with mock_server(json.dumps(payload, ensure_ascii=False).encode()) as (base_url, requests):
                result = subprocess.run(
                    [sys.executable, "-B", str(SCRIPT), "--base-url", base_url,
                     "--task-id", "123", "--output-dir", str(output)],
                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(requests), 1)
            scenes = json.loads((output / "scenarios.json").read_bytes())["scenarios"]
            self.assertEqual(len(scenes), len(payload["points"]))
            self.assertEqual(scenes[0]["description"], goal)
            self.assertEqual(scenes[0]["test_focus"], goal)
            self.assertEqual(scenes[0]["text"], payload["points"][0]["title"] + "\n" + goal)
            self.assertEqual(scenes[0]["source_metadata"]["verification_goal"], goal)
            self.assertTrue(scenes[1]["source_metadata"]["is_excluded"])

    def test_adapter_output_validates_without_fabricated_point_id(self):
        root = SCRIPT.parents[2]
        qa_path = root / "data/evaluation/scenario-benchmark-v0.1/.qa/validate_assets.py"
        spec = importlib.util.spec_from_file_location("benchmark_assets_for_test", qa_path)
        assets = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(assets)
        payload = fixture()
        payload["points"] = [payload["points"][0]]
        payload["points"][0].update(
            title="验证限价全成交订单的撤单处理。", verification_goal="")
        (code, _, stderr), _, _ = self.response_call(payload, "--category", "CAN")
        self.assertEqual(code, 0, stderr)
        scene = self.read_outputs()["scenarios.json"]["scenarios"][0]
        self.assertNotIn("point_id", scene)
        self.assertEqual(scene["source_point_id"], 201)
        calibration = assets.load(assets.HERE / "calibration_samples.json")
        sample = next(s for s in calibration["samples"] if s["sample_id"] == "S031")
        import copy
        data, judgment = copy.deepcopy(sample["input"]), copy.deepcopy(sample["expected_judgments"])
        data["generated_scenarios"] = [scene]
        data["generation_provenance"] = {"provenance": "synthetic_http_mock", "synthetic": True}
        baseline_judgment = judgment["baseline_judgments"][0]
        baseline_judgment["matched_generated_ids"] = [scene["id"]]
        baseline_judgment["evidence"] = [{"generated_id": scene["id"], "quote": scene["text"]}]
        generated_judgment = judgment["generated_judgments"][0]
        generated_judgment["generated_id"] = scene["id"]
        generated_judgment["evidence"] = [{"generated_id": scene["id"], "quote": scene["text"]}]
        schema = assets.load(assets.HERE / "judgment.schema.json")
        gold_rows = {s["id"]: s for s in assets.load(assets.GOLD)["scenarios"]}
        engine = lambda value: assets.fixed_structure(value, schema, schema)
        assets.validate_input(data, gold_rows)
        assets.validate_judgment(data, judgment, schema, engine, gold_rows)
        self.assertEqual(baseline_judgment["evidence"][0]["quote"], scene["title"] + "\n")
        for field in ("id", "category", "title", "description", "test_focus", "text"):
            broken = copy.deepcopy(data)
            del broken["generated_scenarios"][0][field]
            with self.subTest(field=field), self.assertRaises((ValueError, KeyError)):
                assets.validate_input(broken, gold_rows)

    def test_wrong_types_and_associations(self):
        changes = [
            (None, "id", True), (None, "attempt", 0), (None, "version", "1"),
            (None, "points", {}), (None, "task_id", 124), (None, "status", 3),
            (None, "task_status", {}), (None, "retrieval_checkpoint_id", "9"),
            (None, "idempotency_key", {}), (0, "is_selected", 1), (0, "is_excluded", None),
            (0, "sort_order", -1), (0, "id", "201"), (0, "citation_ids", [True]),
            (0, "citation_ids", "2"), (0, "task_id", 99), (0, "checkpoint_id", 18),
        ]
        for index, field, value in changes:
            payload = fixture()
            target = payload if index is None else payload["points"][index]
            target[field] = value
            with self.subTest(field=field, value=value):
                self.assert_schema_failure(payload, "response")
        payload = fixture()
        payload["points"] = ["bad"]
        self.assert_schema_failure(payload, "point must be an object")

    def test_http_errors_no_body_leak(self):
        for status in (400, 401, 403, 404, 422, 500, 502, 503):
            with self.subTest(status=status):
                result, _, _ = self.response_call(b'private-body-secret', status=status)
                self.assertEqual(result[0], 1)
                self.assertIn(f"HTTP {status}", result[2])
                self.assertNotIn("private-body-secret", result[1] + result[2])
                self.assertFalse(self.out.exists())

    def test_html_not_json(self):
        result, _, _ = self.response_call(b"<html>private-login-page</html>", content_type="text/html")
        self.assertEqual(result[0], 1)
        self.assertIn("not JSON", result[2])
        self.assertNotIn("private-login-page", result[2])
        self.assertFalse(self.out.exists())

    def test_malformed_json_and_invalid_utf8(self):
        for body in (b"{broken-private-body}", b"\xff", b"[]", b"null", b'{"id":1,"id":2}', b'{"id":NaN}'):
            with self.subTest(body=body):
                result, _, _ = self.response_call(body)
                self.assertEqual(result[0], 1)
                self.assertFalse(self.out.exists())
                self.assertNotIn("broken-private-body", result[2])

    def test_utf8_bom_raw_preserved(self):
        body = b"\xef\xbb\xbf" + json.dumps(fixture(), ensure_ascii=False).encode()
        result, _, _ = self.response_call(body)
        self.assertEqual(result[0], 0)
        self.assertEqual((self.out / "api_response.json").read_bytes(), body)

    def test_auth_environment_only_not_saved_or_logged(self):
        for option, header, prefix in (("--cookie-env", "Cookie", ""), ("--bearer-token-env", "Authorization", "Bearer ")):
            out = self.root / header
            secret = "session=test-private-credential" if header == "Cookie" else "test-private-bearer"
            with patch.dict(os.environ, {"SCENARIO_TEST_AUTH": secret}), mock_server(json.dumps(fixture()).encode()) as (base_url, requests):
                result = self.invoke(base_url, option, "SCENARIO_TEST_AUTH", output=out)
            self.assertEqual(result[0], 0, result[2])
            self.assertEqual(requests[0]["headers"][header], prefix + secret)
            for file in out.iterdir():
                self.assertNotIn(secret.encode(), file.read_bytes())
                self.assertNotIn(header.encode() + b'":', file.read_bytes())
            self.assertNotIn(secret, result[1] + result[2])

    def test_auth_missing_invalid_and_mutual_exclusion(self):
        cases = [({"SCENARIO_TEST_AUTH": ""}, ["--cookie-env", "SCENARIO_TEST_AUTH"]),
                 ({"SCENARIO_TEST_AUTH": "private\r\nInjected: yes"}, ["--cookie-env", "SCENARIO_TEST_AUTH"]),
                 ({"SCENARIO_TEST_AUTH": "中文"}, ["--bearer-token-env", "SCENARIO_TEST_AUTH"]),
                 ({}, ["--cookie-env", "bad-name"])]
        for env, options in cases:
            with patch.dict(os.environ, env), patch.object(FETCH, "fetch_bytes") as network:
                result = self.invoke("http://127.0.0.1:1", *options)
                self.assertEqual(result[0], 1)
                network.assert_not_called()
                self.assertNotIn("Injected", result[2])
        with redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit) as exc:
            FETCH.main(["--base-url", "http://127.0.0.1:1", "--task-id", "1", "--output-dir", str(self.out), "--cookie-env", "COOKIE", "--bearer-token-env", "TOKEN"])
        self.assertEqual(exc.exception.code, 2)
        self.assertNotIn("TOKEN", stderr.getvalue())

    def test_response_echoed_auth_refused_even_json_escaped(self):
        secret = "test-echo-private-secret"
        for body in (json.dumps({**fixture(), "echo": secret}).encode(), json.dumps({**fixture(), "echo": secret}).replace("test-echo", "test-\\u0065cho").encode()):
            with patch.dict(os.environ, {"SCENARIO_TEST_AUTH": secret}):
                result, _, _ = self.response_call(body, "--bearer-token-env", "SCENARIO_TEST_AUTH")
            self.assertEqual(result[0], 1)
            self.assertFalse(self.out.exists())
            self.assertNotIn(secret, result[1] + result[2])

    def test_echoed_individual_cookie_value_refused(self):
        token = "test-cookie-value-private"
        payload = {**fixture(), "unexpected_echo": token}
        with patch.dict(os.environ, {"SCENARIO_TEST_AUTH": f"sid={token}; aux=test-aux-private"}):
            result, _, _ = self.response_call(payload, "--cookie-env", "SCENARIO_TEST_AUTH")
        self.assertEqual(result[0], 1)
        self.assertFalse(self.out.exists())
        self.assertNotIn(token, result[1] + result[2])

    def test_unserializable_unicode_and_numeric_extensions_fail_safely(self):
        for field, value in (("title", "\ud800"), ("unknown_numeric", float("inf"))):
            payload = fixture()
            payload["points"][0][field] = value
            body = json.dumps(payload).encode()
            if field == "unknown_numeric":
                body = body.replace(b"Infinity", b"1e9999")
            result, _, _ = self.response_call(body)
            self.assertEqual(result[0], 1)
            self.assertFalse(self.out.exists())
            self.assertNotIn("Traceback", result[2])

    def test_sensitive_key_refused_not_redacted(self):
        payload = fixture()
        payload["points"][0]["unknown_meta"] = {"api_key": "private-response-key"}
        self.assert_schema_failure(payload, "sensitive key")

    def test_redirect_no_follow_and_no_credential_forward(self):
        with mock_server(b"unexpected-destination") as (destination, destination_requests):
            for status in (301, 302, 303, 307, 308):
                with self.subTest(status=status), patch.dict(os.environ, {"SCENARIO_TEST_AUTH": "test-redirect-secret"}):
                    result, requests, _ = self.response_call(b"redirect-private-body", "--bearer-token-env", "SCENARIO_TEST_AUTH", status=status, extra_headers={"Location": destination + "/steal"})
                    self.assertEqual(result[0], 1)
                    self.assertIn("redirect refused", result[2])
                    self.assertEqual(len(requests), 1)
                    self.assertNotIn("test-redirect-secret", result[1] + result[2])
            self.assertEqual(destination_requests, [])
        self.assertFalse(self.out.exists())

    def test_bad_urls_rejected_before_network_without_echo(self):
        for url in ("ftp://host", "file:///tmp/file", "http://", "http://user:private-url-secret@host", "http://host?token=private-url-secret", "http://host#fragment", "http://host:99999", "http://host/api", "http://host/prefix/api/", "http://host/a/../b", "http://host/a/%2e%2e/b", "http://host/\nfoo", "http://host\\path"):
            with self.subTest(url=url), patch.object(FETCH, "fetch_bytes") as network:
                result = self.invoke(url)
                self.assertEqual(result[0], 1)
                self.assertIn("invalid base URL", result[2])
                self.assertNotIn("private-url-secret", result[1] + result[2])
                network.assert_not_called()

    def test_bad_task_project_and_timeout_no_network(self):
        for option, value in (("--task-id", "0"), ("--task-id", "-1"), ("--task-id", "1/../2"), ("--task-id", "1?x=2"), ("--task-id", "1.0"), ("--project-id", "0"), ("--timeout", "0"), ("--timeout", "-1"), ("--timeout", "nan"), ("--timeout", "inf"), ("--timeout", "bad")):
            with self.subTest(option=option, value=value), patch.object(FETCH, "fetch_bytes") as network:
                result = self.invoke("http://127.0.0.1:1", option, value)
                self.assertEqual(result[0], 1)
                network.assert_not_called()
        with self.assertRaises(FETCH.FetchError):
            FETCH.endpoint_url("http://localhost", "1/../2")

    def test_timeout_and_transport_details_suppressed(self):
        for error in (socket.timeout("private-secret"), URLError("private-secret"), FETCH.HTTPException("private-secret")):
            with self.subTest(error=type(error).__name__), patch.object(FETCH, "build_opener") as opener:
                opener.return_value.open.side_effect = error
                result = self.invoke("http://127.0.0.1:1", "--timeout", "0.01")
                self.assertEqual(result[0], 1)
                self.assertNotIn("private-secret", result[2])
                self.assertFalse(self.out.exists())

    def test_network_receives_positive_timeout(self):
        with patch.object(FETCH, "build_opener") as opener:
            opener.return_value.open.side_effect = socket.timeout()
            self.invoke("http://127.0.0.1:1", "--timeout", "0.25")
            self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 0.25)

    def test_response_size_bounded(self):
        with patch.object(FETCH, "MAX_BODY_BYTES", 32):
            result, _, _ = self.response_call(fixture())
        self.assertEqual(result[0], 1)
        self.assertIn("limit", result[2])
        self.assertFalse(self.out.exists())

    def test_nonempty_output_rejected_before_network(self):
        self.out.mkdir()
        original = self.out / "keep.txt"
        original.write_text("preserve", encoding="utf-8")
        with patch.object(FETCH, "fetch_bytes") as network:
            result = self.invoke("http://127.0.0.1:1")
        self.assertEqual(result[0], 1)
        self.assertIn("overwriting is refused", result[2])
        network.assert_not_called()
        self.assertEqual(original.read_text(), "preserve")
        self.assertEqual(list(self.out.iterdir()), [original])

    def test_empty_existing_output_allowed(self):
        self.out.mkdir()
        result, _, _ = self.response_call(fixture())
        self.assertEqual(result[0], 0, result[2])

    def test_output_file_or_symlink_refused(self):
        file = self.root / "file"
        file.write_text("keep")
        link = self.root / "link"
        link.symlink_to(self.root / "absent", target_is_directory=True)
        for target in (file, link):
            with self.subTest(target=target.name), patch.object(FETCH, "fetch_bytes") as network:
                result = self.invoke("http://127.0.0.1:1", output=target)
                self.assertEqual(result[0], 1)
                network.assert_not_called()
        self.assertEqual(file.read_text(), "keep")

    def test_real_cli_help_and_mock_export(self):
        help_result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=10)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        for option in ("--base-url", "--task-id", "--output-dir", "--category", "--timeout", "--project-id", "--cookie-env", "--bearer-token-env"):
            self.assertIn(option, help_result.stdout)
        with mock_server(json.dumps(fixture()).encode()) as (base_url, requests):
            result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--base-url", base_url, "--task-id", "123", "--output-dir", str(self.out)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(requests[0]["method"], "GET")
        self.assertEqual(len(self.read_outputs()["scenarios.json"]["scenarios"]), 2)

    def test_cli_usage_does_not_echo_unrecognized_secret(self):
        with redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit) as exc:
            FETCH.main(["--private-unknown-secret"])
        self.assertEqual(exc.exception.code, 2)
        self.assertNotIn("private-unknown-secret", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
