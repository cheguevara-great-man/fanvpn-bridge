from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "set_codex_network_mode.ps1"


@unittest.skipUnless(os.name == "nt", "PowerShell network-mode test is Windows-only")
class NetworkModeScriptTests(unittest.TestCase):
    def run_mode(
        self, codex_home: Path, mode: str, gemini_models_json: str | None = None
    ) -> str:
        command = [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                "-Mode",
                mode,
                "-CodexHome",
                str(codex_home),
            ]
        if gemini_models_json is not None:
            command.extend(["-GeminiModelsJson", gemini_models_json])
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
        return (codex_home / "config.toml").read_text(encoding="utf-8")

    def test_browser_lean_mode_disables_product_backend_features(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            config_path = codex_home / "config.toml"
            original_url = "https://custom.example/backend-api/"
            config_path.write_text(
                f'model = "gpt-test"\nchatgpt_base_url = "{original_url}"\n\n'
                "[features]\napps = true\nplugins = true # custom\n\n"
                "[analytics]\nenabled = true\n",
                encoding="utf-8",
            )

            browser = self.run_mode(codex_home, "Browser")
            self.assertIn('model_provider = "browser_ai_bridge"', browser)
            self.assertIn(f'chatgpt_base_url = "{original_url}"', browser)
            self.assertNotIn("chatgpt-backend", browser)
            self.assertEqual(browser.count("apps = false"), 1)
            self.assertEqual(browser.count("plugins = false"), 1)
            self.assertEqual(browser.count("remote_plugin = false"), 1)
            self.assertEqual(browser.count("enabled = false"), 1)
            self.assertEqual(browser.count("shell_snapshot = false"), 1)
            self.assertIn("managed lean mode", browser)

            direct = self.run_mode(codex_home, "Direct")
            self.assertIn('model_provider = "browser_ai_direct"', direct)
            self.assertIn(f'chatgpt_base_url = "{original_url}"', direct)
            self.assertIn("apps = true", direct)
            self.assertIn("plugins = true # custom", direct)
            self.assertIn("enabled = true", direct)
            self.assertNotIn("remote_plugin =", direct)
            self.assertNotIn("shell_snapshot =", direct)
            self.assertNotIn("managed lean mode", direct)

    def test_browser_mode_is_idempotent_and_restores_absent_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            config_path = codex_home / "config.toml"
            config_path.write_text('model = "gpt-test"\n', encoding="utf-8")

            self.run_mode(codex_home, "Browser")
            second = self.run_mode(codex_home, "Browser")
            third = self.run_mode(codex_home, "Browser")
            self.assertEqual(second, third)
            self.assertEqual(second.count("managed lean mode"), 2)
            self.assertEqual(second.count("apps = false"), 1)

            direct = self.run_mode(codex_home, "Direct")
            self.assertNotIn("apps =", direct)
            self.assertNotIn("plugins =", direct)
            self.assertNotIn("remote_plugin =", direct)
            self.assertNotIn("enabled =", direct)

    def test_gemini_account_mode_uses_local_responses_provider_and_restores_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            config_path = codex_home / "config.toml"
            config_path.write_text(
                'model = "gpt-user-default"\n\n[features]\napps = true\n',
                encoding="utf-8",
            )

            gemini = self.run_mode(codex_home, "GeminiAccount")
            self.assertIn('model_provider = "browser_ai_gemini_account"', gemini)
            self.assertIn(
                'base_url = "http://127.0.0.1:18888/gemini-account/v1"',
                gemini,
            )
            self.assertIn("requires_openai_auth = true", gemini)
            self.assertIn("apps = false", gemini)
            self.assertIn('model = "gemini-3.7-flash-tiered"', gemini)
            self.assertIn("managed Gemini model catalog", gemini)
            catalog_path = codex_home / "browser-ai-bridge-gemini-models.json"
            self.assertTrue(catalog_path.exists())
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            models = {item["slug"]: item for item in catalog["models"]}
            self.assertIn("gemini-3.6-flash-tiered", models)
            self.assertNotIn("gemini-3.6-flash-high", models)
            self.assertEqual(
                models["gemini-3.6-flash-tiered"]["display_name"],
                "Gemini 3.6 Flash",
            )
            self.assertEqual(
                [
                    level["effort"]
                    for level in models["gemini-3.6-flash-tiered"][
                        "supported_reasoning_levels"
                    ]
                ],
                ["low", "medium", "high"],
            )
            self.assertIn("gemini-3.1-pro-high", models)
            self.assertEqual(
                [
                    level["effort"]
                    for level in models["gemini-3.1-pro-high"][
                        "supported_reasoning_levels"
                    ]
                ],
                ["high"],
            )
            self.assertNotIn('model = "gpt-user-default"', gemini)
            self.assertIn("managed Gemini model", gemini)

            gemini_second = self.run_mode(codex_home, "GeminiAccount")
            self.assertEqual(gemini, gemini_second)

            direct = self.run_mode(codex_home, "Direct")
            self.assertIn('model_provider = "browser_ai_direct"', direct)
            self.assertIn('model = "gpt-user-default"', direct)
            self.assertNotIn('model = "gemini-3.7-flash-tiered"', direct)
            self.assertNotIn("managed Gemini model", direct)
            self.assertNotIn("model_catalog_json", direct)
            self.assertIn("apps = true", direct)
            self.assertNotIn("managed lean mode", direct)

    def test_gemini_catalog_uses_current_official_model_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            (codex_home / "config.toml").write_text(
                'model = "gpt-user-default"\n', encoding="utf-8"
            )
            official_models = json.dumps(
                [
                    {
                        "id": "gemini-3.7-flash",
                        "display_name": "Gemini 3.7 Flash",
                        "default_reasoning_level": "medium",
                        "supported_reasoning_levels": ["low", "medium", "high"],
                    },
                    {
                        "id": "gemini-3.8-flash",
                        "display_name": "Gemini 3.8 Flash",
                        "default_reasoning_level": "medium",
                        "supported_reasoning_levels": ["low", "medium", "high"],
                    },
                ]
            )

            gemini = self.run_mode(
                codex_home, "GeminiAccount", official_models
            )
            catalog = json.loads(
                (
                    codex_home / "browser-ai-bridge-gemini-models.json"
                ).read_text(encoding="utf-8")
            )
            models = {item["slug"]: item for item in catalog["models"]}

            self.assertIn('model = "gemini-3.8-flash"', gemini)
            self.assertIn("gemini-3.8-flash", models)
            self.assertIn("gemini-3.7-flash", models)
            self.assertEqual(
                models["gemini-3.8-flash"]["display_name"],
                "Gemini 3.8 Flash",
            )
            self.assertEqual(
                [level["effort"] for level in models["gemini-3.8-flash"]["supported_reasoning_levels"]],
                ["low", "medium", "high"],
            )

    def test_full_and_lean_modes_switch_without_losing_user_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            config_path = codex_home / "config.toml"
            original_url = "https://custom.example/backend-api/"
            config_path.write_text(
                f'chatgpt_base_url = "{original_url}"\nmodel = "gpt-test"\n\n'
                "[features]\napps = true\nplugins = true\nremote_plugin = true\n\n"
                "[analytics]\nenabled = true\n",
                encoding="utf-8",
            )

            full = self.run_mode(codex_home, "BrowserFull")
            self.assertIn(
                'chatgpt_base_url = "http://127.0.0.1:18888/chatgpt-backend/backend-api/"',
                full,
            )
            self.assertNotIn(original_url, full)
            self.assertIn("apps = true", full)
            self.assertIn("plugins = true", full)
            self.assertIn("shell_snapshot = false", full)
            self.assertIn("managed ChatGPT base URL", full)
            full_second = self.run_mode(codex_home, "BrowserFull")
            self.assertEqual(full_second, self.run_mode(codex_home, "BrowserFull"))

            lean = self.run_mode(codex_home, "BrowserLean")
            self.assertIn(f'chatgpt_base_url = "{original_url}"', lean)
            self.assertNotIn("chatgpt-backend", lean)
            self.assertIn("apps = false", lean)
            self.assertIn("plugins = false", lean)

            full_again = self.run_mode(codex_home, "BrowserFull")
            self.assertIn("chatgpt-backend", full_again)
            self.assertIn("apps = true", full_again)
            self.assertIn("plugins = true", full_again)

            direct = self.run_mode(codex_home, "Direct")
            self.assertIn(f'chatgpt_base_url = "{original_url}"', direct)
            self.assertIn("apps = true", direct)
            self.assertIn("plugins = true", direct)
            self.assertNotIn("shell_snapshot =", direct)
            self.assertNotIn("managed ChatGPT base URL", direct)

    def test_browser_modes_restore_existing_shell_snapshot_value_in_direct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            config_path = codex_home / "config.toml"
            config_path.write_text(
                'model = "gpt-test"\n\n[features]\nshell_snapshot = true # user value\n',
                encoding="utf-8",
            )

            full = self.run_mode(codex_home, "BrowserFull")
            self.assertIn("shell_snapshot = false", full)
            self.assertNotIn("shell_snapshot = true", full)
            lean = self.run_mode(codex_home, "BrowserLean")
            self.assertIn("shell_snapshot = false", lean)
            direct = self.run_mode(codex_home, "Direct")
            self.assertIn("shell_snapshot = true # user value", direct)
            self.assertNotIn("managed Windows compatibility", direct)

    def test_upgrade_removes_221_backend_route_and_restores_original_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            config_path = codex_home / "config.toml"
            original_line = (
                "chatgpt_base_url = 'https://custom.example/backend-api/' # keep me"
            )
            encoded = base64.b64encode(original_line.encode()).decode()
            config_path.write_text(
                "# BEGIN Browser AI Bridge managed ChatGPT base URL\n"
                f"# previous-chatgpt-base-url-base64: {encoded}\n"
                'chatgpt_base_url = "http://127.0.0.1:18888/chatgpt-backend/"\n'
                "# END Browser AI Bridge managed ChatGPT base URL\n"
                'model = "gpt-test"\n',
                encoding="utf-8",
            )

            browser = self.run_mode(codex_home, "Browser")
            self.assertIn(original_line, browser)
            self.assertNotIn("chatgpt-backend", browser)
            self.assertNotIn("managed ChatGPT base URL", browser)

    def test_upgrade_removes_unmanaged_legacy_backend_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            config_path = codex_home / "config.toml"
            config_path.write_text(
                'chatgpt_base_url = "http://127.0.0.1:18888/chatgpt-backend/"\n'
                'model = "gpt-test"\n',
                encoding="utf-8",
            )

            direct = self.run_mode(codex_home, "Direct")
            self.assertNotIn("chatgpt_base_url", direct)
            self.assertNotIn("chatgpt-backend", direct)

    def test_cc_switch_provider_snapshot_is_replaced_without_duplicate_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            config_path = codex_home / "config.toml"
            config_path.write_text(
                'model_provider = "browser_ai_bridge"\n\n'
                '[model_providers.browser_ai_bridge]\n'
                'name = "CC Switch snapshot"\n'
                'base_url = "http://127.0.0.1:18888/chatgpt-codex"\n'
                'requires_openai_auth = true\n'
                'wire_api = "responses"\n'
                'supports_websockets = false\n\n'
                '[windows]\nsandbox = "unelevated"\n',
                encoding="utf-8",
            )

            full = self.run_mode(codex_home, "BrowserFull")

            self.assertEqual(
                full.count("[model_providers.browser_ai_bridge]"), 1
            )
            self.assertEqual(
                full.count("[model_providers.browser_ai_direct]"), 1
            )
            self.assertNotIn("CC Switch snapshot", full)
            self.assertIn('[windows]\nsandbox = "unelevated"', full)
            self.assertIn(
                'chatgpt_base_url = "http://127.0.0.1:18888/chatgpt-backend/backend-api/"',
                full,
            )

    def test_hybrid_modes_merge_catalog_and_restore_configured_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            config_path = codex_home / "config.toml"
            config_path.write_text(
                'model = "gpt-5.6-sol"\n\n'
                '[agents]\n'
                'default_subagent_model = "gpt-user-choice"\n'
                'default_subagent_reasoning_effort = "low"\n',
                encoding="utf-8",
            )
            (codex_home / "models_cache.json").write_text(
                json.dumps({
                    "models": [{
                        "slug": "gpt-5.6-sol",
                        "display_name": "GPT-5.6 Sol",
                        "visibility": "list",
                        "model_messages": {"instructions_template": "You are Codex."},
                    }]
                }),
                encoding="utf-8",
            )
            models = json.dumps([{
                "id": "gemini-3.7-flash",
                "display_name": "Gemini 3.7 Flash",
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": ["low", "medium", "high"],
            }])

            configured = self.run_mode(codex_home, "HybridConfigured", models)
            self.assertIn('base_url = "http://127.0.0.1:18888/hybrid/v1"', configured)
            self.assertIn('chatgpt_base_url = "http://127.0.0.1:18888/chatgpt-backend/backend-api/"', configured)
            self.assertIn('default_subagent_model = "gemini-3.7-flash"', configured)
            catalog = json.loads((codex_home / "browser-ai-bridge-gemini-models.json").read_text(encoding="utf-8"))
            slugs = {item["slug"] for item in catalog["models"]}
            self.assertIn("gpt-5.6-sol", slugs)
            self.assertIn("gemini-3.7-flash", slugs)

            native = self.run_mode(codex_home, "HybridNative", models)
            self.assertIn('default_subagent_model = "gpt-user-choice"', native)
            self.assertIn('default_subagent_reasoning_effort = "low"', native)
            self.assertNotIn("managed subagent defaults", native)

            force = self.run_mode(codex_home, "HybridForce", models)
            self.assertIn('model = "gpt-5.6-sol"', force)
            self.assertNotIn('default_subagent_model = "gemini-3.7-flash"', force)


if __name__ == "__main__":
    unittest.main()
