"""Inert, per-turn Codex handoff. No network, subprocess or tool execution."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time

MAX_BYTES = 2 * 1024 * 1024


def reject_constant(_value):
    raise ValueError("non-JSON numeric constant")


def read_json(path):
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("mailbox file is too large")
    return json.loads(raw, parse_constant=reject_constant)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["tools", "request", "result"])
    parser.add_argument("--run", required=True)
    parser.add_argument("--name")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{32}", args.run):
        parser.error("run must be 32 lowercase hexadecimal characters")
    directory = Path(__file__).resolve().parents[1] / "runs" / args.run
    try:
        manifest = read_json(directory / "manifest.json")
        if manifest.get("run") != args.run or manifest.get("schema_version") != 1:
            raise ValueError("invalid manifest")
        tools = {tool["name"]: tool for tool in manifest["tools"]}
        if args.action == "tools":
            if args.name:
                if args.name not in tools:
                    raise ValueError("unknown tool")
                value = tools[args.name]
            else:
                value = [{"name": tool["name"], "type": tool["type"]} for tool in tools.values()]
        elif args.action == "result":
            value = read_json(directory / "result.json")
            if value.get("run") != args.run or value.get("schema_version") != 1:
                raise ValueError("invalid result")
        else:
            if time.time() > manifest["expires_at"]:
                raise ValueError("run expired")
            raw = sys.stdin.buffer.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("request is too large")
            request = json.loads(raw, parse_constant=reject_constant)
            if not isinstance(request, dict) or request.get("name") not in tools:
                raise ValueError("unknown tool")
            custom = tools[request["name"]]["type"] == "custom"
            field = "input" if custom else "arguments"
            if set(request) != {"name", field} or not isinstance(request[field], str if custom else dict):
                raise ValueError("invalid tool input")
            # Exclusive creation prevents replacement or a second call per turn.
            value = {"schema_version": 1, "run": args.run, **request}
            with (directory / "request.json").open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False)
            value = {"run": args.run, "status": "awaiting_codex"}
        print(json.dumps(value, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError):
        # Paths or tool contents must not leak through exception traces.
        parser.error("mailbox operation rejected; check run, tool definition and whether a request already exists")


if __name__ == "__main__":
    main()
