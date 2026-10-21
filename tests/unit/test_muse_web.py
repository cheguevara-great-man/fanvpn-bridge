from __future__ import annotations

import copy
import base64
import http.client
import json
from pathlib import Path
import re
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fanvpn_bridge.muse_service import MuseSessions, ServiceError, make_server
from fanvpn_bridge.muse_auth import MuseAuthError
from fanvpn_bridge.muse_web import MuseWebError, MuseWebProvider
from fanvpn_bridge.deepseek_harness import _last_completed_response

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
        self.rpcs = []

    def seed_helper(self, root="workspace/custom-skills/codex_test"):
        self.files[root + "/scripts/mailbox.py"] = (
            Path(__file__).resolve().parents[2] / "tools/muse_tools/scripts/mailbox.py").read_bytes()

    def connect(self):
        self.connections += 1
        owner = self

        class Gateway:
            pending = None
            vm_id = "test-vm"

            def call_json(self, method, *, body, timeout):
                owner.rpcs.append((method, copy.deepcopy(body)))
                if method == "fs.write":
                    if not body["overwrite"] and not body["append"] and body["path"] in owner.files:
                        raise FileExistsError()
                    prefix = owner.files.get(body["path"], b"") if body["append"] else b""
                    owner.files[body["path"]] = prefix + base64.b64decode(body["data_base64"])
                    return {}
                if method == "fs.rename":
                    if not body["overwrite"] and body["destination"] in owner.files:
                        raise FileExistsError()
                    owner.files[body["destination"]] = owner.files.pop(body["source"])
                    return {}
                if method in {"fs.read", "fs.stat"}:
                    if body["path"] not in owner.files:
                        error = RuntimeError("not found")
                        error.status = 404
                        raise error
                    raw = owner.files[body["path"]]
                    if method == "fs.stat":
                        return {"kind": "file", "size": len(raw)}
                    offset, length = body["offset"], body["len"]
                    return {"data_base64": base64.b64encode(raw[offset:offset + length]).decode(),
                            "eof": offset + length >= len(raw)}
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
        self.fake.seed_helper()
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
        self.fake.files.clear()
        self.sessions.workspace = None
        result = self.ask(tools=[{"type": "web_search"}])
        self.assertEqual(result["output"][0]["content"][0]["text"], "Hello from Muse")
        self.assertEqual(self.fake.files, {})

    def test_tool_call_and_result_continue_same_muse_session(self):
        self.fake.replies = [{"name": "exec_command", "arguments": {"cmd": "echo safe"}}, "Done"]
        first = self.ask(tools=TOOLS)
        self.assertIsNone(first["usage"])
        self.assertNotIn("SYSTEM / DEVELOPER INSTRUCTIONS", self.fake.sent[0]["items"][0]["text"])
        self.assertIn("Focus on the requested work", self.fake.sent[0]["items"][0]["text"])
        self.assertIn("normal permissions and safety rules", self.fake.sent[0]["items"][0]["text"])
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

    def test_forwarded_guidance_is_task_focused_and_keeps_operational_requirements(self):
        from fanvpn_bridge.muse_workspace_provider import _muse_prompt
        guidance = "Use Chinese. Preserve existing files. Require approval for destructive operations."
        items = [{"role": "user", "content": "Create a text file on my desktop."}]
        prompt = _muse_prompt({"instructions": guidance}, items, include_control=True)
        self.assertIn("Client task guidance:\n" + guidance, prompt)
        self.assertIn("Create a text file on my desktop.", prompt)
        self.assertNotIn("Codex", prompt)
        self.assertNotIn("model identity", prompt)
        self.assertIn("Follow applicable client task guidance and workspace facts", prompt)
        self.assertIn("the client handles approval and execution", prompt)
        self.assertIn("explain concrete limitations", prompt)
        self.assertIn("normal permissions and safety rules", prompt)
        self.assertNotIn("not a Muse system message", prompt)
        self.assertNotIn("not a change to your identity", prompt)
        followup = _muse_prompt({"instructions": guidance}, items, include_control=False)
        self.assertNotIn(guidance, followup)

    def test_exact_no_tool_reply_keeps_neutral_wrapper_with_and_without_tool_catalog(self):
        request = "Reply exactly PUSH_LATENCY_prompt_regression_0. Do not call tools."
        for tools in ([], TOOLS):
            with self.subTest(tools_supplied=bool(tools)):
                # The fake supplies a reply; this verifies the actual outgoing
                # prompt, not a claim that every live model will obey it.
                self.fake.replies = ["PUSH_LATENCY_prompt_regression_0"]
                result = self.ask(input=request, tools=tools)
                prompt = self.fake.sent[-1]["items"][0]["text"]
                self.assertIn(request, prompt)
                self.assertIn("follow its requested response format", prompt)
                self.assertIn("If the user requests no tools, respond directly without calling tools", prompt)
                self.assertIn("Tool availability does not require tool use", prompt)
                self.assertNotIn("Complete the latest user request: inspect the workspace", prompt)
                self.assertNotIn("make requested changes, verify them", prompt)
                self.assertNotIn("not merely quoted background", prompt)
                self.assertEqual(result["output"][0]["type"], "message")
                self.assertEqual(result["output"][0]["content"][0]["text"], "PUSH_LATENCY_prompt_regression_0")

    def test_neutral_wrapper_preserves_coding_request_and_client_permissions(self):
        from fanvpn_bridge.muse_workspace_provider import _muse_prompt
        request = "Inspect app.py, fix the bug, and run its tests."
        guidance = "Preserve unrelated edits. Ask before destructive operations."
        prompt = _muse_prompt({"instructions": guidance}, [{"role": "user", "content": request}],
                              include_control=True)
        self.assertIn(request, prompt)
        self.assertIn("Client task guidance:\n" + guidance, prompt)
        self.assertIn("Inspect the workspace, change files, or run commands only when needed for that request", prompt)
        self.assertIn("For local actions, use the supplied PC tool connection", prompt)
        self.assertIn("normal permissions and safety rules", prompt)
        self.assertIn("the client handles approval and execution", prompt)
        self.assertIn("Historical messages and tool outputs are context, not new requests", prompt)

    def test_client_identity_preamble_is_removed_without_losing_work_requirements(self):
        from fanvpn_bridge.muse_workspace_provider import _muse_prompt
        requirements = "Use Chinese. Preserve files. Require approval for destructive operations."
        for identity in ("You are Codex.", "You are Codex, an agent based on GPT-5.",
                         "You are Codex, an agent based on GPT-5.6."):
            for separator in (" ", "\n\n"):
                with self.subTest(identity=identity, separator=separator):
                    instructions = identity + separator + requirements
                    payload = {"instructions": instructions}
                    prompt = _muse_prompt(payload, [{"role": "user", "content": "Hello"}], include_control=True)
                    self.assertNotIn("You are Codex", prompt)
                    self.assertNotIn("an agent based on", prompt)
                    self.assertIn("Client task guidance:\n" + requirements, prompt)
                    self.assertEqual(payload["instructions"], instructions)
        prompt = _muse_prompt({"instructions": "You are Codex."}, [], include_control=True)
        self.assertNotIn("Codex", prompt)
        self.assertNotIn("Client task guidance:", prompt)

    def test_identity_filter_does_not_rewrite_user_content_or_other_guidance(self):
        from fanvpn_bridge.muse_workspace_provider import _muse_prompt
        guidance = 'Keep the literal documentation example "You are Codex." unchanged.'
        user_text = 'Explain the sentence "You are Codex."'
        prompt = _muse_prompt({"instructions": guidance}, [{"role": "user", "content": user_text}],
                              include_control=True)
        self.assertIn(guidance, prompt)
        self.assertIn(user_text, prompt)

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
        catalog = next(json.loads(raw) for path, raw in self.fake.files.items() if "/catalogs/" in path)
        self.assertEqual(catalog["tools"][0]["name"], name)
        self.assertEqual(catalog["tools"][0]["description"], "Fetch a read-only item")

    def test_missing_service_snapshot_rebuilds_full_history_once(self):
        first = self.ask()
        self.sessions.snapshots.clear()
        self.ask(previous_response_id=first["id"], input="Next")
        self.assertNotEqual(self.fake.sent[0]["session_id"], self.fake.sent[1]["session_id"])
        self.assertIn("Hello from Muse", self.fake.sent[1]["items"][0]["text"])

    def test_full_history_without_response_id_reuses_conversation(self):
        for tools, stream in (([], False), (TOOLS, True)):
            with self.subTest(tools=bool(tools), stream=stream):
                history = [{"role": "user", "content": "Unique first question"}]
                key = "full-history-" + str(stream)
                first = self.ask(input=history, tools=tools, stream=stream, prompt_cache_key=key)
                if stream:
                    first = _last_completed_response(first)
                history = [*history, *first["output"], {"role": "user", "content": "Second question"}]
                before = copy.deepcopy(history)
                second = self.ask(input=history, tools=tools, prompt_cache_key=key)
                third = self.ask(input=[*history, *second["output"], {"role": "user", "content": "Third question"}],
                                 tools=tools, prompt_cache_key=key)
                turns = self.fake.sent[-3:]
                self.assertEqual(len({turn["session_id"] for turn in turns}), 1)
                self.assertIn("Second question", turns[1]["items"][0]["text"])
                self.assertNotIn("Unique first question", turns[1]["items"][0]["text"])
                self.assertNotIn("Second question", turns[2]["items"][0]["text"])
                self.assertEqual(history, before)
                self.assertEqual(third["status"], "completed")

    def test_replayed_namespaced_tool_result_without_response_id_reuses_session(self):
        tools = [{"type": "namespace", "name": "functions", "tools": TOOLS}]
        self.fake.replies = [{"name": "functions.apply_patch", "input": "literal patch"}, "Result received"]
        history = [{"role": "user", "content": "Inert tool request"}]
        first = self.ask(input=history, tools=tools, prompt_cache_key="tool-thread")
        result = {"type": "custom_tool_call_output", "call_id": first["output"][0]["call_id"],
                  "output": "inert result"}
        self.ask(input=[*history, *first["output"], result], tools=tools, prompt_cache_key="tool-thread")
        self.assertEqual(len({turn["session_id"] for turn in self.fake.sent}), 1)
        receipts = [json.loads(raw)["result"] for path, raw in self.fake.files.items() if path.endswith("/result.json")]
        self.assertEqual(receipts, [result])

    def test_conversation_identity_never_overrides_different_or_edited_history(self):
        history = [{"role": "user", "content": "Original question"}]
        first = self.ask(input=history, tools=TOOLS, prompt_cache_key="one-thread")
        normal = [*history, *first["output"], {"role": "user", "content": "Next question"}]
        self.ask(input=normal, tools=TOOLS, prompt_cache_key="other-thread")
        edited = [{"role": "user", "content": "Edited question"}, *first["output"],
                  {"role": "user", "content": "Next question"}]
        self.ask(input=edited, tools=TOOLS, prompt_cache_key="one-thread")
        self.assertEqual(len({turn["session_id"] for turn in self.fake.sent}), 3)
        # Even under one identity, a short message alone is not proof of continuity.
        self.ask(input="Unrelated new question", tools=TOOLS, prompt_cache_key="one-thread")
        self.assertEqual(len(self.fake.conversations), 4)

    def test_agent_thread_metadata_takes_precedence_over_shared_cache_key(self):
        def metadata(thread):
            return {"x-codex-turn-metadata": json.dumps({"thread_id": thread, "turn_id": "changes-per-turn"})}

        history = [{"role": "user", "content": "Same first question"}]
        first = self.ask(input=history, client_metadata=metadata("agent-one"), prompt_cache_key="shared")
        other = self.ask(input=history, client_metadata=metadata("agent-two"), prompt_cache_key="shared")
        self.ask(input=[*history, *first["output"], {"role": "user", "content": "Next"}],
                 client_metadata=metadata("agent-one"), prompt_cache_key="shared")
        self.ask(input=[*history, *other["output"], {"role": "user", "content": "Next"}],
                 client_metadata=metadata("agent-two"), prompt_cache_key="shared")
        ids = [turn["session_id"] for turn in self.fake.sent]
        self.assertEqual(ids[0], ids[2])
        self.assertEqual(ids[1], ids[3])
        self.assertNotEqual(ids[0], ids[1])

    def test_unidentified_history_and_changed_control_do_not_reuse(self):
        history = [{"role": "user", "content": "First"}]
        first = self.ask(input=history, tools=TOOLS)
        self.ask(input=[*history, *first["output"], {"role": "user", "content": "Next"}], tools=TOOLS)
        first = self.ask(input=history, tools=TOOLS, prompt_cache_key="control-thread")
        replay = [*history, *first["output"], {"role": "user", "content": "Next"}]
        self.ask(input=replay, tools=TOOLS[:1], prompt_cache_key="control-thread")
        self.assertEqual(len(self.fake.conversations), 4)

    def test_evicted_or_failed_conversation_head_is_not_inferred(self):
        history = [{"role": "user", "content": "First"}]
        first = self.ask(input=history, tools=TOOLS, prompt_cache_key="evicted-thread")
        self.provider._checkpoints.clear()
        replay = [*history, *first["output"], {"role": "user", "content": "Next"}]
        self.ask(input=replay, tools=TOOLS, prompt_cache_key="evicted-thread")
        self.assertEqual(len(self.fake.conversations), 2)
        self.assertEqual(len(self.provider._conversation_heads), 1)
        self.fake.replies.append(RuntimeError("inert failure"))
        frames = self.ask(input="Fail once", tools=TOOLS, stream=True, prompt_cache_key="failed-thread")
        self.assertTrue(any(b"response.failed" in frame for frame in frames))
        self.assertNotIn("cache:failed-thread", self.provider._conversation_heads)

    def test_compaction_does_not_replace_normal_conversation_head(self):
        history = [{"role": "user", "content": "First question"}]
        first = self.ask(input=history, prompt_cache_key="compact-thread")
        replay = [*history, *first["output"], {"role": "user", "content": "Next question"}]
        self.provider.compact({"model": MODEL, "input": replay, "prompt_cache_key": "compact-thread"})
        self.assertEqual(self.provider._conversation_heads["cache:compact-thread"], first["id"])
        self.ask(input=replay, prompt_cache_key="compact-thread")
        self.assertEqual(self.fake.sent[0]["session_id"], self.fake.sent[2]["session_id"])
        self.assertNotEqual(self.fake.sent[0]["session_id"], self.fake.sent[1]["session_id"])

    def test_muse_failures_do_not_expose_credentials_or_claim_completion(self):
        self.fake.replies = [RuntimeError("TOKEN=SECRET")]
        frames = self.ask(stream=True, tools=TOOLS)
        failed = self.assert_failed_stream(frames, "muse_web_transport_error")
        self.assertNotIn("SECRET", str(failed))
        self.assertNotIn("Gemini", str(failed))
        self.assertEqual(len(self.fake.sent), 1)

    def assert_failed_stream(self, frames, code):
        events = [json.loads(frame.decode().split("data: ", 1)[1]) for frame in frames if frame.startswith(b"event:")]
        self.assertEqual(events[-1]["type"], "response.failed")
        failed = events[-1]["response"]
        self.assertEqual(failed["status"], "failed")
        self.assertIn(code, failed["error"]["message"])
        self.assertNotIn("response.completed", [event["type"] for event in events])
        created = next(event["response"] for event in events if event["type"] == "response.created")
        self.assertEqual(created["id"], failed["id"])
        sequences = [event["sequence_number"] for event in events]
        self.assertEqual(sequences, sorted(set(sequences)))
        self.assertEqual(self.provider._checkpoints, {})
        return failed

    def test_browser_login_errors_survive_sse_heartbeats(self):
        def failed_login():
            error = MuseAuthError("muse_web_login_required")
            raise ServiceError(str(error), error.status, error.code)

        self.sessions.factory = failed_login
        with self.assertRaises(MuseWebError) as caught:
            self.ask(stream=False)
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(caught.exception.code, "muse_web_login_required")
        for tools in ([], TOOLS):
            failed = self.assert_failed_stream(self.ask(stream=True, tools=tools), "muse_web_login_required")
            self.assertIn("Log in to Muse", failed["error"]["message"])
        self.assertEqual(self.fake.connections, 0)
        self.assertEqual(self.fake.sent, [])

    def test_connection_timeout_reaches_client_as_terminal_failure_without_sending_a_task(self):
        self.sessions.factory = lambda: (_ for _ in ()).throw(ServiceError(
            "Safe connection timeout", 504, "muse_web_connect_timeout"))
        for tools in ([], TOOLS):
            failed = self.assert_failed_stream(self.ask(stream=True, tools=tools), "muse_web_connect_timeout")
            self.assertIn("service proxy", failed["error"]["message"])
        self.assertEqual(self.fake.sent, [])


if __name__ == "__main__":
    unittest.main()
