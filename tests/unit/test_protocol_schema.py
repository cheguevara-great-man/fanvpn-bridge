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


if __name__ == "__main__":
    unittest.main()
