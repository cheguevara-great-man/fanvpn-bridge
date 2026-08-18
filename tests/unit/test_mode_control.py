from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from fanvpn_bridge.dispatcher import NativeDispatcher
from fanvpn_bridge.errors import BridgeError
from fanvpn_bridge.mode_control import (
    CodexModeController,
    MODE_BROWSER_FULL,
    MODE_BROWSER_LEAN,
    MODE_DIRECT,
    MODE_GEMINI_ACCOUNT,
    MODE_HYBRID_CONFIGURED,
    MODE_HYBRID_FORCE,
    MODE_HYBRID_NATIVE,
    MODE_UNMANAGED,
    ModeControlError,
    _friendly_failure,
)


class CodexModeControllerTests(unittest.TestCase):
    def make_controller(self, root: Path) -> CodexModeController:
        tools = root / "tools"
        tools.mkdir(exist_ok=True)
        (tools / "start_vscode_network_mode.ps1").write_text("# test", encoding="utf-8")
        powershell = root / "powershell.exe"
        powershell.write_bytes(b"test")
        return CodexModeController(
            codex_home=root / "codex",
            settings_path=root / "settings.json",
            state_path=root / "state.json",
            tools_directory=tools,
            powershell_path=powershell,
        )

    def write_config(self, root: Path, content: str) -> None:
        codex_home = root / "codex"
        codex_home.mkdir(exist_ok=True)
        (codex_home / "config.toml").write_text(content, encoding="utf-8")

    def test_detects_all_managed_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            controller = self.make_controller(root)
            self.assertEqual(controller.get_mode(), MODE_UNMANAGED)

            self.write_config(root, 'model_provider = "browser_ai_direct"\n')
            self.assertEqual(controller.get_mode(), MODE_DIRECT)

            self.write_config(root, 'model_provider = "browser_ai_bridge"\n')
            self.assertEqual(controller.get_mode(), MODE_BROWSER_LEAN)

            self.write_config(root, 'model_provider = "browser_ai_gemini_account"\n')
            self.assertEqual(controller.get_mode(), MODE_GEMINI_ACCOUNT)

            self.write_config(
                root,
                'model_provider = "browser_ai_bridge"\n\n'
                '[model_providers.browser_ai_bridge]\n'
                'base_url = "http://127.0.0.1:18888/gemini-account/v1"\n',
            )
            self.assertEqual(controller.get_mode(), MODE_GEMINI_ACCOUNT)

            self.write_config(
                root,
                'model_provider = "browser_ai_bridge"\n'
                'chatgpt_base_url = "http://127.0.0.1:18888/chatgpt-backend/backend-api/"\n',
            )
            self.assertEqual(controller.get_mode(), MODE_BROWSER_FULL)

            state = root / "state.json"
            state.parent.mkdir(parents=True, exist_ok=True)
            self.write_config(
                root,
                'model_provider = "browser_ai_bridge"\n\n'
                '[model_providers.browser_ai_bridge]\n'
                'base_url = "http://127.0.0.1:18888/hybrid/v1"\n',
            )
            for policy, expected in (
                ("force_gemini_37_high", MODE_HYBRID_FORCE),
                ("configured", MODE_HYBRID_CONFIGURED),
                ("native", MODE_HYBRID_NATIVE),
            ):
                (root / "subagent-policy.json").write_text(
                    '{"mode":"' + policy + '"}', encoding="utf-8"
                )
                self.assertEqual(controller.get_mode(), expected)

    def test_set_mode_uses_complete_vscode_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            controller = self.make_controller(root)

            def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
                self.assertTrue(command[0].endswith("powershell.exe"))
                self.assertTrue(command[command.index("-File") + 1].endswith("start_vscode_network_mode.ps1"))
                self.assertEqual(command[command.index("-Mode") + 1], "BrowserFull")
                actual_codex_home = Path(command[command.index("-CodexHome") + 1]).resolve()
                expected_codex_home = (root / "codex").resolve()
                self.assertEqual(actual_codex_home, expected_codex_home)
                self.write_config(
                    root,
                    'model_provider = "browser_ai_bridge"\n'
                    'chatgpt_base_url = "http://127.0.0.1:18888/chatgpt-backend/backend-api/"\n',
                )
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            with patch("fanvpn_bridge.mode_control.subprocess.run", side_effect=run):
                self.assertEqual(controller.set_mode(MODE_BROWSER_FULL), MODE_BROWSER_FULL)

    def test_running_vscode_has_a_specific_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            controller = self.make_controller(Path(temporary_directory))
            completed = SimpleNamespace(returncode=23, stdout=b"", stderr=b"")
            with patch("fanvpn_bridge.mode_control.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(ModeControlError, "Close every VS Code window"):
                    controller.set_mode(MODE_DIRECT)

    def test_known_launcher_failure_is_actionable_and_unknown_failure_is_safe(self) -> None:
        self.assertIn(
            "install_vscode_direct_mode.ps1",
            _friendly_failure(b"", b"Direct mode is not configured."),
        )
        self.assertEqual(
            _friendly_failure(b"secret arbitrary output", b"unexpected"),
            "Mode launch failed; configuration was restored automatically",
        )

    def test_native_controller_restores_files_after_launcher_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            controller = self.make_controller(root)
            self.write_config(root, 'model_provider = "user_provider"\n')
            original = (root / "codex" / "config.toml").read_bytes()
            agents = root / "codex" / "agents"
            agents.mkdir()
            active_role = agents / "browser-ai-bridge-reviewer.toml"
            disabled_role = agents / "browser-ai-bridge-reviewer.toml.disabled"
            active_role.write_text('name = "reviewer"\n', encoding="utf-8")

            def run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
                self.write_config(root, 'model_provider = "browser_ai_bridge"\n')
                active_role.replace(disabled_role)
                return SimpleNamespace(returncode=1, stdout=b"", stderr=b"unexpected")

            with patch("fanvpn_bridge.mode_control.subprocess.run", side_effect=run):
                with self.assertRaises(ModeControlError):
                    controller.set_mode(MODE_BROWSER_LEAN)
            self.assertEqual((root / "codex" / "config.toml").read_bytes(), original)
            self.assertTrue(active_role.is_file())
            self.assertFalse(disabled_role.exists())

    def test_browser_timing_parser_is_strict(self) -> None:
        timing = NativeDispatcher._parse_browser_timing(
            {
                "executor_queue_ms": 12,
                "fetch_head_ms": 345,
                "attempts": 2,
                "preemptions": 1,
            }
        )
        self.assertEqual(timing.fetch_head_ms, 345)
        with self.assertRaises(BridgeError):
            NativeDispatcher._parse_browser_timing(
                {
                    "executor_queue_ms": -1,
                    "fetch_head_ms": 1,
                    "attempts": 1,
                    "preemptions": 0,
                }
            )


if __name__ == "__main__":
    unittest.main()
