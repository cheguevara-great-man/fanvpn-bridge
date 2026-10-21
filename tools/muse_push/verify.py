"""Isolated real-Muse push acceptance; only two read-only hostname commands.

Does not replace the installed service, update Native Host or change networking.
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "native-host"))

from fanvpn_bridge.muse_network import configure_network
from fanvpn_bridge.muse_push import PushSessions
from fanvpn_bridge.muse_push_channel import PushChannel

import importlib.util
spec = importlib.util.spec_from_file_location("pc_acceptance", ROOT / "tools/muse_pc/verify.py")
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    private = Path(os.environ["LOCALAPPDATA"]) / "FanVPNBridge/muse-web-auth"
    parser.add_argument("--config", type=Path, default=private / "service.json")
    parser.add_argument("--relay-config", type=Path, default=private / "push-relay.json")
    parser.add_argument("--tool-workspace", required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    relay = json.loads(args.relay_config.read_text(encoding="utf-8-sig"))
    configure_network(config.get("proxy", "auto"))
    channels = []
    def factory(config, run, expires):
        channel = PushChannel(config, run, expires)
        channels.append(channel)
        return channel
    report = acceptance.verify(config, args.tool_workspace, sessions_class=PushSessions,
                               session_options={"relay_config": relay, "channel_factory": factory})
    events = [event for channel in channels for event in channel.metrics]
    starts = report["local_command_start_ms"]
    if len(events) != len(starts):
        raise SystemExit("Tool delivery measurements could not be matched")
    print(json.dumps({"push_dispatch_seconds": [round((start - event["cloud_sent_ms"]) / 1000, 3)
                                                 for start, event in zip(starts, events)],
                      "relay_to_pc_seconds": [round((event["pc_received_ms"] - event["relay_received_ms"]) / 1000, 3)
                                              for event in events],
                      "clock_note": "UNCALIBRATED clock differences, not pure network delay. Cloud/PC clock skew was observed; "
                                    "use PC-only inter-command intervals for comparative tests.",
                      "inter_command_seconds": [round((b - a) / 1000, 3) for a, b in zip(starts, starts[1:])]}), flush=True)


if __name__ == "__main__":
    main()
