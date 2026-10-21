from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import secrets
import sys
import threading
import time
import unittest
from unittest.mock import patch

from fanvpn_bridge.muse_push import PushSessions
from fanvpn_bridge.muse_push_channel import ChannelReserve, relay_origin
from fanvpn_bridge.muse_service import ServiceError
from fanvpn_bridge.muse_tools import PROTOCOL
from test_muse_pc import WaitingMuse, WORKSPACE
from test_muse_web import TOOLS

ROOT = Path(__file__).resolve().parents[2]


class FakeChannel:
    def __init__(self, fake, run):
        self.fake, self.run, self.closed = fake, run, False
        self.cloud_config = {"url": "https://relay.example", "token": "cloud-test-secret"}
        self.delivered = set()
        self.error = None

    def check(self):
        if self.error or self.closed:
            raise ConnectionError("No replay")

    def next_call(self, timeout):
        self.check()
        for sid, active in self.fake.active.items():
            if active["run"] == self.run:
                sequence = active["index"] + 1
                path = f"{WORKSPACE}/runs/{self.run}/request-{sequence:04d}.json"
                if sequence not in self.delivered and path in self.fake.files:
                    self.delivered.add(sequence)
                    return json.loads(self.fake.files[path])
        time.sleep(min(timeout, 0.005))
        return None

    def result(self, sequence, result):
        self.check()
        sid = next(sid for sid, active in self.fake.active.items() if active["run"] == self.run)
        active = self.fake.active[sid]
        active["output"] = result["output"]
        active["index"] += 1
        self.fake.advance(sid)

    def close(self):
        self.closed = True


class ChannelReserveTests(unittest.TestCase):
    def make_reserve(self, factory, **kwargs):
        reserve = ChannelReserve({}, factory, **kwargs)
        self.addCleanup(reserve.close)
        return reserve

    def test_unused_channel_is_ready_before_task_and_refills_only_once(self):
        channels = []
        def factory(config, run, expires):
            channel = FakeChannel(None, run)
            channels.append(channel)
            return channel
        reserve = self.make_reserve(factory)
        ready = reserve.slot.result(timeout=2)
        self.assertEqual(len(channels), 1)
        self.assertIs(reserve.take()[2], ready[2])
        refill = reserve.slot.result(timeout=2)
        reserve.warm()
        self.assertEqual(len(channels), 2)
        self.assertNotEqual(ready[0], refill[0])
        reserve.close()
        self.assertFalse(ready[2].closed)  # Caller now owns this channel.
        self.assertTrue(refill[2].closed)
        ready[2].close()

    def test_take_while_connecting_does_not_discard_claimed_channel(self):
        entered, release, received = threading.Event(), threading.Event(), []
        def factory(config, run, expires):
            entered.set()
            release.wait(2)
            return FakeChannel(None, run)
        reserve = self.make_reserve(factory)
        self.assertTrue(entered.wait(2))
        worker = threading.Thread(target=lambda: received.append(reserve.take()))
        worker.start()
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(received), 1)
        self.assertFalse(received[0][2].closed)
        received[0][2].close()

    def test_close_during_connection_closes_late_channel(self):
        release = threading.Event()
        def factory(config, run, expires):
            release.wait(2)
            return FakeChannel(None, run)
        reserve = self.make_reserve(factory)
        future = reserve.slot
        reserve.close()
        release.set()
        self.assertTrue(future.result(timeout=2)[2].closed)
        with self.assertRaises(ConnectionError):
            reserve.take()

    def test_expiration_closes_unused_channel_without_background_reconnect(self):
        calls = []
        def factory(config, run, expires):
            calls.append(run)
            return FakeChannel(None, run)
        reserve = self.make_reserve(factory)
        future = reserve.slot
        channel = future.result(timeout=2)[2]
        reserve._expire(future)
        self.assertTrue(channel.closed)
        self.assertIsNone(reserve.slot)
        self.assertEqual(len(calls), 1)

    def test_failed_unused_connection_is_replaced_at_most_once(self):
        calls = []
        def factory(config, run, expires):
            calls.append(run)
            raise ConnectionError("test failure")
        reserve = self.make_reserve(factory)
        with self.assertRaises(ConnectionError):
            reserve.take()
        self.assertEqual(len(calls), 2)

    def test_broken_unused_connection_can_be_replaced_without_replaying_a_task(self):
        channels = []
        def factory(config, run, expires):
            channel = FakeChannel(None, run)
            channels.append(channel)
            return channel
        reserve = self.make_reserve(factory)
        first = reserve.slot.result(timeout=2)[2]
        first.error = True
        replacement = reserve.take()[2]
        self.assertTrue(first.closed)
        self.assertIsNot(first, replacement)
        self.assertFalse(replacement.closed)
        replacement.close()


