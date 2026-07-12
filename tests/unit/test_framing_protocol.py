from __future__ import annotations

import io
import threading
import time
import unittest

from fanvpn_bridge.errors import BridgeError, ErrorCode
from fanvpn_bridge.framing import encode_message, read_message
from fanvpn_bridge.protocol import FlowWindow, decode_body_frame, iter_body_frames


class FramingTests(unittest.TestCase):
    def test_round_trip_unicode_object(self) -> None:
        framed = encode_message({"v": 1, "type": "ping", "nonce": "中文"})
        decoded = read_message(io.BytesIO(framed))
        self.assertEqual(decoded, {"v": 1, "type": "ping", "nonce": "中文"})

    def test_rejects_host_message_over_one_mib(self) -> None:
        with self.assertRaises(BridgeError) as caught:
            encode_message({"value": "x" * (1024 * 1024)})
        self.assertEqual(caught.exception.code, ErrorCode.MESSAGE_TOO_LARGE)

    def test_rejects_non_object_json(self) -> None:
        framed = b"\x02\x00\x00\x00[]"
        with self.assertRaises(BridgeError):
            read_message(io.BytesIO(framed))

    def test_rejects_truncated_length_prefix(self) -> None:
        with self.assertRaises(BridgeError):
            read_message(io.BytesIO(b"\x05\x00"))


class ProtocolTests(unittest.TestCase):
    def test_splits_large_body_and_marks_one_end(self) -> None:
        body = [b"a" * 700_000, b"b" * 600_000]
        frames = list(iter_body_frames("request.body", "request_123456", body))
        self.assertGreater(len(frames), 4)
        self.assertEqual([frame["seq"] for frame in frames], list(range(len(frames))))
        self.assertEqual(sum(bool(frame["end"]) for frame in frames), 1)
        self.assertTrue(frames[-1]["end"])
        decoded = b"".join(
            decode_body_frame(
                frame,
                expected_type="request.body",
                expected_id="request_123456",
                expected_seq=index,
            )[0]
            for index, frame in enumerate(frames)
        )
        self.assertEqual(decoded, b"".join(body))

    def test_empty_body_emits_final_empty_frame(self) -> None:
        frames = list(iter_body_frames("request.body", "request_123456", []))
        self.assertEqual(len(frames), 1)
        decoded, end = decode_body_frame(
            frames[0],
            expected_type="request.body",
            expected_id="request_123456",
            expected_seq=0,
        )
        self.assertEqual(decoded, b"")
        self.assertTrue(end)

    def test_flow_window_blocks_until_ack(self) -> None:
        window = FlowWindow(max_in_flight=1)
        released = threading.Event()

        def waiter() -> None:
            window.wait_to_send(1, timeout=1)
            released.set()

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.05)
        self.assertFalse(released.is_set())
        window.acknowledge(0)
        thread.join(1)
        self.assertTrue(released.is_set())


if __name__ == "__main__":
    unittest.main()
