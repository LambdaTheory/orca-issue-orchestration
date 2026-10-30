"""Offline regressions for the read-only OMC MCP evidence client."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import orch_models as models


SECRET = "fixture-not-a-real-key"
ENDPOINT = "https://models.example.test/omc/api/mcp"


def rpc(result, request_id=1):
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode()


def tool(data, **outer):
    return {"structuredContent": {"status": "success", "data": data}, **outer}


def advice(names, omitted=0):
    return {"recommended": names[0] if names else None, "generated_at_ms": 1000,
            "omitted": omitted, "models": [
                {"model": name, "verdict": "unknown", "rank": None,
                 "reasons": ["no_matching_credentials"], "tightest_window": None,
                 "credentials_total": 0, "credentials_usable": 0,
                 "data_age_seconds": None} for name in names]}


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}


class Protocol(unittest.TestCase):
    def test_json_result_and_rpc_errors(self):
        self.assertEqual(models.decode_response(rpc({"value": 3}), "application/json", 1), {"value": 3})
        cases = [b"broken", rpc({}, 2), b"[]", b'{"id":1,"error":{"message":"private"}}',
                 b'{"id":1}', b'{"id":null,"result":{}}']
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(models.ModelAdviceError):
                models.decode_response(raw, "application/json", 1)

    def test_sse_selects_matching_response_and_joins_data(self):
        nl = chr(10)
        body = ("event: ping" + nl + "data: " + rpc({}, 8).decode() + nl * 2
                + "event: message" + nl + 'data: {"id":1,' + nl
                + 'data: "result":{"ok":true}}' + nl * 2)
        self.assertEqual(models.decode_response(body.encode(), "text/event-stream", 1), {"ok": True})

    def test_sse_missing_matching_response_fails(self):
        with self.assertRaises(models.ModelAdviceError):
            models.decode_response(b"event: ping", "text/event-stream", 1)

    def test_both_tool_layers_are_checked(self):
        self.assertEqual(models.unwrap_tool(tool({"value": 3})), {"value": 3})
        text = {"content": [{"type": "text", "text": json.dumps({"status": "success", "data": {}})}]}
        self.assertEqual(models.unwrap_tool(text), {})
        for value in [tool({}, isError=True), {"structuredContent": {"status": "error"}},
                      {"content": [{"type": "text", "text": "invalid"}]}, [],
                      {"structuredContent": {"status": "success", "data": []}}]:
            with self.subTest(value=value), self.assertRaises(models.ModelAdviceError):
                models.unwrap_tool(value)

    def test_https_endpoint_and_header_safety(self):
        for endpoint in ["http://host.test/mcp", "https://user:pass@host.test/mcp",
                         "https://host.test/mcp?token=private", "https://host.test/mcp#fragment",
                         "https://[invalid"]:
            with self.subTest(endpoint=endpoint), self.assertRaises(models.ModelAdviceError):
                models.OmcClient(endpoint, SECRET)
        for key in ["", "line" + chr(10) + "break", "space key"]:
            with self.assertRaises(models.ModelAdviceError):
                models.OmcClient(ENDPOINT, key)

    def test_redirect_is_never_followed(self):
        redirect = models.NoRedirect()
        self.assertIsNone(redirect.redirect_request(None, None, 302, "moved", {}, "https://other.test"))

    def test_request_preserves_negotiated_headers(self):
        client = models.OmcClient(ENDPOINT, SECRET)
        client.protocol = "2025-06-18"
        client.session = "server-session"
        with patch.object(client.opener, "open", return_value=Response(rpc({}))) as opening:
            self.assertEqual(client.post("tools/list", {}), {})
        request = opening.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer " + SECRET)
        self.assertEqual(request.get_header("Mcp-protocol-version"), client.protocol)
        self.assertEqual(request.get_header("Mcp-session-id"), client.session)
        self.assertIn("text/event-stream", request.get_header("Accept"))

    def test_notification_uses_no_id_and_accepts_202(self):
        client = models.OmcClient(ENDPOINT, SECRET)
        with patch.object(client.opener, "open", return_value=Response(b"", 202)) as opening:
            self.assertIsNone(client.post("notifications/initialized", notification=True))
        self.assertNotIn("id", json.loads(opening.call_args.args[0].data))

    def test_http_error_does_not_echo_server_secrets(self):
        client = models.OmcClient(ENDPOINT, SECRET)
        error = urllib.error.HTTPError(ENDPOINT, 401, SECRET, {}, io.BytesIO(SECRET.encode()))
        with patch.object(client.opener, "open", side_effect=error):
            with self.assertRaises(models.ModelAdviceError) as raised:
                client.post("tools/list", {})
        self.assertEqual(str(raised.exception), "http_401")
        self.assertNotIn(SECRET, str(raised.exception))

    def test_timeout_is_sanitized(self):
        client = models.OmcClient(ENDPOINT, SECRET)
        with patch.object(client.opener, "open", side_effect=TimeoutError(SECRET)):
            with self.assertRaisesRegex(models.ModelAdviceError, "mcp_transport_failed"):
                client.post("tools/list", {})

    def test_response_size_is_bounded(self):
        client = models.OmcClient(ENDPOINT, SECRET)
        with patch.object(client.opener, "open", return_value=Response(b"x" * 21)), patch.object(models, "MAX_RESPONSE", 20):
            with self.assertRaisesRegex(models.ModelAdviceError, "response_too_large"):
                client.post("tools/list", {})

    def test_initialize_and_tool_pagination(self):
        client = models.OmcClient(ENDPOINT, SECRET)
        declared = lambda name: {"name": name, "annotations": {"readOnlyHint": True}}
        responses = [Response(rpc({"protocolVersion": "2025-06-18"}, 1), headers={
                         "Mcp-Session-Id": "server-session", "Content-Type": "application/json"}),
                     Response(b"", 202),
                     Response(rpc({"tools": [declared("models_list")], "nextCursor": "next"}, 2)),
                     Response(rpc({"tools": [declared("model_advice")]}, 3))]
        with patch.object(client.opener, "open", side_effect=responses) as opening:
            self.assertEqual(set(client.initialize()), {"models_list", "model_advice"})
        payloads = [json.loads(call.args[0].data) for call in opening.call_args_list]
        self.assertEqual([payload["method"] for payload in payloads],
                         ["initialize", "notifications/initialized", "tools/list", "tools/list"])
        self.assertEqual(payloads[-1]["params"], {"cursor": "next"})
        self.assertEqual(client.session, "server-session")

    def test_repeated_tool_cursor_is_rejected(self):
        client = models.OmcClient(ENDPOINT, SECRET)
        with patch.object(client, "post", side_effect=[{"protocolVersion": "2025-06-18"}, None,
                          {"tools": [], "nextCursor": "same"}, {"tools": [], "nextCursor": "same"}]):
            with self.assertRaisesRegex(models.ModelAdviceError, "invalid_tool_pagination"):
                client.initialize()

    def test_missing_readonly_tools_is_rejected(self):
        client = models.OmcClient(ENDPOINT, SECRET)
        for directory in [[], [{"name": "models_list", "annotations": None}],
                          [{"name": name, "annotations": {"readOnlyHint": False}}
                           for name in ["models_list", "model_advice"]]]:
            with patch.object(client, "post", side_effect=[{"protocolVersion": "2025-06-18"}, None, {"tools": directory}]):
                with self.assertRaisesRegex(models.ModelAdviceError, "readonly_model_tools_required"):
                    client.initialize()

    def test_unrelated_or_write_tools_cannot_be_called(self):
        client = models.OmcClient(ENDPOINT, SECRET)
        for name in ["quota_refresh", "keys_list", "keys_create", "requests_list"]:
            with self.assertRaisesRegex(models.ModelAdviceError, "tool_not_allowed"):
                client.call_tool(name, {})


class Evidence(unittest.TestCase):
    def test_catalog_excludes_credentials_routes_and_descriptions(self):
        data = {"models": [{"id": "coder", "vision": "yes", "key": SECRET}],
                "model_info": {"coder": {"tool_call": True, "reasoning": True,
                    "description": SECRET, "limit": {"context": 200000},
                    "modalities": {"input": ["text", "image"]}}},
                "providers": [{"key": SECRET}], "routes": [{"credential": SECRET}], "partial": []}
        cleaned = models.clean_catalog(data)
        self.assertNotIn(SECRET, json.dumps(cleaned))
        self.assertTrue(cleaned["models"][0]["tool_call"])
        self.assertEqual(cleaned["models"][0]["context_limit"], 200000)

    def test_empty_catalog_preserves_partial_without_claiming_unavailability(self):
        cleaned = models.clean_catalog({"models": None, "model_info": None,
                                        "partial": ["model_directory_unavailable"]})
        self.assertEqual(cleaned["models"], [])
        self.assertEqual(cleaned["partial"], ["model_directory_unavailable"])

    def test_unknown_remains_unknown_and_null_is_not_zero_budget(self):
        data = advice(["unobserved"])
        cleaned = models.clean_advice(data)
        row = cleaned["models"][0]
        self.assertEqual(row["verdict"], "unknown")
        self.assertIsNone(row["tightest_window"])
        self.assertIsNone(row["data_age_seconds"])

    def test_window_output_is_whitelisted(self):
        data = advice(["coder"])
        data["models"][0]["tightest_window"] = {
            "label": "Weekly", "kind": "weekly", "used_percent": 30,
            "reset_at_ms": 2000, "credential": SECRET}
        self.assertNotIn(SECRET, json.dumps(models.clean_advice(data)))

    def test_invalid_data_fails(self):
        for data in [{}, {"models": [], "generated_at_ms": None},
                     {"models": [{"model": "a", "verdict": "healthy"}], "generated_at_ms": 1}]:
            with self.assertRaises(models.ModelAdviceError):
                models.clean_advice(data)
        with self.assertRaises(models.ModelAdviceError):
            models.clean_catalog({"models": [{}]})

    def test_batches_are_at_most_twenty_and_ranks_stay_separate(self):
        class FakeClient:
            def __init__(self):
                self.calls = []
            def initialize(self):
                pass
            def call_tool(self, name, args):
                self.calls.append((name, args))
                if name == "models_list":
                    return {"models": [{"id": "coder-" + str(i)} for i in range(45)], "partial": []}
                return advice(args["models"])
        client = FakeClient()
        output = models.collect(client)
        self.assertEqual([len(batch["models"]) for batch in output["advice_batches"]], [20, 20, 5])
        self.assertEqual(len(client.calls), 4)
        self.assertTrue(output["advisory_only"])
        self.assertNotIn("selected_model", output)

    def test_missing_or_omitted_candidates_fail_closed(self):
        for answer in [advice([]), advice(["coder"], omitted=1)]:
            client = models.OmcClient(ENDPOINT, SECRET)
            with patch.object(client, "initialize"), patch.object(client, "call_tool", side_effect=[
                    {"models": [{"id": "coder"}]}, answer]):
                with self.assertRaisesRegex(models.ModelAdviceError, "model_advice_incomplete"):
                    models.collect(client, ["coder"])

    def test_main_reads_key_file_without_printing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readonly-key.txt"
            path.write_text(SECRET, encoding="utf-8")
            output = io.StringIO()
            with patch.object(models, "collect", return_value={"status": "success"}), contextlib.redirect_stdout(output):
                self.assertEqual(models.main(["--endpoint", ENDPOINT, "--key-file", str(path)]), 0)
            self.assertNotIn(SECRET, output.getvalue())
            self.assertNotIn(str(path), output.getvalue())

    def test_main_missing_key_is_sanitized(self):
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(models.main(["--endpoint", ENDPOINT]), 1)
        self.assertEqual(json.loads(output.getvalue())["code"], "invalid_readonly_key")


class Flow(unittest.TestCase):
    def test_shared_model_files_are_identical(self):
        suite = Path(__file__).resolve().parents[4]
        for relative in ["references/models.md", "scripts/orch_models.py", "scripts/orch_models_test.py"]:
            a = suite / "windows/skills/orca-issue-orchestration" / relative
            b = suite / "macos/skills/orca-issue-orchestration" / relative
            self.assertEqual(a.read_bytes(), b.read_bytes(), relative)

    def test_dispatch_examples_set_model_only_for_new_agents(self):
        suite = Path(__file__).resolve().parents[4]
        for platform in ["windows", "macos"]:
            skill = suite / platform / "skills/orca-issue-orchestration"
            commands = (skill / "references/commands.md").read_text(encoding="utf-8")
            self.assertIn("scripts/orch_models.py", commands)
            self.assertIn("--model", commands)
            for line in commands.splitlines():
                if line.startswith("orca orchestration worker-start") and "--terminal" in line:
                    self.assertNotIn("--model", line)
            guide = (skill / "references/models.md").read_text(encoding="utf-8")
            for phrase in ["不同批次的 rank 不能直接比较", "负载均衡不自动提高并发上限",
                           "requested_model", "effective_model", "unknown", "不修改客户端配置"]:
                self.assertIn(phrase, guide)


if __name__ == "__main__":
    unittest.main()
