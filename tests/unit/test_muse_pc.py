from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fanvpn_bridge.muse_pc import LIVE_PROTOCOL, PCSessions
from fanvpn_bridge.muse_service import ServiceError, make_server
from fanvpn_bridge.muse_tools import PROTOCOL
from fanvpn_bridge.muse_tools_setup import install
from fanvpn_bridge.muse_web import MuseWebProvider
from test_muse_web import FakeMuse, KEY, MODEL, TOOLS

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = "workspace/custom-skills/codex_pc_test"


class WaitingMuse(FakeMuse):
    """Native cloud turn persists across independent client connections."""
    def __init__(self):
        super().__init__()
        self.seed_helper(WORKSPACE)
        self.files[WORKSPACE + "/scripts/pc.py"] = (ROOT / "tools/muse_pc/scripts/pc.py").read_bytes()
        self.calls = [{"name": "exec_command", "arguments": {"cmd": "hostname"}}]
        self.active = {}
        self.fail_result_write = False
        self.fail_history = False
        self.fail_history_once = False

    def advance(self, session_id):
        active = self.active[session_id]
        index = active["index"]
        if index < len(self.calls):
            run = active["run"]
            value = {"schema_version": 1, "protocol": LIVE_PROTOCOL, "run": run,
                     "sequence": index + 1, "expires_at": time.time() + 60, **self.calls[index]}
            self.files[f"{WORKSPACE}/runs/{run}/request-{index + 1:04d}.json"] = json.dumps(value).encode()
        else:
            self.conversations[session_id][-1]["payload"] = {
                "status": "completed", "display_text": active.get("output", "refused")}

    def connect(self):
        inner = super().connect()
        owner = self

        class Gateway:
            vm_id = "test-vm"

            def call_json(self, method, *, body, timeout):
                if method == "chat.history":
                    owner.rpcs.append((method, copy.deepcopy(body)))
                    if owner.fail_history or owner.fail_history_once:
                        owner.fail_history_once = False
                        raise TimeoutError("sensitive transport detail")
                    return {"chat_events": copy.deepcopy([
                        event for event in owner.conversations[body["session_id"]]
                        if event["seq"] > body.get("after_seq", 0)])}
                if method == "fs.rename" and "/result-" in body["destination"]:
                    if owner.fail_result_write:
                        raise TimeoutError("sensitive transport detail")
                    value = inner.call_json(method, body=body, timeout=timeout)
                    receipt = json.loads(owner.files[body["destination"]])
                    session_id = next(sid for sid, active in owner.active.items() if active["run"] == receipt["run"])
                    active = owner.active[session_id]
                    active["output"] = receipt["result"]["output"]
                    active["index"] += 1
                    owner.advance(session_id)
                    return value
                return inner.call_json(method, body=body, timeout=timeout)

            def _open(self, method, *, body):
                owner.sent.append(copy.deepcopy(body))
                sid = body["session_id"]
                run = re.search(r"Connection/run: ([0-9a-f]{32})", body["items"][0]["text"])[1]
                events = owner.conversations[sid]
                events.append({"seq": len(events) + 1, "event_name": "message.user",
                               "payload": {"content": body["items"][0]["text"]}})
                events.append({"seq": len(events) + 1, "event_name": "message.assistant",
                               "payload": {"status": "in_progress"}})
                owner.active[sid] = {"run": run, "index": 0}
                owner.advance(sid)
                owner.open_hook()

            def close(self):
                inner.close()

        return Gateway()


