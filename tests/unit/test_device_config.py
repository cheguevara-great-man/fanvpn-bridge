from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from fanvpn_bridge.device_config import DeviceConfigController, DeviceConfigError


class DeviceConfigControllerTests(unittest.TestCase):
    def test_writes_valid_enrollment_without_exposing_token_in_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = DeviceConfigController(Path(directory))
            machine_id = str(uuid.uuid4())
            state = controller.apply({
                "machine_id": machine_id,
                "machine_name": "公司电脑-03",
                "report_token": "secret-device-token-1234567890",
                "collector_url": "https://203.0.113.10:9443/v1/usage/events",
                "dashboard_url": "https://203.0.113.10:9443/dashboard",
            })
            self.assertTrue(state["configured"])
            self.assertTrue(state["restart_required"])
            self.assertNotIn("report_token", state)
            saved = json.loads((Path(directory) / "usage-reporting.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["machine_id"], machine_id)
            self.assertEqual(saved["report_token"], "secret-device-token-1234567890")

    def test_rejects_non_https_or_wrong_collector_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = DeviceConfigController(Path(directory))
            with self.assertRaises(DeviceConfigError):
                controller.apply({
                    "machine_id": str(uuid.uuid4()), "machine_name": "PC",
                    "report_token": "secret-device-token-1234567890",
                    "collector_url": "http://example.test/v1/usage/events",
                    "dashboard_url": "https://example.test/dashboard",
                })


if __name__ == "__main__":
    unittest.main()
