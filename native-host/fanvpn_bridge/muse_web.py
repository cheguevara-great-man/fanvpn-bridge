"""Opt-in Muse website model, with Muse-specific workspace tool handoffs.

The native executable has no Noise, browser or credential-extraction dependency.
An independent loopback service owns the private Muse transport. The legacy
Gemini module houses shared text/history/HTTP helpers; its XML tool protocol is
not used for Muse tools.
"""
from __future__ import annotations

import re
import json
import hmac
from collections.abc import Callable, Iterator
from typing import Any

from .gemini_web import GeminiWebError, TextWebProvider, _event_bytes
from .muse_auth import AUTH_ERRORS, MuseAuthError, validate_ticket
from .muse_workspace_provider import _muse_prompt, workspace_responses
from .deepseek_harness import _is_local_compaction_request

MUSE_WEB_PREFIX = "muse-web/"


class MuseWebError(GeminiWebError):
    def __init__(self, message: str, *, status: int = 502, code: str = "muse_web_error") -> None:
        super().__init__(message, status=status, code=code)


def is_muse_web_model(value: object) -> bool:
    return isinstance(value, str) and value.startswith(MUSE_WEB_PREFIX)


def _error(error: GeminiWebError) -> MuseWebError:
    # Shared adapter errors contain no upstream bodies or credentials.
    return MuseWebError(str(error).replace("Gemini Web", "Muse Web").replace("Google login", "Muse login")
                        .replace("FANVPN_GEMINI_WEB_URL", "FANVPN_MUSE_WEB_URL"),
                        status=error.status, code=error.code.replace("gemini_web", "muse_web"))


class MuseWebProvider(TextWebProvider):
    model_prefix = MUSE_WEB_PREFIX
    model_pattern = re.compile(r"^default$")

    def _prompt(self, payload, items, *, include_control, loaded_tools):
        return _muse_prompt(payload, items, include_control=include_control)

    def __init__(self, *, base_url: str | None = None, api_key: str = "", timeout_seconds: float = 600,
                 ticket_provider: Callable[[], dict[str, str]] | None = None) -> None:
        self.ticket_provider = ticket_provider
        if base_url and not api_key:
            raise ValueError("Muse Web requires a local service API key")
        try:
            super().__init__(base_url=base_url, api_key=api_key, timeout_seconds=timeout_seconds)
        except ValueError as exc:
            raise ValueError("Muse Web URL must be http://127.0.0.1:<port>/v1") from exc

    def browser_ticket(self, authorization: str) -> dict[str, str]:
        if not self.enabled:
            raise MuseWebError("Muse Web is disabled", status=503, code="muse_web_disabled")
        if not hmac.compare_digest(authorization.encode("utf-8"), ("Bearer " + self._api_key).encode("utf-8")):
            raise MuseWebError("Local Muse service authentication failed", status=401, code="muse_web_auth_required")
        try:
            if self.ticket_provider is None:
                raise MuseAuthError("muse_web_browser_unavailable")
            return validate_ticket(self.ticket_provider())
        except MuseAuthError as error:
            raise MuseWebError(str(error), status=error.status, code=error.code) from None
        except Exception:
            raise MuseWebError("Muse browser login is unavailable", status=503,
                               code="muse_web_browser_unavailable") from None

    def _check_service_error(self, status: int, body: bytes) -> None:
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError):
            return
        error = value.get("error") if isinstance(value, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, str) and code in AUTH_ERRORS:
            safe = MuseAuthError(code)
            raise MuseWebError(str(safe), status=safe.status, code=safe.code)
        safe_errors = {
            "muse_web_tools_unavailable": "Muse tool workspace is not configured; install the independent handoff skill first",
            "muse_web_tool_result_invalid": "Muse expected the matching current tool result; no replay performed",
            "muse_web_tool_format_invalid": "Muse workspace tool request was invalid; no tool was executed",
            "muse_web_transport_error": "Muse transport failed; no automatic replay; check the Muse conversation",
            "muse_web_timeout": "Muse response timed out; no automatic replay; check the Muse conversation",
            "muse_web_generation_failed": "Muse reported generation failure; check the Muse conversation",
        }
        if isinstance(code, str) and code in safe_errors:
            raise MuseWebError(safe_errors[code], status=status, code=code)

    def _upstream_events(self, body, connection):
        for event in super()._upstream_events(body, connection):
            if event["type"] == "error":
                # Auth may fail after the sidecar has sent an SSE heartbeat.
                # Preserve only known safe error codes, never upstream messages.
                self._check_service_error(502, json.dumps({"error": event}).encode("utf-8"))
            yield event

    def models_response(self, *, force: bool = False) -> dict[str, Any]:
        try:
            result = super().models_response(force=force)
        except GeminiWebError as exc:
            raise _error(exc) from exc
        for row in result["data"]:
            row["display_name"] = "Muse Web (experimental)"
            # This is an adapter input budget, NOT an asserted Muse model limit.
            row["context_window"] = 32768
            row["auto_compact_token_limit"] = 27000
            row["supported_reasoning_levels"] = []
            row["default_reasoning_level"] = "none"
        with self._model_lock:
            for row in result["data"]:
                self._models["default"] = dict(row)
        return result

    def responses(self, payload: dict[str, Any]) -> tuple[bool, dict[str, Any] | Iterator[bytes]]:
        try:
            if payload.get("tools") and not _is_local_compaction_request(payload):
                streaming, result = workspace_responses(self, payload)
            else:
                streaming, result = super().responses(payload)
        except GeminiWebError as exc:
            raise _error(exc) from exc
        if not streaming:
            result["usage"] = None
            return False, result

        def translated() -> Iterator[bytes]:
            try:
                for frame in result:
                    if frame.startswith(b"event: response.completed\n"):
                        event = json.loads(frame.decode("utf-8").split("data: ", 1)[1])
                        event["response"]["usage"] = None
                        frame = _event_bytes(event)
                    yield frame
            except GeminiWebError as exc:
                raise _error(exc) from exc
            finally:
                result.close()

        return True, translated()
