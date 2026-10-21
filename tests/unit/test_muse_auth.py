from __future__ import annotations

import json
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import ModuleType
from unittest.mock import Mock, patch

from fanvpn_bridge.dispatcher import NativeDispatcher
from fanvpn_bridge.muse_auth import BrowserTicketClient, MuseAuthError, validate_ticket
from fanvpn_bridge.muse_service import ServiceError, gateway_factory
from fanvpn_bridge.muse_web import MuseWebError, MuseWebProvider
from tests.helpers import channel_pair

TICKET = {"vm_id": "test-vm_1", "access_token": "test-access", "hatch_token": "test-hatch"}
KEY = "test-only-" + "b" * 40


class MuseAuthTests(unittest.TestCase):
    def test_ticket_validation_rejects_extra_fields_invalid_hosts_and_tokens(self):
        self.assertEqual(validate_ticket(TICKET), TICKET)
        self.assertIsNot(validate_ticket(TICKET), TICKET)
        values = [None, [], {}, {**TICKET, "cookies": "secret"}]
        values += [{**TICKET, "vm_id": vm} for vm in ("", "../evil", "x.example", "a" * 129, [])]
        for name in ("access_token", "hatch_token"):
            values += [{**TICKET, name: token} for token in ("", "a\r\nb", "a b", "中文", "x" * 16385, None)]
        for value in values:
            with self.subTest(value_type=type(value)), self.assertRaises(MuseAuthError) as caught:
                validate_ticket(value)
            self.assertEqual(caught.exception.code, "muse_web_ticket_invalid")
        self.assertEqual(MuseAuthError(["secret"]).code, "muse_web_browser_unavailable")

    def test_ticket_client_rejects_non_loopback_urls(self):
        for url in ("https://127.0.0.1", "http://localhost", "http://example.test", "http://127.0.0.1/path",
                    "http://user@127.0.0.1", "http://127.0.0.1?key=secret", "http://127.0.0.1#x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                BrowserTicketClient(bridge_url=url, api_key=KEY)

    def test_provider_authenticates_before_requesting_browser_ticket(self):
        callback = Mock(return_value=TICKET)
        provider = MuseWebProvider(base_url="http://127.0.0.1:8008/v1", api_key=KEY, ticket_provider=callback)
        for auth in ("", "Bearer wrong"):
            with self.assertRaises(MuseWebError) as caught:
                provider.browser_ticket(auth)
            self.assertEqual(caught.exception.status, 401)
        callback.assert_not_called()
        self.assertEqual(provider.browser_ticket("Bearer " + KEY), TICKET)
        callback.side_effect = RuntimeError("SECRET")
        with self.assertRaises(MuseWebError) as caught:
            provider.browser_ticket("Bearer " + KEY)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_factory_uses_fresh_browser_tickets_without_reading_cookies(self):
        module = ModuleType("muse_cli.gateway")
        module.AuthError = type("AuthError", (RuntimeError,), {})
        module.Gateway = Mock()
        module.load_cookies = Mock(side_effect=AssertionError("Cookie access must not occur"))
        client = Mock()
        second = {**TICKET, "hatch_token": "fresh-ticket"}
        client.ticket.side_effect = [TICKET, second, MuseAuthError("muse_web_login_required")]
        with patch.dict(sys.modules, {"muse_cli.gateway": module}), patch(
                "fanvpn_bridge.muse_service.BrowserTicketClient", return_value=client):
            factory = gateway_factory({"api_key": KEY})
            factory()
            factory()
            with self.assertRaises(ServiceError) as caught:
                factory()
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(caught.exception.code, "muse_web_login_required")
        self.assertEqual(module.Gateway.call_count, 2)
        self.assertEqual(module.Gateway.call_args_list[0].args, ("",))
        self.assertEqual(module.Gateway.call_args_list[0].kwargs, TICKET)
        self.assertEqual(module.Gateway.call_args_list[1].kwargs, second)
        module.load_cookies.assert_not_called()


class MuseTicketClientTests(unittest.TestCase):
    def setUp(self):
        owner = self
        self.status, self.value, self.redirect, self.raw = 200, TICKET, None, None
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.requests.append((self.path, dict(self.headers)))
                raw = owner.raw if owner.raw is not None else json.dumps(owner.value).encode()
                self.send_response(owner.status)
                self.send_header("Content-Length", str(len(raw)))
                if owner.redirect:
                    self.send_header("Location", owner.redirect)
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = BrowserTicketClient(bridge_url=f"http://127.0.0.1:{self.server.server_port}", api_key=KEY)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_private_post_and_safe_error_mapping_without_redirects(self):
        self.assertEqual(self.client.ticket(), TICKET)
        path, headers = self.requests[0]
        self.assertEqual(path, "/muse-web/auth")
        self.assertEqual(headers["Authorization"], "Bearer " + KEY)
        self.assertEqual(headers["Content-Length"], "0")
        self.status, self.value = 401, {"error": {"code": "muse_web_login_required", "message": "SECRET"}}
        with self.assertRaises(MuseAuthError) as caught:
            self.client.ticket()
        self.assertEqual(caught.exception.code, "muse_web_login_required")
        self.assertNotIn("SECRET", str(caught.exception))
        self.status, self.value, self.redirect = 302, {}, "https://evil.test/"
        with self.assertRaises(MuseAuthError):
            self.client.ticket()
        self.assertEqual(len(self.requests), 3)

    def test_invalid_and_oversized_replies_are_rejected(self):
        for raw in (b"not json SECRET", b"x" * (128 * 1024 + 1), b'{"error":{"code":[]}}'):
            self.raw = raw
            self.status = 502
            with self.assertRaises(MuseAuthError) as caught:
                self.client.ticket()
            self.assertNotIn("SECRET", str(caught.exception))


class MuseAuthDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.host, self.peer = channel_pair()
        self.dispatcher = NativeDispatcher(self.host, max_chunk_bytes=262144, max_in_flight=4,
                                           request_timeout_seconds=2)
        # Exercise the actual Native Messaging reader, without browser/network access.
        self.peer.send({"v": 1, "type": "hello_ack", "executor": "offscreen", "extension_version": "test",
                        "muse_browser_auth": True})
        self.dispatcher.start(handshake_timeout=1)
        self.assertEqual(self.peer.receive()["type"], "hello")
        self.pool = ThreadPoolExecutor(max_workers=5)

    def tearDown(self):
        self.dispatcher.shutdown()
        self.pool.shutdown(wait=True)

    def reply(self, request, **values):
        self.peer.send({"v": 1, "type": "control.muse_auth.result", "id": request["id"], **values})

    def test_parallel_tickets_are_isolated_and_reader_still_answers_ping(self):
        first = self.pool.submit(self.dispatcher.get_muse_auth_ticket)
        one = self.peer.receive()
        second = self.pool.submit(self.dispatcher.get_muse_auth_ticket)
        two = self.peer.receive()
        self.assertNotEqual(one["id"], two["id"])
        self.peer.send({"v": 1, "type": "ping", "nonce": "auth-wait"})
        self.assertEqual(self.peer.receive()["type"], "pong")
        other = {**TICKET, "vm_id": "other"}
        self.reply(two, ok=True, ticket=other)
        self.reply(one, ok=True, ticket=TICKET)
        self.assertEqual(first.result(1), TICKET)
        self.assertEqual(second.result(1), other)
        self.assertEqual(self.dispatcher._pending_muse_auth, {})

    def test_failure_and_malformed_result_do_not_break_dispatcher(self):
        for result, code in (({"ok": False, "error_code": "muse_web_login_required"}, "muse_web_login_required"),
                             ({"ok": False, "error_code": ["SECRET"]}, "muse_web_browser_unavailable"),
                             ({"ok": True, "ticket": {**TICKET, "vm_id": "../evil"}}, "muse_web_ticket_invalid")):
            future = self.pool.submit(self.dispatcher.get_muse_auth_ticket)
            self.reply(self.peer.receive(), **result)
            with self.assertRaises(MuseAuthError) as caught:
                future.result(1)
            self.assertEqual(caught.exception.code, code)
        self.assertTrue(self.dispatcher.snapshot().native_channel_connected)

    def test_timeout_cleans_up_and_ignores_late_result(self):
        future = self.pool.submit(self.dispatcher.get_muse_auth_ticket, timeout=0.03)
        request = self.peer.receive()
        with self.assertRaises(MuseAuthError) as caught:
            future.result(1)
        self.assertEqual(caught.exception.code, "muse_web_auth_timeout")
        self.assertEqual(self.dispatcher._pending_muse_auth, {})
        self.reply(request, ok=True, ticket=TICKET)
        next_future = self.pool.submit(self.dispatcher.get_muse_auth_ticket)
        self.reply(self.peer.receive(), ok=True, ticket=TICKET)
        self.assertEqual(next_future.result(1), TICKET)

    def test_pending_auth_is_bounded_and_shutdown_wakes_waiters(self):
        futures = [self.pool.submit(self.dispatcher.get_muse_auth_ticket) for _ in range(4)]
        for _ in futures:
            self.assertEqual(self.peer.receive()["type"], "control.muse_auth.get")
        with self.assertRaises(MuseAuthError) as caught:
            self.dispatcher.get_muse_auth_ticket()
        self.assertEqual(caught.exception.code, "muse_web_rate_limited")
        self.dispatcher.shutdown()
        for future in futures:
            with self.assertRaises(MuseAuthError) as caught:
                future.result(1)
            self.assertEqual(caught.exception.code, "muse_web_browser_unavailable")

    def test_old_extension_is_rejected_before_sending_auth(self):
        self.dispatcher._muse_auth_supported = False
        with self.assertRaises(MuseAuthError) as caught:
            self.dispatcher.get_muse_auth_ticket()
        self.assertEqual(caught.exception.code, "muse_web_extension_required")
        self.assertEqual(self.dispatcher._pending_muse_auth, {})


if __name__ == "__main__":
    unittest.main()
