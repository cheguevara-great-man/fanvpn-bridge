"""Explicit, collision-safe installation of an independent Muse workspace skill.

Does not update Native Host, start services, edit local settings or touch proxies.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

from .muse_service import gateway_factory
from .muse_network import configure_network
from .muse_tools import WorkspaceTools


def install(gateway, skill_name, templates, *, pc=False):
    workspace = WorkspaceTools("workspace/custom-skills/" + skill_name)
    try:
        gateway.call_json("fs.stat", body={"path": workspace.root}, timeout=30)
    except Exception as error:
        if not workspace.missing(error):
            raise
    else:
        raise ValueError("Muse skill directory already exists; nothing overwritten")
    digests = {}
    # Publish discovery metadata last, only after source/instructions are verified.
    names = ("SKILL.md", "scripts/mailbox.py", "scripts/pc.py", "skill.yaml") if pc else (
        "SKILL.md", "scripts/mailbox.py", "skill.yaml")
    if pc and (templates / "scripts/persistent.py").is_file():
        names = (*names[:-1], "scripts/persistent.py", names[-1])
    for name in names:
        source = (templates.parent / "muse_tools" / name
                  if pc and name == "scripts/mailbox.py" else templates / name)
        raw = source.read_text(encoding="utf-8")
        raw = raw.replace("__SKILL_NAME__", skill_name).replace("__SKILL_ROOT__", "~/" + workspace.root).encode("utf-8")
        gateway.call_json("fs.write", body={"path": workspace.root + "/" + name,
                          "data_base64": base64.b64encode(raw).decode("ascii"), "overwrite": False,
                          "append": False, "create_parent": True}, timeout=30)
        if workspace.read_bytes(gateway, workspace.root + "/" + name) != raw:
            raise ValueError("Muse skill verification failed; no settings changed")
        digests[name] = hashlib.sha256(raw).hexdigest()
    return {"tool_workspace": workspace.root, "sha256": digests}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-name", required=True)
    parser.add_argument("--pc", action="store_true", help="Install the separate experimental synchronous PC helper")
    parser.add_argument("--push", action="store_true", help="Install an independent direct-message PC helper")
    parser.add_argument("--proxy", help="auto, direct, or an explicit proxy URL (overrides config)")
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("LOCALAPPDATA", Path.home())) /
                        "FanVPNBridge/muse-web-auth/service.json")
    args = parser.parse_args()
    if args.pc and args.push:
        parser.error("Select either --pc or --push")
    gateway = None
    try:
        # Validate name before connecting to the account.
        WorkspaceTools("workspace/custom-skills/" + args.skill_name)
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        configure_network(args.proxy if args.proxy is not None else config.get("proxy", "auto"))
        gateway = gateway_factory(config)()
        directory = "tools/muse_push" if args.push else ("tools/muse_pc" if args.pc else "tools/muse_tools")
        templates = Path(__file__).resolve().parents[2] / directory
        result = install(gateway, args.skill_name, templates, pc=args.pc or args.push)
        print(json.dumps(result, indent=2))
    except Exception:
        # Upstream exception strings may contain credentials.
        raise SystemExit("Muse skill installation failed; existing settings and services were not changed") from None
    finally:
        if gateway is not None:
            gateway.close()


if __name__ == "__main__":
    main()
