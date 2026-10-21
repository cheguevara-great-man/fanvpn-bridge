"""Explicit installed-Bridge benchmark; permits only read-only hostname calls.

Creates one isolated cloud chat, tests an immediate followup and one after an
idle interval. Never updates services, network settings, cookies or credentials.
Cross-machine latency estimates use local/VM timestamps and assume aligned clocks.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "native-host"))

from fanvpn_bridge.muse_network import configure_network
from fanvpn_bridge.muse_service import gateway_factory
from verify import message


def benchmark(config, workspace, idle_seconds, command_count=5, *, push=False):
    tag = "PC_BENCH_" + secrets.token_hex(16)
    payload = {
        "model": "muse-web/default", "prompt_cache_key": tag,
        "tools": [{"type": "namespace", "name": "functions", "tools": [{
            "type": "function", "name": "exec_command",
            "description": "Read-only local PC tool. Only cmd=hostname is allowed.",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string", "enum": ["hostname"]}},
                           "required": ["cmd"], "additionalProperties": False},
        }]}],
    }
    initial = (tag + "_COLD Please use the supplied PC tool connection to run functions.exec_command with "
               f"cmd=hostname exactly {command_count} times sequentially on my Windows PC. Each call must finish before "
               "the next begins. Do not run it in the cloud. The client permits only this read-only command. "
               "After the last actual result, reply with its exact output, including its receipt, and no "
               "other text. If the connection is refused, report the refusal instead.")
    history = [{"role": "user", "content": initial}]
    send_times, calls = {}, []
    gateway = None
    phase = "cold_request"
    started = time.monotonic()

    def send(label=None):
        connection = http.client.HTTPConnection("127.0.0.1", 18888, timeout=260)
        try:
            if label:
                send_times[label] = time.time_ns() / 1_000_000
            connection.request("POST", "/muse-web/v1/responses",
                               json.dumps({**payload, "input": history}, ensure_ascii=False).encode("utf-8"),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            value = json.loads(response.read(2 * 1024 * 1024 + 1))
            if response.status != 200:
                raise RuntimeError("Installed Bridge rejected the benchmark request")
            return value
        finally:
            connection.close()

    try:
        print(f"Benchmarking installed Bridge/Muse with {command_count} read-only commands", flush=True)
        response = send("COLD")
        phase = "tool_calls"
        for index in range(command_count):
            outputs = response.get("output", [])
            if len(outputs) != 1:
                raise ValueError("Expected exactly one allowed PC call")
            call = outputs[0]
            if (call.get("type") != "function_call" or call.get("name") != "exec_command"
                    or call.get("namespace") != "functions" or json.loads(call["arguments"]) != {"cmd": "hostname"}):
                raise ValueError("Model did not request the exact allowed command")
            binary = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/hostname.exe"
            local_start_ms = time.time_ns() / 1_000_000
            command_start = time.monotonic()
            actual = subprocess.run([str(binary)], shell=False, capture_output=True, timeout=10, check=True)
            elapsed = time.monotonic() - command_start
            hostname = actual.stdout.decode("utf-8").strip()
            if not hostname or actual.stderr.strip():
                raise ValueError("Unexpected local command result")
            calls.append({"sequence": index + 1, "local_start_ms": local_start_ms,
                          "execution_seconds": round(elapsed, 4)})
            output = hostname + "\nPC_RECEIPT_" + secrets.token_hex(24)
            result = {"type": "function_call_output", "call_id": call["call_id"], "output": output}
            history.extend([call, result])
            print(f"Read-only command {index + 1}/{command_count} executed", flush=True)
            response = send()
        final = message(response)
        try:
            envelope = json.loads(final)
        except ValueError:
            envelope = None
        if final != output and envelope != result:
            raise ValueError("Final reply did not exactly confirm the actual last result")
        history.extend(response["output"])
        phase = "followups"
        for label in ("WARM", "AFTER_IDLE"):
            if label == "AFTER_IDLE":
                print(f"Waiting {idle_seconds:g} seconds to test beyond the old 30-second idle limit", flush=True)
                until = time.monotonic() + idle_seconds
                while time.monotonic() < until:
                    time.sleep(min(0.25, max(0, until - time.monotonic())))
            expected = tag + "_" + label
            history.append({"role": "user", "content": "Reply exactly " + expected + ". Do not call tools."})
            response = send(label)
            if message(response) != expected:
                print(json.dumps({"followup": label, "expected": expected,
                                  "actual": message(response)[:200], "response_id": response.get("id"),
                                  "output_types": [item.get("type") for item in response.get("output", [])]}), flush=True)
                raise ValueError("Followup did not produce the exact expected reply")
            history.extend(response["output"])
            print(label + " followup confirmed", flush=True)
        phase = "read_only_observation"
        gateway = gateway_factory(config)()
        sessions = gateway.call_json("sessions.list", timeout=20).get("sessions", [])
        matched = None
        for session in sorted(sessions, key=lambda s: s.get("updated_at_ms", 0), reverse=True)[:5]:
            events = gateway.call_json("chat.history", body={"session_id": session["session_id"], "limit": 50},
                                       timeout=20).get("chat_events", [])
            users = [e for e in events if e.get("event_name") == "message.user"
                     and tag in e.get("payload", {}).get("content", "")]
            if any(tag + "_COLD" in e["payload"]["content"] for e in users):
                matched = session["session_id"], users
                break
        if matched is None:
            raise ValueError("Benchmark cloud conversation was not found")
        session_id, users = matched
        received = {}
        for label, start_ms in send_times.items():
            event = next(e for e in users if tag + "_" + label in e["payload"]["content"])
            received[label] = round((event["occurred_at_ms"] - start_ms) / 1000, 3)
        first = next(e for e in users if tag + "_COLD" in e["payload"]["content"])
        run = re.search(r"Connection/run: ([0-9a-f]{32})", first["payload"]["content"])[1]
        inter_command = [round((right["local_start_ms"] - left["local_start_ms"]) / 1000, 3)
                         for left, right in zip(calls, calls[1:])]
        for call in calls:
            if not push:
                stat = gateway.call_json("fs.stat", body={
                    "path": f"{workspace}/runs/{run}/request-{call['sequence']:04d}.json"}, timeout=20)
                call["dispatch_seconds"] = round((call["local_start_ms"] - stat["modified_ms"]) / 1000, 3)
            call.pop("local_start_ms")
        report = {"session_id": session_id, "message_to_muse_seconds": received, "commands": calls,
                  "transport": "push" if push else "files", "inter_command_seconds": inter_command,
                  "idle_seconds": idle_seconds, "actual_results_confirmed": True, "same_cloud_session": True,
                  "clock_note": "One-way values are UNCALIBRATED clock differences, not measured network latency. "
                                "Inter-command intervals use only the PC clock and include model thinking/cloud scheduling.",
                  "elapsed_seconds": round(time.monotonic() - started, 1)}
        print(json.dumps(report, ensure_ascii=True), flush=True)
        return report
    except Exception as error:
        print(json.dumps({"failed_phase": phase, "error_type": type(error).__name__,
                          "executed_commands": len(calls), "send_times_ms": send_times, "commands": calls,
                          "benchmark_tag": tag, "response_id": response.get("id") if 'response' in locals() else None,
                          "elapsed_seconds": round(time.monotonic() - started, 1)}),
              flush=True)
        raise SystemExit("Installed benchmark did not fully pass; no automatic replay") from None
    finally:
        if gateway is not None:
            gateway.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(os.environ["LOCALAPPDATA"]) /
                        "FanVPNBridge/muse-web-auth/service.json")
    parser.add_argument("--tool-workspace", required=True)
    parser.add_argument("--idle-seconds", type=float, default=40)
    parser.add_argument("--commands", type=int, choices=range(1, 6), default=5)
    parser.add_argument("--push", action="store_true", help="Push mode has no command files to stat")
    args = parser.parse_args()
    if not 31 <= args.idle_seconds < 300:
        parser.error("idle-seconds must be at least 31 and less than 300")
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    configure_network(config.get("proxy", "auto"))
    benchmark(config, args.tool_workspace, args.idle_seconds, args.commands, push=args.push)


if __name__ == "__main__":
    main()
