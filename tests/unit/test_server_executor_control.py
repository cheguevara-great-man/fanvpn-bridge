from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fanvpn_bridge.server_executor_control import (
    MODE_BROWSER_CHAIN,
    MODE_SERVER_CENTER,
    ServerExecutorTransportController,
)
from fanvpn_bridge.server_client import write_server_client_config


class ServerExecutorTransportControllerTests(unittest.TestCase):
    def make_controller(self, root: Path, *, ready: bool = True):
        cache = root / "cache"
        codex = root / "codex"
        config = cache / "server-executor.json"
        write_server_client_config(
            config,
            executor_url="https://107.174.199.230:9444/v1/codex",
            device_token="x" * 32,
            transport="direct",
        )
        (codex / "config.toml").parent.mkdir(parents=True, exist_ok=True)
        (codex / "config.toml").write_text(
            'model_provider = "browser_ai_bridge"\n\n'
            '[model_providers.browser_ai_bridge]\n'
            'base_url = "http://127.0.0.1:18888/chatgpt-codex"\n',
            encoding="utf-8",
        )
        calls: list[object] = []
        controller = ServerExecutorTransportController(
            cache_base=cache,
            codex_home=codex,
            client_config_path=config,
            process_starter=lambda command: calls.append(command),
            process_stopper=lambda: calls.append("stop"),
            readiness_probe=lambda: ready,
        )
        return controller, config, codex / "config.toml", calls

    def test_switch_to_server_center_uses_browser_transport_and_remembers_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, config_path, codex_path, calls = self.make_controller(Path(temporary))
            state = controller.set_mode(MODE_SERVER_CENTER)
            self.assertEqual(state["mode"], MODE_SERVER_CENTER)
            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8"))["transport"], "browser")
            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8"))["browser_bridge_url"],
                "http://127.0.0.1:18888/server-executor",
            )
            config = codex_path.read_text(encoding="utf-8")
            self.assertIn('model_provider = "server_codex_executor"', config)
            self.assertIn("[model_providers.server_codex_executor]", config)
            self.assertEqual(calls[0], "stop")
            self.assertIsInstance(calls[1], list)

    def test_switching_back_restores_previous_provider_and_stops_only_client(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _config_path, codex_path, calls = self.make_controller(Path(temporary))
            controller.set_mode(MODE_SERVER_CENTER)
            state = controller.set_mode(MODE_BROWSER_CHAIN)
            self.assertEqual(state["mode"], MODE_BROWSER_CHAIN)
            self.assertIn('model_provider = "browser_ai_bridge"', codex_path.read_text(encoding="utf-8"))
            self.assertEqual(calls.count("stop"), 2)

    def test_missing_device_registration_has_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = ServerExecutorTransportController(
                cache_base=root / "cache",
                codex_home=root / "codex",
                client_config_path=root / "cache" / "server-executor.json",
                readiness_probe=lambda: False,
            )
            with self.assertRaisesRegex(RuntimeError, "注册这台设备"):
                controller.set_mode(MODE_SERVER_CENTER)

    def test_failed_start_restores_transport_and_stops_partial_client(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, config_path, codex_path, calls = self.make_controller(root)
            controller._process_starter = lambda _command: (_ for _ in ()).throw(OSError("start failed"))
            with self.assertRaisesRegex(RuntimeError, "无法启动"):
                controller.set_mode(MODE_SERVER_CENTER)
            restored = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(restored["transport"], "direct")
            self.assertNotIn("browser_bridge_url", restored)
            self.assertIn('model_provider = "browser_ai_bridge"', codex_path.read_text(encoding="utf-8"))
            self.assertGreaterEqual(calls.count("stop"), 2)

    def test_recovers_selected_server_client_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, config_path, codex_path, calls = self.make_controller(root, ready=False)
            codex_path.write_text('model_provider = "server_codex_executor"\n', encoding="utf-8")
            self.assertTrue(controller.recover_selected_mode())
            self.assertEqual(calls[0], "stop")
            self.assertIsInstance(calls[1], list)
            recovered = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(recovered["transport"], "browser")
            self.assertEqual(recovered["browser_bridge_url"], "http://127.0.0.1:18888/server-executor")

    def test_invalid_recovery_configuration_falls_back_to_browser_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            codex = root / "codex"
            codex.mkdir(parents=True)
            (codex / "config.toml").write_text(
                'model_provider = "server_codex_executor"\n', encoding="utf-8"
            )
            (cache / "server-executor-ui.json").parent.mkdir(parents=True)
            (cache / "server-executor-ui.json").write_text(
                '{"previous_model_provider":"browser_ai_bridge"}\n', encoding="utf-8"
            )
            calls: list[object] = []
            controller = ServerExecutorTransportController(
                cache_base=cache,
                codex_home=codex,
                client_config_path=cache / "missing.json",
                process_stopper=lambda: calls.append("stop"),
                readiness_probe=lambda: False,
            )
            self.assertFalse(controller.recover_selected_mode())
            self.assertIn(
                'model_provider = "browser_ai_bridge"',
                (codex / "config.toml").read_text(encoding="utf-8"),
            )
            self.assertGreaterEqual(calls.count("stop"), 1)


if __name__ == "__main__":
    unittest.main()
