"""Attach Codex account authentication to fixed first-party product routes.

Codex deliberately withholds ChatGPT MCP credentials from custom origins.  The
loopback Bridge is such an origin even though its allowlisted upstream is the
first-party ChatGPT service.  This module restores the credential only after
route resolution has proven that the request targets one of the two official
ChatGPT-hosted MCP endpoints.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import Header, ResolvedRoute


_AUTHENTICATED_PRODUCT_PATHS = frozenset(
    {
        "/backend-api/ps/mcp",
        "/backend-api/wham/apps",
    }
)
_AUTHENTICATED_PRODUCT_PREFIXES = ("/backend-api/wham/",)
_MAX_AUTH_FILE_BYTES = 1024 * 1024


class CodexProductAuth:
    """Loads current Codex credentials on demand and adds missing MCP headers."""

    def __init__(self, auth_path: Path) -> None:
        self._auth_path = auth_path

    def attach(self, route: ResolvedRoute, headers: list[Header]) -> list[Header]:
        if route.name != "chatgpt-backend":
            return headers
        upstream = urlsplit(route.upstream_url)
        if (
            upstream.scheme != "https"
            or upstream.hostname != "chatgpt.com"
            or upstream.port not in {None, 443}
            or upstream.username is not None
            or upstream.password is not None
        ):
            return headers
        path = upstream.path
        if path not in _AUTHENTICATED_PRODUCT_PATHS and not path.startswith(
            _AUTHENTICATED_PRODUCT_PREFIXES
        ):
            return headers
        names = {header.name.lower() for header in headers}
        if "authorization" in names:
            return headers

        credentials = self._load_credentials()
        if credentials is None:
            return headers
        access_token, account_id = credentials
        attached = [*headers, Header("Authorization", f"Bearer {access_token}")]
        if account_id and "chatgpt-account-id" not in names:
            attached.append(Header("ChatGPT-Account-ID", account_id))
        return attached

    def _load_credentials(self) -> tuple[str, str | None] | None:
        try:
            if self._auth_path.stat().st_size > _MAX_AUTH_FILE_BYTES:
                return None
            raw = json.loads(self._auth_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("tokens"), dict):
            return None
        tokens = raw["tokens"]
        access_token = tokens.get("access_token")
        account_id = tokens.get("account_id")
        if not isinstance(access_token, str) or not access_token.strip():
            return None
        if not isinstance(account_id, str) or not account_id.strip():
            account_id = None
        return access_token.strip(), account_id.strip() if account_id else None
