"""Explicit live PC RPC acceptance. Only permits the read-only hostname command.

Does not update Native Host, restart services, apply model-generated code, or use
the colleague's relay/token. Leaves one isolated cloud conversation for auditing.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "native-host"))

from fanvpn_bridge.muse_network import configure_network
from fanvpn_bridge.muse_pc import PCSessions
from fanvpn_bridge.muse_service import gateway_factory, make_server
from fanvpn_bridge.muse_web import MuseWebProvider


def message(response):
    return "".join(part["text"] for item in response["output"] if item.get("type") == "message"
                   for part in item["content"] if part.get("type") == "output_text").strip()


def verify(config, workspace, *, sessions_class=PCSessions, session_options=None, audit=None, command_count=2):
    if type(command_count) is not int or not 1 <= command_count <= 5:
        raise ValueError("Acceptance permits between one and five read-only commands")
    audit = {} if audit is None else audit
    factory = gateway_factory(config)
    request_started = [0.0]
    submissions = []

    class Gateway:
        def __init__(self):
            started = time.monotonic()
            self.inner = factory()
            self.vm_id = self.inner.vm_id
            audit["gateway_connections"] = audit.get("gateway_connections", 0) + 1
            audit["connect_seconds"] = round(audit.get("connect_seconds", 0) + time.monotonic() - started, 3)

        def call_json(self, method, **kwargs):
            audit["last_rpc"] = method
            started = time.monotonic()
            value = self.inner.call_json(method, **kwargs)
            stats = audit.setdefault("rpc_stats", {}).setdefault(method, {"calls": 0, "seconds": 0, "json_bytes": 0})
            stats["calls"] += 1
            stats["seconds"] = round(stats["seconds"] + time.monotonic() - started, 3)
            stats["json_bytes"] += len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            if method == "chat.history":
                after = kwargs["body"].get("after_seq", 0)
                windows = audit.setdefault("history_after_values", [])
                if after not in windows:
                    windows.append(after)
                for event in value.get("chat_events", []):
                    if event.get("event_name") != "message.user":
                        continue
                    for submission in submissions:
                        if (not submission["confirmed"] and event.get("payload", {}).get("content") == submission["text"]
                                and type(event.get("occurred_at_ms")) in (int, float)):
                            submission["confirmed"] = True
                            audit.setdefault("message_to_cloud_clock_estimates_seconds", []).append(
                                round((event["occurred_at_ms"] - submission["started_ms"]) / 1000, 3))
            if method == "session.start":
                audit["session_id"] = value.get("session_id") or value.get("id")
                print("Isolated Muse session: " + str(audit["session_id"]), flush=True)
            return value

        def _open(self, method, **kwargs):
            audit.setdefault("before_submit_seconds", []).append(round(time.monotonic() - request_started[0], 3))
            audit["chat_submissions"] = audit.get("chat_submissions", 0) + 1
            submissions.append({"text": kwargs["body"]["items"][0]["text"], "confirmed": False,
                                "started_ms": time.time_ns() / 1_000_000 -
                                (time.monotonic() - request_started[0]) * 1000})
            return self.inner._open(method, **kwargs)

        def close(self):
            self.inner.close()

    sessions = sessions_class(Gateway, timeout=240, tool_workspace=workspace, **(session_options or {}))
    key = secrets.token_hex(32)
    server = make_server({"api_key": key, "port": 0}, sessions)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = MuseWebProvider(base_url=f"http://127.0.0.1:{server.server_port}/v1",
                               api_key=key, timeout_seconds=260)

    def send(body):
        request_started[0] = time.monotonic()
        return provider.responses(body)
    payload = {"model": "muse-web/default", "prompt_cache_key": "pc-acceptance-" + secrets.token_hex(16),
               "tools": [{"type": "namespace", "name": "functions", "tools": [{
                   "type": "function", "name": "exec_command", "description":
                   "Read-only local PC acceptance tool. Only cmd=hostname is permitted; all other commands are rejected.",
                   "parameters": {"type": "object", "properties": {"cmd": {"type": "string", "enum": ["hostname"]}},
                                  "required": ["cmd"], "additionalProperties": False}}]}],
               "input": "Please use the supplied Codex PC connection to run functions.exec_command with cmd=hostname "
               f"exactly {command_count} times sequentially on my Windows PC. The client will execute only this read-only command. Do not run hostname "
               "in the cloud or use a different connection. After the actual tool result returns, reply with its "
               "exact output field from the last call, including its verification receipt, and no other text. "
               "If the connection is not "
               "permitted, report the refusal instead."}
    started = time.monotonic()
    report = {"local_command_executed": False, "local_command_count": 0, "same_native_turn": False}
    try:
        print("Starting isolated live PC RPC acceptance", flush=True)
        _, response = send(payload)
        print("Received first Muse response", flush=True)
        report["first_response_id"] = response["id"]
        state = sessions.snapshots[response["id"]]
        report["run"] = state.run
        history = [{"role": "user", "content": payload["input"]}]
        for index in range(command_count):
            report.setdefault("tool_response_received_ms", []).append(time.time_ns() / 1_000_000)
            if len(response["output"]) != 1:
                raise ValueError("Expected one local tool call")
            call = response["output"][0]
            if (call.get("type") != "function_call" or call.get("name") != "exec_command"
                    or call.get("namespace") != "functions" or json.loads(call["arguments"]) != {"cmd": "hostname"}):
                report["muse_reply"] = message(response)[:1600]
                raise ValueError("Muse did not request the exact allowed local command")
            # No generated command is evaluated: exact allowlist, absolute binary, shell=False.
            binary = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/hostname.exe"
            local_start_ms = time.time_ns() / 1_000_000
            actual = subprocess.run([str(binary)], shell=False, capture_output=True, timeout=10, check=True)
            report.setdefault("local_command_completed_ms", []).append(time.time_ns() / 1_000_000)
            report.setdefault("local_command_start_ms", []).append(local_start_ms)
            hostname = actual.stdout.decode("utf-8").strip()
            if not hostname or actual.stderr.strip():
                raise ValueError("Unexpected hostname result")
            report["local_command_executed"] = True
            report["local_command_count"] += 1
            print(f"Allowed hostname call {index + 1} executed; delivering its actual result", flush=True)
            marker = "PC_RECEIPT_" + secrets.token_hex(24)
            output = hostname + "\n" + marker
            result = {"type": "function_call_output", "call_id": call["call_id"], "output": output}
            report.setdefault("tool_call_ids", []).append(call["call_id"])
            history.extend([*response["output"], result])
            # Exercise canonical full history, not only previous_response_id.
            report.setdefault("local_result_submitted_ms", []).append(time.time_ns() / 1_000_000)
            _, response = send({**payload, "input": history})
        final = response
        report["final_reply"] = message(final)[:1600]
        report["same_native_turn"] = (audit.get("chat_submissions") == 1
                                      and sessions.snapshots[final["id"]].session_id == state.session_id)
        report["tool_round_gateway_connections"] = audit.get("gateway_connections", 0)
        # An exact JSON result envelope also proves receipt; arbitrary mentions/substrings do not.
        try:
            envelope = json.loads(message(final))
        except ValueError:
            envelope = None
        report["actual_result_received"] = message(final) == output or envelope == result
        if not report["same_native_turn"] or not report["actual_result_received"]:
            raise ValueError("Muse did not continue its original turn from the actual result")
        report["incremental_history_active"] = state.history_after > 0 and state.submitted_text is None
        if not report["incremental_history_active"] or report["tool_round_gateway_connections"] != 1:
            raise ValueError("Incremental history or tool connection reuse was not active")
        followup = "PC_FOLLOWUP_" + secrets.token_hex(16)
        history.extend([*final["output"], {"role": "user", "content": "Reply exactly " + followup + ". Do not call tools."}])
        _, next_response = send({**payload, "input": history})
        report["followup_same_session"] = (sessions.snapshots[next_response["id"]].session_id == state.session_id
                                            and audit.get("chat_submissions") == 2
                                            and message(next_response) == followup)
        if not report["followup_same_session"]:
            raise ValueError("Next user message did not stay in the same Muse conversation")
        report["followup_reused_connection"] = audit.get("gateway_connections") == 1
        if not report["followup_reused_connection"]:
            raise ValueError("Immediate followup did not reuse the idle connection")
        print(json.dumps({**report, **audit, "elapsed_seconds": round(time.monotonic() - started, 1)},
                         ensure_ascii=True), flush=True)
        return report
    except Exception as error:
        # Known generated reply/audit IDs only; never print transport exception contents.
        report["error_code"] = getattr(error, "code", None)
        print(json.dumps({**report, **audit, "error_type": type(error).__name__,
                          "elapsed_seconds": round(time.monotonic() - started, 1)}, ensure_ascii=True), flush=True)
        raise SystemExit("Live PC acceptance did not pass; no installed service was changed") from None
    finally:
        server.shutdown()
        server.server_close()
        sessions.close()
        thread.join(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool-workspace", required=True)
    parser.add_argument("--config", type=Path, default=Path(os.environ["LOCALAPPDATA"]) /
                        "FanVPNBridge/muse-web-auth/service.json")
    parser.add_argument("--proxy")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    configure_network(args.proxy if args.proxy is not None else config.get("proxy", "auto"))
    verify(config, args.tool_workspace)


if __name__ == "__main__":
    main()
