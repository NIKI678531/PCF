"""Offline regression tests for the scanner, not the production application."""

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "scan_logic_bugs.py"
SPEC = importlib.util.spec_from_file_location("logic_bug_scan", MODULE_PATH)
scanner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = scanner
SPEC.loader.exec_module(scanner)


def source(path="app.py"):
    return scanner.Source(path, ("def ratio(current, previous):", "    return abs(current - previous) / previous"), "hash")


def finding(**changes):
    item = {
        "severity": "high", "confidence": "high", "title": "Negative denominator",
        "file": "app.py", "line_start": 2, "line_end": 2,
        "evidence": "return abs(current - previous) / previous",
        "description": "The denominator can be negative.", "trigger": "previous=-100",
        "impact": "An excessive difference is accepted.",
        "suggestion": "Use an absolute denominator and handle zero.",
        "verification": "Compare -100 with -1000000 and test zero.",
    }
    item.update(changes)
    return item


def report():
    return {
        "status": "running", "started_at": "2026-10-06T00:00:00+00:00", "finished_at": None,
        "batches_total": 0, "batches_completed": 0, "findings": [], "errors": [],
        "source_files": [], "excluded_files": [], "commit": "a" * 40,
        "repository": "example/repo", "branch": "main",
    }


def decision(keep=True, **changes):
    item = {"id": "R001", "keep": keep, "severity": "high", "reason": "Confirmed from the expression."}
    item.update(changes)
    return {"decisions": [item]}


