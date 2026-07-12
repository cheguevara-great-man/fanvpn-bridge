"""Smoke-test the packaged EXE as a real Native Messaging child process."""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXE = ROOT / "dist" / "fanvpn-bridge" / "fanvpn-bridge.exe"


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
    process = subprocess.Popen(
        [str(exe)],
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
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:18888/__bridge/health",
                    timeout=0.5,
                ) as response:
                    health = json.loads(response.read())
                    break
            except OSError:
                time.sleep(0.05)
        if not health or not health.get("native_channel_connected"):
            raise RuntimeError(f"Packaged gateway health check failed: {health}")

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
