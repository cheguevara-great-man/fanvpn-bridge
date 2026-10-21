"""Explicit first installation on the user's Linux VPS; never edits proxy/nginx.

Run on that server only, from the uploaded staging directory. Refuses to replace
an existing installation. Credentials are generated there, never printed.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=19445)
    args = parser.parse_args()
    if os.name != "posix" or os.geteuid() != 0:
        raise SystemExit("Run explicitly as root on the Linux relay server")
    if not 1024 <= args.port <= 65535:
        raise SystemExit("Invalid independent relay port")
    # The host is also a fixed certificate directory component; no arbitrary paths.
    import ipaddress
    ipaddress.ip_address(args.host)
    certificate = Path("/etc/letsencrypt/live") / args.host
    for name in ("fullchain.pem", "privkey.pem"):
        if not (certificate / name).is_file():
            raise SystemExit("Existing verified TLS certificate not found")
    target = Path("/opt/muse-tool-relay")
    unit = Path("/etc/systemd/system/muse-tool-relay.service")
    settings = Path("/etc/muse-tool-relay.json")
    bundle = Path("/root/muse-tool-relay-client.json")
    if any(p.exists() for p in (target, unit, settings, bundle)):
        raise SystemExit("Existing relay installation found; nothing overwritten")
    with socket.socket() as probe:
        probe.bind(("0.0.0.0", args.port))
    target.mkdir(mode=0o755)
    source = Path(__file__).resolve().parent
    for name in ("relay.py", "requirements-relay.txt"):
        shutil.copyfile(source / name, target / name)
    subprocess.run(["python3", "-m", "venv", str(target / "venv")], check=True)
    subprocess.run([str(target / "venv/bin/python"), "-m", "pip", "install", "--disable-pip-version-check",
                    "-r", str(target / "requirements-relay.txt")], check=True)
    token = secrets.token_hex(32)
    config = {"host": "0.0.0.0", "port": args.port, "admin_token": token,
              "cert_file": str(certificate / "fullchain.pem"), "key_file": str(certificate / "privkey.pem")}
    for path, value in ((settings, config), (bundle, {"url": f"https://{args.host}:{args.port}", "admin_token": token})):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle)
    unit.write_text("""[Unit]
Description=Muse PC authenticated message relay (no command execution)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/opt/muse-tool-relay/venv/bin/python /opt/muse-tool-relay/relay.py --config /etc/muse-tool-relay.json
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
MemoryMax=192M
TasksMax=32
UMask=0077

[Install]
WantedBy=multi-user.target
""")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "muse-tool-relay.service"], check=True)
    print("Independent relay installed; proxy, nginx and firewall were not modified. Credentials were not printed.")


if __name__ == "__main__":
    main()
