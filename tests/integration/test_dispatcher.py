from __future__ import annotations

import json
import threading
import unittest

from fanvpn_bridge.config import RouteConfig
from fanvpn_bridge.contracts import EgressRequest, Header
from fanvpn_bridge.dispatcher import NativeDispatcher
from fanvpn_bridge.routing import RouteTable
from tests.helpers import CollectingSink, FakeExtension, channel_pair


class DispatcherIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host_channel, extension_channel = channel_pair()

        def responder(head: dict[str, object], body: bytes):
            payload = json.dumps(
                {
                    "method": head["method"],
                    "url": head["url"],
                    "body_bytes": len(body),
                    "prefix": body[:8].decode("ascii", errors="replace"),
                },
                separators=(",", ":"),
            ).encode()
            return 200, [["content-type", "application/json"]], [payload]

        self.extension = FakeExtension(extension_channel, responder)
        self.extension.start()
        self.dispatcher = NativeDispatcher(
            self.host_channel,
            max_chunk_bytes=256 * 1024,
            max_in_flight=4,
            request_timeout_seconds=5,
        )
        self.dispatcher.start(handshake_timeout=2)
        self.routes = RouteTable(
            {
                "openai": RouteConfig(
                    name="openai",
                    upstream_base_url="https://api.openai.com",
                )
            }
        )

    def tearDown(self) -> None:
        self.dispatcher.shutdown()

    def make_request(self, request_id: str, body: bytes) -> CollectingSink:
        sink = CollectingSink()
        route = self.routes.resolve("openai", "/v1/responses")
        request = EgressRequest(
            request_id=request_id,
            method="POST",
            route=route,
            headers=[Header("content-type", "application/json")],
        )
        self.dispatcher.submit(request, [body], sink)
        self.assertTrue(sink.done.wait(3), "response did not finish")
        self.assertIsNone(sink.error)
        return sink

    def test_request_larger_than_native_message_limit_is_chunked(self) -> None:
        body = b"abcdefgh" + b"x" * (2 * 1024 * 1024)
        sink = self.make_request("large_request_0001", body)
        response = json.loads(sink.body)
        self.assertEqual(response["body_bytes"], len(body))
        self.assertEqual(response["prefix"], "abcdefgh")
        self.assertLess(max(self.host_channel.sent_sizes), 1024 * 1024)
        self.assertGreater(self.extension.expected_seq["large_request_0001"], 4)

    def test_concurrent_requests_remain_isolated(self) -> None:
        results: dict[int, int] = {}
        failures: list[Exception] = []

        def worker(index: int) -> None:
            try:
                body = bytes([65 + index]) * (300_000 + index)
                sink = self.make_request(f"concurrent_request_{index:02d}", body)
                results[index] = json.loads(sink.body)["body_bytes"]
            except Exception as exc:  # pragma: no cover - reported by assertion below
                failures.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(failures, [])
        self.assertEqual(results, {index: 300_000 + index for index in range(4)})
        self.assertEqual(self.dispatcher.snapshot().active_requests, 0)


if __name__ == "__main__":
    unittest.main()
