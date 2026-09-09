from __future__ import annotations

import io
import json
import tempfile
import base64
import hashlib
import zipfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

from fanvpn_bridge.web_harness import WebHarnessController, WebHarnessError, clean_web_history, is_web_model, relay_web_response, runtime_token
from fanvpn_bridge.web_harness_install import install_archive


class WebHarnessTests(unittest.TestCase):
    def test_native_encrypted_history_is_untouched(self):
        payload = {"input": [{"type": "reasoning", "encrypted_content": "native-secret", "id": "rs_native"}]}
        self.assertIs(clean_web_history(payload), payload)

    def test_reasoning_cleanup_preserves_message_ids_and_turn_metadata(self):
        context = {
            "type": "message", "id": "msg_context", "role": "user",
            "internal_chat_message_metadata_passthrough": {"turn_id": "turn_two"},
            "content": [{"type": "input_text", "text": "<environment_context />"}],
        }
        payload = {"input": [context, {
            "type": "reasoning", "id": "rs_" + "a" * 32,
            "summary": [{"type": "summary_text", "text": "Planning"}],
        }]}
        cleaned = clean_web_history(payload)
        self.assertEqual(cleaned["input"][0], context)
        self.assertEqual(payload["input"][0], context)

    def test_web_checkpoint_is_translated_without_decrypting_native_reasoning(self):
        payload = {"previous_response_id": "web-local", "input": [
            {"type": "compaction", "encrypted_content": "ocx1:" + base64.b64encode(b"next steps").decode()},
            {"type": "reasoning", "encrypted_content": "native-secret", "id": "rs_native"}]}
        cleaned = clean_web_history(payload)
        self.assertNotIn("previous_response_id", cleaned)
        self.assertIn("next steps", cleaned["input"][0]["content"][0]["text"])
        self.assertEqual(cleaned["input"][1]["encrypted_content"], "native-secret")

    def test_install_rejects_traversal_even_with_valid_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "package.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("../outside", "bad")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            with patch.object(WebHarnessController, "status", return_value={}):
                with self.assertRaises(WebHarnessError):
                    install_archive(archive, digest, Path(directory) / "home")
            self.assertFalse((Path(directory) / "outside").exists())

    def test_install_preserves_previous_version(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "package.zip"
            home = Path(directory) / "home"
            home.mkdir()
            (home / "installation.json").write_text(json.dumps({"directory": "versions/previous"}))
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("WebHarness.exe", "test fixture")
                package.writestr("resources/app.asar", "test fixture")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            with patch.object(WebHarnessController, "status", return_value={}):
                install_archive(archive, digest, home)
            state = json.loads((home / "installation.json").read_text())
            self.assertEqual(state["previous_directory"], "versions/previous")
            self.assertTrue((home / state["directory"] / "WebHarness.exe").exists())

    def test_only_explicit_web_namespace_is_local(self):
        self.assertTrue(is_web_model("chatgpt-web/plus"))
        for value in (None, {}, "gpt-6", "gemini-3.7-flash", "prefix-chatgpt-web/x"):
            self.assertFalse(is_web_model(value))

    def test_installation_cannot_escape_own_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "installation.json").write_text(json.dumps({"directory": "../outside"}))
            with self.assertRaises(WebHarnessError):
                WebHarnessController(home).open()

    def test_control_token_must_not_contain_header_injection(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "config.json").write_text(json.dumps({"controlToken": "a" * 42 + "\r\nX: y"}))
            with self.assertRaises(WebHarnessError):
                runtime_token(home)

    def test_gateway_network_file_contains_only_loopback_proxy(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "web"
            local_app_data = Path(temporary) / "local"
            config = local_app_data / "FanVPNBridge" / "direct-proxy.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({
                "host": "proxy.example", "port": 443,
                "username": "private-user", "password": "private-password",
            }))
            with patch.dict("os.environ", {"LOCALAPPDATA": str(local_app_data)}):
                WebHarnessController(home)._write_network("gateway")
            value = json.loads((home / "network.json").read_text())
            self.assertEqual(value, {
                "mode": "gateway",
                "tunnelProxyUrl": "http://127.0.0.1:18889",
                "browserProxyUrl": "http://127.0.0.1:18889",
            })
            self.assertNotIn("private", (home / "network.json").read_text())

    def test_open_starts_gateway_proxy_before_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "web"
            executable = home / "versions" / "current" / "WebHarness.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"fixture")
            (home / "installation.json").write_text(json.dumps({"directory": "versions/current"}))
            (home / "network.json").write_text(json.dumps({
                "mode": "gateway", "tunnelProxyUrl": "http://127.0.0.1:18889",
                "browserProxyUrl": "http://127.0.0.1:18889",
            }))
            controller = WebHarnessController(home)
            with patch.object(controller, "_ensure_gateway_proxy") as ensure, \
                    patch.object(controller, "_show_running_launcher", return_value=False), \
                    patch.object(controller, "status", return_value={}), \
                    patch("fanvpn_bridge.web_harness.subprocess.Popen") as launch:
                controller.open()
            ensure.assert_called_once_with()
            launch.assert_called_once()

    def test_open_shows_an_existing_launcher_without_spawning_another(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "web"
            executable = home / "versions" / "current" / "WebHarness.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"fixture")
            (home / "installation.json").write_text(json.dumps({"directory": "versions/current"}))
            controller = WebHarnessController(home)
            with patch.object(controller, "_show_running_launcher", return_value=True), \
                    patch.object(controller, "status", return_value={"running": True}), \
                    patch("fanvpn_bridge.web_harness.subprocess.Popen") as launch:
                result = controller.open()
            self.assertEqual(result, {"running": True})
            launch.assert_not_called()

    def test_gateway_proxy_ready_requires_the_managed_health_response(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"status":"ok","mode":"vscode-direct-proxy"}'
        with patch("fanvpn_bridge.web_harness.http.client.HTTPConnection", return_value=connection):
            self.assertTrue(WebHarnessController._gateway_proxy_ready())
        connection.request.assert_called_once_with(
            "GET",
            "http://browser-ai-bridge.local/ready",
            headers={"Host": "browser-ai-bridge.local"},
        )
        connection.close.assert_called_once_with()

        response.read.return_value = b'{"status":"ok","mode":"other-service"}'
        with patch("fanvpn_bridge.web_harness.http.client.HTTPConnection", return_value=connection):
            self.assertFalse(WebHarnessController._gateway_proxy_ready())

    def test_gateway_proxy_start_registers_the_shared_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            local_app_data = Path(temporary)
            config = local_app_data / "FanVPNBridge" / "direct-proxy.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({
                "host": "proxy.example", "port": 443,
                "username": "user", "password": "password",
            }))
            process = MagicMock(pid=4567)
            controller = WebHarnessController(Path(temporary) / "web")
            with patch.dict("os.environ", {"LOCALAPPDATA": str(local_app_data)}), \
                    patch.object(controller, "_gateway_proxy_ready", side_effect=[False, True]), \
                    patch("fanvpn_bridge.web_harness.subprocess.Popen", return_value=process):
                controller._ensure_gateway_proxy()
            self.assertEqual(
                (config.parent / "direct-proxy.pid").read_text(encoding="ascii"),
                "4567",
            )

    def test_stream_strips_account_credentials_and_flushes_each_chunk(self):
        handler = MagicMock()
        handler.headers = Message()
        for key, value in {"Authorization": "Bearer private", "Cookie": "secret", "ChatGPT-Account-ID": "private-account",
                           "Connection": "X-Private", "X-Private": "secret", "Content-Type": "application/json"}.items():
            handler.headers[key] = value
        handler.wfile = io.BytesIO()
        upstream = MagicMock()
        response = upstream.getresponse.return_value
        response.status = 200
        response.getheaders.return_value = [("Content-Type", "text/event-stream"), ("Set-Cookie", "secret")]
        response.read1.side_effect = [b"data: first\n\n", b"data: [DONE]\n\n", b""]
        with patch("fanvpn_bridge.web_harness.http.client.HTTPConnection", return_value=upstream), patch(
            "fanvpn_bridge.web_harness.runtime_token", return_value="local-control"
        ):
            relay_web_response(handler, "POST", "/responses", b'{}')
        headers = upstream.request.call_args.args[3]
        self.assertEqual(headers, {"Content-Type": "application/json", "X-Bridge-Web-Token": "local-control"})
        self.assertEqual(upstream.request.call_args.args[1], "/bridge/v1/responses")
        self.assertIn(b"data: first", handler.wfile.getvalue())
        self.assertTrue(handler.wfile.getvalue().endswith(b"0\r\n\r\n"))
        self.assertNotIn(unittest.mock.call("Set-Cookie", "secret"), handler.send_header.call_args_list)
        upstream.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
