from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fanvpn_bridge.muse_service import MuseSessions, ServiceError
from fanvpn_bridge.muse_tools import FILE_CHUNK_BYTES, MAX_BYTES, MailboxError, PROTOCOL, WorkspaceTools, response_frames
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
                              capture_output=True, timeout=5, env={**os.environ, "PYTHONIOENCODING": "utf-8"})

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

    def referenced_catalog(self, tools=TOOLS):
        raw = json.dumps({"schema_version": 1, "tools": tools}, ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        directory = self.root / "catalogs"
        directory.mkdir(exist_ok=True)
        path = directory / (digest + ".json")
        path.write_bytes(raw)
        self.write("manifest", {"schema_version": 2, "run": self.run,
                               "expires_at": time.time() + 60, "catalog_sha256": digest})
        return path

    def test_referenced_catalog_lists_definitions_and_preserves_exact_request_and_result(self):
        self.referenced_catalog()
        directory = json.loads(self.invoke("tools").stdout)
        self.assertEqual(directory, [{"name": tool["name"], "type": tool["type"]} for tool in TOOLS])
        self.assertEqual(json.loads(self.invoke("tools", None, "--name", "apply_patch").stdout), TOOLS[1])
        text = "literal 中文 `x`\n" * 6000
        request = self.invoke("request", {"name": "apply_patch", "input": text})
        self.assertEqual(request.returncode, 0, request.stderr)
        self.assertEqual(json.loads((self.directory / "request.json").read_bytes())["input"], text)
        result = {"schema_version": 1, "run": self.run, "result": {"output": text}}
        self.write("result", result)
        self.assertEqual(json.loads(self.invoke("result").stdout), result)

    def test_reference_rejects_corrupt_missing_or_unsafe_catalog(self):
        path = self.referenced_catalog()
        path.write_bytes(path.read_bytes() + b" ")
        self.assertEqual(self.invoke("tools").returncode, 2)
        path.unlink()
        self.assertEqual(self.invoke("request", {"name": "exec_command", "arguments": {}}).returncode, 2)
        for digest in ("../outside", "A" * 64, "f" * 63, None):
            self.write("manifest", {"schema_version": 2, "run": self.run, "catalog_sha256": digest})
            self.assertEqual(self.invoke("tools").returncode, 2)
        self.assertFalse((self.directory / "request.json").exists())

    def test_reference_rejects_bad_schema_or_duplicate_tools(self):
        for tools in (None, [None], [TOOLS[0], TOOLS[0]], [{"name": "x", "type": "unknown"}]):
            self.referenced_catalog(tools)
            self.assertEqual(self.invoke("tools").returncode, 2)
        self.referenced_catalog()
        manifest = json.loads((self.directory / "manifest.json").read_bytes())
        for changes in ({"schema_version": 3}, {"run": "b" * 32}):
            self.write("manifest", {**manifest, **changes})
            self.assertEqual(self.invoke("tools").returncode, 2)


class WorkspaceSessionTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMuse()
        self.fake.seed_helper()
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

    def test_old_or_missing_helper_fails_before_session_or_model_submission(self):
        helper = WORKSPACE + "/scripts/mailbox.py"
        for source in (None, b"# legacy helper"):
            self.fake.files.clear()
            if source is not None:
                self.fake.files[helper] = source
            with self.assertRaises(ServiceError) as caught:
                self.call()
            self.assertEqual(caught.exception.code, "muse_web_tools_unavailable")
        self.assertEqual(self.fake.sent, [])
        self.assertEqual(self.fake.conversations, {})

    def test_independent_runs_share_one_unchanged_catalog(self):
        response, result = self.pending()
        self.call(previous_response_id=response["id"], tool_results=[result])
        self.call()  # A new conversation also reuses it.
        manifests = [json.loads(raw) for path, raw in self.fake.files.items() if path.endswith("/manifest.json")]
        self.assertEqual(len(manifests), 3)
        self.assertEqual(len({value["run"] for value in manifests}), 3)
        self.assertEqual(len({value["catalog_sha256"] for value in manifests}), 1)
        self.assertTrue(all("tools" not in value for value in manifests))
        uploads = [body for method, body in self.fake.rpcs if method == "fs.write" and "/catalogs/" in body["path"]]
        self.assertEqual(len(uploads), 1)

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
            value = {"text": "", "eof": False}

            def call_json(self, *_args, **_kwargs):
                return self.value

        gateway = Gateway()
        for value in ({"text": "", "eof": False}, {"text": "not json", "eof": True},
                      {"text": '{"x": NaN}', "eof": True}):
            gateway.value = value
            with self.assertRaises(MailboxError):
                workspace.read(gateway, workspace.path("a" * 32, "request"))

    def test_large_unicode_file_roundtrip_uses_bounded_chunks(self):
        fake, workspace = FakeMuse(), WorkspaceTools(WORKSPACE)
        gateway = fake.connect()
        value = {"input": "中文 `x` \\\"\n" * 9000}
        path = workspace.path("a" * 32, "result")
        workspace.write(gateway, path, value)
        self.assertEqual(workspace.read(gateway, path), value)
        writes = [body for method, body in fake.rpcs if method == "fs.write"]
        self.assertGreater(len(writes), 2)
        self.assertFalse(writes[0]["append"])
        self.assertTrue(all(body["append"] for body in writes[1:]))
        self.assertTrue(all(len(json.dumps(body).encode()) < 40 * 1024 for body in writes))
        self.assertTrue(all(body["len"] <= FILE_CHUNK_BYTES for method, body in fake.rpcs if method == "fs.read"))
        self.assertFalse(any(path.endswith(".tmp") for path in fake.files))

    def test_interrupted_upload_is_not_published_or_retried(self):
        fake, workspace = FakeMuse(), WorkspaceTools(WORKSPACE)
        inner = fake.connect()
        calls = []

        class Gateway:
            def call_json(self, method, **kwargs):
                calls.append(method)
                if method == "fs.write" and kwargs["body"]["append"]:
                    raise TimeoutError("ambiguous write")
                return inner.call_json(method, **kwargs)

        path = workspace.path("a" * 32, "result")
        with self.assertRaises(TimeoutError):
            workspace.write(Gateway(), path, {"text": "x" * 70000})
        self.assertNotIn(path, fake.files)
        self.assertEqual(calls, ["fs.write", "fs.write"])

    def test_catalog_reuse_changes_restart_deletion_and_vm_scope(self):
        fake, workspace = FakeMuse(), WorkspaceTools(WORKSPACE)
        gateway = fake.connect()
        large = [{**TOOLS[0], "description": "中文 `test`\n" * 6000}]
        run = workspace.prepare(gateway, large, 60)
        digest = json.loads(fake.files[workspace.path(run, "manifest")])["catalog_sha256"]
        catalog = f"{WORKSPACE}/catalogs/{digest}.json"
        uploads = lambda: sum(method == "fs.write" and "/catalogs/" in body["path"] for method, body in fake.rpcs)
        first_uploads = uploads()
        self.assertGreater(first_uploads, 2)
        workspace.prepare(gateway, copy.deepcopy(large), 60)
        WorkspaceTools(WORKSPACE).prepare(gateway, large, 60)
        self.assertEqual(uploads(), first_uploads)
        del fake.files[catalog]
        workspace.prepare(gateway, large, 60)
        self.assertEqual(uploads(), first_uploads * 2)
        changed = [{**large[0], "parameters": {"type": "object", "required": ["new"]}}]
        workspace.prepare(gateway, changed, 60)
        self.assertEqual(len([path for path in fake.files if "/catalogs/" in path]), 2)
        fake.files[catalog] = b"corrupt"
        # A different VM must not inherit trust in this digest.
        gateway.vm_id = "other-vm"
        with self.assertRaises(MailboxError):
            workspace.prepare(gateway, large, 60)
        self.assertEqual(fake.files[catalog], b"corrupt")

    def test_parallel_first_use_uploads_one_catalog_and_distinct_manifests(self):
        fake, workspace = FakeMuse(), WorkspaceTools(WORKSPACE)
        with ThreadPoolExecutor(max_workers=4) as pool:
            runs = list(pool.map(lambda _: workspace.prepare(fake.connect(), TOOLS, 60), range(4)))
        self.assertEqual(len(set(runs)), 4)
        writes = [body for method, body in fake.rpcs if method == "fs.write" and "/catalogs/" in body["path"]]
        self.assertEqual(len(writes), 1)

    def test_oversize_or_malformed_chunk_fails_closed(self):
        fake, workspace = FakeMuse(), WorkspaceTools(WORKSPACE)
        gateway = fake.connect()
        path = workspace.path("a" * 32, "result")
        with self.assertRaises(MailboxError):
            workspace.write_bytes(gateway, path, b"x" * (MAX_BYTES + 1))
        self.assertEqual(fake.rpcs, [])
        fake.files[path] = b"x" * (MAX_BYTES + 1)
        with self.assertRaises(MailboxError):
            workspace.read_bytes(gateway, path)

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

    def test_installer_selects_network_before_connecting(self):
        from fanvpn_bridge import muse_tools_setup

        for arguments, configured, expected in (([], "direct", "direct"),
                                                 (["--proxy", "direct"], "auto", "direct"),
                                                 ([], None, "auto")):
            events = []
            config = {"api_key": "test-only"}
            if configured is not None:
                config["proxy"] = configured
            gateway = FakeMuse().connect()

            def factory(_config):
                events.append("factory")
                return lambda: gateway

            with self.subTest(arguments=arguments, configured=configured), \
                    patch.object(sys, "argv", ["setup", "--skill-name", "codex_test", *arguments]), \
                    patch.object(Path, "read_text", return_value=json.dumps(config)), \
                    patch.object(muse_tools_setup, "configure_network", side_effect=lambda value: events.append(value)), \
                    patch.object(muse_tools_setup, "gateway_factory", side_effect=factory), \
                    patch.object(muse_tools_setup, "install", return_value={}), \
                    patch.object(sys, "stdout", io.StringIO()):
                muse_tools_setup.main()
            self.assertEqual(events, [expected, "factory"])


if __name__ == "__main__":
    unittest.main()
