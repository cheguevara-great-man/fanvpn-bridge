from __future__ import annotations

import copy
import base64
import http.client
import json
import re
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fanvpn_bridge.muse_service import MuseSessions, ServiceError, make_server
from fanvpn_bridge.muse_auth import MuseAuthError
from fanvpn_bridge.muse_web import MuseWebError, MuseWebProvider

KEY = "test-only-" + "a" * 40
MODEL = "muse-web/default"
TOOLS = [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
         {"type": "custom", "name": "apply_patch", "description": "Edit files"}]


class FakeMuse:
    def __init__(self):
        self.conversations = {}
        self.sent = []
        self.replies = []
        self.closed = 0
        self.connections = 0
        self.open_hook = lambda: None
        self.lock = threading.Lock()
        self.files = {}

    def connect(self):
        self.connections += 1
        owner = self

        class Gateway:
            pending = None

            def call_json(self, method, *, body, timeout):
                if method == "fs.write":
                    if not body["overwrite"] and body["path"] in owner.files:
                        raise FileExistsError()
                    owner.files[body["path"]] = base64.b64decode(body["data_base64"])
                    return {}
                if method == "fs.rename":
                    if not body["overwrite"] and body["destination"] in owner.files:
                        raise FileExistsError()
                    owner.files[body["destination"]] = owner.files.pop(body["source"])
                    return {}
                if method == "fs.read":
                    if body["path"] not in owner.files:
                        error = RuntimeError("not found")
                        error.status = 404
                        raise error
                    return {"data_base64": base64.b64encode(owner.files[body["path"]]).decode(), "eof": True}
                if method == "session.start":
                    with owner.lock:
                        session_id = "session_" + str(len(owner.conversations))
                        owner.conversations[session_id] = []
                    return {"session_id": session_id}
                events = owner.conversations[body["session_id"]]
                if self.pending:
                    events[-1]["payload"].update(self.pending.pop(0))
                return {"chat_events": copy.deepcopy(events)}

            def _open(self, method, *, body):
                owner.sent.append(copy.deepcopy(body))
                events = owner.conversations[body["session_id"]]
                reply = owner.replies.pop(0) if owner.replies else "Hello from Muse"
                if isinstance(reply, dict):
                    run = re.search(r"Current run: ([0-9a-f]{32})", body["items"][0]["text"])[1]
                    manifest_path = next(path for path in owner.files if path.endswith(f"/{run}/manifest.json"))
                    request = {"schema_version": 1, "run": run, **reply}
                    owner.files[manifest_path.replace("manifest.json", "request.json")] = json.dumps(request).encode()
                    reply = "Submitted to Codex; awaiting the result."
                if isinstance(reply, Exception):
                    raise reply
                updates = reply if isinstance(reply, list) else [{"display_text": reply, "status": "completed"}]
                events.append({"seq": len(events) + 1, "event_name": "message.assistant", "payload": {}})
                self.pending = list(updates)
                owner.open_hook()
                return 1

            def close(self):
                owner.closed += 1

        return Gateway()


class MuseSessionTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMuse()
        self.sessions = MuseSessions(self.fake.connect, timeout=0.1, poll=0.001)

    def ask(self, **extra):
        return self.sessions.generate({"model": "default", "input": "Hello", **extra})

    def test_isolated_sessions_and_continuation(self):
        first = self.ask()
        second = self.ask(previous_response_id=first["id"], input="Continue")
        other = self.ask()
        self.assertEqual(self.fake.sent[0]["session_id"], self.fake.sent[1]["session_id"])
        self.assertNotEqual(self.fake.sent[0]["session_id"], self.fake.sent[2]["session_id"])
        self.assertEqual(len({first["id"], second["id"], other["id"]}), 3)
        self.assertIsNone(first["usage"])
        self.assertEqual(self.fake.closed, 3)

    def test_only_explicit_completed_status_is_accepted(self):
        self.fake.replies = [[{"display_text": "partial", "status": "in_progress"},
                              {"display_text": "final", "status": "completed"}]]
        result = self.ask()
        self.assertEqual(result["output"][0]["content"][0]["text"], "final")

    def test_parallel_agents_use_different_sessions_and_connections(self):
        barrier = threading.Barrier(3)
        self.fake.open_hook = lambda: barrier.wait(2)
        self.sessions.timeout = 3
        with ThreadPoolExecutor(max_workers=3) as pool:
            replies = list(pool.map(lambda index: self.ask(input=f"Agent {index}"), range(3)))
        self.assertEqual(len({reply["id"] for reply in replies}), 3)
        self.assertEqual(len({body["session_id"] for body in self.fake.sent}), 3)
        self.assertEqual(self.fake.connections, 3)
        self.assertEqual(self.fake.closed, 3)

    def test_background_push_is_not_a_reply(self):
        self.fake.replies = [[{"display_text": "background", "status": "completed", "reply_to_message_id": "self"},
                              {"display_text": "actual", "status": "completed", "reply_to_message_id": ""}]]
        self.assertEqual(self.ask()["output"][0]["content"][0]["text"], "actual")

    def test_ambiguous_send_is_never_retried_and_exception_is_redacted(self):
        first = self.ask()
        self.fake.replies = [RuntimeError("SECRET_COOKIE_VALUE")]
        with self.assertRaises(ServiceError) as caught:
            self.ask(previous_response_id=first["id"])
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(len(self.fake.sent), 2)
        with self.assertRaisesRegex(ServiceError, "Previous response not found"):
            self.ask(previous_response_id=first["id"])
        self.assertEqual(len(self.fake.sent), 2)

    def test_old_snapshot_and_evicted_snapshot_fail_before_sending(self):
        first = self.ask()
        second = self.ask(previous_response_id=first["id"])
        with self.assertRaisesRegex(ServiceError, "Previous response not found"):
            self.ask(previous_response_id=first["id"])
        self.sessions.max_snapshots = 1
        self.ask()
        with self.assertRaisesRegex(ServiceError, "Previous response not found"):
            self.ask(previous_response_id=second["id"])
        self.assertEqual(len(self.fake.sent), 3)

    def test_inflight_conversation_is_not_submitted_twice(self):
        first = self.ask()
        entered, release = threading.Event(), threading.Event()
        self.fake.open_hook = lambda: (entered.set(), release.wait(2))
        errors = []

        def worker():
            try:
                self.ask(previous_response_id=first["id"])
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaisesRegex(ServiceError, "already generating"):
                self.ask(previous_response_id=first["id"])
        finally:
            release.set()
            thread.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.fake.sent), 2)

    def test_timeout_invalidates_session_without_resend(self):
        first = self.ask()
        self.fake.replies = [[{"display_text": "not final", "status": "in_progress"}]]
        with self.assertRaisesRegex(ServiceError, "timed out"):
            self.ask(previous_response_id=first["id"])
        self.assertFalse(self.sessions.snapshots[first["id"]].valid)
        self.assertEqual(len(self.fake.sent), 2)

    def test_cancellation_invalidates_session_and_closes_gateway(self):
        first = self.ask()

        def disconnected():
            if len(self.fake.sent) == 2:
                raise ConnectionAbortedError()

        with self.assertRaises(ConnectionAbortedError):
            self.sessions.generate({"model": "default", "input": "Next", "previous_response_id": first["id"]}, disconnected)
        self.assertFalse(self.sessions.snapshots[first["id"]].valid)
        self.assertEqual(self.fake.closed, 2)

    def test_invalid_body_and_capacity_do_not_open_gateway(self):
        for body in ({"model": "other", "input": "hello"}, {"model": "default", "input": ""},
                     {"model": "default", "input": "hi", "tools": TOOLS}):
            with self.assertRaises(ServiceError):
                self.sessions.generate(body)
        constrained = MuseSessions(self.fake.connect, max_parallel=1)
        constrained.slots.acquire()
        try:
            with self.assertRaises(ServiceError) as caught:
                constrained.generate({"model": "default", "input": "hello"})
            self.assertEqual(caught.exception.status, 429)
        finally:
            constrained.slots.release()
        self.assertEqual(self.fake.connections, 0)


class MuseWebTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMuse()
        self.sessions = MuseSessions(self.fake.connect, timeout=1, poll=0.001,
                                     tool_workspace="workspace/custom-skills/codex_test")
        self.server = make_server({"api_key": KEY, "port": 0}, self.sessions)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.provider = MuseWebProvider(base_url=self.url, api_key=KEY, timeout_seconds=2)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def ask(self, **extra):
        streaming, result = self.provider.responses({"model": MODEL, "input": "Hello", **extra})
        return list(result) if streaming else result

    def test_catalog_is_truthful_and_does_not_read_login(self):
        result = self.provider.models_response()
        self.assertEqual([row["id"] for row in result["data"]], [MODEL])
        self.assertEqual(result["data"][0]["supported_reasoning_levels"], [])
        self.assertFalse(result["data"][0]["supports_search_tool"])
        self.assertEqual(self.fake.connections, 0)
        with self.assertRaises(MuseWebError):
            self.ask(reasoning={"effort": "high"})
        self.assertEqual(self.fake.sent, [])

    def test_disabled_no_probe_and_no_insecure_urls(self):
        with patch("http.client.HTTPConnection", side_effect=AssertionError("unexpected probe")):
            self.assertEqual(MuseWebProvider().models_response()["data"], [])
            with self.assertRaises(MuseWebError):
                MuseWebProvider().responses({"model": MODEL})
        for url in ("http://localhost:8008/v1", "http://example.com/v1", "https://127.0.0.1/v1",
                    "http://127.0.0.1/v1?secret=x"):
            with self.assertRaises(ValueError):
                MuseWebProvider(base_url=url, api_key=KEY)
        with self.assertRaises(ValueError):
            MuseWebProvider(base_url=self.url)

    def test_auth_origin_and_body_size(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        for headers in ({}, {"Authorization": "Bearer wrong"},
                        {"Authorization": "Bearer " + KEY, "Origin": "https://evil.invalid"}):
            connection.request("GET", "/v1/models", headers=headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
        connection.request("POST", "/v1/responses", headers={"Authorization": "Bearer " + KEY, "Content-Length": "99999999"})
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        response.read()
        connection.close()
        self.assertEqual(self.fake.connections, 0)

    def test_plain_stream_has_terminal_event_and_final_text(self):
        frames = self.ask(stream=True)
        self.assertTrue(any(b"response.output_text.delta" in frame for frame in frames))
        self.assertTrue(any(b"response.completed" in frame for frame in frames))
        self.assertIn(b"Hello from Muse", b"".join(frames))
        completed = next(json.loads(frame.decode().split("data: ", 1)[1]) for frame in frames
                         if frame.startswith(b"event: response.completed\n"))
        self.assertIsNone(completed["response"]["usage"])

    def test_optional_hosted_search_only_does_not_require_workspace(self):
        self.sessions.workspace = None
        result = self.ask(tools=[{"type": "web_search"}])
        self.assertEqual(result["output"][0]["content"][0]["text"], "Hello from Muse")
        self.assertEqual(self.fake.files, {})

    def test_tool_call_and_result_continue_same_muse_session(self):
        self.fake.replies = [{"name": "exec_command", "arguments": {"cmd": "echo safe"}}, "Done"]
        first = self.ask(tools=TOOLS)
        self.assertIsNone(first["usage"])
        self.assertNotIn("SYSTEM / DEVELOPER INSTRUCTIONS", self.fake.sent[0]["items"][0]["text"])
        self.assertIn("not a Muse system message", self.fake.sent[0]["items"][0]["text"])
        call = first["output"][0]
        self.assertEqual(call["type"], "function_call")
        self.assertEqual(json.loads(call["arguments"]), {"cmd": "echo safe"})
        self.ask(tools=TOOLS, previous_response_id=first["id"], input=[{
            "type": "function_call_output", "call_id": call["call_id"], "output": "safe",
        }])
        self.assertEqual(self.fake.sent[0]["session_id"], self.fake.sent[1]["session_id"])
        result = next(json.loads(raw) for path, raw in self.fake.files.items() if path.endswith("/result.json"))
        self.assertEqual(result["result"]["output"], "safe")
        self.assertIn("result --run", self.fake.sent[1]["items"][0]["text"])

    def test_long_chinese_namespaced_freeform_is_exact(self):
        patch_text = "*** Begin Patch\n*** Add File: example.md\n+" + "中文 `code` | 表格\n+" * 500 + "end\n*** End Patch"
        self.fake.replies = [{"name": "functions.apply_patch", "input": patch_text}]
        tools = [{"type": "namespace", "name": "functions", "tools": [TOOLS[1]]}]
        result = self.ask(tools=tools)
        call = result["output"][0]
        self.assertEqual((call["namespace"], call["name"], call["input"]), ("functions", "apply_patch", patch_text))

    def test_namespaced_freeform_stream_ids_match_completed_call_and_can_continue(self):
        patch_text = "*** Begin Patch\n*** End Patch\n"
        self.fake.replies = [{"name": "functions.apply_patch", "input": patch_text}, "Final"]
        tools = [{"type": "namespace", "name": "functions", "tools": [TOOLS[1]]}]
        frames = self.ask(tools=tools, stream=True)
        events = [json.loads(frame.decode().split("data: ", 1)[1]) for frame in frames if frame.startswith(b"event:")]
        final = events[-2]["response"] if events[-1]["type"] != "response.completed" else events[-1]["response"]
        call = final["output"][0]
        for event in events:
            if "item" in event:
                self.assertEqual(event["item"]["id"], call["id"])
                self.assertEqual(event["item"]["call_id"], call["call_id"])
                self.assertEqual(event["item"]["namespace"], "functions")
                self.assertEqual(event["item"]["name"], "apply_patch")
            if "item_id" in event:
                self.assertEqual(event["item_id"], call["id"])
        self.ask(tools=tools, previous_response_id=final["id"], input=[{
            "type": "custom_tool_call_output", "call_id": call["call_id"], "output": "synthetic only",
        }])
        self.assertEqual(len({body["session_id"] for body in self.fake.sent}), 1)

    def test_tool_result_with_lost_checkpoint_or_changed_tools_is_not_replayed(self):
        self.fake.replies = [{"name": "exec_command", "arguments": {"cmd": "inert"}}]
        first = self.ask(tools=TOOLS)
        result = {"type": "function_call_output", "call_id": first["output"][0]["call_id"], "output": "synthetic"}
        with self.assertRaises(MuseWebError):
            self.ask(tools=TOOLS[:1], previous_response_id=first["id"], input=[result])
        self.provider._checkpoints.clear()
        with self.assertRaises(MuseWebError):
            self.ask(tools=TOOLS, previous_response_id=first["id"], input=[result])
        self.assertEqual(len(self.fake.sent), 1)

    def test_malformed_output_fails_without_repair(self):
        self.fake.replies = [{"name": "apply_patch", "arguments": {"input": "invalid"}}]
        with self.assertRaises(MuseWebError) as caught:
            self.ask(tools=TOOLS)
        self.assertEqual(caught.exception.code, "muse_web_tool_format_invalid")
        self.assertEqual(len(self.fake.sent), 1)

    def test_tool_directory_and_result_keep_the_same_session(self):
        name = "mcp__example.fetch"
        self.fake.replies = [{"name": name, "arguments": {"path": "read-only"}}, "Done"]
        tools = [{"type": "namespace", "name": "mcp__example", "tools": [{"type": "function", "name": "fetch",
                  "description": "Fetch a read-only item", "parameters": {"type": "object"}}]}]
        result = self.ask(tools=tools)
        call = result["output"][0]
        self.assertEqual((call["namespace"], call["name"]), ("mcp__example", "fetch"))
        self.ask(tools=tools, previous_response_id=result["id"], input=[{
            "type": "function_call_output", "call_id": call["call_id"], "output": "safe result",
        }])
        self.assertEqual(len(self.fake.sent), 2)
        self.assertEqual(len({body["session_id"] for body in self.fake.sent}), 1)
        self.assertNotIn("Fetch a read-only item", self.fake.sent[0]["items"][0]["text"])
        manifest = next(json.loads(raw) for path, raw in self.fake.files.items() if path.endswith("/manifest.json"))
        self.assertEqual(manifest["tools"][0]["name"], name)
        self.assertEqual(manifest["tools"][0]["description"], "Fetch a read-only item")

    def test_missing_service_snapshot_rebuilds_full_history_once(self):
        first = self.ask()
        self.sessions.snapshots.clear()
        self.ask(previous_response_id=first["id"], input="Next")
        self.assertNotEqual(self.fake.sent[0]["session_id"], self.fake.sent[1]["session_id"])
        self.assertIn("Hello from Muse", self.fake.sent[1]["items"][0]["text"])

    def test_muse_failures_do_not_expose_credentials_or_claim_completion(self):
        self.fake.replies = [RuntimeError("TOKEN=SECRET")]
        with self.assertRaises(MuseWebError) as caught:
            self.ask(stream=True)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("Gemini", str(caught.exception))
        self.assertEqual(len(self.fake.sent), 1)

    def test_browser_login_errors_survive_sse_heartbeats(self):
        def failed_login():
            error = MuseAuthError("muse_web_login_required")
            raise ServiceError(str(error), error.status, error.code)

        self.sessions.factory = failed_login
        for stream in (False, True):
            with self.subTest(stream=stream), self.assertRaises(MuseWebError) as caught:
                self.ask(stream=stream)
            self.assertEqual(caught.exception.status, 401)
            self.assertEqual(caught.exception.code, "muse_web_login_required")
            self.assertIn("Log in to Muse", str(caught.exception))
        self.assertEqual(self.fake.connections, 0)
        self.assertEqual(self.fake.sent, [])


if __name__ == "__main__":
    unittest.main()
