"""Measure actual result delivery in an isolated push session, not model thinking.

Installs a fresh diagnostic-only helper directory. Existing helpers, running
services, credentials and proxy configuration are not modified. Five readonly
hostname calls only. Cloud trace files contain IDs/timestamps, never command
results, and are read once AFTER the test: they do not transport results.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "native-host"))
from fanvpn_bridge.muse_network import configure_network
from fanvpn_bridge.muse_push import PushSessions
from fanvpn_bridge.muse_tools import WorkspaceTools
from latency import acceptance, clock_bounds, corrected_interval, sample_clock


def timed_source(source):
    """Add only arrival timestamps to an independently installed helper copy."""
    read = "        raw = response.read(MAX_BYTES + 1)\n"
    returned = "        return result\n"
    if source.count(read) != 1 or source.count(returned) != 1:
        raise ValueError("Helper source changed; timing instrumentation not applied")
    source = source.replace("import json\n", "import json\nimport hashlib\nimport os\n", 1)
    source = source.replace(read, read + "        received_ms = time.time_ns() / 1_000_000\n")
    trace = (
        '        digest = hashlib.sha256(result["call_id"].encode()).hexdigest()\n'
        '        trace = root / "runs" / run / ("return-timing-" + digest + ".json")\n'
        '        with trace.open("x", encoding="utf-8") as handle:\n'
        '            json.dump({"call_id": result["call_id"], "received_ms": received_ms,\n'
        '                       "connection_generation": _opener.generation, "broker_pid": os.getpid()}, handle)\n'
    )
    return source.replace(returned, trace + returned)


def install_probe(gateway):
    name = "codex_push_return_" + secrets.token_hex(8)
    workspace = WorkspaceTools("workspace/custom-skills/" + name)
    try:
        gateway.call_json("fs.stat", body={"path": workspace.root}, timeout=20)
    except Exception as error:
        if not workspace.missing(error):
            raise
    else:
        raise ValueError("Diagnostic directory already exists; nothing overwritten")
    for item in ("SKILL.md", "scripts/mailbox.py", "scripts/pc.py", "scripts/persistent.py", "skill.yaml"):
        template = ROOT / "tools" / ("muse_tools" if item == "scripts/mailbox.py" else "muse_push") / item
        source = template.read_text(encoding="utf-8")
        if item == "scripts/pc.py":
            source = timed_source(source)
        raw = source.replace("__SKILL_NAME__", name).replace("__SKILL_ROOT__", "~/" + workspace.root).encode()
        workspace.write_bytes(gateway, workspace.root + "/" + item, raw)
        if workspace.read_bytes(gateway, workspace.root + "/" + item) != raw:
            raise ValueError("Diagnostic upload did not match its source")
    print("Independent timing helper: " + workspace.root, flush=True)
    return workspace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    private = Path(os.environ["LOCALAPPDATA"]) / "FanVPNBridge/muse-web-auth"
    parser.add_argument("--config", type=Path, default=private / "service.json")
    parser.add_argument("--relay-config", type=Path, default=private / "push-relay.json")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    relay = json.loads(args.relay_config.read_text(encoding="utf-8-sig"))
    configure_network(config.get("proxy", "auto"))
    clock = acceptance.gateway_factory(config)()
    try:
        workspace = install_probe(clock)
        before = sample_clock(clock)
        report = acceptance.verify(config, workspace.root, sessions_class=PushSessions, command_count=5,
                                   session_options={"relay_config": relay})
        offset = clock_bounds(before + sample_clock(clock))
        delivered, connections = [], []
        for call_id, submitted, completed in zip(report["tool_call_ids"], report["local_result_submitted_ms"],
                                                 report["local_command_completed_ms"]):
            digest = hashlib.sha256(call_id.encode()).hexdigest()
            trace = workspace.read(clock, f"{workspace.root}/runs/{report['run']}/return-timing-{digest}.json")
            if trace["call_id"] != call_id:
                raise ValueError("Cloud arrival timestamp does not match the actual result")
            connections.append((trace["broker_pid"], trace["connection_generation"]))
            delivered.append({"submit_to_cloud": corrected_interval(trace["received_ms"] - submitted, offset,
                                                                     remote_received=True),
                              "completed_to_cloud": corrected_interval(trace["received_ms"] - completed, offset,
                                                                        remote_received=True)})
        print(json.dumps({"result_return": delivered, "cloud_connection_identities": connections,
                          "all_five_calls_reused_one_connection": len(set(connections)) == 1,
                          "mean_submit_to_cloud_seconds": round(sum(item["submit_to_cloud"]["estimate_seconds"]
                                                                     for item in delivered) / len(delivered), 3),
                          "clock_offset_remote_minus_pc_ms": [round(value, 3) for value in offset],
                          "scope": "Actual hostname result submitted via the isolated HTTP client to cloud helper "
                                   "receiving all response bytes. Excludes command execution/model thinking/VS Code UI "
                                   "and local Bridge conversion. Diagnostic timestamps only are read after completion; "
                                   "results travel via the unchanged push transport."}), flush=True)
    finally:
        clock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit("Result-return timing failed: " + type(error).__name__ + "; no automatic replay") from None
