"""Read-only timing of the deployed push implementation in an isolated session.

No service/config/network changes. Default: five allowlisted hostname calls.
--installed-messages: three tagged messages through the existing Bridge, no commands.
The HTTP client here stands in for Codex; VS Code UI/approval time is excluded.
Uses the VM's connection.ping clock before/after, not uncorrected timestamps.
"""
from __future__ import annotations

import argparse
import importlib.util
import http.client
import json
import math
import os
from pathlib import Path
import secrets
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "native-host"))
from fanvpn_bridge.muse_network import configure_network
from fanvpn_bridge.muse_push import PushSessions
from fanvpn_bridge.muse_push_channel import PushChannel
from fanvpn_bridge.muse_service import gateway_factory

spec = importlib.util.spec_from_file_location("pc_latency_acceptance", ROOT / "tools/muse_pc/verify.py")
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


def sample_clock(gateway, count=6):
    samples = []
    for _ in range(count):
        started = time.time_ns() / 1_000_000
        mono = time.monotonic()
        value = gateway.call_json("connection.ping", body={}, timeout=10)
        ended = time.time_ns() / 1_000_000
        remote = value.get("server_ts_ms")
        if (type(remote) not in (int, float) or not math.isfinite(remote)
                or abs((ended - started) - (time.monotonic() - mono) * 1000) > 20):
            raise ValueError("Clock sample invalid or local wall clock stepped")
        samples.append({"rtt_ms": ended - started, "low_ms": remote - ended,
                        "high_ms": remote - started})
    return samples


def clock_bounds(samples):
    # Server generated the timestamp between local send and receive. Intersection
    # gives bounds without assuming that the network paths are symmetric.
    low = max(item["low_ms"] for item in samples)
    high = min(item["high_ms"] for item in samples)
    if low > high:
        raise ValueError("VM clock bounds changed during the measurement")
    return low, high


def corrected_interval(raw_ms, offset, *, remote_received):
    low, high = offset
    if remote_received:  # Remote receive minus PC send: subtract remote-PC offset.
        bounds = raw_ms - high, raw_ms - low
    else:  # PC receive minus remote send: add remote-PC offset.
        bounds = raw_ms + low, raw_ms + high
    if bounds[1] < 0:
        raise ValueError("Endpoint timestamp is inconsistent with the calibrated VM clock")
    return {"estimate_seconds": round(max(0, sum(bounds) / 2) / 1000, 3),
            "bounds_seconds": [round(max(0, bound) / 1000, 3) for bound in bounds]}


