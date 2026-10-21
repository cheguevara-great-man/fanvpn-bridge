"""Process-local Muse egress selection. Called once, before starting the sidecar."""
from __future__ import annotations

import http.client
import json
import os
from urllib.parse import urlsplit

LOCAL_PROXY = "http://127.0.0.1:18889"
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _local_proxy_ready() -> bool:
    connection = http.client.HTTPConnection("127.0.0.1", 18889, timeout=2)
    try:
        connection.request("GET", "http://browser-ai-bridge.local/ready")
        response = connection.getresponse()
        raw = response.read(4097)
        return (response.status == 200 and len(raw) <= 4096
                and json.loads(raw) == {"status": "ok", "mode": "vscode-direct-proxy"})
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def configure_network(selection: object = "auto") -> str:
    """Do not inherit a terminal's proxy or change any system/browser settings.

    Auto verifies the existing Bridge forward proxy; it never starts one or
    silently falls back to direct access. Return a credential-free route label.
    """
    if selection == "auto":
        if not _local_proxy_ready():
            raise ValueError("Muse auto proxy is unavailable. Start the existing local forward proxy, "
                             "set proxy to another URL, or explicitly select direct. "
                             "Port 18888 is a Bridge API, not a forward proxy.")
        proxy, label = LOCAL_PROXY, "auto (verified local forward proxy)"
    elif selection == "direct":
        proxy, label = "", "direct"
    else:
        try:
            if not isinstance(selection, str) or not selection.isascii() or any(
                    ord(char) <= 32 or ord(char) >= 127 for char in selection):
                raise ValueError()
            parsed = urlsplit(selection)
            if (parsed.scheme not in {"http", "https", "socks5", "socks5h"}
                    or not parsed.hostname or not parsed.port
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
                raise ValueError()
            if parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port in {18888, 8008}:
                raise ValueError()
        except (ValueError, TypeError):
            # Never echo a proxy URL: it may contain authentication credentials.
            raise ValueError("Muse proxy must be auto, direct, or an HTTP(S)/SOCKS5 proxy URL with an explicit port; "
                             "Bridge/service API ports cannot be used as proxies") from None
        proxy, label = selection, "explicit proxy"

    # Empty values also override libcurl's environment proxy discovery in direct
    # mode. This affects this dedicated process and its children only.
    for name in PROXY_VARIABLES:
        os.environ[name] = proxy
    bypass = list(dict.fromkeys(part.strip() for name in ("NO_PROXY", "no_proxy")
                               for part in os.environ.get(name, "").split(",") if part.strip()))
    bypass.extend(host for host in ("127.0.0.1", "localhost", "::1") if host not in bypass)
    # Explicit/auto proxies must not be bypassed for Muse by inherited '*' rules.
    if proxy:
        bypass = ["127.0.0.1", "localhost", "::1"]
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(bypass)
    return label
