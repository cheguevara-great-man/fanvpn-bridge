"""Inert, per-turn Codex handoff. No network, subprocess or tool execution."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time

MAX_BYTES = 2 * 1024 * 1024
CATALOG_SCHEMA_VERSION = 2


def reject_constant(_value):
    raise ValueError("non-JSON numeric constant")


def read_bytes(path):
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("mailbox file is too large")
    return raw


def read_json(path):
    return json.loads(read_bytes(path), parse_constant=reject_constant)


def manifest_tools(root, manifest):
    if manifest["schema_version"] == 1:
        definitions = manifest["tools"]
    elif manifest["schema_version"] == CATALOG_SCHEMA_VERSION:
        digest = manifest["catalog_sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid catalog identity")
        raw = read_bytes(root / "catalogs" / (digest + ".json"))
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("catalog hash mismatch")
        catalog = json.loads(raw, parse_constant=reject_constant)
        if catalog["schema_version"] != 1:
            raise ValueError("unsupported catalog")
        definitions = catalog["tools"]
    else:
        raise ValueError("unsupported manifest")
    if not isinstance(definitions, list):
        raise ValueError("invalid tool definitions")
    tools = {}
    for tool in definitions:
        if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]
                or tool.get("type") not in {"function", "custom"} or tool["name"] in tools):
            raise ValueError("invalid tool definition")
        tools[tool["name"]] = tool
    return tools


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["tools", "request", "result"])
    parser.add_argument("--run", required=True)
    parser.add_argument("--name")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{32}", args.run):
        parser.error("run must be 32 lowercase hexadecimal characters")
    root = Path(__file__).resolve().parents[1]
    directory = root / "runs" / args.run
    try:
        manifest = read_json(directory / "manifest.json")
        if not isinstance(manifest, dict) or manifest.get("run") != args.run:
            raise ValueError("invalid manifest")
        tools = manifest_tools(root, manifest)
        if args.action == "tools":
            if args.name:
                if args.name not in tools:
                    raise ValueError("unknown tool")
                value = tools[args.name]
            else:
                value = [{"name": tool["name"], "type": tool["type"]} for tool in tools.values()]
        elif args.action == "result":
            value = read_json(directory / "result.json")
            if not isinstance(value, dict) or value.get("run") != args.run or value.get("schema_version") != 1:
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
