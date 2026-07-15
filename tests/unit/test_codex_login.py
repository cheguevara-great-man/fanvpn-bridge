from __future__ import annotations

import base64
import hashlib
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener

from fanvpn_bridge.codex_login import (
    DEFAULT_CLIENT_ID,
    ExchangedTokens,
    build_authorize_url,
    generate_pkce,
    run_codex_login,
    save_auth_document,
)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _id_token(account_id: str = "account-test") -> str:
    header = _base64url(json.dumps({"alg": "none"}).encode("utf-8"))
    payload = _base64url(
        json.dumps(
            {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
        ).encode("utf-8")
    )
    return f"{header}.{payload}.signature"


class _BridgeState:
    def __init__(self) -> None:
        self.form: dict[str, list[str]] | None = None


class _BridgeHandler(BaseHTTPRequestHandler):
    server_version = "FakeBridge/1.0"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/ready":
            self._json(200, {"ready": True})
        elif self.path == "/routes":
            self._json(200, {"routes": ["auth-openai"]})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/auth-openai/oauth/token":
            self._json(404, {"error": "not_found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("ascii")
        state = self.server.state  # type: ignore[attr-defined]
        state.form = parse_qs(body, keep_blank_values=True)
        self._json(
            200,
            {
                "id_token": _id_token(),
                "access_token": "access-test",
                "refresh_token": "refresh-test",
            },
        )

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, value: dict[str, object]) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CodexLoginTests(unittest.TestCase):
    def test_pkce_and_official_authorize_parameters(self) -> None:
        pkce = generate_pkce()
        expected = _base64url(hashlib.sha256(pkce.verifier.encode("ascii")).digest())
        self.assertEqual(pkce.challenge, expected)
        self.assertGreaterEqual(len(pkce.verifier), 43)

        url = build_authorize_url(
            redirect_uri="http://localhost:1455/auth/callback",
            pkce=pkce,
            state="state-test",
        )
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", "https://auth.openai.com/oauth/authorize")
        self.assertEqual(query["client_id"], [DEFAULT_CLIENT_ID])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["state"], ["state-test"])
        self.assertIn("offline_access", query["scope"][0].split())

    def test_auth_file_is_atomic_and_existing_file_is_backed_up(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            existing = home / "auth.json"
            existing.write_text('{"old": true}\n', encoding="utf-8")
            document = {"auth_mode": "chatgpt", "tokens": {"id_token": "secret"}}

            auth_path, backup_path = save_auth_document(home, document)

            self.assertEqual(json.loads(auth_path.read_text(encoding="utf-8")), document)
            self.assertIsNotNone(backup_path)
            assert backup_path is not None
            self.assertEqual(json.loads(backup_path.read_text(encoding="utf-8")), {"old": True})
            self.assertEqual(list(home.glob(".auth.*.tmp")), [])

    def test_complete_login_exchanges_through_bridge_and_writes_codex_auth(self) -> None:
        bridge_state = _BridgeState()
        bridge = ThreadingHTTPServer(("127.0.0.1", 0), _BridgeHandler)
        bridge.state = bridge_state  # type: ignore[attr-defined]
        bridge_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        bridge_thread.start()
        opened = threading.Event()
        captured_url: list[str] = []

        def open_browser(url: str) -> bool:
            captured_url.append(url)
            opened.set()
            return True

        with TemporaryDirectory() as directory:
            result_box: list[object] = []

            def login() -> None:
                try:
                    result_box.append(
                        run_codex_login(
                            codex_home=Path(directory),
                            bridge_url=f"http://127.0.0.1:{bridge.server_address[1]}",
                            callback_ports=(0,),
                            timeout_seconds=5,
                            browser_opener=open_browser,
                        )
                    )
                except BaseException as error:  # surfaced in the test thread below
                    result_box.append(error)

            login_thread = threading.Thread(target=login)
            login_thread.start()
            self.assertTrue(opened.wait(2))
            query = parse_qs(urlsplit(captured_url[0]).query)
            callback = f"{query['redirect_uri'][0]}?code=code-test&state={query['state'][0]}"
            with build_opener(ProxyHandler({})).open(callback, timeout=2) as response:
                self.assertEqual(response.status, 200)
            login_thread.join(3)
            self.assertFalse(login_thread.is_alive())
            self.assertEqual(len(result_box), 1)
            if isinstance(result_box[0], BaseException):
                raise result_box[0]

            auth = json.loads((Path(directory) / "auth.json").read_text(encoding="utf-8"))
            self.assertEqual(auth["auth_mode"], "chatgpt")
            self.assertEqual(auth["tokens"]["account_id"], "account-test")
            self.assertEqual(auth["tokens"]["access_token"], "access-test")
            self.assertEqual(auth["tokens"]["refresh_token"], "refresh-test")
            self.assertEqual(bridge_state.form["grant_type"], ["authorization_code"])
            self.assertEqual(bridge_state.form["code"], ["code-test"])
            self.assertEqual(bridge_state.form["client_id"], [DEFAULT_CLIENT_ID])
            self.assertEqual(bridge_state.form["redirect_uri"], query["redirect_uri"])
            self.assertTrue(bridge_state.form["code_verifier"][0])

        bridge.shutdown()
        bridge.server_close()
        bridge_thread.join(2)

    def test_state_mismatch_does_not_exchange_or_end_login(self) -> None:
        exchange_count = 0
        opened = threading.Event()
        captured_url: list[str] = []

        def open_browser(url: str) -> bool:
            captured_url.append(url)
            opened.set()
            return True

        def exchange(_code: str, _callback: str, _verifier: str) -> ExchangedTokens:
            nonlocal exchange_count
            exchange_count += 1
            return ExchangedTokens(_id_token(), "access", "refresh")

        bridge_state = _BridgeState()
        bridge = ThreadingHTTPServer(("127.0.0.1", 0), _BridgeHandler)
        bridge.state = bridge_state  # type: ignore[attr-defined]
        bridge_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        bridge_thread.start()
        with TemporaryDirectory() as directory:
            failures: list[BaseException] = []

            def login() -> None:
                try:
                    run_codex_login(
                        codex_home=Path(directory),
                        bridge_url=f"http://127.0.0.1:{bridge.server_address[1]}",
                        callback_ports=(0,),
                        timeout_seconds=5,
                        browser_opener=open_browser,
                        token_exchanger=exchange,
                    )
                except BaseException as error:
                    failures.append(error)

            login_thread = threading.Thread(target=login)
            login_thread.start()
            self.assertTrue(opened.wait(2))
            query = parse_qs(urlsplit(captured_url[0]).query)
            redirect_uri = query["redirect_uri"][0]
            opener = build_opener(ProxyHandler({}))
            try:
                opener.open(f"{redirect_uri}?code=bad&state=wrong", timeout=2)
            except Exception:
                pass
            self.assertEqual(exchange_count, 0)
            self.assertTrue(login_thread.is_alive())
            with opener.open(
                f"{redirect_uri}?code=good&state={query['state'][0]}", timeout=2
            ) as response:
                self.assertEqual(response.status, 200)
            login_thread.join(3)
            self.assertEqual(failures, [])
            self.assertEqual(exchange_count, 1)

        bridge.shutdown()
        bridge.server_close()
        bridge_thread.join(2)


if __name__ == "__main__":
    unittest.main()
