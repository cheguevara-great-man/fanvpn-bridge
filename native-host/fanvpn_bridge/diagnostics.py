"""Opt-in request diagnostics for product-backend investigation.

Normal runtime logs intentionally omit URLs and header values.  A local marker
file can enable additional metadata while an endpoint-mapping problem is being
investigated.  Credential-bearing headers remain redacted in every mode.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .contracts import Header, ResolvedRoute


_SENSITIVE_HEADER_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "secret",
    "token",
    "api-key",
    "api_key",
    "apikey",
    "account-id",
    "account_id",
)
_MAX_RESPONSE_PREVIEW_BYTES = 4096


@dataclass(frozen=True, slots=True)
class DiagnosticOptions:
    level: str = "off"

    @property
    def enabled(self) -> bool:
        return self.level in {"safe", "full"}


def diagnostics_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) / "FanVPNBridge" if local_app_data else Path.home() / ".fanvpn-bridge"
    return base / "diagnostics.json"


def load_diagnostic_options(path: Path | None = None) -> DiagnosticOptions:
    source = path or diagnostics_path()
    if not source.is_file():
        return DiagnosticOptions()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return DiagnosticOptions()
    level = raw.get("level") if isinstance(raw, dict) else None
    return DiagnosticOptions(level=level if level in {"safe", "full"} else "off")


def request_family(route: ResolvedRoute) -> str:
    path = urlsplit(route.upstream_url).path
    marker = "/backend-api"
    if path.startswith(marker):
        path = path[len(marker) :] or "/"
    families = (
        ("/ps/plugins/installed", "plugins-installed"),
        ("/plugins/featured", "plugins-featured"),
        ("/ps/plugins/suggested", "plugins-suggested"),
        ("/ps/plugins/list", "plugins-list"),
        ("/ps/plugins/workspace/", "plugins-workspace"),
        ("/ps/mcp", "apps-mcp"),
        ("/codex/analytics-events/", "analytics"),
    )
    for prefix, family in families:
        if path == prefix or path.startswith(prefix):
            return family
    return "other"


def diagnostic_url(route: ResolvedRoute, options: DiagnosticOptions) -> str:
    if options.level == "full":
        return route.upstream_url
    parsed = urlsplit(route.upstream_url)
    redacted_query = urlencode(
        [(name, "<redacted>") for name, _value in parse_qsl(parsed.query, keep_blank_values=True)]
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, redacted_query, ""))


def diagnostic_headers(headers: list[Header], options: DiagnosticOptions) -> str:
    if options.level != "full":
        return ",".join(sorted({header.name.lower() for header in headers})) or "none"
    rendered: list[str] = []
    for header in headers:
        name = header.name.lower()
        value = "<redacted>" if any(part in name for part in _SENSITIVE_HEADER_PARTS) else header.value
        rendered.append(f"{name}={value!r}")
    return ",".join(rendered) or "none"


def diagnostic_body_preview(data: bytes) -> str:
    """Render a bounded response-body preview as one log-safe JSON string."""

    preview = data[:_MAX_RESPONSE_PREVIEW_BYTES].decode("utf-8", errors="replace")
    try:
        parsed = json.loads(preview)
    except json.JSONDecodeError:
        preview = re.sub(
            r'(?i)("(?:authorization|cookie|access_token|refresh_token|id_token|api[_-]?key|account[_-]?id|secret)"\s*:\s*)"[^"]*"',
            r'\1"<redacted>"',
            preview,
        )
        preview = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", preview)
    else:
        preview = json.dumps(_redact_json_value(parsed), ensure_ascii=False, separators=(",", ":"))
    return json.dumps(preview, ensure_ascii=False)


def _redact_json_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: (
                "<redacted>"
                if any(part in str(key).lower() for part in _SENSITIVE_HEADER_PARTS)
                else _redact_json_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    return value
