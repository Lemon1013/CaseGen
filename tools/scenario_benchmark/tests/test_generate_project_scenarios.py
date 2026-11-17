"""Offline mocks for fresh project scenario sampling; never contact a model.

Runs under the plain standard library so the frozen campaign can execute both suites with
`python3 -B` and no third-party test runner:

    PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tools/scenario_benchmark/tests \
        -p 'test_generate_project_scenarios.py' -v
"""
import argparse
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "generate_project_scenarios.py"
spec = importlib.util.spec_from_file_location("generate_project_scenarios", SCRIPT)
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)


def meta():
    return {"project": [{"id": 3, "status": "active"}], "space": [{"id": 2, "project_id": 3, "scope": "project", "status": "active"}],
            "documents": [{"id": 2, "status": "ready", "char_count": 100}],
            "pages": [{"id": 9, "source_document_id": 2, "status": "published"}],
            "chunks": [{"id": 35, "document_id": 2}], "external_bindings": [],
            "model": [{"id": 1}], "prompts": [{"id": 1, "type": "generate"}, {"id": 8, "type": "test_points"}]}


def proposal():
    point = {"id": 77, "task_id": 11, "checkpoint_id": 8, "stable_key": "p1", "title": " 原标题 ", "verification_goal": "原关注点\n不改写",
             "dimension": "boundary", "priority": "P1", "sort_order": 0, "is_selected": True, "is_excluded": False,
             "citation_ids": [1], "created_at": "2026-10-14T00:00:00", "updated_at": "2026-10-14T00:00:00"}
    return {"id": 8, "task_id": 11, "retrieval_checkpoint_id": 7, "status": "pending", "attempt": 1, "version": 1,
            "created_at": point["created_at"], "updated_at": point["updated_at"], "points": [point]}


class SamplerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp_path = Path(self.temp.name)

    def monkeypatch(self, target, attribute, value):
        """Minimal monkeypatch: save, replace, restore on cleanup."""
        original = getattr(target, attribute)
        self.addCleanup(setattr, target, attribute, original)
        setattr(target, attribute, value)

    def test_metadata_rejects_scope_or_readiness(self):
        for field, change in (("space", {"project_id": 4}), ("space", {"status": "archived"}),
                              ("documents", {"status": "pending"}), ("pages", {"source_document_id": 99}),
                              ("chunks", {"document_id": 99}), ("project", {"id": 4})):
            with self.subTest(field=field, change=change):
                m = meta()
                m[field][0].update(change)
                with self.assertRaises(g.GenerationError):
                    g.validate_metadata(m, 3, 2)

    def test_metadata_public_identity(self):
        m = meta()
        g.validate_metadata(m, 3, 2)
        m["external_bindings"] = [{"enabled": 1}]
        with self.assertRaises(g.GenerationError):
            g.validate_metadata(m, 3, 2)

    def test_six_requirements_no_reference_leak(self):
        self.assertEqual(len(g.SCOPES), 6)
        for scope in g.SCOPES:
            p = g.task_payload(scope, meta(), 3, 2)
            self.assertEqual(p["model_id"], 1)
            self.assertEqual(p["prompt_template_id"], 1)
            self.assertEqual(p["reference_case_ids"], [])
            self.assertEqual(p["reference_text"], "")
            self.assertFalse(p["run_generate"])
            self.assertFalse(p["auto_review"])
            self.assertEqual(p["generation_granularity"], "detailed")
            self.assertEqual(p["test_dimensions"], ["positive", "negative", "boundary"])
            self.assertIn(scope, p["description"])
            self.assertTrue(all(w not in p["description"].lower() for w in ("gold", "baseline", "calibration", "auc19")))

    # The generic 10-dimension decomposition framework is part of the requirement DESCRIPTION, so the old
    # first sentence stays byte-identical and the framework is appended after it. The first sentence
    # legitimately carries the business scope name (e.g. 集合竞价撮合成交); only the appended framework
    # section must stay free of any project/gold-specific vocabulary.
    FRAMEWORK_MARKERS = ("# 测试点拆分框架（通用测试设计脚手架）", "1. 取值形态：", "10. 生效条件：",
                         "不得合并成一条规则描述")
    FRAMEWORK_FORBIDDEN_TERMS = (
        # gold core features
        "尾数", "小数位", "小数点", "涨跌幅", "价格范围",
        # gold-specific time windows
        "9:20", "14:57", "9:25", "15:00",
        # gold per-category counts / labels
        "19", "14", "13", "21", "24", "26", "AUC", "AMT", "ACC", "OPN", "CLS", "CAN", "auc-000", "can-026",
        # project/business vocabulary (belongs to the scope, never to the generic scaffold)
        "集合竞价", "撮合", "撤单", "订单类型", "开盘价", "收盘价", "成交金额", "基准",
        # gold/control/experiment vocabulary
        "gold", "baseline", "calibration", "auc19", "对照", "覆盖率", "缺失", "基准线",
    )

    def test_framework_appended_without_gold_leakage(self):
        import re as _re
        for scope in g.SCOPES:
            with self.subTest(scope=scope):
                p = g.task_payload(scope, meta(), 3, 2)
                text = p["description"]
                self.assertTrue(text.startswith(
                    "业务范围：%s。请仅基于本项目已导入的交易规则资料，生成该范围内的纯测试场景与测试关注点。"
                    "只描述要验证的业务行为，不生成可执行用例、操作步骤或执行断言。"
                    "资料不足的部分应明确说明，不自行补充规则。" % scope))
                for marker in self.FRAMEWORK_MARKERS:
                    self.assertIn(marker, text)
                self.assertEqual(text.count("\n1. 取值形态："), 1)
                self.assertNotIn("\n11. ", text)
                self.assertEqual(p["generation_granularity"], "detailed")
                # The framework section starts at the generic heading; everything before it is the old
                # first sentence (byte-identical) plus the scope name only.
                framework = text.split("# 测试点拆分框架（通用测试设计脚手架）", 1)[1]
                lowered = framework.lower()
                for term in self.FRAMEWORK_FORBIDDEN_TERMS:
                    self.assertNotIn(term.lower(), lowered, f"gold-specific term leaked into framework: {term}")
                # Any digit inside the framework must be one of the ten framework ordinals 1..10.
                self.assertTrue({int(t) for t in _re.findall(r"\d+", framework)} <= set(range(1, 11)))

    def test_framework_text_is_identical_across_scopes(self):
        bodies = set()
        for scope in g.SCOPES:
            text = g.task_payload(scope, meta(), 3, 2)["description"]
            bodies.add(text.replace(scope, "{scope}"))
        self.assertEqual(len(bodies), 1)

    def test_select_source_isolation(self):
        m = meta()
        c = {"candidate_citations": [{"id": 1, "citation_type": "wiki", "wiki_page_id": 9},
              {"id": 2, "citation_type": "source", "source_chunk_id": 35},
              {"id": 3, "citation_type": "external", "wiki_page_id": 9},
              {"id": 4, "citation_type": "wiki", "wiki_page_id": 99},
              {"id": 5, "citation_type": "source", "source_chunk_id": 35, "available": False}]}
        self.assertEqual(g.select_citations(c, m), [1, 2])
        c["conflict_groups"] = [{"id": 1}]
        with self.assertRaises(g.GenerationError):
            g.select_citations(c, m)

    def test_sensitive_raw_rejected(self):
        for key in ("password", "password_hash", "api_key", "cookie", "token", "authorization", "session_token"):
            with self.subTest(key=key):
                with self.assertRaises(g.FetchError):
                    g.safe_json(json.dumps({"nested": [{key: "value"}]}).encode())

    def test_embedded_sensitive_json_rejected(self):
        raw = g.json_bytes({"detail_json": json.dumps({"nested": {"api_key": "not-archive-safe"}})})
        with self.assertRaises(g.FetchError):
            g.safe_json(raw)
        with self.assertRaises(g.FetchError):
            g.safe_json(g.json_bytes({"detail_json": '{"x":1,"x":2}'}))

    def test_echo_credentials_rejected(self):
        with self.assertRaises(g.FetchError):
            g.safe_json(b'{"text":"runtime-secret"}', ["runtime-secret"])

    def test_exclusive_and_lock(self):
        out = self.tmp_path / "run" / "generation"
        with g.output_lock(out, False):
            g.save_json(out / "fingerprint.json", {"sha256": "x"})
            with self.assertRaises(g.GenerationError):
                with g.output_lock(out, True):
                    pass
        with self.assertRaises(g.GenerationError):
            with g.output_lock(out, False):
                pass
        with self.assertRaises(FileExistsError):
            g.save_json(out / "fingerprint.json", {})


