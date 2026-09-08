"""Persist the independent Hybrid GPT transport selection."""

from __future__ import annotations

import json
import os
from pathlib import Path


GPT_ROUTE_DIRECT = "direct"
GPT_ROUTE_BROWSER_FULL = "browser_full"
GPT_ROUTE_SERVER_CENTER = "server_center"
SUPPORTED_GPT_ROUTES = frozenset({
    GPT_ROUTE_DIRECT,
    GPT_ROUTE_BROWSER_FULL,
    GPT_ROUTE_SERVER_CENTER,
})

VSCODE_NETWORK_SYSTEM = "system"
VSCODE_NETWORK_SERVER = "server"
SUPPORTED_VSCODE_NETWORKS = frozenset({VSCODE_NETWORK_SYSTEM, VSCODE_NETWORK_SERVER})


class HybridRouteStore:
    """Small atomic state file read by the loopback Hybrid router."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, str]:
        value: object = {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        data = value if isinstance(value, dict) else {}
        gpt_route = data.get("gpt_route")
        vscode_network = data.get("vscode_network")
        return {
            "gpt_route": str(gpt_route) if gpt_route in SUPPORTED_GPT_ROUTES else GPT_ROUTE_BROWSER_FULL,
            "vscode_network": (
                str(vscode_network)
                if vscode_network in SUPPORTED_VSCODE_NETWORKS
                else VSCODE_NETWORK_SYSTEM
            ),
        }

    def write(self, *, gpt_route: str, vscode_network: str) -> dict[str, str]:
        if gpt_route not in SUPPORTED_GPT_ROUTES:
            raise ValueError("Unsupported Hybrid GPT route")
        if vscode_network not in SUPPORTED_VSCODE_NETWORKS:
            raise ValueError("Unsupported VS Code network")
        value = {"gpt_route": gpt_route, "vscode_network": vscode_network}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.next")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
        return value