class SourceTests(unittest.TestCase):
    def test_only_tracked_python_is_read_and_app_is_never_executed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "app.py").write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
            (root / "untracked.py").write_text("should_not_be_sent = True", encoding="utf-8")
            (root / ".env").write_text("SECRET=private", encoding="utf-8")
            with patch.object(scanner, "git", return_value=b"app.py\0.env\0scripts/scan_logic_bugs.py\0"):
                files, excluded = scanner.collect_sources(root, "")
            self.assertEqual(list(files), ["app.py"])
            self.assertEqual(excluded, ["scripts/scan_logic_bugs.py"])
            self.assertEqual(len(files["app.py"].sha256), 64)

    def test_large_file_batches_cover_every_line(self):
        item = scanner.Source("large.py", tuple(f"value_{n} = {n}" for n in range(180)), "hash")
        batches = scanner.build_batches({item.path: item}, limit=150)
        self.assertGreater(len(batches), 1)
        for number, line in enumerate(item.lines, 1):
            self.assertTrue(any(f"{number}: {line}\n" in batch for batch in batches))

    def test_redaction_preserves_line_count(self):
        text = ('token = "sk-' + 'x' * 30 + '"\npassword = "private"\n'
                'url = "https://example.invalid/?sig=signature&api-version=1"\n')
        result = scanner.redact(text)
        self.assertNotIn("private", result)
        self.assertNotIn("signature", result)
        self.assertNotIn("sk-", result)
        self.assertEqual(len(result.splitlines()), len(text.splitlines()))
        self.assertIn("api-version=1", result)

    def test_explicit_key_is_redacted(self):
        self.assertEqual(scanner.redact("Bearer arbitrary-key", "arbitrary-key"), "Bearer [REDACTED]")

    def test_missing_source_is_failure(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(scanner, "git", return_value=b"missing.py\0"):
            with self.assertRaises(scanner.ScanError):
                scanner.collect_sources(Path(folder), "")


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.sources = {"app.py": source()}

    def test_grounded_finding_is_accepted(self):
        self.assertEqual(scanner.validate_finding(finding(), self.sources), finding())

    def test_invalid_severity_path_lines_and_evidence_are_rejected(self):
        for changes in (
            {"severity": "critical"}, {"confidence": "certain"}, {"file": "../secret.py"},
            {"line_start": 0}, {"line_start": True}, {"line_end": 999},
            {"evidence": "invented_code()"}, {"trigger": ""},
        ):
            with self.subTest(changes=changes), self.assertRaises(scanner.ScanError):
                scanner.validate_finding(finding(**changes), self.sources)

    def test_missing_findings_is_not_treated_as_clean_scan(self):
        with self.assertRaises(scanner.ScanError):
            scanner.validate_findings({}, self.sources)

    def test_incomplete_or_duplicate_review_is_rejected(self):
        candidates = [finding(id="R001"), finding(id="R002")]
        for data in ({"decisions": []}, decision(), {"decisions": decision()["decisions"] * 2}):
            with self.subTest(data=data), self.assertRaises(scanner.ScanError):
                scanner.validate_decisions(data, candidates)

    def test_non_boolean_review_is_rejected(self):
        with self.assertRaises(scanner.ScanError):
            scanner.validate_decisions(decision(keep="false"), [finding(id="R001")])

    def test_invalid_response_gets_one_repair_attempt(self):
        client = Mock()
        client.request.side_effect = [{"findings": [finding(line_start=0)]}, {"findings": [finding()]}]
        result = scanner.validated_request(client, "prompt", lambda data: scanner.validate_findings(data, self.sources))
        self.assertEqual(result, [finding()])
        self.assertEqual(client.request.call_count, 2)


class ScanTests(unittest.TestCase):
    def test_complete_scan_is_reviewed_and_default_exit_is_zero(self):
        client = Mock()
        client.request.side_effect = [{"findings": [finding()]}, decision()]
        result = report()
        scanner.scan({"app.py": source()}, client, result, lambda: None)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["findings"][0]["review_status"], "reviewed")
        self.assertEqual(scanner.exit_code(result, False), 0)
        self.assertEqual(scanner.exit_code(result, True), 2)

    def test_all_three_risk_levels_are_kept(self):
        client = Mock()
        candidates = [finding(severity=level, title=level) for level in scanner.SEVERITIES]
        decisions = [{"id": f"R{n:03d}", "keep": True, "severity": level, "reason": "Evidence"}
                     for n, level in enumerate(scanner.SEVERITIES, 1)]
        client.request.side_effect = [{"findings": candidates}, {"decisions": decisions}]
        result = report()
        scanner.scan({"app.py": source()}, client, result, lambda: None)
        self.assertEqual([f["severity"] for f in scanner.active_findings(result)], list(scanner.SEVERITIES))

    def test_no_findings_needs_no_review_and_is_complete(self):
        client = Mock()
        client.request.return_value = {"findings": []}
        result = report()
        scanner.scan({"app.py": source()}, client, result, lambda: None)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(client.request.call_count, 1)
        self.assertEqual(scanner.exit_code(result, True), 0)

    def test_dismissed_high_risk_does_not_fail_gate(self):
        client = Mock()
        client.request.side_effect = [{"findings": [finding()]}, decision(keep=False)]
        result = report()
        scanner.scan({"app.py": source()}, client, result, lambda: None)
        self.assertEqual(scanner.active_findings(result), [])
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(scanner.exit_code(result, True), 0)

    def test_one_failed_batch_does_not_hide_other_results(self):
        client = Mock()
        client.request.side_effect = [scanner.ScanError("network"), scanner.ScanError("network"),
                                      {"findings": [finding(file="other.py")]}, decision()]
        result = report()
        scanner.scan({"app.py": source(), "other.py": source("other.py")}, client, result, lambda: None)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["batches_completed"], 1)
        self.assertEqual(len(scanner.active_findings(result)), 1)
        self.assertEqual(scanner.exit_code(result, False), 1)

    def test_failed_review_leaves_candidates_visible_and_fails_scan(self):
        client = Mock()
        client.request.side_effect = [{"findings": [finding()]}, {"decisions": []}, {"decisions": []}]
        result = report()
        scanner.scan({"app.py": source()}, client, result, lambda: None)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["findings"][0]["review_status"], "pending")
        self.assertEqual(scanner.exit_code(result, False), 1)

    def test_fatal_error_stops_all_batches_without_format_retry(self):
        client = Mock()
        client.request.side_effect = scanner.FatalScanError("HTTP 401")
        with self.assertRaises(scanner.FatalScanError):
            scanner.scan({"app.py": source(), "other.py": source("other.py")}, client, report(), lambda: None)
        self.assertEqual(client.request.call_count, 1)


