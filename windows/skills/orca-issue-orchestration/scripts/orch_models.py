#!/usr/bin/env python3
"""Read-only OMC model evidence. Never launches or reconfigures an agent."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


MAX_RESPONSE = 2 * 1024 * 1024


class ModelAdviceError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must never carry the bearer token to a different endpoint.
        return None


def decode_response(raw, content_type, request_id):
    try:
        text = raw.decode("utf-8-sig")
        if "text/event-stream" not in content_type:
            message = json.loads(text)
        else:
            message = None
            text = text.replace(chr(13) + chr(10), chr(10))
            for event in text.split(chr(10) * 2):
                data = chr(10).join(line[5:].lstrip(" ") for line in event.splitlines()
                                   if line.startswith("data:"))
                if not data:
                    continue
                candidate = json.loads(data)
                if isinstance(candidate, dict) and candidate.get("id") == request_id:
                    message = candidate
                    break
        if not isinstance(message, dict) or message.get("id") != request_id:
            raise ModelAdviceError("invalid_rpc_response")
        if message.get("error"):
            raise ModelAdviceError("json_rpc_error")
        if "result" not in message:
            raise ModelAdviceError("missing_rpc_result")
        return message["result"]
    except (UnicodeError, ValueError) as exc:
        raise ModelAdviceError("invalid_mcp_response") from exc


def unwrap_tool(result):
    if not isinstance(result, dict):
        raise ModelAdviceError("invalid_tool_result")
    inner = result.get("structuredContent")
    if not isinstance(inner, dict):
        try:
            texts = [item["text"] for item in result.get("content", [])
                     if isinstance(item, dict) and item.get("type") == "text"]
            inner = next((json.loads(text) for text in texts if text.strip()), None)
        except (ValueError, TypeError, KeyError) as exc:
            raise ModelAdviceError("invalid_tool_content") from exc
    if result.get("isError") or not isinstance(inner, dict) or inner.get("status") != "success":
        raise ModelAdviceError("omc_tool_error")
    if not isinstance(inner.get("data"), dict):
        raise ModelAdviceError("missing_tool_data")
    return inner["data"]


class OmcClient:
    def __init__(self, endpoint, key, timeout=20):
        try:
            parsed = urllib.parse.urlsplit(endpoint)
        except (ValueError, TypeError) as exc:
            raise ModelAdviceError("trusted_https_endpoint_required") from exc
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ModelAdviceError("trusted_https_endpoint_required")
        if not key or any(char.isspace() for char in key):
            raise ModelAdviceError("invalid_readonly_key")
        self.endpoint = endpoint
        self.key = key
        self.timeout = timeout
        self.protocol = None
        self.session = None
        self.next_id = 0
        self.opener = urllib.request.build_opener(NoRedirect())

    def post(self, method, params=None, notification=False):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            self.next_id += 1
            payload["id"] = self.next_id
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "Authorization": "Bearer " + self.key}
        if self.protocol:
            headers["Mcp-Protocol-Version"] = self.protocol
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(self.endpoint, method="POST", headers=headers,
                                         data=json.dumps(payload).encode("utf-8"))
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise ModelAdviceError("response_too_large")
                if method == "initialize":
                    self.session = response.headers.get("Mcp-Session-Id")
                if notification:
                    if response.status not in (200, 202, 204):
                        raise ModelAdviceError("initialized_notification_rejected")
                    return None
                if response.status != 200:
                    raise ModelAdviceError("unexpected_http_status")
                return decode_response(raw, response.headers.get("Content-Type", ""), payload["id"])
        except urllib.error.HTTPError as exc:
            # Do not echo server bodies, URLs or request headers into task logs.
            raise ModelAdviceError("http_" + str(exc.code)) from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise ModelAdviceError("mcp_transport_failed") from exc

    def initialize(self):
        result = self.post("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "orca-issue-models", "version": "1"}})
        if not isinstance(result, dict) or not isinstance(result.get("protocolVersion"), str):
            raise ModelAdviceError("missing_protocol_version")
        self.protocol = result["protocolVersion"]
        self.post("notifications/initialized", notification=True)
        tools = {}
        cursors = set()
        cursor = None
        for _ in range(20):
            page = self.post("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(page, dict) or not isinstance(page.get("tools"), list):
                raise ModelAdviceError("invalid_tool_directory")
            for tool in page["tools"]:
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    tools[tool["name"]] = tool
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in cursors:
                raise ModelAdviceError("invalid_tool_pagination")
            cursors.add(cursor)
        else:
            raise ModelAdviceError("tool_directory_incomplete")
        for name in ("models_list", "model_advice"):
            if name not in tools or (tools[name].get("annotations") or {}).get("readOnlyHint") is not True:
                raise ModelAdviceError("readonly_model_tools_required")
        return tools

    def call_tool(self, name, arguments):
        if name not in ("models_list", "model_advice"):
            raise ModelAdviceError("tool_not_allowed")
        return unwrap_tool(self.post("tools/call", {"name": name, "arguments": arguments}))


def clean_catalog(data):
    rows = data.get("models") or []
    info = data.get("model_info") or {}
    if not isinstance(rows, list) or not isinstance(info, dict):
        raise ModelAdviceError("invalid_model_directory")
    catalog = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise ModelAdviceError("invalid_model_directory")
        model_id = row["id"]
        metadata = info.get(model_id) or {}
        if not isinstance(metadata, dict):
            raise ModelAdviceError("invalid_model_metadata")
        limits = metadata.get("limit") or {}
        modalities = metadata.get("modalities") or {}
        if not isinstance(limits, dict) or not isinstance(modalities, dict):
            raise ModelAdviceError("invalid_model_metadata")
        catalog.append({"model": model_id, "vision": row.get("vision"),
                        "tool_call": metadata.get("tool_call"),
                        "reasoning": metadata.get("reasoning"),
                        "context_limit": limits.get("context"),
                        "input_modalities": modalities.get("input")})
    return {"models": catalog, "partial": data.get("partial") or [],
            "metadata_updated_at": data.get("metadata_updated_at")}


def clean_advice(data):
    fields = ("model", "verdict", "rank", "reasons", "tightest_window",
              "credentials_total", "credentials_usable", "data_age_seconds")
    rows = data.get("models")
    if not isinstance(rows, list) or not isinstance(data.get("generated_at_ms"), int):
        raise ModelAdviceError("invalid_model_advice")
    models = []
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("model"), str)
                or row.get("verdict") not in ("ok", "caution", "avoid", "unknown")):
            raise ModelAdviceError("invalid_model_advice")
        cleaned = {field: row.get(field) for field in fields}
        window = row.get("tightest_window")
        if window is not None:
            if not isinstance(window, dict):
                raise ModelAdviceError("invalid_quota_window")
            cleaned["tightest_window"] = {field: window.get(field) for field in
                                         ("label", "kind", "used_percent", "reset_at_ms")}
        models.append(cleaned)
    return {"recommended": data.get("recommended"), "models": models,
            "generated_at_ms": data["generated_at_ms"], "omitted": data.get("omitted", 0)}


def collect(client, models=None):
    client.initialize()
    catalog = clean_catalog(client.call_tool("models_list", {}))
    names = list(dict.fromkeys(models or [row["model"] for row in catalog["models"]]))
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ModelAdviceError("invalid_model_name")
    groups = [names[index:index + 20] for index in range(0, len(names), 20)] or [[]]
    advice = []
    for group in groups:
        data = clean_advice(client.call_tool("model_advice", {"models": group} if group else {}))
        if group and ({row["model"] for row in data["models"]} != set(group) or data["omitted"]):
            raise ModelAdviceError("model_advice_incomplete")
        advice.append(data)
    return {"status": "success", "catalog": catalog, "advice_batches": advice,
            "advisory_only": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=os.environ.get("OMC_MCP_ENDPOINT"))
    parser.add_argument("--key-file", default=os.environ.get("OMC_MCP_KEY_FILE"))
    parser.add_argument("--models", nargs="+", help="Exact OMC names; requests are batched in groups of 20")
    args = parser.parse_args(argv)
    try:
        if not args.endpoint:
            raise ModelAdviceError("omc_endpoint_required")
        try:
            key = (Path(args.key_file).read_text(encoding="utf-8-sig").strip()
                   if args.key_file else os.environ.get("OMC_MCP_KEY", "").strip())
        except (OSError, UnicodeError) as exc:
            raise ModelAdviceError("readonly_key_file_unreadable") from exc
        result = collect(OmcClient(args.endpoint, key), args.models)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ModelAdviceError as exc:
        print(json.dumps({"status": "error", "code": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
