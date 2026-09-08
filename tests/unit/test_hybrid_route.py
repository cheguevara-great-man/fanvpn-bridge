from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fanvpn_bridge.hybrid_route import HybridRouteStore


class HybridRouteStoreTests(unittest.TestCase):
    def test_defaults_are_browser_full_and_system_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = HybridRouteStore(Path(directory) / "hybrid-route.json").read()
            self.assertEqual(state, {"gpt_route": "browser_full", "vscode_network": "system"})

    def test_write_is_validated_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = HybridRouteStore(Path(directory) / "hybrid-route.json")
            store.write(gpt_route="direct", vscode_network="server")
            self.assertEqual(store.read(), {"gpt_route": "direct", "vscode_network": "server"})
            with self.assertRaises(ValueError):
                store.write(gpt_route="arbitrary", vscode_network="server")


if __name__ == "__main__":
    unittest.main()