class PushSessionTests(unittest.TestCase):
    def setUp(self):
        self.fake = WaitingMuse()
        self.fake.files[WORKSPACE + "/scripts/pc.py"] = (ROOT / "tools/muse_push/scripts/pc.py").read_bytes()
        self.channels = []
        def factory(config, run, expires):
            channel = FakeChannel(self.fake, run)
            self.channels.append(channel)
            return channel
        self.sessions = PushSessions(self.fake.connect, tool_workspace=WORKSPACE, timeout=3,
                                     relay_config={"url": "https://relay.example"}, channel_factory=factory,
                                     prewarm=False)
        self.addCleanup(self.sessions.close)
        self.body = {"model": "default", "input": "Run the test", "tools": TOOLS, "tool_protocol": PROTOCOL}

    def ask(self, **extra):
        return self.sessions.generate({**self.body, **extra})

    def resume(self, response, output="actual result"):
        call = response["output"][0]
        return self.ask(previous_response_id=response["id"], tool_result_has_context=False, tool_results=[{
            "type": "custom_tool_call_output" if call["type"] == "custom_tool_call" else "function_call_output",
            "call_id": call["call_id"], "output": output,
        }])

    def test_same_turn_calls_do_not_read_or_write_request_result_files(self):
        self.fake.calls *= 5
        response = self.ask()
        started = time.monotonic()
        for _ in range(4):
            response = self.resume(response)
            self.assertEqual(response["output"][0]["type"], "function_call")
        # No sleeps or network history queries on the tool-result hot path.
        self.assertLess(time.monotonic() - started, 0.5)
        final = self.resume(response)
        self.assertEqual(final["output"][0]["content"][0]["text"], "actual result")
        for method, body in self.fake.rpcs:
            if method.startswith("fs."):
                self.assertNotIn("/request-", json.dumps(body))
                self.assertNotIn("/result-", json.dumps(body))
        self.assertEqual(len(self.fake.sent), 1)
        self.assertTrue(self.channels[0].closed)
        self.assertEqual(self.fake.connections, 1)

    def test_custom_tool_input_and_result_preserved(self):
        self.fake.calls = [{"name": "apply_patch", "input": "*** Begin Patch\n+中文\n*** End Patch"}]
        response = self.ask()
        self.assertEqual(response["output"][0]["input"], self.fake.calls[0]["input"])
        final = self.resume(response, "工具拒绝：未批准")
        self.assertEqual(final["output"][0]["content"][0]["text"], "工具拒绝：未批准")

    def test_disconnect_never_publishes_a_result_or_replays_a_call(self):
        response = self.ask()
        self.channels[0].error = True
        with self.assertRaises(ServiceError):
            self.resume(response)
        self.assertEqual(self.fake.active[next(iter(self.fake.active))]["index"], 0)
        self.assertEqual(len(self.fake.sent), 1)

    def test_legacy_workspace_cannot_be_used_as_push_workspace(self):
        self.fake.files[WORKSPACE + "/scripts/pc.py"] = (ROOT / "tools/muse_pc/scripts/pc.py").read_bytes()
        with self.assertRaises(ServiceError):
            self.ask()
        self.assertEqual(len(self.fake.sent), 0)

    def test_relay_must_be_verified_https_without_embedded_credentials(self):
        for url in ("http://example.com", "https://user:secret@example.com", "https://x/path", "https://x?token=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                relay_origin(url)

    def test_client_disconnect_during_preparation_discards_the_new_channel(self):
        def progress():
            if self.channels:
                raise ConnectionAbortedError()
        with self.assertRaises(ConnectionAbortedError):
            self.sessions.generate(self.body, progress)
        self.assertTrue(self.channels[0].closed)
        self.assertEqual(self.sessions.workspace.exchanges, {})

    def test_successful_followup_reuses_gateway_but_not_previous_tool_room(self):
        final = self.resume(self.ask())
        next_response = self.ask(previous_response_id=final["id"], input="Next task")
        self.assertEqual(next_response["output"][0]["type"], "function_call")
        self.assertEqual(self.fake.connections, 1)
        self.assertEqual(len(self.channels), 2)
        self.assertNotEqual(self.channels[0].run, self.channels[1].run)

    def test_pending_and_completed_turns_have_no_five_minute_disconnect_timer(self):
        response = self.ask()
        state = self.sessions.snapshots[response["id"]]
        self.assertIsNotNone(state.gateway)
        self.assertIsNone(state.gateway_timer)
        final = self.resume(response)
        self.assertIs(self.sessions.snapshots[final["id"]], state)
        self.assertIsNotNone(state.gateway)
        self.assertIsNone(state.gateway_timer)

    def test_wrong_tool_result_does_not_trigger_a_second_execution(self):
        first = self.ask()
        with self.assertRaises(ServiceError):
            self.ask(previous_response_id=first["id"], tool_result_has_context=False, tool_results=[{
                "type": "function_call_output", "call_id": "wrong-call", "output": "not executed",
            }])
        self.assertEqual(self.fake.active[next(iter(self.fake.active))]["index"], 0)
        self.resume(first)


try:
    from aiohttp import ClientSession
    from aiohttp.test_utils import TestServer
except ImportError:
    ClientSession = None


@unittest.skipIf(ClientSession is None, "Run with the isolated Muse push dependencies")
class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        spec = importlib.util.spec_from_file_location("muse_push_relay_test", ROOT / "tools/muse_push/relay.py")
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.module
        spec.loader.exec_module(self.module)
        self.admin = secrets.token_hex(32)
        self.server = TestServer(self.module.make_app(self.admin))
        await self.server.start_server()
        self.client = ClientSession()
        self.run = secrets.token_hex(16)
        async with self.client.post(self.server.make_url("/rooms"), json={
            "run": self.run, "expires_at": time.time() + 60,
        }, headers={"Authorization": "Bearer " + self.admin}) as response:
            self.assertEqual(response.status, 200)
            self.tokens = await response.json()
        self.cloud_headers = {"Authorization": "Bearer " + self.tokens["cloud_token"]}

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()

    async def pc(self):
        ws = await self.client.ws_connect(self.server.make_url(f"/rooms/{self.run}/pc"), headers={
            "Authorization": "Bearer " + self.tokens["pc_token"],
        })
        self.assertEqual(await ws.receive_json(), {"type": "ready"})
        return ws

    def call_body(self):
        return {"request_id": secrets.token_hex(16), "name": "exec_command", "arguments": {"cmd": "hostname"},
                "expires_at": time.time() + 30}

    async def test_cloud_call_waits_for_real_result_and_rejects_replay(self):
        ws = await self.pc()
        body = self.call_body()
        request = asyncio.create_task(self.client.post(self.server.make_url(f"/rooms/{self.run}/call"),
                                                       json=body, headers=self.cloud_headers))
        event = await ws.receive_json()
        self.assertEqual(event["request"]["arguments"], body["arguments"])
        self.assertFalse(request.done())
        result = {"type": "function_call_output", "call_id": "pc-test-call", "output": "真实结果"}
        await ws.send_json({"type": "result", "sequence": 1, "request_id": body["request_id"], "result": result})
        self.assertEqual((await ws.receive_json())["type"], "result_accepted")
        response = await request
        self.assertEqual(await response.json(), {"result": result})
        async with self.client.post(self.server.make_url(f"/rooms/{self.run}/call"), json=body,
                                    headers=self.cloud_headers) as repeated:
            self.assertEqual(repeated.status, 409)
        await ws.close()

    async def test_wrong_auth_and_expired_requests_never_reach_pc(self):
        ws = await self.pc()
        url = self.server.make_url(f"/rooms/{self.run}/call")
        async with self.client.post(url, json=self.call_body()) as response:
            self.assertEqual(response.status, 401)
        body = self.call_body()
        body["expires_at"] = time.time() - 1
        async with self.client.post(url, json=body, headers=self.cloud_headers) as response:
            self.assertEqual(response.status, 400)
        await ws.close()

    async def test_pc_disconnect_invalidates_pending_call_and_room(self):
        ws = await self.pc()
        request = asyncio.create_task(self.client.post(self.server.make_url(f"/rooms/{self.run}/call"),
                                                       json=self.call_body(), headers=self.cloud_headers))
        await ws.receive_json()
        await ws.close()
        response = await asyncio.wait_for(request, 2)
        self.assertEqual(response.status, 409)
        async with self.client.post(self.server.make_url(f"/rooms/{self.run}/call"), json=self.call_body(),
                                    headers=self.cloud_headers) as retry:
            self.assertEqual(retry.status, 410)

    async def test_concurrent_cloud_call_rejected_without_delivering_an_extra_command(self):
        ws = await self.pc()
        url = self.server.make_url(f"/rooms/{self.run}/call")
        first = self.call_body()
        request = asyncio.create_task(self.client.post(url, json=first, headers=self.cloud_headers))
        await ws.receive_json()
        async with self.client.post(url, json=self.call_body(), headers=self.cloud_headers) as response:
            self.assertEqual(response.status, 409)
        await ws.close()
        response = await request
        self.assertEqual(response.status, 409)

    async def test_expired_wait_cancels_pc_and_cannot_be_retried(self):
        ws = await self.pc()
        body = self.call_body()
        body["expires_at"] = time.time() + 0.15
        task = asyncio.create_task(self.client.post(self.server.make_url(f"/rooms/{self.run}/call"),
                                                    json=body, headers=self.cloud_headers))
        await ws.receive_json()
        self.assertEqual((await ws.receive_json())["type"], "cancel")
        self.assertEqual((await task).status, 409)
        async with self.client.post(self.server.make_url(f"/rooms/{self.run}/call"), json=self.call_body(),
                                    headers=self.cloud_headers) as retry:
            self.assertEqual(retry.status, 410)
        await ws.close()

    async def test_pc_and_cloud_tokens_cannot_be_interchanged(self):
        async with self.client.post(self.server.make_url(f"/rooms/{self.run}/call"), json=self.call_body(),
                                    headers={"Authorization": "Bearer " + self.tokens["pc_token"]}) as response:
            self.assertEqual(response.status, 401)
        async with self.client.post(self.server.make_url("/rooms"), json={},
                                    headers=self.cloud_headers) as response:
            self.assertEqual(response.status, 401)

    async def test_wrong_result_identity_ends_room_without_completing_the_tool(self):
        ws = await self.pc()
        task = asyncio.create_task(self.client.post(self.server.make_url(f"/rooms/{self.run}/call"),
                                                    json=self.call_body(), headers=self.cloud_headers))
        await ws.receive_json()
        await ws.send_json({"type": "result", "request_id": "wrong", "sequence": 1, "result": {}})
        self.assertEqual((await asyncio.wait_for(task, 2)).status, 409)
        await ws.close()


if __name__ == "__main__":
    unittest.main()
