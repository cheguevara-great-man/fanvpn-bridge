"""Dependency-free checks for the architecture scaffold."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "contracts" / "native-messaging-v1.schema.json"
ROUTES_PATH = ROOT / "config" / "routes.example.json"
EXTENSION_MANIFEST_PATH = ROOT / "chrome-extension" / "manifest.json"
NATIVE_HOST_PATH = ROOT / "native-host"


def main() -> int:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    routes = json.loads(ROUTES_PATH.read_text(encoding="utf-8"))
    extension_manifest = json.loads(EXTENSION_MANIFEST_PATH.read_text(encoding="utf-8"))

    assert schema["$defs"]["protocolVersion"]["const"] == 1
    assert routes["listen"]["host"] == "127.0.0.1"
    assert routes["protocol"]["max_chunk_bytes"] <= 256 * 1024
    assert routes["protocol"]["max_in_flight"] <= 16
    assert routes["protocol"]["max_active_requests"] <= 64
    assert routes["protocol"]["max_request_body_bytes"] <= 32 * 1024 * 1024

    for name, route in routes["routes"].items():
        assert name and "/" not in name
        assert route["upstream_base_url"].startswith("https://")

    sys.path.insert(0, str(NATIVE_HOST_PATH))
    module = importlib.import_module("fanvpn_bridge")
    dispatcher = importlib.import_module("fanvpn_bridge.dispatcher")
    assert module.ErrorCode.PROTOCOL_MISMATCH == "PROTOCOL_MISMATCH"
    assert dispatcher.HOST_VERSION == extension_manifest["version"]

    print("architecture scaffold: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