class MockClient:
    project, space = 3, 2
    secrets = []
    def __init__(self, output, proposal):
        self.output, self.proposal, self.calls = output, proposal, []
        self.statuses = iter(["awaiting_confirmation", "awaiting_test_point_confirmation"])
    def request(self, method, path, data=None, archive=None, auth=False):
        self.calls.append((method, path, data))
        if path == "/api/tasks":
            result = {"id": 11, "project_id": 3, "wiki_space_id": 2}
        elif path.endswith("/generate") or path.endswith("/confirm"):
            result = {"id": 11}
        elif path.endswith("/retrieval-checkpoint"):
            result = {"id": 7, "task_id": 11, "status": "pending", "version": 1, "candidate_citations": [{"id": 1, "citation_type": "wiki", "wiki_page_id": 9}]}
        elif path.endswith("/test-points"):
            result = self.proposal
        elif path.endswith("/events") or path.endswith("/citations"):
            result = []
        else:
            result = {"id": 11, "status": next(self.statuses)}
        if archive:
            g.exclusive(archive, g.json_bytes(result))
        return result


class SamplerFlowTests(SamplerTests):
    def test_pending_snapshot_verbatim_and_resume_no_resend(self):
        self.monkeypatch(g.time, "sleep", lambda seconds: None)
        client = MockClient(self.tmp_path, proposal())
        directory = self.tmp_path / "scope-01"
        p = g.task_payload(g.SCOPES[0], meta(), 3, 2)
        items = g.sample_scope(client, directory, g.SCOPES[0], p, meta(), 20, 1)
        self.assertEqual(items[0]["id"], "TP-11-77")
        self.assertEqual(items[0]["title"], proposal()["points"][0]["title"])
        self.assertEqual(items[0]["description"], proposal()["points"][0]["verification_goal"])
        self.assertEqual(items[0]["text"], items[0]["title"] + "\n" + items[0]["description"])
        self.assertIsNone(items[0]["category"])
        self.assertEqual(items[0]["source_metadata"]["citation_ids"], [1])
        self.assertTrue(all(method != "PUT" and not path.endswith("/test-points/confirm") for method, path, _ in client.calls))
        manifest = g.safe_json((directory / "manifest.json").read_bytes())
        self.assertEqual(manifest["proposal_sha256"], g.sha((directory / "proposal.raw.json").read_bytes()))
        count = len(client.calls)
        self.assertEqual(g.sample_scope(client, directory, g.SCOPES[0], p, meta(), 20, 1), items)
        self.assertEqual(len(client.calls), count)

    def test_uncertain_create_never_repeated(self):
        directory = self.tmp_path / "scope-01"
        directory.mkdir()
        g.save_json(directory / "create.started.json", {})
        client = MockClient(self.tmp_path, proposal())
        with self.assertRaises(g.GenerationError):
            g.sample_scope(client, directory, "scope", {}, meta(), 20, 1)
        self.assertEqual(client.calls, [])

    def test_nonpending_snapshot_refused(self):
        p = proposal()
        p["status"] = "confirmed"
        with self.assertRaises(g.GenerationError):
            g.freeze(MockClient(self.tmp_path, p), self.tmp_path, 11, "scope")

    def test_dry_run_no_auth_or_http(self):
        def explode(*args, **kwargs):
            raise AssertionError("database touched")
        self.monkeypatch(g, "metadata", explode)
        self.monkeypatch(g.getpass, "getpass", explode)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(g.run(argparse.Namespace(execute=False, project_id=3, space_id=2)), 0)
        self.assertIn('"http_requests": 0', stdout.getvalue())

    def test_auth_cancel_no_login(self):
        m = meta()
        self.monkeypatch(g, "metadata", lambda *args: m)
        self.monkeypatch(g.getpass, "getpass", lambda *args: "")
        calls = []
        class DryClient:
            jar = type("Jar", (), {"clear": lambda self: None})()
            def __init__(self, *a): pass
            def request(self, method, path):
                calls.append(path)
                return {"status": "ok"}
            def login(self, *a): raise AssertionError("login must not occur")
            def logout(self): pass
        self.monkeypatch(g, "Client", DryClient)
        args = argparse.Namespace(execute=True, resume=False, db="unused", project_id=3, space_id=2,
            base_url="http://127.0.0.1:8001", origin="http://127.0.0.1:5173", output_dir=str(self.tmp_path / "run" / "generation"),
            timeout=30, password_env=None, password_stdin=False, username="admin")
        with self.assertRaises(g.GenerationError):
            g.run(args)
        self.assertEqual(calls, ["/api/health"])

    def test_resume_fingerprint_rejects_change(self):
        out = self.tmp_path / "run" / "generation"
        out.mkdir(parents=True)
        g.save_json(out / "fingerprint.json", {"sha256": "other"})
        self.monkeypatch(g, "metadata", lambda *args: meta())
        args = argparse.Namespace(execute=True, resume=True, db="unused", project_id=3, space_id=2,
                                  base_url="http://127.0.0.1:8001", output_dir=str(out))
        with self.assertRaisesRegex(g.GenerationError, "fingerprint mismatch"):
            g.run(args)

    def test_output_symlink_ancestor_refused(self):
        target = self.tmp_path / "target"
        target.mkdir()
        link = self.tmp_path / "alias"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(g.GenerationError, "symlink"):
            with g.output_lock(link / "run" / "generation", False):
                pass

    def test_http_redirect_refused_without_body_archive(self):
        from urllib.error import HTTPError
        client = g.Client("http://127.0.0.1:8001", "http://127.0.0.1:5173", self.tmp_path, 3, 2)
        class Opener:
            def open(self, request, timeout):
                assert "project_id=3" in request.full_url
                assert "wiki_space_id=2" in request.full_url
                raise HTTPError(request.full_url, 302, "redirect", {}, None)
        client.opener = Opener()
        with self.assertRaises(g.ApiError) as caught:
            client.request("GET", "/api/tasks/11", archive=self.tmp_path / "raw.json")
        self.assertEqual(caught.exception.status, 302)
        self.assertFalse((self.tmp_path / "raw.json").exists())

    def test_http_sensitive_response_not_archived(self):
        for body in (b'{"api_key":"secret"}', b'{"detail_json":"{\\"password_hash\\":\\"secret\\"}"}'):
            with self.subTest(body=body):
                client = g.Client("http://127.0.0.1:8001", "http://127.0.0.1:5173", self.tmp_path, 3, 2)
                class Response(io.BytesIO):
                    status = 200
                    headers = type("Headers", (), {"get_content_type": lambda self: "application/json", "get": lambda self, key, default: default})()
                class Opener:
                    def open(self, request, timeout): return Response(body)
                client.opener = Opener()
                with self.assertRaises(g.FetchError):
                    client.request("GET", "/api/tasks/11/test-points", archive=self.tmp_path / "raw.json")
                self.assertFalse((self.tmp_path / "raw.json").exists())

    def test_login_failure_after_cookie_issuance_attempts_cleanup(self):
        from http.cookiejar import Cookie
        client = g.Client("http://127.0.0.1:8001", "http://127.0.0.1:5173", self.tmp_path, 3, 2)
        calls = []
        def request(method, path, data=None, auth=False):
            calls.append(path)
            if path.endswith("login"):
                client.jar.set_cookie(Cookie(0, "casegen_session", "offline-cookie", None, False,
                    "127.0.0.1", False, False, "/", True, False, None, True, None, None, {}))
                raise g.FetchError("unexpected sensitive field")
        self.monkeypatch(client, "request", request)
        with self.assertRaises(g.FetchError):
            client.login("admin", "offline-password")
        self.assertTrue(client.logged_in)
        client.logout()
        self.assertEqual(calls, ["/api/auth/login", "/api/auth/logout"])
        self.assertEqual(list(client.jar), [])

    def test_cli_nonfinite_timing_refused(self):
        for option in ("--timeout", "--deadline", "--poll-interval"):
            with self.subTest(option=option):
                self.monkeypatch(sys, "argv", [str(SCRIPT), option, "nan"])
                with self.assertRaises(SystemExit) as caught:
                    g.main()
                self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
