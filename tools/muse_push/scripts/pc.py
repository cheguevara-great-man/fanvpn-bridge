"""Send one PC tool request over HTTPS and wait for its real approved result.

Reads task metadata/catalog only. Does not exchange commands/results via files,
execute commands, install dependencies, or retry an ambiguous request.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request

from mailbox import MAX_BYTES, manifest_tools, read_json, reject_constant

LIVE_PROTOCOL = "codex-pc-rpc-v1"


def call(root, run, request, *, timeout=600, _opener=None):
    if not isinstance(run, str) or not re.fullmatch(r"[0-9a-f]{32}", run):
        raise ValueError("Invalid run")
    if not math.isfinite(timeout) or not 0 < timeout <= 1800:
        raise ValueError("Invalid timeout")
    if _opener is None:
        from persistent import exchange
        return exchange(root, run, request, timeout)
    manifest = read_json(root / "runs" / run / "manifest.json")
    if (manifest.get("run") != run or manifest.get("schema_version") != 2
            or manifest.get("protocol") != LIVE_PROTOCOL):
        raise ValueError("Invalid task metadata")
    tools = manifest_tools(root, manifest)
    if not isinstance(request, dict) or request.get("name") not in tools:
        raise ValueError("Unknown tool")
    field = "input" if tools[request["name"]]["type"] == "custom" else "arguments"
    if set(request) != {"name", field} or not isinstance(request[field], str if field == "input" else dict):
        raise ValueError("Invalid tool arguments")
    deadline = min(manifest["expires_at"], time.time() + timeout)
    if deadline <= time.time():
        raise ValueError("PC tool run expired")
    relay = manifest["relay"]
    origin = urlsplit(relay["url"])
    if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
            or origin.path not in {"", "/"} or origin.query or origin.fragment):
        raise ValueError("Invalid relay origin")
    # Respect the VM's configured egress proxy and its permission checks. Raw
    # HTTPSConnection ignores that environment and can take an unusable route.
    message = Request(relay["url"].rstrip("/") + f"/rooms/{run}/call", data=json.dumps({
            "request_id": secrets.token_hex(16), "expires_at": deadline,
            "sent_at_ms": time.time_ns() / 1_000_000, **request,
        }, ensure_ascii=False, allow_nan=False).encode("utf-8"), headers={
            "Authorization": "Bearer " + relay["token"], "Content-Type": "application/json",
        }, method="POST")
    # A broken connection must fail promptly, but an approved tool may legitimately
    # take minutes. urllib supplies proxy tunneling; TLS validation stays enabled.
    with _opener.open(message, timeout=max(1, deadline - time.time())) as response:
        raw = response.read(MAX_BYTES + 1)
        if response.status != 200 or len(raw) > MAX_BYTES:
            raise ValueError("PC exchange failed; no retry")
        result = json.loads(raw, parse_constant=reject_constant)["result"]
        if (set(result) != {"type", "call_id", "output"}
                or result["type"] != ("custom_tool_call_output" if field == "input" else "function_call_output")
                or not isinstance(result["call_id"], str) or not result["call_id"]
                or not isinstance(result["output"], (str, list))):
            raise ValueError("Invalid tool result")
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Oversized request")
        result = call(Path(__file__).resolve().parents[1], args.run,
                      json.loads(raw, parse_constant=reject_constant), timeout=args.timeout)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except Exception as error:
        # Neither authentication headers nor upstream response bodies are logged.
        cause = getattr(error, "reason", error)
        diagnostic = type(error).__name__ + "/" + type(cause).__name__
        code = getattr(error, "code", None)
        if type(code) is int:
            diagnostic += " HTTP=" + str(code)
        parser.exit(2, "PC push exchange failed (" + diagnostic + "); outcome unknown; do not replay the action.\n")


if __name__ == "__main__":
    main()
