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
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

from .gemini_web import GeminiWebError, TextWebProvider, _event_bytes
from .muse_auth import AUTH_ERRORS, MuseAuthError, validate_ticket
from .muse_workspace_provider import _muse_prompt, workspace_responses
from .deepseek_harness import (_COMPACT_PROMPT, _deepseek_conversation_key, _history_item_fingerprint,
                               _is_local_compaction_request, _prompt_input_items, _response_object)

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
        self._conversation_heads: dict[str, str] = {}

    @staticmethod
    def _conversation_key(payload):
        # Summarization is a separate model task, not a user-chat continuation.
        if _is_local_compaction_request(payload) or payload.get("instructions") == _COMPACT_PROMPT:
            return None
        return _deepseek_conversation_key(payload)

    def _history(self, payload):
        key = self._conversation_key(payload)
        if not payload.get("previous_response_id") and key:
            with self._checkpoint_lock:
                previous = self._conversation_heads.get(key)
                checkpoint = self._checkpoints.get(previous)
            if checkpoint is not None:
                # Codex can replay canonical history instead of sending a
                # previous_response_id. Identity alone is not proof: require
                # the complete committed prefix plus genuinely new input.
                items = _prompt_input_items(payload)
                history = checkpoint.history
                if history and len(items) > len(history) and all(
                    _history_item_fingerprint(old) == _history_item_fingerprint(new)
                    for old, new in zip(history, items)
                ):
                    payload = {**payload, "previous_response_id": previous}
        return super()._history(payload)

    def _remember_conversation(self, payload, response):
        key = self._conversation_key(payload)
        if not key or response.get("status") != "completed":
            return
        with self._checkpoint_lock:
            if response.get("id") not in self._checkpoints:
                return
            self._conversation_heads[key] = response["id"]
            # Reuse the existing checkpoint lifetime/limit, not another cache.
            self._conversation_heads = {identity: head for identity, head in self._conversation_heads.items()
                                        if head in self._checkpoints}

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
            "muse_web_connect_timeout": "Muse VM connection timed out before sending a task; check the Muse service proxy",
            "muse_web_connect_failed": "Cannot connect to the Muse VM before sending a task; check the service proxy and VM",
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
            self._remember_conversation(payload, result)
            return False, result

        def translated() -> Iterator[bytes]:
            last_response = None
            sequence = -1
            try:
                for frame in result:
                    if frame.startswith(b"event: response."):
                        event = json.loads(frame.decode("utf-8").split("data: ", 1)[1])
                        sequence = max(sequence, event.get("sequence_number", -1))
                        if isinstance(event.get("response"), dict):
                            last_response = event["response"]
                        if event["type"] == "response.completed":
                            event["response"]["usage"] = None
                            self._remember_conversation(payload, event["response"])
                            frame = _event_bytes(event)
                    yield frame
            except GeminiWebError as exc:
                # A bare error event is ignored by some Responses clients, which
                # then report only EOF and retry. Give Muse failures an explicit
                # terminal response instead of concealing them as a closed stream.
                safe = _error(exc)
                if last_response is None:
                    last_response = _response_object("resp_muse_" + uuid.uuid4().hex, payload["model"],
                                                     int(time.time()), "in_progress", [], {})
                    last_response["usage"] = None
                    sequence += 1
                    yield _event_bytes({"type": "response.created", "sequence_number": sequence,
                                        "response": last_response})
                failed = {**last_response, "status": "failed", "usage": None,
                          "error": {"code": "server_error", "message": f"{safe.code}: {safe}"}}
                yield _event_bytes({"type": "response.failed", "sequence_number": sequence + 1, "response": failed})
            finally:
                result.close()

        return True, translated()
