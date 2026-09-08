from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class NativeMessagingSchemaTests(unittest.TestCase):
    def test_browser_timing_belongs_to_responses_and_errors_only(self) -> None:
        schema = json.loads(
            (ROOT / "contracts" / "native-messaging-v1.schema.json").read_text(encoding="utf-8")
        )
        definitions = schema["$defs"]
        request_properties = definitions["requestHead"]["allOf"][1]["properties"]
        response_properties = definitions["responseHead"]["allOf"][1]["properties"]
        error_properties = definitions["messageError"]["allOf"][1]["properties"]

        self.assertNotIn("timing", request_properties)
        self.assertEqual(response_properties["timing"]["$ref"], "#/$defs/browserTiming")
        self.assertEqual(error_properties["timing"]["$ref"], "#/$defs/browserTiming")

    def test_mode_schema_covers_the_independent_profile(self) -> None:
        schema = json.loads(
            (ROOT / "contracts" / "native-messaging-v1.schema.json").read_text(encoding="utf-8")
        )
        definitions = schema["$defs"]
        profile = definitions["codexProfile"]
        self.assertEqual(
            set(profile["required"]),
            {"vscode_network", "model_mode", "gpt_route", "subagent_policy"},
        )
        mode_set = definitions["controlModeSet"]["allOf"][1]["properties"]
        self.assertIn("vscode_network", mode_set)
        self.assertIn("gpt_route", mode_set)
        self.assertIn("subagent_policy", mode_set)
        mode_result = definitions["controlModeResult"]["allOf"][1]["properties"]
        self.assertEqual(mode_result["profile"]["$ref"], "#/$defs/codexProfile")


if __name__ == "__main__":
    unittest.main()