class PCSessionTests(unittest.TestCase):
    def setUp(self):
        self.fake = WaitingMuse()
        self.sessions = PCSessions(self.fake.connect, tool_workspace=WORKSPACE, timeout=0.2, poll=0.001)
        self.addCleanup(self.sessions.close)
        self.body = {"model": "default", "input": "Run the requested test", "tools": TOOLS,
                     "tool_protocol": PROTOCOL}

    def ask(self, **extra):
        return self.sessions.generate({**self.body, **extra})

    @staticmethod
    def result(response, output="real result"):
        call = response["output"][0]
        return {"type": "custom_tool_call_output" if call["type"] == "custom_tool_call" else "function_call_output",
                "call_id": call["call_id"], "output": output}

    def resume(self, response, **extra):
        return self.ask(previous_response_id=response["id"], tool_results=[self.result(response)],
                        tool_result_has_context=False, **extra)

    def test_real_result_resumes_same_native_turn_without_a_new_message(self):
        first = self.ask()
        self.assertEqual(self.fake.closed, 0)
        final = self.resume(first)
        self.assertEqual(final["output"][0]["content"][0]["text"], "real result")
        self.assertEqual(len(self.fake.sent), 1)
        self.assertEqual(len(self.fake.conversations), 1)
        self.assertEqual(self.fake.connections, 1)
        self.assertEqual(self.fake.closed, 0)
        # A later user message stays in that same session and reuses its socket.
        self.ask(previous_response_id=final["id"], input="Next task")
        self.assertEqual(len(self.fake.sent), 2)
        self.assertEqual(len(self.fake.conversations), 1)
        self.assertEqual(self.fake.connections, 1)

    def test_pc_default_polls_faster_without_changing_legacy_sessions(self):
        from fanvpn_bridge.muse_service import MuseSessions
        sessions = PCSessions(self.fake.connect, tool_workspace=WORKSPACE)
        self.addCleanup(sessions.close)
        self.assertEqual(sessions.poll, 0.25)
        self.assertEqual(MuseSessions(self.fake.connect).poll, 2)
        self.assertEqual(self.sessions.poll, 0.001)

    def test_live_connection_checks_helpers_once_across_sequential_calls(self):
        self.fake.calls.append(self.fake.calls[0])
        first = self.ask()
        second = self.resume(first)
        self.resume(second)
        self.assertEqual(self.fake.connections, 1)
        self.assertEqual(self.fake.closed, 0)
        reads = [body["path"] for method, body in self.fake.rpcs if method == "fs.read"]
        for helper in ("mailbox.py", "pc.py"):
            self.assertEqual(reads.count(WORKSPACE + "/scripts/" + helper), 1)

    def test_five_minute_idle_lease_does_not_extend_tool_expiry(self):
        first = self.ask()
        state = self.sessions.snapshots[first["id"]]
        self.assertGreater(state.gateway_timer.interval, 0)
        self.assertLessEqual(state.gateway_timer.interval, state.pending["expires"] - time.monotonic() + 0.1)
        self.assertLessEqual(state.gateway_timer.interval, 60)
        self.resume(first)
        self.assertIsNone(state.pending)
        self.assertEqual(state.gateway_timer.interval, 300)

    def test_idle_socket_expires_without_losing_pending_call_or_replaying_task(self):
        with patch("fanvpn_bridge.muse_pc.PC_CONNECTION_IDLE_SECONDS", 0.02):
            first = self.ask()
        state = self.sessions.snapshots[first["id"]]
        deadline = time.monotonic() + 2
        while self.fake.closed == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(self.fake.closed, 1)
        self.assertIsNone(state.gateway)
        self.assertTrue(state.valid)
        self.assertIsNotNone(state.pending)
        self.resume(first)
        self.assertEqual(self.fake.connections, 2)
        self.assertEqual(len(self.fake.sent), 1)

    def test_cancelled_idle_timer_cannot_close_a_renewed_connection(self):
        self.fake.calls.append(self.fake.calls[0])
        first = self.ask()
        state = self.sessions.snapshots[first["id"]]
        timer, gateway = state.gateway_timer, state.gateway
        second = self.resume(first)
        self.assertIs(state.gateway, gateway)
        self.sessions._expire_gateway(state, timer)
        self.assertIs(state.gateway, gateway)
        self.assertEqual(self.fake.closed, 0)
        self.resume(second)

    def test_completed_turn_connection_expires_and_next_task_connects_fresh(self):
        first = self.ask()
        with patch("fanvpn_bridge.muse_pc.PC_CONNECTION_IDLE_SECONDS", 0.02):
            final = self.resume(first)
        deadline = time.monotonic() + 2
        while self.fake.closed == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(self.fake.closed, 1)
        self.ask(previous_response_id=final["id"], input="Next task")
        self.assertEqual(self.fake.connections, 2)
        self.assertEqual(len(self.fake.conversations), 1)

    def test_stale_completed_connection_recovers_only_before_new_task_submission(self):
        final = self.resume(self.ask())
        self.fake.fail_history_once = True
        self.ask(previous_response_id=final["id"], input="Next task")
        self.assertEqual(self.fake.connections, 2)
        self.assertEqual(self.fake.closed, 1)
        self.assertEqual(len(self.fake.sent), 2)
        self.assertEqual(len(self.fake.conversations), 1)

    def test_history_cursor_skips_prompt_but_keeps_mutable_assistant_events(self):
        first = self.ask()
        state = self.sessions.snapshots[first["id"]]
        self.assertEqual(state.history_after, 1)
        self.assertIsNone(state.submitted_text)
        final = self.resume(first)
        queries = [body for method, body in self.fake.rpcs if method == "chat.history"]
        self.assertEqual([body["after_seq"] for body in queries], [0, 0, 1, 1])
        # The same assistant event (seq=2) changes from in_progress to completed.
        self.assertEqual(final["output"][0]["content"][0]["text"], "real result")
        self.assertEqual(self.fake.conversations[state.session_id][-1]["seq"], 2)

    def test_unrelated_user_message_cannot_advance_history_cursor(self):
        from fanvpn_bridge.muse_pc import PCTurn
        gateway = self.fake.connect()
        self.addCleanup(gateway.close)
        self.fake.conversations["scope"] = [
            {"seq": 1, "event_name": "message.user", "payload": {"content": "Different task"}},
            {"seq": 2, "event_name": "message.assistant", "payload": {"status": "cancelled"}},
        ]
        state = PCTurn("scope", submitted_text="Our actual task")
        events = self.sessions._history(gateway, state, 1)
        self.assertEqual(state.history_after, 0)
        with self.assertRaisesRegex(ServiceError, "did not complete"):
            self.sessions._terminal(events, state.baseline)

    def test_later_commentary_does_not_hide_earlier_mutable_answer(self):
        from fanvpn_bridge.muse_pc import PCTurn
        gateway = self.fake.connect()
        self.addCleanup(gateway.close)
        self.fake.conversations["scope"] = [
            {"seq": 1, "event_name": "message.user", "payload": {"content": "Our actual task"}},
            {"seq": 2, "event_name": "message.assistant", "payload": {"status": "in_progress"}},
            {"seq": 3, "event_name": "message.assistant", "payload": {"status": "completed",
             "display_text": "Working", "metadata": {"message_kind": "commentary"}}},
        ]
        state = PCTurn("scope", submitted_text="Our actual task")
        self.sessions._history(gateway, state, 1)
        self.fake.conversations["scope"][1]["payload"] = {"status": "completed", "display_text": "Done"}
        terminal = self.sessions._terminal(self.sessions._history(gateway, state, 1), state.baseline)
        self.assertEqual(terminal["display_text"], "Done")
        self.assertEqual(state.history_after, 1)

    def test_new_user_anchor_excludes_delayed_previous_answer(self):
        from fanvpn_bridge.muse_pc import PCTurn
        gateway = self.fake.connect()
        self.addCleanup(gateway.close)
        self.fake.conversations["scope"] = [
            {"seq": 2, "event_name": "message.assistant",
             "payload": {"status": "completed", "display_text": "Old answer"}},
            {"seq": 3, "event_name": "message.user", "payload": {"content": "New task"}},
            {"seq": 4, "event_name": "message.assistant", "payload": {"status": "in_progress"}},
        ]
        state = PCTurn("scope", baseline=1, submitted_text="New task")
        events = self.sessions._history(gateway, state, 1)
        self.assertEqual(state.baseline, 3)
        self.assertIsNone(self.sessions._terminal(events, state.baseline))

    def test_unconfirmed_user_message_cannot_release_old_answer_or_local_call(self):
        def hide_user_message():
            events = self.fake.conversations["session_0"]
            events[0]["payload"]["content"] = "Snapshot has not exposed our submitted message"
            events.insert(0, {"seq": 0.5, "event_name": "message.assistant",
                              "payload": {"status": "completed", "display_text": "Old answer"}})
        self.fake.open_hook = hide_user_message
        with self.assertRaisesRegex(ServiceError, "timed out"):
            self.ask()

    def test_new_task_send_failure_on_warm_socket_is_not_retried(self):
        final = self.resume(self.ask())
        def failed_send():
            raise TimeoutError("uncertain send outcome")
        self.fake.open_hook = failed_send
        with self.assertRaisesRegex(ServiceError, "transport failed"):
            self.ask(previous_response_id=final["id"], input="Next task")
        self.assertEqual(self.fake.connections, 1)
        self.assertEqual(len(self.fake.sent), 2)
        with self.assertRaisesRegex(ServiceError, "Previous PC response"):
            self.ask(previous_response_id=final["id"], input="Next task")

    def test_eviction_and_shutdown_release_idle_sockets(self):
        self.sessions.max_snapshots = 1
        first = self.ask()
        first_state = self.sessions.snapshots[first["id"]]
        self.ask(input="Other task")
        self.assertIsNone(first_state.gateway)
        self.assertEqual(self.fake.closed, 1)
        self.sessions.close()
        self.assertEqual(self.fake.closed, 2)
        with self.assertRaisesRegex(ServiceError, "stopping"):
            self.ask()

    def test_broken_reused_socket_does_not_reconnect_or_resubmit(self):
        first = self.ask()
        self.fake.fail_history = True
        with self.assertRaisesRegex(ServiceError, "transport failed"):
            self.resume(first)
        self.assertEqual(self.fake.connections, 1)
        self.assertEqual(self.fake.closed, 1)
        self.assertEqual(len(self.fake.sent), 1)
        self.assertFalse(any("/result-" in path for path in self.fake.files))

    def test_sequential_freeform_and_function_use_same_run(self):
        patch = '*** Begin Patch\n*** Add File: example.txt\n+中文 `x` "q" C:\\tmp\n*** End Patch'
        self.fake.calls.append({"name": "apply_patch", "input": patch})
        first = self.ask()
        second = self.resume(first)
        self.assertEqual(second["output"][0]["input"], patch)
        final = self.resume(second)
        self.assertEqual(final["output"][0]["type"], "message")
        self.assertEqual(len(self.fake.sent), 1)
        self.assertEqual(self.fake.closed, 0)
        self.assertEqual(len([p for p in self.fake.files if p.endswith("manifest.json")]), 1)
        self.assertEqual(len([p for p in self.fake.files if "/result-" in p]), 2)

    def test_invalid_results_and_new_context_fail_before_connect(self):
        first = self.ask()
        result = self.result(first)
        cases = [[], [{**result, "call_id": "other"}], [{**result, "type": "custom_tool_call_output"}],
                 [{**result, "output": 1}], [result, result]]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ServiceError):
                self.ask(previous_response_id=first["id"], tool_results=values, tool_result_has_context=False)
        for flag in (True, None):
            with self.assertRaisesRegex(ServiceError, "append task context"):
                self.ask(previous_response_id=first["id"], tool_results=[result], tool_result_has_context=flag)
        with self.assertRaises(ServiceError):
            self.resume(first, tools=TOOLS[:1])
        self.assertEqual(self.fake.connections, 1)
        self.resume(first)

    def test_expired_pending_is_not_delivered(self):
        first = self.ask()
        self.sessions.snapshots[first["id"]].pending["expires"] = 0
        with self.assertRaises(ServiceError):
            self.resume(first)
        self.assertEqual(self.fake.connections, 1)

    def test_finished_cloud_turn_does_not_receive_a_late_result(self):
        first = self.ask()
        state = self.sessions.snapshots[first["id"]]
        self.fake.conversations[state.session_id][-1]["payload"] = {"status": "completed", "display_text": "Stopped"}
        with self.assertRaisesRegex(ServiceError, "stopped waiting"):
            self.resume(first)
        self.assertFalse(state.valid)
        self.assertFalse(any("/result-" in p for p in self.fake.files))

    def test_completed_commentary_is_not_native_turn_completion(self):
        def commentary():
            self.fake.conversations["session_0"].insert(0, {
                "seq": 0.5, "event_name": "message.assistant", "payload": {"status": "completed",
                    "display_text": "", "metadata": {"message_kind": "commentary", "recipient": "commentary"}}})
        self.fake.open_hook = commentary
        first = self.ask()
        final = self.resume(first)
        self.assertEqual(final["output"][0]["content"][0]["text"], "real result")

    def test_ambiguous_result_write_invalidates_without_resubmitting(self):
        first = self.ask()
        self.fake.fail_result_write = True
        with self.assertRaisesRegex(ServiceError, "transport failed"):
            self.resume(first)
        with self.assertRaisesRegex(ServiceError, "Previous PC response"):
            self.resume(first)
        self.assertEqual(len(self.fake.sent), 1)

    def test_refusal_is_returned_without_fabricating_tools(self):
        self.fake.calls = []
        final = self.ask()
        self.assertEqual(final["output"][0]["content"][0]["text"], "refused")
        self.assertFalse(any("/request-" in p for p in self.fake.files))

    def test_request_after_native_completion_is_not_executed(self):
        def completed():
            self.fake.conversations["session_0"][-1]["payload"] = {"status": "completed"}
        self.fake.open_hook = completed
        with self.assertRaisesRegex(ServiceError, "finished before"):
            self.ask()

    def test_missing_helper_does_not_send_task(self):
        del self.fake.files[WORKSPACE + "/scripts/pc.py"]
        with self.assertRaisesRegex(ServiceError, "helper is missing"):
            self.ask()
        self.assertEqual(self.fake.sent, [])

    def test_cancellation_invalidates_pending_result_without_replay(self):
        first = self.ask()
        def cancel():
            if any("/result-" in p for p in self.fake.files):
                raise ConnectionAbortedError()
        with self.assertRaises(ConnectionAbortedError):
            self.sessions.generate({**self.body, "previous_response_id": first["id"],
                                   "tool_results": [self.result(first)], "tool_result_has_context": False}, cancel)
        self.assertFalse(self.sessions.snapshots[first["id"]].valid)
        self.assertEqual(len(self.fake.sent), 1)

    def test_independent_parallel_sessions_reject_crossed_results(self):
        self.sessions.timeout = 2
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda i: self.ask(input=str(i)), range(2)))
        with self.assertRaises(ServiceError):
            self.ask(previous_response_id=second["id"], tool_results=[self.result(first)], tool_result_has_context=False)
        for response in (first, second):
            self.resume(response)
        self.assertEqual(len(self.fake.conversations), 2)
        self.assertEqual(len(self.fake.sent), 2)

    def test_provider_namespace_and_full_history_continuation(self):
        server = make_server({"api_key": KEY, "port": 0}, self.sessions)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        provider = MuseWebProvider(base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key=KEY, timeout_seconds=5)
        self.fake.calls = [{"name": "functions.apply_patch", "input": "exact patch"}]
        payload = {"model": MODEL, "tools": [{"type": "namespace", "name": "functions", "tools": [TOOLS[1]]}],
                   "input": [{"role": "user", "content": "Do this"}], "prompt_cache_key": "isolated-pc-test"}
        try:
            _, first = provider.responses(payload)
            self.assertEqual(first["output"][0]["namespace"], "functions")
            result = self.result(first)
            _, final = provider.responses({**payload, "input": [*payload["input"], *first["output"], result]})
            self.assertEqual(final["output"][0]["content"][0]["text"], "real result")
            self.assertEqual(len(self.fake.sent), 1)
        finally:
            server.shutdown()
            server.server_close()
            self.sessions.close()
            thread.join(2)

    def test_installer_keeps_legacy_helper_and_publishes_metadata_last(self):
        fake = FakeMuse()
        result = install(fake.connect(), "codex_pc_test", ROOT / "tools/muse_pc", pc=True)
        self.assertEqual(len(result["sha256"]), 4)
        writes = [body["path"] for method, body in fake.rpcs if method == "fs.write"]
        self.assertTrue(writes[-1].endswith("skill.yaml"))
        self.assertEqual(fake.files[WORKSPACE + "/scripts/mailbox.py"],
                         (ROOT / "tools/muse_tools/scripts/mailbox.py").read_bytes().replace(b"\r\n", b"\n"))
        fake.files[WORKSPACE] = b"directory"
        with self.assertRaises(ValueError):
            install(fake.connect(), "codex_pc_test", ROOT / "tools/muse_pc", pc=True)


class PCHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        for source in (ROOT / "tools/muse_tools/scripts/mailbox.py", ROOT / "tools/muse_pc/scripts/pc.py"):
            shutil.copyfile(source, self.root / "scripts" / source.name)
        self.run = "a" * 32
        self.directory = self.root / "runs" / self.run
        self.directory.mkdir(parents=True)
        raw = json.dumps({"schema_version": 1, "tools": TOOLS}).encode()
        digest = hashlib.sha256(raw).hexdigest()
        (self.root / "catalogs").mkdir()
        (self.root / "catalogs" / (digest + ".json")).write_bytes(raw)
        self.manifest = {"schema_version": 2, "protocol": LIVE_PROTOCOL, "run": self.run,
                         "catalog_sha256": digest, "expires_at": time.time() + 30}
        self.write("manifest.json", self.manifest)

    def write(self, name, value):
        path = self.directory / name
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def start(self, request=None, timeout=3):
        process = subprocess.Popen([sys.executable, str(self.root / "scripts/pc.py"), "--run", self.run,
                                    "--timeout", str(timeout)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        process.stdin.write(json.dumps(request or {"name": "exec_command", "arguments": {"cmd": "hostname"}}).encode())
        process.stdin.close()
        process.stdin = None
        return process

    def wait_request(self, sequence=1):
        path = self.directory / f"request-{sequence:04d}.json"
        deadline = time.monotonic() + 3
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists())
        return json.loads(path.read_text(encoding="utf-8"))

    def receipt(self, sequence=1, **extra):
        return {"schema_version": 1, "protocol": LIVE_PROTOCOL, "run": self.run,
                "sequence": sequence, "result": {"type": "function_call_output", "call_id": "test-call",
                                                   "output": "真实的结果"}, **extra}

    def test_waits_for_real_result_then_allows_next_call(self):
        for sequence in (1, 2):
            process = self.start()
            request = self.wait_request(sequence)
            self.assertEqual(request["arguments"], {"cmd": "hostname"})
            self.assertIsNone(process.poll())
            self.write(f"result-{sequence:04d}.json", self.receipt(sequence))
            out, err = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, err)
            self.assertEqual(json.loads(out)["output"], "真实的结果")

    def test_freeform_unicode_is_preserved(self):
        value = '*** Begin Patch\n+中文 "x" `code` C:\\tmp\n*** End Patch'
        process = self.start({"name": "apply_patch", "input": value})
        self.assertEqual(self.wait_request()["input"], value)
        receipt = self.receipt()
        receipt["result"]["type"] = "custom_tool_call_output"
        self.write("result-0001.json", receipt)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)

    def test_concurrent_call_rejected_without_duplicate_request(self):
        first = self.start()
        self.wait_request()
        second = self.start()
        _, err = second.communicate(timeout=5)
        self.assertEqual(second.returncode, 2)
        self.assertNotIn(b"Traceback", err)
        self.assertFalse((self.directory / "request-0002.json").exists())
        self.write("result-0001.json", self.receipt())
        first.communicate(timeout=5)

    def test_timeout_is_not_replayed(self):
        process = self.start(timeout=0.1)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 2)
        second = self.start()
        second.communicate(timeout=5)
        self.assertEqual(second.returncode, 2)
        self.assertFalse((self.directory / "request-0002.json").exists())

    def test_cross_run_or_bad_result_is_rejected(self):
        process = self.start()
        self.wait_request()
        self.write("result-0001.json", self.receipt(run="b" * 32))
        _, err = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 2)
        self.assertNotIn(b"Traceback", err)

    def test_malformed_prior_receipt_does_not_unlock_another_call(self):
        process = self.start()
        self.wait_request()
        self.write("result-0001.json", self.receipt(sequence=True))
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 2)
        process = self.start()
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 2)
        self.assertFalse((self.directory / "request-0002.json").exists())

    def test_call_limit_is_enforced(self):
        for sequence in range(1, 65):
            self.write(f"request-{sequence:04d}.json", {"name": "exec_command"})
            self.write(f"result-{sequence:04d}.json", self.receipt(sequence))
        process = self.start()
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 2)
        self.assertFalse((self.directory / "request-0065.json").exists())

    def test_expired_manifest_unknown_tools_and_invalid_arguments_never_publish(self):
        for request in ({"name": "unknown", "arguments": {}}, {"name": "exec_command", "input": "x"}):
            process = self.start(request)
            process.communicate(timeout=5)
            self.assertEqual(process.returncode, 2)
        self.write("manifest.json", {**self.manifest, "expires_at": 0})
        process = self.start()
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 2)
        self.assertFalse((self.directory / "request-0001.json").exists())


if __name__ == "__main__":
    unittest.main()