class ReportTests(unittest.TestCase):
    def test_report_has_pinned_code_link_and_all_decision_fields(self):
        result = report()
        result.update(status="complete", findings=[finding(id="R001", review_status="reviewed")])
        rendered = scanner.render_markdown(result)
        self.assertIn("/blob/" + "a" * 40 + "/app.py#L2-L2", rendered)
        for text in ("触发条件", "影响", "修复建议", "验证方法", "模型置信度", "已复核"):
            self.assertIn(text, rendered)

    def test_model_markdown_cannot_embed_image_or_html(self):
        text = scanner.md('<img src="https://example.invalid"> ![x](https://example.invalid)')
        self.assertNotIn("<img", text)
        self.assertNotIn("![x](", text)

    def test_incomplete_scan_does_not_claim_clean_result(self):
        result = report()
        result["status"] = "partial"
        rendered = scanner.render_markdown(result)
        self.assertIn("不能将报告", rendered)
        self.assertNotIn("本轮未保留候选风险", rendered)

    def test_reports_and_summary_are_written_without_secret(self):
        result = report()
        result["errors"] = ["arbitrary-secret"]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scanner.write_report(result, root / "reports", "arbitrary-secret", root / "summary.md")
            for path in (root / "reports/report.md", root / "reports/report.json", root / "summary.md"):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("arbitrary-secret", text)
                self.assertNotIn("arbitrary", text)
                self.assertIn("REDACTED", text)
            json.loads((root / "reports/report.json").read_text(encoding="utf-8"))

    def test_missing_secret_still_produces_failed_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.dict(os.environ, {"NEW_API_KEY": "", "GITHUB_STEP_SUMMARY": str(root / "summary.md")}):
                code = scanner.main(["--output-dir", str(root / "reports")])
            self.assertEqual(code, 1)
            result = json.loads((root / "reports/report.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "failed")
            self.assertTrue((root / "summary.md").exists())


class ClientTests(unittest.TestCase):
    def client(self):
        client = scanner.LlmClient("private-test-key", time.monotonic() + 60)
        client.opener = Mock()
        return client

    @staticmethod
    def response(data):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(data).encode()
        return response

    def test_chat_contract_and_json_fence(self):
        client = self.client()
        client.opener.open.return_value = self.response({
            "choices": [{"message": {"content": '```json\n{"findings": []}\n```'}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 10},
        })
        self.assertEqual(client.request("prompt"), {"findings": []})
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, scanner.ENDPOINT)
        self.assertEqual(json.loads(request.data)["model"], "gpt-5.6-sol")
        self.assertEqual(client.usage["total_tokens"], 10)

    def test_auth_error_is_not_retried_or_echoed(self):
        client = self.client()
        client.opener.open.side_effect = urllib.error.HTTPError(scanner.ENDPOINT, 401, "private-test-key", {}, None)
        with self.assertRaises(scanner.ScanError) as caught:
            client.request("prompt")
        self.assertNotIn("private-test-key", str(caught.exception))
        self.assertEqual(client.opener.open.call_count, 1)

    def test_transient_error_has_bounded_retries(self):
        client = self.client()
        client.opener.open.side_effect = urllib.error.URLError("private-test-key")
        with patch.object(scanner.time, "sleep"), self.assertRaises(scanner.ScanError):
            client.request("prompt")
        self.assertEqual(client.opener.open.call_count, 3)

    def test_truncated_response_cannot_pass(self):
        client = self.client()
        client.opener.open.return_value = self.response({
            "choices": [{"message": {"content": '{"findings": []}'}, "finish_reason": "length"}],
        })
        with self.assertRaises(scanner.ScanError):
            client.request("prompt")

    def test_deadline_prevents_further_requests(self):
        client = self.client()
        client.deadline = 0
        with self.assertRaises(scanner.ScanError):
            client.request("prompt")
        client.opener.open.assert_not_called()

    def test_redirects_are_not_followed(self):
        self.assertIsNone(scanner.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))


if __name__ == "__main__":
    unittest.main()
