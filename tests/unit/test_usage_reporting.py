from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fanvpn_bridge.usage_reporting import TokenUsage, UsageExtractor, UsageReporter


class ImmediateDispatcher:
    def __init__(self) -> None:
        self.requests = []

    def submit(self, request, body, response) -> None:
        self.requests.append((request, b"".join(body)))
        url = request.route.upstream_url
        if url.endswith("/backend-api/wham/usage"):
            response.start(200, [])
            response.write(json.dumps({
                "plan_type": "pro",
                "rate_limit": {
                    "allowed": True, "limit_reached": False,
                    "primary_window": {
                        "used_percent": 18, "limit_window_seconds": 604800,
                        "reset_at": int(time.time()) + 3600,
                    },
                },
            }).encode())
        elif "/v1/usage/policy?" in url:
            response.start(200, [])
            response.write(b'{"blocked":false,"reason":"within_machine_credit_limit"}')
        else:
            response.start(202, [])
            response.write(b'{"accepted":1}')
        response.finish()

    def cancel(self, request_id: str, reason: str) -> None:
        return


class UsageReportingTests(unittest.TestCase):
    def test_extracts_final_responses_sse_usage_across_chunks(self) -> None:
        extractor = UsageExtractor()
        payload = {
            "type": "response.completed",
            "response": {
                "model": "gpt-test",
                "reasoning": {"effort": "high"},
                "service_tier": "priority",
                "usage": {
                    "input_tokens": 120,
                    "input_tokens_details": {"cached_tokens": 80},
                    "output_tokens": 30,
                    "output_tokens_details": {"reasoning_tokens": 10},
                    "total_tokens": 150,
                },
            },
        }
        wire = b"event: response.completed\ndata: " + json.dumps(payload).encode() + b"\n\n"
        for offset in range(0, len(wire), 17):
            extractor.feed(wire[offset : offset + 17])
        self.assertEqual(
            extractor.finish(),
            TokenUsage(120, 30, 150, 80, 10, "gpt-test", "high", "priority"),
        )

    def test_extracts_non_streaming_openai_usage(self) -> None:
        extractor = UsageExtractor()
        extractor.feed(
            b'{"model":"gpt-json","usage":{"prompt_tokens":7,'
            b'"completion_tokens":5,"total_tokens":12}}'
        )
        self.assertEqual(extractor.finish(), TokenUsage(7, 5, 12, model="gpt-json"))

    def test_ignores_response_content_without_usage(self) -> None:
        extractor = UsageExtractor()
        extractor.feed(b'data: {"type":"response.output_text.delta","delta":"private"}\n')
        self.assertIsNone(extractor.finish())

    def test_extracts_usage_from_completion_larger_than_capture_window(self) -> None:
        extractor = UsageExtractor()
        extractor.feed(b'data: {"output":[{"text":"' + b"x" * (2 * 1024 * 1024 + 100))
        extractor.feed(
            b'"}],"model":"gpt-large","usage":{"input_tokens":9,'
            b'"output_tokens":4,"total_tokens":13}}\n\n'
        )
        self.assertEqual(extractor.finish(), TokenUsage(9, 4, 13, model="gpt-large"))

    def test_persists_then_delivers_an_anonymous_usage_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dispatcher = ImmediateDispatcher()
            reporter = UsageReporter(
                Path(directory), dispatcher,
                collector_url="https://203.0.113.10:9443/v1/usage/events",
                report_token="secret-report-token",
                machine_id=str(uuid.uuid4()),
                machine_name="WORKSTATION-1",
            )
            reporter.record(TokenUsage(10, 2, 12, model="gpt-test"), route="chatgpt-codex")
            deadline = time.time() + 3
            while time.time() < deadline and reporter.snapshot()["delivered_events"] != 1:
                time.sleep(0.02)
            snapshot = reporter.snapshot()
            reporter.close()
            self.assertEqual(snapshot["pending_events"], 0)
            self.assertEqual(snapshot["delivered_total_tokens"], 12)
            request, body = dispatcher.requests[0]
            event = json.loads(body)
            self.assertNotIn("prompt", event)
            self.assertNotIn("response", event)
            self.assertEqual(event["machine_name"], "WORKSTATION-1")
            self.assertEqual(request.route.upstream_url, "https://203.0.113.10:9443/v1/usage/events")

    def test_syncs_only_sanitized_official_quota_and_machine_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text(json.dumps({
                "tokens": {"access_token": "private-token", "account_id": "private-account"}
            }), encoding="utf-8")
            dispatcher = ImmediateDispatcher()
            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}):
                reporter = UsageReporter(
                    Path(directory) / "runtime", dispatcher,
                    collector_url="https://203.0.113.10:9443/v1/usage/events",
                    report_token="secret-report-token",
                    machine_id=str(uuid.uuid4()), machine_name="WORKSTATION-1",
                )
            reporter._sync_quota_and_policy()
            snapshot = reporter.snapshot()
            reporter.close()
            quota_request = next(
                (request, body) for request, body in dispatcher.requests
                if request.route.upstream_url.endswith("/v1/usage/quota")
            )
            quota = json.loads(quota_request[1])
            self.assertEqual(quota["used_percent"], 18)
            self.assertNotIn("email", quota)
            self.assertNotIn("account_id", quota)
            self.assertNotIn("access_token", quota)
            self.assertFalse(snapshot["policy"]["blocked"])


if __name__ == "__main__":
    unittest.main()
