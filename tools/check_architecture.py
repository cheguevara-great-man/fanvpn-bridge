"""Dependency-free checks for the architecture scaffold."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "contracts" / "native-messaging-v1.schema.json"
ROUTES_PATH = ROOT / "config" / "routes.example.json"
NATIVE_HOST_PATH = ROOT / "native-host"


def main() -> int:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    routes = json.loads(ROUTES_PATH.read_text(encoding="utf-8"))

    assert schema["$defs"]["protocolVersion"]["const"] == 1
    assert routes["listen"]["host"] == "127.0.0.1"
    assert routes["protocol"]["max_chunk_bytes"] <= 256 * 1024
    assert routes["protocol"]["max_in_flight"] <= 16

    for name, route in routes["routes"].items():
        assert name and "/" not in name
        assert route["upstream_base_url"].startswith("https://")

    sys.path.insert(0, str(NATIVE_HOST_PATH))
    module = importlib.import_module("fanvpn_bridge")
    assert module.ErrorCode.PROTOCOL_MISMATCH == "PROTOCOL_MISMATCH"

    print("architecture scaffold: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
