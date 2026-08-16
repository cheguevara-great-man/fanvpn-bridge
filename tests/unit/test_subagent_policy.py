from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fanvpn_bridge.contracts import Header
from fanvpn_bridge.subagent_policy import (
    POLICY_CONFIGURED,
    POLICY_FORCE_GEMINI,
    SubagentPolicyConfig,
    SubagentPolicyStore,
)


class SubagentPolicyTests(unittest.TestCase):
    def test_force_policy_overrides_collaboration_subagent_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SubagentPolicyStore(Path(directory) / "policy.json")
            store.write(SubagentPolicyConfig(POLICY_FORCE_GEMINI, "gemini-3.7-flash", "high"))
            original = {"model": "gpt-5.6-sol", "reasoning": {"effort": "medium"}}
            applied = store.apply(original, [Header("x-openai-subagent", "collab_spawn")])
            self.assertTrue(applied.overridden)
            self.assertEqual(applied.payload["model"], "gemini-3.7-flash")
            self.assertEqual(applied.payload["reasoning"]["effort"], "high")
            self.assertEqual(original["model"], "gpt-5.6-sol")

            main = store.apply(original, [])
            self.assertFalse(main.overridden)
            self.assertEqual(main.payload, original)

    def test_internal_compaction_and_memory_are_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SubagentPolicyStore(Path(directory) / "policy.json")
            store.write(SubagentPolicyConfig(POLICY_FORCE_GEMINI))
            for kind in ("compact", "memory_consolidation"):
                applied = store.apply({"model": "gpt-test"}, [Header("X-OpenAI-Subagent", kind)])
                self.assertFalse(applied.overridden)

    def test_configured_policy_does_not_rewrite_explicit_codex_choice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SubagentPolicyStore(Path(directory) / "policy.json")
            store.write(SubagentPolicyConfig(POLICY_CONFIGURED))
            applied = store.apply(
                {"model": "gpt-5.6-terra", "reasoning": {"effort": "low"}},
                [Header("x-openai-subagent", "collab_spawn")],
            )
            self.assertFalse(applied.overridden)
            self.assertEqual(applied.payload["model"], "gpt-5.6-terra")

    def test_corrupt_policy_fails_closed_to_native(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text("not-json", encoding="utf-8")
            store = SubagentPolicyStore(path)
            self.assertEqual(store.read().mode, "native")


if __name__ == "__main__":
    unittest.main()
