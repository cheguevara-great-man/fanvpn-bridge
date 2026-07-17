"""Smoke-test the packaged EXE as a real Native Messaging child process."""

from __future__ import annotations

import json
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXE = ROOT / "dist" / "browser-ai-bridge" / "browser-ai-bridge.exe"


def read_native(stream) -> dict[str, object]:
    prefix = stream.read(4)
    if len(prefix) != 4:
        raise RuntimeError("EXE did not emit a Native Messaging frame")
    (length,) = struct.unpack("=I", prefix)
    payload = stream.read(length)
    if len(payload) != length:
        raise RuntimeError("EXE emitted a truncated Native Messaging frame")
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("EXE emitted a non-object Native Messaging frame")
    return value


def write_native(stream, value: dict[str, object]) -> None:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    stream.write(struct.pack("=I", len(payload)) + payload)
    stream.flush()


def main() -> int:
    exe = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_EXE
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    required_helpers = (
        "set_codex_network_mode.ps1",
        "set_vscode_claude_network_mode.ps1",
        "set_vscode_codex_product_endpoint.ps1",
        "set_vscode_codex_mode.ps1",
        "start_vscode_network_mode.ps1",
        "configure_usage_reporting.ps1",
    )
    missing_helpers = [name for name in required_helpers if not (exe.parent / "tools" / name).is_file()]
    if missing_helpers:
        raise RuntimeError(f"Packaged mode helpers are missing: {', '.join(missing_helpers)}")
    with tempfile.TemporaryDirectory(prefix="browser-ai-bridge-smoke-") as temporary_directory:
        config = json.loads((exe.parent / "routes.json").read_text(encoding="utf-8"))
        config["listen"]["port"] = port
        config_path = Path(temporary_directory) / "routes.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        process = subprocess.Popen(
            [str(exe), "--config", str(config_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin and process.stdout and process.stderr
        try:
            hello = read_native(process.stdout)
            if hello.get("type") != "hello" or hello.get("v") != 1:
                raise RuntimeError(f"Unexpected handshake: {hello}")
            write_native(
                process.stdin,
                {
                    "v": 1,
                    "type": "hello_ack",
                    "extension_version": "smoke-test",
                    "executor": "offscreen",
                },
            )

            deadline = time.monotonic() + 5
            health = None
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            while time.monotonic() < deadline:
                try:
                    with opener.open(
                        f"http://127.0.0.1:{port}/ready",
                        timeout=0.5,
                    ) as response:
                        health = json.loads(response.read())
                        break
                except OSError:
                    time.sleep(0.05)
            if not health or not health.get("ready") or health.get("pid") != process.pid:
                raise RuntimeError(f"Packaged gateway readiness check failed: {health}")

            process.stdin.close()
            return_code = process.wait(timeout=5)
            if return_code != 0:
                raise RuntimeError(process.stderr.read().decode("utf-8", errors="replace"))
            print("packaged native host: OK")
            return 0
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