def installed_messages(clock):
    """Observe three tagged lightweight messages through the running Bridge."""
    tag = "PUSH_LATENCY_" + secrets.token_hex(16)
    print("Installed timing tag: " + tag, flush=True)
    history, starts, receipts = [], [], []
    tools = [{"type": "function", "name": "exec_command", "description": "Only read-only hostname is allowed.",
              "parameters": {"type": "object", "properties": {"cmd": {"type": "string", "enum": ["hostname"]}},
                             "required": ["cmd"], "additionalProperties": False}}]
    for index in range(3):
        expected = tag + "_" + str(index)
        history.append({"role": "user", "content": "Reply exactly " + expected + ". Do not call tools."})
        connection = http.client.HTTPConnection("127.0.0.1", 18888, timeout=260)
        try:
            payload = json.dumps({"model": "muse-web/default", "prompt_cache_key": tag,
                                  "tools": tools, "input": history}).encode()
            starts.append(time.time_ns() / 1_000_000)
            connection.request("POST", "/muse-web/v1/responses", payload, {"Content-Type": "application/json"})
            response = connection.getresponse()
            result = json.loads(response.read(2 * 1024 * 1024 + 1))
            if (response.status != 200 or not acceptance.message(result)
                    or any(item.get("type") != "message" for item in result.get("output", []))):
                error = result.get("error", {})
                print(json.dumps({"failed_phase": "installed_reply", "http_status": response.status,
                                  "error_code": error.get("code") if isinstance(error, dict) else None,
                                  "output_types": [item.get("type") for item in result.get("output", [])]}), flush=True)
                raise ValueError("Installed Bridge returned an error or a tool instead of text; no tool executed")
            # Echo compliance is NOT needed to timestamp delivery. In particular,
            # a refusal is a delivered message, not proof of a transport failure.
            receipts.append(acceptance.message(result) == expected)
            history.extend(result["output"])
            print(f"Installed Bridge message {index + 1}/3 confirmed", flush=True)
        finally:
            connection.close()
    listed = clock.call_json("sessions.list", timeout=15).get("sessions", [])
    for session in sorted(listed, key=lambda item: item.get("updated_at_ms", 0), reverse=True)[:5]:
        events = clock.call_json("chat.history", body={"session_id": session["session_id"], "limit": 50},
                                 timeout=15).get("chat_events", [])
        users = [event for event in events if event.get("event_name") == "message.user"
                 and tag in event.get("payload", {}).get("content", "")]
        if len(users) != 3:
            continue
        raw = []
        for index, start in enumerate(starts):
            event = next(event for event in users if tag + "_" + str(index) in event["payload"]["content"])
            raw.append(event["occurred_at_ms"] - start)
        return raw, receipts
    raise ValueError("Could not locate all three messages in their isolated cloud conversation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    private = Path(os.environ["LOCALAPPDATA"]) / "FanVPNBridge/muse-web-auth"
    parser.add_argument("--config", type=Path, default=private / "service.json")
    parser.add_argument("--relay-config", type=Path, default=private / "push-relay.json")
    parser.add_argument("--tool-workspace", required=True)
    parser.add_argument("--installed-messages", action="store_true", help="Three inert messages through the installed Bridge; no commands")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    relay = json.loads(args.relay_config.read_text(encoding="utf-8-sig"))
    configure_network(config.get("proxy", "auto"))
    clock = gateway_factory(config)()
    try:
        before = sample_clock(clock)
        print("VM clock calibrated before acceptance", flush=True)
        if args.installed_messages:
            raw, receipts = installed_messages(clock)
            offset = clock_bounds(before + sample_clock(clock))
            print(json.dumps({"installed_message_to_muse": dict(zip(("fresh", "warm_1", "warm_2"),
                             [corrected_interval(value, offset, remote_received=True) for value in raw])),
                              "clock_offset_remote_minus_pc_ms": [round(v, 3) for v in offset],
                              "exact_echo_receipts": receipts,
                              "scope": "Installed Bridge HTTP request start to Muse user event, excluding model thinking. "
                                       "Lightweight input and one allowed tool definition; no commands executed. "
                                       "VS Code UI queue before Bridge receipt is not measured. "
                                       "Arrival is verified by tagged user events, not by echo compliance."}), flush=True)
            return
        channels, audit = [], {}

        def factory(config, run, expires):
            try:
                channel = PushChannel(config, run, expires)
            except Exception as error:
                audit["channel_creation_error_type"] = type(error).__name__
                raise
            channels.append(channel)
            return channel

        report = acceptance.verify(config, args.tool_workspace, sessions_class=PushSessions, audit=audit,
                                   command_count=5, session_options={"relay_config": relay, "channel_factory": factory})
        after = sample_clock(clock)
        offset = clock_bounds(before + after)
        events = [event for channel in channels for event in channel.metrics]
        received = report["tool_response_received_ms"]
        if len(events) != len(received):
            raise ValueError("Could not match delivered commands to PC response timestamps")
        commands = [corrected_interval(pc - event["cloud_sent_ms"], offset, remote_received=False)
                    for pc, event in zip(received, events)]
        messages = [corrected_interval(raw * 1000, offset, remote_received=True)
                    for raw in audit["message_to_cloud_clock_estimates_seconds"]]
        print(json.dumps({"clock_offset_remote_minus_pc_ms": [round(v, 3) for v in offset],
                          "clock_min_rtt_ms": round(min(item["rtt_ms"] for item in before + after), 3),
                          "message_to_muse": dict(zip(("fresh", "warm"), messages)),
                          "command_to_client": commands,
                          "scope": "Same push implementation in an isolated HTTP service. Client request start to "
                                   "Muse user event; cloud pc.py sending tool to client receiving tool response. "
                                   "Excludes model thinking, VS Code UI, approval/execution and local Bridge conversion. "
                                   "Clock bounds assume daemon/chat/helper share the same VM realtime clock."}), flush=True)
    finally:
        clock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit("Latency measurement failed: " + type(error).__name__ + "; no automatic replay") from None
