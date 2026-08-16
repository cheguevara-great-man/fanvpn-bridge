from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fanvpn_bridge.subagent_config import SubagentConfigurationController
from fanvpn_bridge.subagent_policy import SubagentPolicyStore


class SubagentConfigurationTests(unittest.TestCase):
    def test_updates_only_managed_defaults_and_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text(
                "# BEGIN Browser AI Bridge managed subagent defaults\n"
                "# previous-subagent-model-base64: absent\n"
                "# previous-subagent-effort-base64: absent\n"
                "# END Browser AI Bridge managed subagent defaults\n"
                "model = \"gpt-main\"\n\n"
                "[agents]\n"
                "default_subagent_model = \"gemini-3.7-flash\"\n"
                "default_subagent_reasoning_effort = \"high\"\n",
                encoding="utf-8",
            )
            (home / "browser-ai-bridge-gemini-models.json").write_text(
                json.dumps({"models": [{
                    "slug": "gemini-3.7-flash", "display_name": "Gemini 3.7 Flash",
                    "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
                }]}), encoding="utf-8",
            )
            store = SubagentPolicyStore(home / "policy.json")
            controller = SubagentConfigurationController(home, store)
            result = controller.apply({
                "default_model": "gemini-3.7-flash",
                "default_reasoning_effort": "low",
                "roles": [{
                    "name": "reviewer",
                    "description": "Review correctness and security.",
                    "developer_instructions": "Report concrete findings with evidence.",
                    "model": "gemini-3.7-flash",
                    "model_reasoning_effort": "high",
                }],
            })
            text = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "gpt-main"', text)
            self.assertIn('default_subagent_reasoning_effort = "low"', text)
            role_path = home / "agents" / "browser-ai-bridge-reviewer.toml"
            self.assertTrue(role_path.is_file())
            self.assertEqual(result["roles"][0]["name"], "reviewer")
            self.assertEqual(store.read().default_reasoning_effort, "low")

            controller.apply({
                "default_model": "gemini-3.7-flash",
                "default_reasoning_effort": "high",
                "roles": [],
            })
            self.assertFalse(role_path.exists())


if __name__ == "__main__":
    unittest.main()
