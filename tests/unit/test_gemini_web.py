from __future__ import annotations

import copy
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from fanvpn_bridge.gemini_web import (
    GeminiWebError, GeminiWebProvider, _MAX_TOOL_LOAD_ROUNDS, _TOOL_LOADER, _TOOL_REMINDER,
)


MODEL = "gemini-web/gemini-4-argon"
TOOLS = [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
         {"type": "custom", "name": "apply_patch", "description": "Edit files"}]
PLUGIN_TOOLS = [{"type": "namespace", "name": "mcp__codex_apps__example", "description": "Example plugin",
                 "tools": [{"type": "function", "name": "fetch", "description": "Fetch plugin data",
                            "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                                           "required": ["path"]}},
                           {"type": "function", "name": "search", "description": "Search plugin data",
                            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}]}]
FETCH_TOOL = "mcp__codex_apps__example.fetch"


def load_tools(*names: str) -> str:
    return f'<{_TOOL_LOADER}>' + json.dumps({"names": list(names)}) + f'</{_TOOL_LOADER}>'


def answer_frames(text: str, response_id: str = "resp_web_1", *, preview: str | None = None) -> bytes:
    response = {"id": response_id, "object": "response", "status": "completed", "model": "gemini-4-argon",
                "output": [{"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": text, "annotations": []}]}],
                "usage": {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130}}
    events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
              {"type": "response.output_text.delta", "delta": preview if preview is not None else text},
              {"type": "response.output_text.done", "text": text},
              {"type": "response.completed", "response": response}]
    return b"".join(("event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()
                    for event in events) + b"data: [DONE]\n\n"


class GeminiWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[tuple[str, str, object]] = []
        self.replies: list[tuple[int, bytes]] = []
        self.reply_gate: threading.Event | None = None
        self.reply_prefix = 0
        self.models = [{"id": "gemini-4-argon", "display_name": "Gemini 4 Argon", "context_window": 65536,
                        "thinking_levels": ["low", "medium", "high"], "available_account_count": 1},
                       {"id": "gemini-4-argon-task"}, {"id": "gemini-3.1-flash-image"},
                       {"id": "gemini-3.1-pro", "available_account_count": 0}]
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def handle(self):
                try:
                    super().handle()
                except ConnectionAbortedError:
                    # Windows reports this when the cancellation test closes its socket.
                    pass

            def do_GET(self):
                owner.requests.append(("GET", self.path, self.headers.get("Authorization")))
                raw = json.dumps({"data": owner.models}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(("POST", self.path, body))
                status, raw = owner.replies.pop(0)
                if status == 200 and not body.get("stream"):
                    for line in raw.decode().splitlines():
                        if line.startswith("data: {"):
                            event = json.loads(line[6:])
                            if event.get("type") == "response.completed":
                                raw = json.dumps(event["response"], ensure_ascii=False).encode()
                                break
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream" if status == 200 else "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw[:owner.reply_prefix])
                    self.wfile.flush()
                    if owner.reply_gate is not None:
                        owner.reply_gate.wait(timeout=3)
                    self.wfile.write(raw[owner.reply_prefix:])
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.provider = GeminiWebProvider(base_url=f"http://127.0.0.1:{self.server.server_port}/v1", timeout_seconds=3)

    def tearDown(self) -> None:
        if self.reply_gate is not None:
            self.reply_gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, **extra):
        payload = {"model": MODEL, "input": "你好", **extra}
        streaming, result = self.provider.responses(payload)
        return list(result) if streaming else result

    def posts(self):
        return [row[2] for row in self.requests if row[0] == "POST"]

    def test_disabled_by_default_and_no_upstream_probe(self):
        provider = GeminiWebProvider()
        self.assertEqual(provider.models_response(), {"object": "list", "data": [], "enabled": False})
        with self.assertRaises(GeminiWebError) as error:
            provider.responses({"model": MODEL, "input": "test"})
        self.assertEqual(error.exception.status, 503)
        self.assertEqual(self.requests, [])

    def test_only_explicit_loopback_service_is_allowed(self):
        for url in ("https://example.com/v1", "http://localhost:8007/v1", "http://user:password@127.0.0.1/v1",
                    "http://127.0.0.1/v1?key=secret", "http://127.0.0.1/other"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                GeminiWebProvider(base_url=url)

    def test_dynamic_model_catalog_filters_unavailable_and_specialized_models(self):
        rows = self.provider.models_response()["data"]
        self.assertEqual([row["id"] for row in rows], [MODEL])
        self.assertEqual(rows[0]["context_window"], 65536)
        self.assertEqual(rows[0]["input_modalities"], ["text"])
        self.assertFalse(rows[0]["supports_search_tool"])
        self.provider.models_response()
        self.assertEqual(len(self.requests), 1)
        self.provider.models_response(force=True)
        self.assertEqual(len(self.requests), 2)

    def test_unknown_model_does_not_silently_fall_back(self):
        with self.assertRaises(GeminiWebError) as error:
            self.request(model="gemini-web/gemini-5-flash")
        self.assertEqual(error.exception.code, "gemini_web_model_unavailable")
        self.assertEqual(self.posts(), [])

    def test_plain_stream_keeps_deltas_and_authoritative_final_text(self):
        self.replies = [(200, answer_frames("最终正文", preview="预览"))]
        frames = self.request(stream=True)
        self.assertIn(b"response.output_text.delta", b"".join(frames))
        self.assertIn("最终正文", b"".join(frames).decode())
        self.assertIn(MODEL, b"".join(frames).decode())

    def test_raw_patch_never_enters_an_upstream_json_tool_argument(self):
        patch_text = '*** Begin Patch\n*** Add File: 文档.md\n+' + ('中文 `code` | "quote" \\路径\n+' * 250) + '尾部\n*** End Patch'
        self.replies = [(200, answer_frames(f"<apply_patch>\n{patch_text}\n</apply_patch>"))]
        result = self.request(tools=TOOLS)
        call = result["output"][0]
        self.assertEqual(call["type"], "custom_tool_call")
        self.assertEqual(call["input"], patch_text)
        self.assertEqual(result["usage"]["total_tokens"], 130)
        upstream = self.posts()[0]
        self.assertNotIn("tools", upstream)
        self.assertNotIn("tool_choice", upstream)
        self.assertFalse(upstream["stream"])
        self.assertIn("raw freeform tool input", upstream["input"])

    def test_structured_shell_tool_uses_json(self):
        self.replies = [(200, answer_frames('<exec_command>{"cmd":"echo ok"}</exec_command>'))]
        result = self.request(tools=TOOLS)
        self.assertEqual(result["output"][0]["type"], "function_call")
        self.assertEqual(json.loads(result["output"][0]["arguments"]), {"cmd": "echo ok"})

    def test_namespaced_function_preserves_identity_in_every_stream_event(self):
        tools = [{"type": "namespace", "name": "collaboration", "description": "Agent tools",
                  "tools": [{"type": "function", "name": "send_message", "description": "Send a message",
                             "parameters": {"type": "object", "properties": {"message": {"type": "string"}}}}]}]
        payload = {"model": MODEL, "input": "hello", "tools": tools, "stream": True}
        original = copy.deepcopy(payload)
        self.replies = [(200, answer_frames('<collaboration.send_message>{"message":"hello"}</collaboration.send_message>'))]
        _, frames = self.provider.responses(payload)
        calls = []
        for frame in frames:
            if not frame.startswith(b"event:"):
                continue
            event = json.loads(frame.decode().split("data: ", 1)[1])
            if event["type"] in {"response.output_item.added", "response.output_item.done"}:
                calls.append(event["item"])
            elif event["type"] == "response.completed":
                calls.extend(event["response"]["output"])
        self.assertEqual(len(calls), 3)
        for call in calls:
            self.assertEqual((call["namespace"], call["name"]), ("collaboration", "send_message"))
        self.assertEqual(len({call["call_id"] for call in calls}), 1)
        prompt = self.posts()[0]["input"]
        self.assertIn("<collaboration.send_message>", prompt)
        self.assertIn("Agent tools", prompt)
        self.assertIn('"properties"', prompt)
        self.assertEqual(payload, original)

    def test_namespaced_custom_tool_preserves_raw_patch(self):
        patch_text = '*** Begin Patch\n*** Add File: 文档.md\n+中文 `code` | "quote" \\路径\n*** End Patch'
        tools = [{"type": "namespace", "name": "files", "tools": [TOOLS[1]]}]
        self.replies = [(200, answer_frames(f"<files.apply_patch>\n{patch_text}\n</files.apply_patch>"))]
        call = self.request(tools=tools)["output"][0]
        self.assertEqual((call["type"], call["namespace"], call["name"]), ("custom_tool_call", "files", "apply_patch"))
        self.assertEqual(call["input"], patch_text)

    def test_same_child_name_in_different_namespaces_stays_distinct(self):
        tools = [{"type": "namespace", "name": namespace,
                  "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]}
                 for namespace in ("first", "second")]
        self.replies = [(200, answer_frames('<first.lookup>{}</first.lookup>\n<second.lookup>{}</second.lookup>'))]
        calls = self.request(tools=tools)["output"]
        self.assertEqual([(call["namespace"], call["name"]) for call in calls],
                         [("first", "lookup"), ("second", "lookup")])
        self.assertNotEqual(calls[0]["call_id"], calls[1]["call_id"])

    def test_namespaced_tool_continuation_reuses_sparse_and_full_history(self):
        tools = [{"type": "namespace", "name": "collaboration", "tools": [TOOLS[0]]}]
        for full_history in (False, True):
            with self.subTest(full_history=full_history):
                self.replies = [(200, answer_frames('<collaboration.exec_command>{"cmd":"echo ok"}</collaboration.exec_command>')),
                                (200, answer_frames("done", "resp_web_2"))]
                first = self.request(tools=tools)
                tool_result = {"type": "function_call_output", "call_id": first["output"][0]["call_id"], "output": "ok"}
                items = ([{"role": "user", "content": "你好"}, *first["output"], tool_result]
                         if full_history else [tool_result])
                original = copy.deepcopy(items)
                self.request(tools=tools, previous_response_id=first["id"], input=items)
                second = self.posts()[-1]
                self.assertEqual(second.get("previous_response_id"), first["id"])
                self.assertIn("TOOL RESULT", second["input"])
                self.assertNotIn("ASSISTANT TOOL CALL", second["input"])
                self.assertEqual(items, original)

    def test_full_namespaced_history_after_restart_uses_qualified_prompt_names(self):
        tools = [{"type": "namespace", "name": "collaboration", "tools": [TOOLS[0]]}]
        items = [{"role": "user", "content": "hello"},
                 {"type": "function_call", "namespace": "collaboration", "name": "exec_command",
                  "call_id": "call_previous", "arguments": '{"cmd":"echo ok"}'},
                 {"type": "function_call_output", "call_id": "call_previous", "output": "ok"}]
        self.replies = [(200, answer_frames("done"))]
        self.request(tools=tools, previous_response_id="resp_missing", input=items)
        upstream = self.posts()[0]
        self.assertNotIn("previous_response_id", upstream)
        history_call = upstream["input"].split("ASSISTANT TOOL CALL:\n", 1)[1].splitlines()[0]
        self.assertEqual(json.loads(history_call)["name"], "collaboration.exec_command")
        self.assertEqual(items[1]["name"], "exec_command")

    def test_namespaced_completed_retry_replays_canonical_call_identity(self):
        tools = [{"type": "namespace", "name": "collaboration", "tools": [TOOLS[0]]}]
        self.replies = [(200, answer_frames('<collaboration.exec_command>{"cmd":"echo ok"}</collaboration.exec_command>'))]
        first = self.request(tools=tools, prompt_cache_key="namespace-replay")
        second = self.request(tools=tools, prompt_cache_key="namespace-replay")
        self.assertEqual(first, second)
        self.assertEqual(first["output"][0]["namespace"], "collaboration")
        self.assertEqual(len(self.posts()), 1)

    def test_unsupported_builtins_and_ambiguous_aliases_fail_before_generation(self):
        for tools in ([{"type": "tool_search"}], [{"type": "file_search"}],
                      [TOOLS[0], TOOLS[0]],
                      [{"type": "function", "name": "files.apply_patch"},
                       {"type": "namespace", "name": "files", "tools": [TOOLS[1]]}],
                      [{"type": "namespace", "name": "missing_children"}],
                      [{"type": "namespace", "name": "nested", "tools": [{"type": "namespace", "name": "child", "tools": []}]}]):
            with self.subTest(tools=tools), self.assertRaises(GeminiWebError) as error:
                self.request(tools=tools)
            self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.posts(), [])

    def test_default_hosted_search_does_not_block_chat_or_other_search_functions(self):
        search_function = {"type": "function", "name": "search_docs", "parameters": {"type": "object"}}
        for available in ([], [search_function]):
            with self.subTest(available=available):
                self.replies = [(200, answer_frames("hello"))]
                payload = {"model": MODEL, "input": "hello", "instructions": "Be concise.",
                           "tools": [{"type": "web_search", "external_web_access": False}, *available]}
                original = copy.deepcopy(payload)
                with self.assertLogs("fanvpn_bridge.gemini_web", level="WARNING") as logs:
                    _, result = self.provider.responses(payload)
                self.assertEqual(result["status"], "completed")
                prompt = self.posts()[-1]["input"]
                self.assertIn("Be concise.", prompt)
                self.assertIn("OpenAI-hosted web_search is unavailable", prompt)
                self.assertIn("Never invent search execution or search results", prompt)
                self.assertNotIn("<web_search>", prompt)
                if available:
                    self.assertIn("<search_docs>", prompt)
                self.assertIn("gemini_web_hosted_search_unavailable", logs.output[0])
                self.assertEqual(payload, original)

    def test_tools_must_be_a_list_even_when_empty(self):
        for tools in ({}, "", 0):
            with self.subTest(tools=tools), self.assertRaises(GeminiWebError):
                self.request(tools=tools)
        self.assertEqual(self.posts(), [])

    def test_malformed_model_output_is_not_repaired_or_executed(self):
        for text in ('<apply_patch>*** Begin Patch\n*** End Patch', '<exec_command>{"cmd":"python -c "bad""}</exec_command>'):
            with self.subTest(text=text):
                self.replies = [(200, answer_frames(text))]
                with self.assertRaises(GeminiWebError) as error:
                    self.request(tools=TOOLS)
                self.assertEqual(error.exception.code, "gemini_web_tool_format_invalid")
        self.assertEqual(len(self.posts()), 2)

    def test_tool_result_reuses_session_without_repeating_definitions(self):
        self.replies = [(200, answer_frames('<exec_command>{"cmd":"echo ok"}</exec_command>')),
                        (200, answer_frames("完成", "resp_web_2"))]
        first = self.request(tools=TOOLS)
        result = self.request(tools=TOOLS, previous_response_id=first["id"],
                              input=[{"type": "function_call_output", "call_id": first["output"][0]["call_id"], "output": "ok"}])
        self.assertEqual(result["id"], "resp_web_2")
        second = self.posts()[1]
        self.assertEqual(second["previous_response_id"], first["id"])
        self.assertIn("TOOL RESULT", second["input"])
        self.assertIn("CODEX TOOL REMINDER", second["input"])
        self.assertIn("matching closing tag", second["input"])
        self.assertNotIn("### Tool", second["input"])
        self.assertNotIn("<apply_patch>", second["input"])
        self.assertNotIn("USER:\n你好", second["input"])

    def test_large_plugin_catalog_sends_names_but_not_unused_schemas(self):
        plugins = copy.deepcopy(PLUGIN_TOOLS)
        plugins[0]["tools"] = [
            {"type": "function", "name": f"action_{index}",
             "description": "UNUSED_DESCRIPTION_" + "long description " * 100,
             "parameters": {"type": "object", "properties": {"UNUSED_PARAMETER": {"type": "string"}}}}
            for index in range(264)
        ]
        payload = {"model": MODEL, "input": "hello", "tools": TOOLS + plugins}
        original = copy.deepcopy(payload)
        self.replies = [(200, answer_frames("hello"))]
        _, result = self.provider.responses(payload)
        self.assertEqual(result["status"], "completed")
        prompt = self.posts()[0]["input"]
        self.assertIn("<exec_command>", prompt)
        self.assertIn("<apply_patch>", prompt)
        self.assertIn("mcp__codex_apps__example: action_0", prompt)
        self.assertIn("action_263", prompt)
        self.assertIn("namespace + '.' + child", prompt)
        self.assertNotIn("mcp__codex_apps__example.action_263", prompt)
        self.assertIn(f"<{_TOOL_LOADER}>", prompt)
        self.assertNotIn("UNUSED_DESCRIPTION", prompt)
        self.assertNotIn("UNUSED_PARAMETER", prompt)
        self.assertLess(len(prompt), 25000)
        self.assertEqual(payload, original)
        self.assertEqual(len(self.posts()), 1)

    def test_plugin_schema_loading_is_internal_and_preserves_codex_identity(self):
        self.replies = [(200, answer_frames(load_tools(FETCH_TOOL), "resp_discovery")),
                        (200, answer_frames(f'<{FETCH_TOOL}>{{"path":"notes"}}</{FETCH_TOOL}>', "resp_final"))]
        frames = self.request(tools=TOOLS + PLUGIN_TOOLS, stream=True)
        final = json.loads(next(frame for frame in frames if frame.startswith(b"event: response.completed\n"))
                           .decode().split("data: ", 1)[1])["response"]
        self.assertEqual(final["id"], "resp_final")
        call = final["output"][0]
        self.assertEqual((call["namespace"], call["name"]), ("mcp__codex_apps__example", "fetch"))
        self.assertEqual(json.loads(call["arguments"]), {"path": "notes"})
        self.assertNotIn(b"resp_discovery", b"".join(frames))
        self.assertNotIn(_TOOL_LOADER.encode(), b"".join(frames))
        self.assertNotIn("resp_discovery", self.provider._checkpoints)
        self.assertIn(FETCH_TOOL, self.provider._checkpoints["resp_final"].loaded_tools)
        self.assertEqual(final["usage"], {"input_tokens": 200, "output_tokens": 60, "total_tokens": 260})
        loaded = self.posts()[1]
        self.assertEqual(loaded["previous_response_id"], "resp_discovery")
        self.assertIn("Fetch plugin data", loaded["input"])
        self.assertIn('"required":["path"]', loaded["input"])
        self.assertNotIn("Search plugin data", loaded["input"])
        self.assertNotIn("### Tool exec_command", loaded["input"])

    def test_loaded_plugin_is_reused_without_another_discovery_request(self):
        call_text = f'<{FETCH_TOOL}>{{"path":"notes"}}</{FETCH_TOOL}>'
        self.replies = [(200, answer_frames(load_tools(FETCH_TOOL), "resp_discovery")),
                        (200, answer_frames(call_text, "resp_final")),
                        (200, answer_frames(call_text, "resp_followup"))]
        first = self.request(tools=TOOLS + PLUGIN_TOOLS)
        self.request(tools=TOOLS + PLUGIN_TOOLS, previous_response_id=first["id"],
                     input=[{"type": "function_call_output", "call_id": first["output"][0]["call_id"], "output": "ok"}])
        self.assertEqual(len(self.posts()), 3)
        followup = self.posts()[-1]
        self.assertEqual(followup["previous_response_id"], "resp_final")
        self.assertNotIn("Fetch plugin data", followup["input"])
        self.assertNotIn("DEFERRED TOOL DIRECTORY", followup["input"])
        self.assertNotIn("### Tool", followup["input"])

    def test_expired_snapshot_restores_only_loaded_plugin_schemas(self):
        self.replies = [(200, answer_frames(load_tools(FETCH_TOOL), "resp_discovery")),
                        (200, answer_frames("loaded", "resp_final")),
                        (400, b'{"error":{"message":"previous response not found"}}'),
                        (200, answer_frames("done", "resp_restored"))]
        first = self.request(tools=TOOLS + PLUGIN_TOOLS)
        self.request(tools=TOOLS + PLUGIN_TOOLS, previous_response_id=first["id"], input="continue")
        restored = self.posts()[-1]
        self.assertNotIn("previous_response_id", restored)
        self.assertIn("Fetch plugin data", restored["input"])
        self.assertNotIn("Search plugin data", restored["input"])
        self.assertIn("### Tool exec_command", restored["input"])
        self.assertIn("continue", restored["input"])

    def test_full_history_after_restart_restores_previously_used_plugin(self):
        items = [{"role": "user", "content": "fetch notes"},
                 {"type": "function_call", "namespace": "mcp__codex_apps__example", "name": "fetch",
                  "call_id": "call_old", "arguments": '{"path":"notes"}'},
                 {"type": "function_call_output", "call_id": "call_old", "output": "ok"}]
        self.replies = [(200, answer_frames(f'<{FETCH_TOOL}>{{"path":"more"}}</{FETCH_TOOL}>'))]
        self.request(tools=TOOLS + PLUGIN_TOOLS, previous_response_id="resp_evicted", input=items)
        self.assertNotIn("previous_response_id", self.posts()[0])
        self.assertIn("Fetch plugin data", self.posts()[0]["input"])
        self.assertNotIn("Search plugin data", self.posts()[0]["input"])

    def test_plugin_freeform_loading_keeps_patch_raw(self):
        plugins = [{"type": "namespace", "name": "mcp__files", "tools": [TOOLS[1]]}]
        name = "mcp__files.apply_patch"
        patch_text = '*** Begin Patch\n*** Add File: 文档.md\n+中文 "quote" \\path\n*** End Patch'
        self.replies = [(200, answer_frames(load_tools(name), "resp_discovery")),
                        (200, answer_frames(f"<{name}>\n{patch_text}\n</{name}>", "resp_final"))]
        call = self.request(tools=TOOLS + plugins)["output"][0]
        self.assertEqual((call["namespace"], call["name"]), ("mcp__files", "apply_patch"))
        self.assertEqual(call["input"], patch_text)

    def test_deferred_tools_are_not_executable_before_loading(self):
        self.replies = [(200, answer_frames(f'<{FETCH_TOOL}>{{"path":"notes"}}</{FETCH_TOOL}>'))]
        with self.assertRaises(GeminiWebError) as error:
            self.request(tools=TOOLS + PLUGIN_TOOLS)
        self.assertEqual(error.exception.code, "gemini_web_tool_not_loaded")
        self.assertEqual(self.provider._checkpoints, {})
        self.assertEqual(len(self.posts()), 1)

    def test_invalid_schema_loading_is_rejected_without_repair_or_retry(self):
        for text in (load_tools("unknown.tool"), load_tools("fetch"), load_tools("exec_command"), load_tools(FETCH_TOOL, FETCH_TOOL),
                     load_tools(), load_tools(*([FETCH_TOOL] * 9)),
                     f'<{_TOOL_LOADER}>{{"names":"{FETCH_TOOL}"}}</{_TOOL_LOADER}>',
                     f'<{_TOOL_LOADER}>{{"names":["{FETCH_TOOL}"],"extra":true}}</{_TOOL_LOADER}>',
                     f'<{_TOOL_LOADER}>{{"names":["{FETCH_TOOL}"]}}',
                     load_tools(FETCH_TOOL) + '<exec_command>{"cmd":"echo ok"}</exec_command>'):
            with self.subTest(text=text), self.assertRaises(GeminiWebError) as error:
                self.replies = [(200, answer_frames(text))]
                self.request(tools=TOOLS + PLUGIN_TOOLS)
            self.assertEqual(error.exception.code, "gemini_web_tool_format_invalid")
        self.assertEqual(len(self.posts()), 10)
        self.assertEqual(self.provider._checkpoints, {})

    def test_schema_loading_has_a_bounded_round_limit(self):
        plugins = copy.deepcopy(PLUGIN_TOOLS)
        plugins[0]["tools"] = [{"type": "function", "name": f"action_{index}", "parameters": {"type": "object"}}
                               for index in range(_MAX_TOOL_LOAD_ROUNDS + 1)]
        self.replies = [(200, answer_frames(load_tools(f"mcp__codex_apps__example.action_{index}"), f"resp_{index}"))
                        for index in range(_MAX_TOOL_LOAD_ROUNDS + 1)]
        with self.assertRaises(GeminiWebError) as error:
            self.request(tools=TOOLS + plugins)
        self.assertEqual(error.exception.code, "gemini_web_tool_load_limit")
        self.assertEqual(len(self.posts()), _MAX_TOOL_LOAD_ROUNDS + 1)
        self.assertEqual(self.provider._checkpoints, {})

    def test_discovery_quota_error_is_not_retried_and_commits_no_checkpoint(self):
        self.replies = [(200, answer_frames(load_tools(FETCH_TOOL), "resp_discovery")),
                        (429, b'{"error":"COOKIE_SECRET"}')]
        with self.assertRaises(GeminiWebError) as error:
            self.request(tools=TOOLS + PLUGIN_TOOLS)
        self.assertEqual(error.exception.status, 429)
        self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(self.provider._checkpoints, {})

    def test_discovery_does_not_leak_loaded_tools_to_another_conversation(self):
        self.replies = [(200, answer_frames(load_tools(FETCH_TOOL), "resp_discovery")),
                        (200, answer_frames("loaded", "resp_final")),
                        (200, answer_frames("hello", "resp_independent"))]
        self.request(tools=TOOLS + PLUGIN_TOOLS)
        self.request(tools=TOOLS + PLUGIN_TOOLS)
        self.assertNotIn("Fetch plugin data", self.posts()[-1]["input"])
        self.assertNotIn(FETCH_TOOL, self.provider._checkpoints["resp_independent"].loaded_tools)

    def test_definition_loader_name_collision_fails_before_generation(self):
        with self.assertRaises(GeminiWebError):
            self.request(tools=TOOLS + [{"type": "function", "name": _TOOL_LOADER}])
        self.assertEqual(self.posts(), [])

    def test_short_reminder_preserves_tool_syntax_and_selection_rules(self):
        self.assertLess(len(_TOOL_REMINDER), 800)
        for required in ("Codex executes tools", "never invent results", "final RESPONSE", "no surrounding prose",
                         "matching closing tag", "valid JSON", "correct escaping", "raw tool input",
                         "exec_command", "apply_patch", "semantic boundaries", "loading alone", "TOOL RESULT"):
            with self.subTest(rule=required):
                self.assertIn(required, _TOOL_REMINDER)

    def test_directory_preserves_namespace_identity_and_flat_mcp_names(self):
        plugins = [{"type": "namespace", "name": namespace,
                    "tools": [{"type": "function", "name": "fetch", "parameters": {"type": "object"}}]}
                   for namespace in ("mcp__one", "mcp__two")]
        plugins.append({"type": "function", "name": "mcp__flat_fetch", "parameters": {"type": "object"}})
        self.replies = [(200, answer_frames(load_tools("mcp__two.fetch", "mcp__flat_fetch"), "resp_discovery")),
                        (200, answer_frames('<mcp__two.fetch>{}</mcp__two.fetch>\n'
                                            '<mcp__flat_fetch>{}</mcp__flat_fetch>', "resp_final"))]
        result = self.request(tools=TOOLS + plugins)
        directory = self.posts()[0]["input"].split("DEFERRED TOOL DIRECTORY", 1)[1]
        self.assertIn("mcp__one: fetch", directory)
        self.assertIn("mcp__two: fetch", directory)
        self.assertIn("mcp__flat_fetch", directory)
        self.assertNotIn("mcp__one.fetch", directory)
        self.assertEqual((result["output"][0]["namespace"], result["output"][0]["name"]), ("mcp__two", "fetch"))
        self.assertEqual(result["output"][1]["name"], "mcp__flat_fetch")
        self.assertNotIn("namespace", result["output"][1])

    def test_multiple_definitions_can_be_loaded_in_one_request(self):
        search = "mcp__codex_apps__example.search"
        self.replies = [(200, answer_frames(load_tools(FETCH_TOOL, search), "resp_discovery")),
                        (200, answer_frames(f'<{FETCH_TOOL}>{{"path":"notes"}}</{FETCH_TOOL}>\n'
                                            f'<{search}>{{"query":"notes"}}</{search}>', "resp_final"))]
        result = self.request(tools=TOOLS + PLUGIN_TOOLS)
        self.assertEqual([call["name"] for call in result["output"]], ["fetch", "search"])
        self.assertEqual(len(self.posts()), 2)
        self.assertIn("Fetch plugin data", self.posts()[1]["input"])
        self.assertIn("Search plugin data", self.posts()[1]["input"])

    def test_changed_tool_definition_restarts_with_updated_schema(self):
        self.replies = [(200, answer_frames(load_tools(FETCH_TOOL), "resp_discovery")),
                        (200, answer_frames(f'<{FETCH_TOOL}>{{"path":"notes"}}</{FETCH_TOOL}>', "resp_final")),
                        (200, answer_frames("done", "resp_changed"))]
        first = self.request(tools=TOOLS + PLUGIN_TOOLS)
        changed = copy.deepcopy(PLUGIN_TOOLS)
        changed[0]["tools"][0]["description"] = "UPDATED_PLUGIN_DESCRIPTION"
        self.request(tools=TOOLS + changed, previous_response_id=first["id"], input="continue")
        self.assertNotIn("previous_response_id", self.posts()[-1])
        self.assertIn("UPDATED_PLUGIN_DESCRIPTION", self.posts()[-1]["input"])
        self.assertNotIn("Search plugin data", self.posts()[-1]["input"])

    def test_discovery_retry_replays_final_call_without_loading_again(self):
        self.replies = [(200, answer_frames(load_tools(FETCH_TOOL), "resp_discovery")),
                        (200, answer_frames(f'<{FETCH_TOOL}>{{"path":"notes"}}</{FETCH_TOOL}>', "resp_final"))]
        first = self.request(tools=TOOLS + PLUGIN_TOOLS, prompt_cache_key="discovery-replay")
        second = self.request(tools=TOOLS + PLUGIN_TOOLS, prompt_cache_key="discovery-replay")
        self.assertEqual(first, second)
        self.assertEqual(len(self.posts()), 2)

    def test_discovery_snapshot_loss_rebuilds_full_reduced_prompt_once(self):
        self.replies = [(200, answer_frames(load_tools(FETCH_TOOL), "resp_discovery")),
                        (400, b'{"error":{"message":"previous response not found"}}'),
                        (200, answer_frames(f'<{FETCH_TOOL}>{{"path":"notes"}}</{FETCH_TOOL}>', "resp_restored"))]
        result = self.request(tools=TOOLS + PLUGIN_TOOLS)
        self.assertEqual(result["id"], "resp_restored")
        self.assertNotIn("previous_response_id", self.posts()[-1])
        self.assertIn("USER:\n你好", self.posts()[-1]["input"])
        self.assertIn("Fetch plugin data", self.posts()[-1]["input"])
        self.assertNotIn("Search plugin data", self.posts()[-1]["input"])
        self.assertEqual(len(self.posts()), 3)

    def test_already_loaded_plugin_cannot_trigger_redundant_definition_requests(self):
        self.replies = [(200, answer_frames(load_tools(FETCH_TOOL), "resp_discovery")),
                        (200, answer_frames(load_tools(FETCH_TOOL), "resp_repeated"))]
        with self.assertRaises(GeminiWebError) as error:
            self.request(tools=TOOLS + PLUGIN_TOOLS)
        self.assertEqual(error.exception.code, "gemini_web_tool_format_invalid")
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(self.provider._checkpoints, {})

    def test_unsupported_output_is_rejected_before_internal_schema_loading(self):
        raw = answer_frames(load_tools(FETCH_TOOL))
        events = []
        for line in raw.decode().splitlines():
            if line.startswith("data: {"):
                event = json.loads(line[6:])
                if event["type"] == "response.completed":
                    event["response"]["output"].append({"type": "function_call", "name": "untrusted"})
                events.append(event)
        invalid = b"".join(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode()
                           for event in events)
        self.replies = [(200, invalid)]
        with self.assertRaises(GeminiWebError):
            self.request(tools=TOOLS + PLUGIN_TOOLS)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.provider._checkpoints, {})

    def test_only_explicit_expired_service_snapshot_restarts_with_full_history(self):
        self.replies = [(200, answer_frames("第一轮")),
                        (400, json.dumps({"error": {"message": "未知会话标识"}}, ensure_ascii=False).encode()),
                        (200, answer_frames("第二轮", "resp_web_2"))]
        first = self.request()
        result = self.request(previous_response_id=first["id"], input="继续")
        self.assertEqual(result["id"], "resp_web_2")
        self.assertNotIn("previous_response_id", self.posts()[-1])
        self.assertIn("你好", self.posts()[-1]["input"])
        self.assertIn("第一轮", self.posts()[-1]["input"])
        self.assertIn("继续", self.posts()[-1]["input"])

    def test_auth_and_quota_errors_are_not_retried_and_never_echo_secrets(self):
        for status in (401, 403, 429, 503):
            with self.subTest(status=status):
                self.replies = [(status, b'{"error":"COOKIE_SECRET PROMPT_SECRET"}')]
                with self.assertRaises(GeminiWebError) as error:
                    self.request()
                self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(len(self.posts()), 4)

    def test_truncated_stream_does_not_commit_a_checkpoint(self):
        self.replies = [(200, b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_broken"}}\n\n')]
        with self.assertRaises(GeminiWebError):
            self.request()
        self.assertNotIn("resp_broken", self.provider._checkpoints)

    def test_sparse_history_after_bridge_restart_fails_explicitly(self):
        with self.assertRaises(GeminiWebError) as error:
            self.request(previous_response_id="resp_missing", input=[{"type": "function_call_output", "output": "ok"}])
        self.assertEqual(error.exception.code, "gemini_web_history_missing")
        self.assertEqual(self.posts(), [])

    def test_editing_full_history_starts_a_new_branch(self):
        self.replies = [(200, answer_frames("原回复")), (200, answer_frames("新回复", "resp_web_2"))]
        first = self.request()
        self.request(previous_response_id=first["id"], input=[{"role": "user", "content": "编辑的问题"},
                                                           {"role": "assistant", "content": "分支回复"},
                                                           {"role": "user", "content": "继续"}])
        self.assertNotIn("previous_response_id", self.posts()[-1])
        self.assertNotIn("你好", self.posts()[-1]["input"])

    def test_finished_same_conversation_retry_replays_the_same_call_ids(self):
        self.replies = [(200, answer_frames('<exec_command>{"cmd":"echo ok"}</exec_command>'))]
        first = self.request(tools=TOOLS, prompt_cache_key="conversation-test")
        second = self.request(tools=TOOLS, prompt_cache_key="conversation-test")
        self.assertEqual(first, second)
        self.assertEqual(len(self.posts()), 1)

    def test_compaction_preserves_user_messages_and_summary(self):
        self.replies = [(200, answer_frames("已完成步骤一，下一步测试"))]
        result = self.provider.compact({"model": MODEL, "input": [{"role": "user", "content": "构建项目"}], "tools": TOOLS})
        self.assertEqual(result["object"], "response.compaction")
        self.assertIn("构建项目", json.dumps(result, ensure_ascii=False))
        self.assertIn("下一步测试", json.dumps(result, ensure_ascii=False))

    def test_unsupported_images_and_reasoning_are_explicit_errors(self):
        for extra in ({"input": [{"type": "message", "content": [{"type": "input_image", "image_url": "data:image/png;base64,AA=="}]}]},
                      {"reasoning": {"effort": "xhigh"}}, {"tools": [{"type": "namespace", "name": "functions"}]}):
            with self.subTest(extra=extra), self.assertRaises(GeminiWebError):
                self.request(**extra)
        self.assertEqual(self.posts(), [])

    def test_sidecar_transport_does_not_use_global_proxy_settings(self):
        self.replies = [(200, answer_frames("ok"))]
        with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1"}):
            self.assertEqual(self.request()["status"], "completed")

    def test_waiting_upstream_keeps_codex_stream_alive(self):
        self.replies = [(200, answer_frames("最终结果"))]
        self.reply_gate = threading.Event()
        with patch("fanvpn_bridge.gemini_web._HEARTBEAT_SECONDS", 0.05):
            _, events = self.provider.responses({"model": MODEL, "input": "hello", "stream": True})
            self.assertEqual(next(events), b": gemini-web waiting\n\n")
            self.reply_gate.set()
            self.assertIn(b"response.completed", b"".join(events))

    def test_cancelling_stream_closes_upstream_and_never_commits_partial_history(self):
        raw = answer_frames("最终结果")
        self.replies = [(200, raw)]
        self.reply_prefix = raw.index(b"\n\n") + 2
        self.reply_gate = threading.Event()
        _, events = self.provider.responses({"model": MODEL, "input": "hello", "stream": True})
        self.assertIn(b"response.created", next(events))
        workers = [thread for thread in threading.enumerate() if thread.name == "gemini-web-response"]
        events.close()
        for worker in workers:
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.provider._checkpoints, {})
