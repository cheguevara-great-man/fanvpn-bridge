"""Explicit live acceptance with inert tools; never executes a requested action.

Starts an ephemeral loopback test server, not the installed Bridge or sidecar.
Leaves isolated Muse conversations and mailbox receipts for inspection.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "native-host"))

from fanvpn_bridge.muse_service import MuseSessions, gateway_factory, make_server
from fanvpn_bridge.muse_web import MuseWebError, MuseWebProvider


def verify(config, workspace, only="both"):
    factory = gateway_factory(config)
    audit = {}

    class AuditedGateway:
        def __init__(self):
            self.inner = factory()

        def call_json(self, method, **kwargs):
            audit["last_rpc"] = method
            try:
                value = self.inner.call_json(method, **kwargs)
                if method == "session.start":
                    audit["session_id"] = value.get("session_id") or value.get("id")
                return value
            except Exception as error:
                audit["failed_rpc"] = method
                audit["error_type"] = type(error).__name__
                audit["gateway_status"] = getattr(error, "status", None)
                raise

        def _open(self, method, **kwargs):
            audit["last_rpc"] = method
            return self.inner._open(method, **kwargs)

        def close(self):
            self.inner.close()

    sessions = MuseSessions(AuditedGateway, timeout=240, poll=2, tool_workspace=workspace)
    key = secrets.token_hex(32)
    server = make_server({"api_key": key, "port": 0}, sessions)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = MuseWebProvider(base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key=key, timeout_seconds=260)
    reports = []
    try:
        for custom in ((False,) if only == "function" else (True,) if only == "freeform" else (False, True)):
            label = "namespaced_freeform" if custom else "function"
            print("Starting inert Muse acceptance: " + label, flush=True)
            started = time.monotonic()
            expected = ("*** Begin Patch\n*** Add File: never-created.md\n"
                        "+中文 `code` | \"quoted\" | C:\\example\\file\n+第二行\n*** End Patch\n")
            tool = ({"type": "custom", "name": "apply_patch", "description":
                     "Inert protocol acceptance probe. Input is checked but NEVER applied or executed."} if custom else {
                     "type": "function", "name": "codex_probe", "description":
                     "Inert protocol acceptance probe. Returns a random verifier marker; performs no other action.",
                     "parameters": {"type": "object", "properties": {"challenge": {"type": "string"}},
                                    "required": ["challenge"], "additionalProperties": False}})
            tools = [{"type": "namespace", "name": "functions", "tools": [tool]}] if custom else [tool]
            challenge = secrets.token_hex(16)
            instructions = ("This is an inert Codex protocol acceptance test, not a file-editing task. "
                            "Use only the explicitly supplied Codex handoff skill/helper; do not use muse-bridge, "
                            "edit files, execute the requested input or change settings/memories. "
                            "After receiving the Codex tool result, reply with exactly its marker text, no other words.")
            prompt = ("Request functions.apply_patch exactly once with this exact raw input (JSON-encoded here for clarity):\n"
                      + json.dumps(expected, ensure_ascii=False) if custom else
                      'Request codex_probe exactly once with {"challenge":"' + challenge + '"}.')
            payload = {"model": "muse-web/default", "instructions": instructions, "tools": tools, "input": prompt}
            _, response = provider.responses(payload)
            print("Received Muse handoff candidate: " + label, flush=True)
            if len(response["output"]) != 1:
                raise ValueError("Expected one inert tool call")
            call = response["output"][0]
            if custom:
                valid = (call.get("type") == "custom_tool_call" and call.get("name") == "apply_patch"
                         and call.get("namespace") == "functions" and call.get("input") == expected)
            else:
                valid = (call.get("type") == "function_call" and call.get("name") == "codex_probe"
                         and json.loads(call["arguments"]) == {"challenge": challenge})
            if not valid:
                raise ValueError("Muse did not hand off the exact expected tool input")
            state = sessions.snapshots[response["id"]]
            cloud_session = state.session_id
            run = state.pending["run"]
            # Generated AFTER the call: the model cannot guess this from the initial prompt.
            marker = "CODEX_INERT_RESULT_" + secrets.token_hex(24)
            result = {"type": "custom_tool_call_output" if custom else "function_call_output",
                      "call_id": call["call_id"], "output": marker}
            _, final = provider.responses({**payload, "previous_response_id": response["id"], "input": [result]})
            print("Received Muse result continuation: " + label, flush=True)
            text = "".join(part["text"] for item in final["output"] if item.get("type") == "message"
                           for part in item["content"] if part.get("type") == "output_text")
            if text.strip() != marker or sessions.snapshots[final["id"]].session_id != cloud_session:
                raise ValueError("Muse did not return the actual Codex result in the same conversation")
            gateway = sessions.factory()
            try:
                receipt = sessions.workspace.read(gateway, sessions.workspace.path(run, "result"))
                if receipt["result"] != result or receipt["run"] != run:
                    raise ValueError("Independent Muse result receipt did not match")
                for name in ("SKILL.md", "scripts/mailbox.py", "skill.yaml"):
                    raw = (ROOT / "tools/muse_tools" / name).read_text(encoding="utf-8")
                    raw = raw.replace("__SKILL_NAME__", workspace.rsplit("/", 1)[1]).replace("__SKILL_ROOT__", "~/" + workspace)
                    if sessions.workspace.read_bytes(gateway, workspace + "/" + name) != raw.encode("utf-8"):
                        raise ValueError("Live test unexpectedly changed the helper skill")
            finally:
                gateway.close()
            reports.append({"test": label, "same_session": True, "exact_input": True,
                            "independent_result_receipt": True, "skill_unchanged": True,
                            "elapsed_seconds": round(time.monotonic() - started, 1)})
            print(json.dumps(reports[-1]), flush=True)
        return reports
    except Exception:
        # Operation names, status codes and our isolated session ID only, no bodies/tickets.
        print("Probe transport audit: " + json.dumps(audit), flush=True)
        raise
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool-workspace", required=True)
    parser.add_argument("--only", choices=["both", "function", "freeform"], default="both")
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("LOCALAPPDATA", Path.home())) /
                        "FanVPNBridge/muse-web-auth/service.json")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        verify(config, args.tool_workspace, args.only)
    except MuseWebError as error:
        # Provider errors are fixed, sanitized messages, not upstream bodies.
        print(json.dumps({"code": error.code, "status": error.status, "message": str(error)}), flush=True)
        raise SystemExit("Inert Muse acceptance did not pass; installed services and network were not changed") from None
    except Exception as error:
        print("Acceptance check failed: " + type(error).__name__, flush=True)
        raise SystemExit("Inert Muse acceptance did not pass; installed services and network were not changed") from None


if __name__ == "__main__":
    main()
