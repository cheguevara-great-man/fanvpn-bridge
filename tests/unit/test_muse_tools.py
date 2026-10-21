from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from fanvpn_bridge.muse_service import MuseSessions, ServiceError
from fanvpn_bridge.muse_tools import MailboxError, PROTOCOL, WorkspaceTools, response_frames
from fanvpn_bridge.muse_tools_setup import install
from test_muse_web import FakeMuse, TOOLS

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = "workspace/custom-skills/codex_test"


class MailboxHelperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "scripts").mkdir()
        self.script = self.root / "scripts/mailbox.py"
        shutil.copyfile(ROOT / "tools/muse_tools/scripts/mailbox.py", self.script)
        self.run = "a" * 32
        self.directory = self.root / "runs" / self.run
        self.directory.mkdir(parents=True)
        self.manifest = {"schema_version": 1, "run": self.run, "expires_at": time.time() + 60, "tools": TOOLS}
        self.write("manifest", self.manifest)

    def write(self, kind, value):
        (self.directory / (kind + ".json")).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def invoke(self, action, value=None, *extra, run=None):
        return subprocess.run([sys.executable, str(self.script), action, "--run", run or self.run, *extra],
                              input=json.dumps(value, ensure_ascii=False).encode("utf-8") if value else b"",
                              capture_output=True, timeout=5)

    def test_directory_does_not_dump_all_tool_definitions(self):
        value = self.invoke("tools")
        self.assertEqual(value.returncode, 0, value.stderr)
        directory = json.loads(value.stdout)
        self.assertTrue(all(set(tool) == {"name", "type"} for tool in directory))
        value = self.invoke("tools", None, "--name", "exec_command")
        self.assertEqual(json.loads(value.stdout), TOOLS[0])

    def test_long_freeform_is_exact_and_request_cannot_be_overwritten(self):
        text = "*** Begin Patch\n*** Add File: 中文.md\n+" + '中文 `code` | "quotes" \\ 路径\n+' * 1000 + "\n*** End Patch\n"
        result = self.invoke("request", {"name": "apply_patch", "input": text})
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.directory / "request.json"
        before = path.read_bytes()
        self.assertEqual(json.loads(before)["input"], text)
        self.assertEqual(json.loads(result.stdout)["status"], "awaiting_codex")
        duplicate = self.invoke("request", {"name": "apply_patch", "input": "changed"})
        self.assertEqual(duplicate.returncode, 2)
        self.assertEqual(path.read_bytes(), before)

    def test_result_and_invalid_inputs(self):
        result = {"schema_version": 1, "run": self.run, "result": {"output": "literal result"}}
        self.write("result", result)
        self.assertEqual(json.loads(self.invoke("result").stdout), result)
        for value in ({"name": "unknown", "arguments": {}}, {"name": "exec_command", "arguments": "{}"},
                      {"name": "apply_patch", "arguments": {}}, {"name": "exec_command", "arguments": {}, "extra": 1},
                      {"name": "exec_command", "arguments": {"n": float("nan")}}):
            with self.subTest(value=value):
                self.assertEqual(self.invoke("request", value).returncode, 2)
                self.assertFalse((self.directory / "request.json").exists())
        self.assertEqual(self.invoke("tools", run="../outside").returncode, 2)

    def test_expired_request_is_rejected_but_result_remains_readable(self):
        self.manifest["expires_at"] = time.time() - 1
        self.write("manifest", self.manifest)
        result = self.invoke("request", {"name": "exec_command", "arguments": {}})
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.directory / "request.json").exists())


class WorkspaceSessionTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMuse()
        self.sessions = MuseSessions(self.fake.connect, timeout=0.2, poll=0.001, tool_workspace=WORKSPACE)
        self.body = {"model": "default", "input": "Inert tool test", "tools": TOOLS, "tool_protocol": PROTOCOL}

    def call(self, **extra):
        return self.sessions.generate({**self.body, **extra})

    def pending(self, custom=False):
        self.fake.replies.append({"name": "apply_patch", "input": "literal input"} if custom else
                                 {"name": "exec_command", "arguments": {"cmd": "not executed"}})
        response = self.call()
        item = response["output"][0]
        result = {"type": "custom_tool_call_output" if custom else "function_call_output",
                  "call_id": item["call_id"], "output": "synthetic result"}
        return response, result

    def test_matching_result_is_written_once_and_same_session_continues(self):
        response, result = self.pending()
        self.call(previous_response_id=response["id"], tool_results=[result])
        stored = [json.loads(raw) for path, raw in self.fake.files.items() if path.endswith("/result.json")]
        self.assertEqual([value["result"] for value in stored], [result])
        self.assertEqual(len({body["session_id"] for body in self.fake.sent}), 1)
        with self.assertRaises(ServiceError):
            self.call(previous_response_id=response["id"], tool_results=[result])
        self.assertEqual(len(self.fake.sent), 2)

    def test_wrong_missing_duplicate_changed_tools_and_cross_session_results_are_rejected_before_connect(self):
        first, result = self.pending()
        second, other = self.pending()
        connections = self.fake.connections
        cases = [[], [other], [result, result], [{**result, "type": "custom_tool_call_output"}],
                 [{**result, "output": {"unsafe": "shape"}}]]
        for values in cases:
            with self.subTest(results=values), self.assertRaises(ServiceError) as caught:
                self.call(previous_response_id=first["id"], tool_results=values)
            self.assertEqual(caught.exception.code, "muse_web_tool_result_invalid")
        with self.assertRaises(ServiceError):
            self.call(previous_response_id=first["id"], tool_results=[result], tools=TOOLS[:1])
        self.assertEqual(self.fake.connections, connections)
        self.assertTrue(self.sessions.snapshots[first["id"]].valid)
        self.call(previous_response_id=second["id"], tool_results=[other])

    def test_result_expiry_or_no_pending_call_never_opens_gateway(self):
        response, result = self.pending(custom=True)
        self.sessions.snapshots[response["id"]].pending["expires"] = 0
        for previous in (response["id"], None):
            with self.assertRaises(ServiceError):
                self.call(previous_response_id=previous, tool_results=[result])
        self.assertEqual(self.fake.connections, 1)

    def test_custom_output_list_and_sequential_calls(self):
        response, result = self.pending(custom=True)
        result["output"] = [{"type": "input_text", "text": "denied by Codex approval"}]
        self.fake.replies.append({"name": "exec_command", "arguments": {"cmd": "another inert call"}})
        next_response = self.call(previous_response_id=response["id"], tool_results=[result])
        self.assertNotEqual(response["output"][0]["call_id"], next_response["output"][0]["call_id"])
        self.assertEqual(next_response["output"][0]["type"], "function_call")
        self.assertEqual(len({body["session_id"] for body in self.fake.sent}), 1)

    def test_cancellation_after_result_write_invalidates_without_replay(self):
        response, result = self.pending()

        def cancel():
            if len(self.fake.sent) == 2:
                raise ConnectionAbortedError()

        with self.assertRaises(ConnectionAbortedError):
            self.sessions.generate({**self.body, "previous_response_id": response["id"], "tool_results": [result]}, cancel)
        with self.assertRaises(ServiceError):
            self.call(previous_response_id=response["id"], tool_results=[result])
        self.assertFalse(self.sessions.snapshots[response["id"]].valid)
        self.assertEqual(len(self.fake.sent), 2)

    def test_unknown_tool_and_run_mismatch_are_rejected(self):
        for request in ({"name": "unavailable", "arguments": {}},
                        {"name": "exec_command", "arguments": {}, "run": "b" * 32}):
            self.fake.replies.append(request)
            with self.assertRaises(ServiceError) as caught:
                self.call()
            self.assertEqual(caught.exception.code, "muse_web_tool_format_invalid")

    def test_unconfigured_service_does_not_silently_use_xml(self):
        self.sessions.workspace = None
        with self.assertRaises(ServiceError) as caught:
            self.call()
        self.assertEqual(caught.exception.code, "muse_web_tools_unavailable")
        self.assertEqual(self.fake.connections, 0)

    def test_stream_projection_has_consistent_ids_and_exact_output(self):
        for custom in (False, True):
            response, _ = self.pending(custom=custom)
            output = response["output"][0]
            frames = list(response_frames(response))
            events = [json.loads(frame.decode().split("data: ", 1)[1]) for frame in frames if frame.startswith(b"event:")]
            for event in events:
                if "item_id" in event:
                    self.assertEqual(event["item_id"], output["id"])
                if "item" in event:
                    self.assertEqual(event["item"]["call_id"], output["call_id"])
                    self.assertEqual(event["item"]["id"], output["id"])
            self.assertEqual(events[-1]["response"], response)


class WorkspaceFileTests(unittest.TestCase):
    def test_paths_and_not_found_detection(self):
        for path in ("/tmp/other", "workspace/custom-skills/../other", "workspace/custom-skills/bad-name", ""):
            with self.assertRaises(ValueError):
                WorkspaceTools(path)
        workspace = WorkspaceTools(WORKSPACE)
        error = RuntimeError("hidden")
        error.status = 400
        error.payload = '{"error":{"code":"invalid_params"}}'
        self.assertFalse(workspace.missing(error))
        error.payload = '{"error":{"code":"not_found"}}'
        self.assertTrue(workspace.missing(error))
        error.status = 403
        error.payload = "permission denied"
        self.assertFalse(workspace.missing(error))

    def test_partial_reads_and_bad_json_fail_closed(self):
        workspace = WorkspaceTools(WORKSPACE)

        class Gateway:
            value = {"text": "{}", "eof": False}

            def call_json(self, *_args, **_kwargs):
                return self.value

        gateway = Gateway()
        for value in ({"text": "{}", "eof": False}, {"text": "not json", "eof": True},
                      {"text": '{"x": NaN}', "eof": True}):
            gateway.value = value
            with self.assertRaises(MailboxError):
                workspace.read(gateway, workspace.path("a" * 32, "request"))

    def test_setup_renders_and_checks_only_new_independent_skill(self):
        fake = FakeMuse()
        inner = fake.connect()

        class Gateway:
            exists = False

            def call_json(self, method, **kwargs):
                if method == "fs.stat":
                    if self.exists:
                        return {"kind": "directory"}
                    error = RuntimeError()
                    error.status = 404
                    raise error
                return inner.call_json(method, **kwargs)

        gateway = Gateway()
        result = install(gateway, "codex_test", ROOT / "tools/muse_tools")
        self.assertEqual(result["tool_workspace"], WORKSPACE)
        self.assertEqual(len(fake.files), 3)
        self.assertIn(b'include_in_prompt: false', fake.files[WORKSPACE + "/skill.yaml"])
        self.assertTrue(all(path.startswith(WORKSPACE + "/") for path in fake.files))
        self.assertNotIn(b"__SKILL", b"".join(fake.files.values()))
        before = copy.deepcopy(fake.files)
        gateway.exists = True
        with self.assertRaises(ValueError):
            install(gateway, "codex_test", ROOT / "tools/muse_tools")
        self.assertEqual(fake.files, before)


if __name__ == "__main__":
    unittest.main()
