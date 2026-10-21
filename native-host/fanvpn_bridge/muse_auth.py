"""Bounded, memory-only Muse browser connection tickets. No Cookie access."""
from __future__ import annotations

import http.client
import json
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

AUTH_ERRORS = {
    "muse_web_login_required": (401, "Log in to Muse in Chrome, then retry"),
    "muse_web_tab_required": (409, "Open the signed-in Muse page in Chrome, then retry"),
    "muse_web_browser_unavailable": (503, "Muse browser login is unavailable; check Chrome and the Bridge extension"),
    "muse_web_extension_required": (503, "Update the Bridge extension to enable automatic Muse login"),
    "muse_web_vm_unavailable": (503, "Muse VM is unavailable; check the Muse page"),
    "muse_web_rate_limited": (429, "Muse login requests are rate limited; try again later"),
    "muse_web_auth_timeout": (504, "Timed out obtaining the Muse browser ticket"),
    "muse_web_ticket_invalid": (502, "Muse returned an invalid browser connection ticket"),
}


class MuseAuthError(RuntimeError):
    def __init__(self, code: object) -> None:
        self.code = code if isinstance(code, str) and code in AUTH_ERRORS else "muse_web_browser_unavailable"
        self.status, message = AUTH_ERRORS[self.code]
        super().__init__(message)


def validate_ticket(value: object) -> dict[str, str]:
    if (not isinstance(value, Mapping) or set(value) != {"vm_id", "access_token", "hatch_token"}
            or not isinstance(value["vm_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value["vm_id"])):
        raise MuseAuthError("muse_web_ticket_invalid")
    for key in ("access_token", "hatch_token"):
        token = value[key]
        if (not isinstance(token, str) or not 1 <= len(token) <= 16384
                or not token.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in token)):
            raise MuseAuthError("muse_web_ticket_invalid")
    return dict(value)


class BrowserTicketClient:
    def __init__(self, *, bridge_url: str = "http://127.0.0.1:18888", api_key: str, timeout: float = 40) -> None:
        parsed = urlsplit(bridge_url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path not in {"", "/"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Muse auth Bridge URL must be http://127.0.0.1:<port>")
        self.host, self.port = parsed.hostname, parsed.port or 80
        self.key, self.timeout = api_key, timeout

    def ticket(self) -> dict[str, str]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.request("POST", "/muse-web/auth", body=b"", headers={
                "Authorization": "Bearer " + self.key, "Accept": "application/json",
            })
            response = connection.getresponse()
            # http.client does not follow redirects or consult system proxies.
            raw = response.read(128 * 1024 + 1)
            if len(raw) > 128 * 1024:
                raise MuseAuthError("muse_web_ticket_invalid")
            value = json.loads(raw)
            if response.status != 200:
                error = value.get("error") if isinstance(value, dict) else None
                raise MuseAuthError(error.get("code") if isinstance(error, dict) else "muse_web_browser_unavailable")
            return validate_ticket(value)
        except MuseAuthError:
            raise
        except TimeoutError:
            raise MuseAuthError("muse_web_auth_timeout") from None
        except (OSError, http.client.HTTPException, ValueError):
            raise MuseAuthError("muse_web_browser_unavailable") from None
        finally:
            connection.close()
