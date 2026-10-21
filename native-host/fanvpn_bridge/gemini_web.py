"""Opt-in Gemini Web transport, with Codex remaining the sole tool executor.

The loopback Gemini-Web2API service owns Google authentication and the changing
web protocol. We deliberately do not send it Codex's tools: its JSON-only tool
bridge cannot represent freeform apply_patch input. Reuse the existing direct
tool protocol instead, without repairing malformed model output.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import logging
import queue
import re
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .deepseek_harness import (
    _COMPACT_PROMPT,
    _build_compact_v1_output,
    _deepseek_control_signature,
    _deepseek_tool_definitions,
    _extract_compact_user_messages,
    _history_item_fingerprint,
    _is_local_compaction_request,
    _last_completed_response,
    _looks_like_tool_call_attempt,
    _parse_tool_calls,
    _prompt_input_items,
    _responses_events,
    _responses_to_deepseek_prompt,
    _tool_protocol_prompt,
)

GEMINI_WEB_PREFIX = "gemini-web/"
_MAX_BODY = 32 * 1024 * 1024
_MODEL_ID = re.compile(r"^gemini-(?:\d+[a-z0-9.-]*|auto)$")
_HEARTBEAT_SECONDS = 5.0
_TOOL_LOADER = "bridge_load_tool_definitions"
_MAX_TOOL_LOAD_ROUNDS = 4
_TOOL_REMINDER = (
    "CODEX TOOL REMINDER:\n"
    "Reuse supplied definitions. Codex executes tools; never invent results. Tool calls belong in the final RESPONSE: "
    "only complete direct XML blocks, no surrounding prose, attributes, wrappers or alternative syntax. "
    "Use each tool's exact tag and matching closing tag. Function bodies: valid JSON arguments with correct escaping. "
    "Freeform bodies: raw tool input, never JSON-encoded. "
    "Use exec_command for reads/scripts; apply_patch for text/code edits, split at semantic boundaries. "
    "Load deferred definitions before calling them; request loading alone and never reload known definitions. "
    "Wait for TOOL RESULT; if no tool is needed, answer normally."
)
_TOOL_LOADER_DEFINITION = {
    "type": "function", "name": _TOOL_LOADER,
    "description": (
        "Load the full definitions of tools from the deferred tool directory before calling them. "
        "This only retrieves tool metadata; it does not execute those tools. "
        "Request 1 to 8 exact qualified tool names together. Do not request already loaded tools."
    ),
    "parameters": {
        "type": "object", "properties": {
            "names": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
        }, "required": ["names"], "additionalProperties": False,
    },
}
_HOSTED_SEARCH_NOTICE = (
    "WEB MODEL CAPABILITIES:\n"
    "OpenAI-hosted web_search is unavailable on this web route, even if the client enables it by default. "
    "It is not a callable tool here. Use an explicitly supplied search function if available; otherwise explain "
    "when a task requires search you cannot perform. Never invent search execution or search results."
)


class GeminiWebError(RuntimeError):
    def __init__(self, message: str, *, status: int = 502, code: str = "gemini_web_error") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def is_gemini_web_model(value: object) -> bool:
    return isinstance(value, str) and value.startswith(GEMINI_WEB_PREFIX)


@dataclass
class _Checkpoint:
    history: list[dict[str, Any]]
    control: str
    loaded_tools: frozenset[str] = frozenset()


def _event_bytes(event: Mapping[str, Any]) -> bytes:
    return (f"event: {event['type']}\ndata: "
            + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8")


def _answer(response: Mapping[str, Any]) -> str:
    if not isinstance(response.get("output"), list):
        raise GeminiWebError("Gemini Web completion has no valid output list")
    return "".join(
        str(part.get("text") or "")
        for item in response.get("output", []) if isinstance(item, dict) and item.get("type") == "message"
        for part in item.get("content", []) if isinstance(part, dict) and part.get("type") == "output_text"
    )


def _has_images(value: object) -> bool:
    if isinstance(value, list):
        return any(_has_images(item) for item in value)
    if isinstance(value, dict):
        return value.get("type") in {"input_image", "image", "output_image"} or any(
            _has_images(item) for item in value.values()
        )
    return False


def _prompt_tool_items(items: object) -> object:
    """Use qualified names in the text protocol, preserving caller-owned input."""
    result = copy.deepcopy(items)
    if isinstance(result, list):
        for item in result:
            if (isinstance(item, dict) and item.get("type") in {"function_call", "custom_tool_call"}
                    and item.get("namespace")):
                item["name"] = f"{item['namespace']}.{item.get('name', '')}"
    return result


def _tool_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, tuple[str, str]]]:
    """Flatten namespace definitions for prompting, not for Codex execution."""
    tools = payload.get("tools")
    if tools is None:
        tools = []
    if not isinstance(tools, list):
        raise GeminiWebError("Gemini Web tools must be a list", status=400)
    flattened: list[dict[str, Any]] = []
    routes: dict[str, tuple[str, str]] = {}
    names: set[str] = set()
    hosted_search_requested = False

    def add(tool: object, namespace: str = "", description: str = "") -> None:
        if not isinstance(tool, dict) or tool.get("type") not in {"function", "custom"}:
            kind = tool.get("type") if isinstance(tool, dict) else "invalid"
            raise GeminiWebError(
                f"Gemini Web does not support tool type {kind}; refresh its model catalog to disable unsupported built-in tools",
                status=400, code="gemini_web_tool_unsupported")
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            raise GeminiWebError("Gemini Web tools need nonempty names", status=400)
        alias = f"{namespace}.{name}" if namespace else name
        if alias in names:
            raise GeminiWebError("Gemini Web tool names are ambiguous", status=400)
        names.add(alias)
        normalized = copy.deepcopy(tool)
        normalized["name"] = alias
        if description:
            normalized["description"] = description + "\n" + str(tool.get("description") or "")
        flattened.append(normalized)
        if namespace:
            routes[alias] = (namespace, name)

    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "web_search":
            # Codex's default search setting can attach this hosted tool even
            # when the model catalog declares supports_search_tool=false.
            # Do not advertise an executor we do not have, or reject greetings
            # merely because an optional hosted capability was attached.
            hosted_search_requested = True
        elif isinstance(tool, dict) and tool.get("type") == "namespace":
            namespace, children = tool.get("name"), tool.get("tools")
            if not isinstance(namespace, str) or not namespace.strip() or not isinstance(children, list):
                raise GeminiWebError("Gemini Web namespaces need a name and tool list", status=400)
            for child in children:
                add(child, namespace, str(tool.get("description") or ""))
        else:
            add(tool)
    normalized_payload = {**payload, "tools": flattened, "input": _prompt_tool_items(payload.get("input"))}
    if hosted_search_requested:
        logging.getLogger(__name__).warning("gemini_web_hosted_search_unavailable tool=web_search")
        normalized_payload["instructions"] = str(payload.get("instructions") or "") + "\n\n" + _HOSTED_SEARCH_NOTICE
    return normalized_payload, routes


def _restore_tool_frame(frame: bytes, routes: dict[str, tuple[str, str]]) -> bytes:
    if not routes or frame == b"data: [DONE]\n\n":
        return frame
    event = json.loads(frame.decode("utf-8").split("data: ", 1)[1])
    items = [event.get("item")]
    if isinstance(event.get("response"), dict):
        items.extend(event["response"].get("output") or [])
    for item in items:
        if isinstance(item, dict) and item.get("type") in {"function_call", "custom_tool_call"}:
            route = routes.get(item.get("name"))
            if route:
                item["namespace"], item["name"] = route
    return _event_bytes(event)


def _initial_tool_names(payload: Mapping[str, Any], history: list[dict[str, Any]]) -> set[str]:
    tools = _deepseek_tool_definitions(payload)
    available = {str(tool["name"]) for tool in tools}
    # Keep caller-supplied functions and Codex's programming tools immediately
    # available. The large connected MCP catalogs are discoverable, not removed.
    loaded = {name for name in available if not name.startswith("mcp__")}
    loaded.update(str(item.get("name") or "") for item in history
                  if item.get("type") in {"function_call", "custom_tool_call"})
    return loaded & available


def _gemini_prompt(payload: Mapping[str, Any], items: list[dict[str, Any]], *,
                   include_control: bool, loaded_tools: set[str]) -> str:
    # Only this provider uses compact reminders; DeepSeek's existing protocol is
    # unchanged. Resend definitions when establishing a fresh webpage session.
    text = _responses_to_deepseek_prompt({**payload, "tools": []}, input_items=items,
                                        include_control=include_control)
    tools = _deepseek_tool_definitions(payload)
    if not tools:
        return text
    sections = [text]
    if not include_control:
        sections.append(_TOOL_REMINDER)
        return "\n\n".join(section for section in sections if section)
    ready = [tool for tool in tools if tool["name"] in loaded_tools]
    deferred = [tool for tool in tools if tool["name"] not in loaded_tools]
    if deferred:
        ready.extend(_deepseek_tool_definitions({"tools": [_TOOL_LOADER_DEFINITION]}))
    sections.append(_tool_protocol_prompt(ready))
    if deferred:
        groups: dict[str, list[str]] = {}
        ungrouped: list[str] = []
        for tool in deferred:
            name = str(tool["name"])
            if "." in name:
                namespace, child = name.rsplit(".", 1)
                groups.setdefault(namespace, []).append(child)
            else:
                ungrouped.append(name)
        directory = ["DEFERRED TOOL DIRECTORY (names only, not callable until loaded):",
                     "Grouped entries list a namespace once, followed by its child names. "
                     "The exact qualified name is namespace + '.' + child; never use a child name alone.",
                     f"Use <{_TOOL_LOADER}> with exact qualified names from this directory to load their "
                     "descriptions and parameter schemas. Emit that request alone, then wait. "
                     "Do not mix it with executable tool calls. Normal programming tools above need no loading."]
        directory.extend(namespace + ": " + ", ".join(names) for namespace, names in groups.items())
        if ungrouped:
            directory.append("Ungrouped entries (use the full name as written):\n" + "\n".join(ungrouped))
        sections.append("\n\n".join(directory))
    return "\n\n".join(section for section in sections if section)


def _tool_definition_result(calls: list[dict[str, Any]], payload: Mapping[str, Any],
                            loaded_tools: set[str]) -> str:
    if len(calls) != 1 or calls[0]["name"] != _TOOL_LOADER:
        raise GeminiWebError("Tool definition loading must not be mixed with executable tool calls",
                             code="gemini_web_tool_format_invalid")
    arguments = calls[0].get("arguments")
    names = arguments.get("names") if isinstance(arguments, dict) else None
    available = {str(tool["name"]): tool for tool in _deepseek_tool_definitions(payload)}
    if (not isinstance(arguments, dict) or set(arguments) != {"names"}
            or not isinstance(names, list) or not 1 <= len(names) <= 8
            or any(not isinstance(name, str) or name not in available or name in loaded_tools for name in names)
            or len(set(names)) != len(names)):
        raise GeminiWebError("Tool definition loading requires 1 to 8 distinct, available, unloaded tool names",
                             code="gemini_web_tool_format_invalid")
    loaded_tools.update(names)
    return ("TOOL DEFINITION RESULT:\nThe following tools are now loaded. Codex still executes all real tools.\n"
            + _tool_protocol_prompt([available[name] for name in names]))


class TextWebProvider:
    """Shared Responses/tool loop for opt-in loopback text services.

    Defaults preserve Gemini's existing wire protocol. Other website transports
    override model metadata, not the strict tool parser or continuation engine.
    """

    model_prefix = GEMINI_WEB_PREFIX
    model_pattern = _MODEL_ID

    def __init__(self, *, base_url: str | None = None, api_key: str = "", timeout_seconds: float = 600) -> None:
        self.enabled = bool(base_url)
        parsed = urlsplit(base_url or "http://127.0.0.1:8007/v1")
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/v1"):
            raise ValueError("Gemini Web URL must be http://127.0.0.1:<port>/v1")
        self._host, self._port = parsed.hostname, parsed.port or 80
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._models: dict[str, dict[str, Any]] = {}
        self._models_at = 0.0
        self._model_lock = threading.Lock()
        # Bound memory, but do not expire a conversation simply for being idle.
        # Evicted sparse histories fail explicitly rather than silently losing context.
        self._checkpoints: OrderedDict[str, _Checkpoint] = OrderedDict()
        self._checkpoint_lock = threading.Lock()
        self._replays: OrderedDict[str, list[bytes]] = OrderedDict()

    def _connection(self, *, models: bool = False) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self._host, self._port, timeout=min(self._timeout, 15) if models else self._timeout)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _check_status(self, response: http.client.HTTPResponse) -> None:
        if response.status == 200:
            return
        body = response.read(_MAX_BODY + 1)
        if len(body) > _MAX_BODY:
            raise GeminiWebError("Gemini Web error response exceeded the size limit")
        self._check_service_error(response.status, body)
        # Never echo an upstream error body: it may contain a prompt or credentials.
        if response.status in {400, 404} and any(
            marker in body.decode("utf-8", errors="replace").lower()
            for marker in ("未知会话标识", "unknown conversation", "previous response not found")
        ):
            raise GeminiWebError("Gemini Web service lost its conversation snapshot", code="gemini_web_session_missing")
        if response.status in {401, 403}:
            raise GeminiWebError("Gemini Web authentication failed; check the local service and Google login",
                                 status=401, code="gemini_web_auth_required")
        raise GeminiWebError(f"Gemini Web service returned HTTP {response.status}",
                             status=429 if response.status == 429 else 502)

    def _check_service_error(self, status: int, body: bytes) -> None:
        """Provider hook for known, sanitized service errors; never echo bodies."""

    def models_response(self, *, force: bool = False) -> dict[str, Any]:
        if not self.enabled:
            return {"object": "list", "data": [], "enabled": False}
        with self._model_lock:
            if force or not self._models or time.monotonic() - self._models_at >= 60:
                connection = self._connection(models=True)
                try:
                    connection.request("GET", "/v1/models", headers=self._headers())
                    response = connection.getresponse()
                    self._check_status(response)
                    raw = response.read(_MAX_BODY + 1)
                    if len(raw) > _MAX_BODY:
                        raise GeminiWebError("Gemini Web model directory exceeded the size limit")
                    value = json.loads(raw)
                    rows = value.get("data") if isinstance(value, dict) else None
                    if not isinstance(rows, list):
                        raise GeminiWebError("Gemini Web model directory is invalid")
                    models = {}
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        model_id = row.get("id")
                        if not isinstance(model_id, str) or not self.model_pattern.fullmatch(model_id):
                            continue
                        if re.search(r"(?:^|-)(?:image|video|music|task|canvas|research|learning)(?:-|$)", model_id):
                            continue
                        if row.get("available_account_count") == 0:
                            continue
                        raw_levels = row.get("thinking_levels") or []
                        levels = [level for level in raw_levels if isinstance(level, str) and level in {"low", "medium", "high"}] if isinstance(raw_levels, list) else []
                        window = row.get("context_window") or row.get("input_token_limit") or 32768
                        if not isinstance(window, int) or isinstance(window, bool) or window < 1024:
                            window = 32768
                        models[model_id] = {
                            "id": self.model_prefix + model_id, "object": "model", "owned_by": self.model_prefix.rstrip("/"),
                            "display_name": f"Gemini Web · {row.get('display_name') or row.get('name') or model_id}",
                            "supported_reasoning_levels": levels,
                            "default_reasoning_level": "medium" if "medium" in levels else (levels[0] if levels else "none"),
                            "context_window": window, "auto_compact_token_limit": int(window * 0.85),
                            "input_modalities": ["text"],
                            "supports_search_tool": False,
                        }
                    self._models, self._models_at = models, time.monotonic()
                except (OSError, http.client.HTTPException, ValueError) as exc:
                    raise GeminiWebError("Cannot read the local Gemini Web model directory") from exc
                finally:
                    connection.close()
            return {"object": "list", "data": copy.deepcopy(list(self._models.values())), "enabled": True}

    def _history(self, payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
        items = copy.deepcopy(_prompt_input_items(payload))
        previous = payload.get("previous_response_id")
        if not previous:
            return items, items, None
        with self._checkpoint_lock:
            checkpoint = self._checkpoints.get(str(previous))
        if checkpoint is None:
            # A complete replay can safely establish a fresh service session.
            if any(item.get("role") == "user" for item in items) and any(
                item.get("role") == "assistant" or item.get("type") in {"function_call", "custom_tool_call"} for item in items
            ):
                return items, items, None
            raise GeminiWebError("Gemini Web continuation needs full history after a Bridge restart or checkpoint eviction",
                                 status=409, code="gemini_web_history_missing")
        history = copy.deepcopy(checkpoint.history)
        prefix = tuple(_history_item_fingerprint(item) for item in history)
        fingerprints = tuple(_history_item_fingerprint(item) for item in items)
        if len(fingerprints) >= len(prefix) and fingerprints[:len(prefix)] == prefix:
            delta = items[len(prefix):]
            history = items
        elif any(item.get("role") == "user" for item in items) and any(
            item.get("role") == "assistant" or item.get("type") in {"function_call", "custom_tool_call"} for item in items
        ):
            # Edited or branched full history must not be appended to the old branch.
            return items, items, None
        else:
            delta = items
            history.extend(items)
        reuse = checkpoint.control == _deepseek_control_signature(payload)
        return history, delta if reuse else history, str(previous) if reuse else None

    def _prompt(self, payload, items, *, include_control, loaded_tools):
        return _gemini_prompt(payload, items, include_control=include_control, loaded_tools=loaded_tools)

    def responses(self, payload: dict[str, Any]) -> tuple[bool, dict[str, Any] | Iterator[bytes]]:
        if not self.enabled:
            raise GeminiWebError("Gemini Web is disabled; configure FANVPN_GEMINI_WEB_URL explicitly", status=503)
        model = str(payload.get("model") or "")
        if not model.startswith(self.model_prefix):
            raise GeminiWebError("Unsupported Gemini Web model", status=400)
        self.models_response()
        model_id = model[len(self.model_prefix):]
        if model_id not in self._models:
            raise GeminiWebError("Requested Gemini Web model is not in the account directory; refresh the catalog",
                                 status=400, code="gemini_web_model_unavailable")
        if _has_images(payload.get("input")):
            raise GeminiWebError("This experimental Gemini Web adapter supports text input only", status=400)
        payload, tool_routes = _tool_payload(payload)
        tools = payload["tools"]
        if any(tool["name"] == _TOOL_LOADER for tool in tools):
            raise GeminiWebError("Tool name conflicts with the Gemini Web definition loader", status=400)
        text_config = payload.get("text") or {}
        if not isinstance(text_config, dict) or not isinstance(text_config.get("format", {}), dict):
            raise GeminiWebError("Invalid Gemini Web text configuration", status=400)
        if payload.get("tool_choice") not in (None, "auto") or text_config.get("format", {}).get("type", "text") != "text":
            raise GeminiWebError("This Gemini Web adapter currently supports automatic tools and plain-text output only", status=400)
        if _is_local_compaction_request(payload):
            payload = {**payload, "tools": [], "instructions": _COMPACT_PROMPT, "previous_response_id": None}
        history, turn_items, previous = self._history(payload)
        loaded_tools = _initial_tool_names(payload, history)
        if previous:
            with self._checkpoint_lock:
                checkpoint = self._checkpoints.get(previous)
                if checkpoint is not None:
                    loaded_tools.update(checkpoint.loaded_tools)
        body = {
            # Upstream's streamed projection rejects rewritten snapshots. Tool
            # turns are buffered anyway, so use its non-streaming authoritative
            # result instead of inheriting that failure mode.
            "model": model_id, "stream": not bool(tools),
            "input": self._prompt(payload, turn_items, include_control=not previous, loaded_tools=loaded_tools),
        }
        if previous:
            body["previous_response_id"] = previous
        if isinstance(payload.get("reasoning"), dict):
            effort = payload["reasoning"].get("effort")
            if effort in {"low", "medium", "high"}:
                if effort not in self._models[model_id]["supported_reasoning_levels"]:
                    raise GeminiWebError("Requested reasoning level is not supported by this webpage model", status=400)
                body["reasoning"] = {"effort": effort}
            elif effort not in (None, "none"):
                raise GeminiWebError("Unsupported Gemini Web reasoning level", status=400)
        replay_key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        # Only coalesce requests with a proven conversation identity. Independent
        # new conversations asking the same question must remain independent.
        replay_key = replay_key if payload.get("previous_response_id") or payload.get("prompt_cache_key") else ""
        events = self._stream(payload, body, history, replay_key=replay_key, tool_routes=tool_routes,
                              loaded_tools=loaded_tools)
        if payload.get("stream") is True:
            return True, events
        return False, _last_completed_response(list(events))

    def _upstream_events(self, body: dict[str, Any], connection: http.client.HTTPConnection) -> Iterator[dict[str, Any]]:
        connection.request("POST", "/v1/responses", json.dumps(body, ensure_ascii=False).encode("utf-8"), self._headers())
        response = connection.getresponse()
        self._check_status(response)
        if not body["stream"]:
            raw = response.read(_MAX_BODY + 1)
            if len(raw) > _MAX_BODY:
                raise GeminiWebError("Gemini Web response exceeded the size limit")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise GeminiWebError("Gemini Web response is invalid")
            yield {"type": "response.completed", "response": value}
            return
        if "text/event-stream" not in str(response.getheader("Content-Type", "")).lower():
            raise GeminiWebError("Gemini Web service did not return an event stream")
        data: list[str] = []
        total = 0
        while True:
            raw = response.readline(_MAX_BODY + 1)
            total += len(raw)
            if total > _MAX_BODY:
                raise GeminiWebError("Gemini Web stream exceeded the size limit")
            if not raw:
                break
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line:
                if data:
                    text = "\n".join(data)
                    data = []
                    if text == "[DONE]":
                        break
                    event = json.loads(text)
                    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                        raise GeminiWebError("Gemini Web stream event is invalid")
                    yield event
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
        if data:
            raise GeminiWebError("Gemini Web stream ended inside an event")

    def _stream(self, payload: dict[str, Any], body: dict[str, Any], history: list[dict[str, Any]], *,
                replay_key: str, tool_routes: dict[str, tuple[str, str]], loaded_tools: set[str]) -> Iterator[bytes]:
        with self._checkpoint_lock:
            replay = self._replays.get(replay_key)
        if replay is not None:
            yield from replay
            return
        pending: queue.Queue[object] = queue.Queue(maxsize=8)
        cancelled = threading.Event()
        connection = self._connection()
        end = object()
        tools = _deepseek_tool_definitions(payload)
        available_names = {str(tool["name"]) for tool in tools}
        parse_names = available_names | {_TOOL_LOADER}
        freeform_names = {str(tool["name"]) for tool in tools if tool["freeform"]}

        def put(value: object) -> None:
            while not cancelled.is_set():
                try:
                    pending.put(value, timeout=0.1)
                    return
                except queue.Full:
                    pass

        def run() -> None:
            current = body
            restored = False
            load_rounds = 0
            discovery_usage: dict[str, int] = {}
            try:
                while not cancelled.is_set():
                    seen_event = False
                    next_request = None
                    try:
                        for event in self._upstream_events(current, connection):
                            if cancelled.is_set():
                                return
                            seen_event = True
                            response = event.get("response")
                            if tools and event["type"] == "response.completed" and isinstance(response, dict):
                                if response.get("status") != "completed" or not response.get("id"):
                                    raise GeminiWebError("Gemini Web completion is invalid")
                                text = _answer(response)
                                if not text.strip() or any(
                                    not isinstance(item, dict) or item.get("type") not in {"message", "reasoning"}
                                    for item in response["output"]
                                ):
                                    raise GeminiWebError("Gemini Web completed without a supported text answer")
                                calls = _parse_tool_calls(text, parse_names, freeform_names)
                                if calls and any(call["name"] == _TOOL_LOADER for call in calls):
                                    if load_rounds >= _MAX_TOOL_LOAD_ROUNDS:
                                        raise GeminiWebError("Gemini Web tool definition loading exceeded its round limit",
                                                             code="gemini_web_tool_load_limit")
                                    definitions = _tool_definition_result(calls, payload, loaded_tools)
                                    load_rounds += 1
                                    usage = response.get("usage") or {}
                                    for key in ("input_tokens", "output_tokens", "total_tokens"):
                                        value = usage.get(key)
                                        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                                            discovery_usage[key] = discovery_usage.get(key, 0) + value
                                    next_request = {**current, "previous_response_id": response["id"], "input": definitions}
                                    continue
                                if calls and any(call["name"] not in loaded_tools for call in calls):
                                    raise GeminiWebError("Gemini Web called a deferred tool before loading its definition",
                                                         code="gemini_web_tool_not_loaded")
                                if discovery_usage:
                                    usage = dict(response.get("usage") or {})
                                    for key, value in discovery_usage.items():
                                        usage[key] = usage.get(key, 0) + value
                                    event = {**event, "response": {**response, "usage": usage}}
                            # Tool turns are buffered. Internal metadata requests
                            # and their response IDs must never reach Codex.
                            if not tools or event["type"] in {"response.completed", "response.failed", "response.incomplete", "error"}:
                                put(event)
                    except GeminiWebError as error:
                        if (seen_event or restored or error.code != "gemini_web_session_missing"
                                or not current.get("previous_response_id")):
                            raise
                        # Rebuild the complete reduced protocol, including any
                        # definitions loaded in earlier turns, exactly once.
                        connection.close()
                        current = {key: value for key, value in current.items() if key != "previous_response_id"}
                        current["input"] = self._prompt(payload, history, include_control=True, loaded_tools=loaded_tools)
                        restored = True
                        continue
                    if next_request is None:
                        break
                    current = next_request
            except GeminiWebError as error:
                put(error)
            except (OSError, ValueError, http.client.HTTPException) as error:
                put(GeminiWebError("Gemini Web service disconnected or returned an invalid stream"))
            finally:
                connection.close()
                put(end)

        worker = threading.Thread(target=run, name="gemini-web-response", daemon=True)
        worker.start()
        completed = False
        response_id = ""
        started = time.monotonic()
        observed_frames: list[bytes] = []
        try:
            while True:
                if time.monotonic() - started > self._timeout:
                    raise GeminiWebError("Gemini Web request timed out", status=504)
                try:
                    event = pending.get(timeout=min(_HEARTBEAT_SECONDS, self._timeout))
                except queue.Empty:
                    yield b": gemini-web waiting\n\n"
                    continue
                if event is end:
                    break
                if isinstance(event, Exception):
                    raise event
                assert isinstance(event, dict)
                if completed:
                    raise GeminiWebError("Gemini Web emitted events after completion")
                kind = event["type"]
                upstream_response = event.get("response")
                if kind in {"response.failed", "response.incomplete", "error"}:
                    raise GeminiWebError("Gemini Web generation failed or did not complete")
                if isinstance(upstream_response, dict):
                    incoming_id = str(upstream_response.get("id") or "")
                    if response_id and incoming_id and incoming_id != response_id:
                        raise GeminiWebError("Gemini Web changed the response identity mid-stream")
                    upstream_response = {**upstream_response, "model": payload["model"]}
                    event = {**event, "response": upstream_response}
                    response_id = str(upstream_response.get("id") or response_id)
                if kind != "response.completed":
                    if not tools:
                        frame = _event_bytes(event)
                        observed_frames.append(frame)
                        yield frame
                    continue
                if not isinstance(upstream_response, dict) or upstream_response.get("status") != "completed" or not response_id:
                    raise GeminiWebError("Gemini Web completion is invalid")
                text = _answer(upstream_response)
                if not text.strip() or any(not isinstance(item, dict) or item.get("type") not in {"message", "reasoning"} for item in upstream_response.get("output", [])):
                    raise GeminiWebError("Gemini Web completed without a supported text answer")
                calls = _parse_tool_calls(text, parse_names, freeform_names)
                if calls is None and _looks_like_tool_call_attempt(text, parse_names):
                    raise GeminiWebError("Gemini Web emitted an invalid tool call; no tool was executed",
                                         code="gemini_web_tool_format_invalid")
                if tools:
                    frames = list(_responses_events(payload["model"], "" if calls else text, calls or [], response_id=response_id))
                    final = _last_completed_response(frames)
                    final["usage"] = upstream_response.get("usage") or final["usage"]
                    final_frames = []
                    for frame in frames:
                        if frame.startswith(b"event: response.completed\n"):
                            event_value = json.loads(frame.decode("utf-8").split("data: ", 1)[1])
                            frame = _event_bytes({**event_value, "response": final})
                        final_frames.append(frame)
                else:
                    final = upstream_response
                    final_frames = [_event_bytes(event)]
                checkpoint = _Checkpoint(history + copy.deepcopy(final["output"]), _deepseek_control_signature(payload),
                                         frozenset(loaded_tools))
                final_frames = [_restore_tool_frame(frame, tool_routes) for frame in final_frames]
                with self._checkpoint_lock:
                    self._checkpoints[response_id] = checkpoint
                    while len(self._checkpoints) > 32:
                        self._checkpoints.popitem(last=False)
                    if replay_key:
                        self._replays[replay_key] = observed_frames + final_frames
                        while len(self._replays) > 16:
                            self._replays.popitem(last=False)
                completed = True
                yield from final_frames
                return
            if not completed:
                raise GeminiWebError("Gemini Web stream disconnected before completion")
        finally:
            cancelled.set()
            if connection.sock is not None:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

    def compact(self, payload: dict[str, Any]) -> dict[str, Any]:
        summary_payload = {**payload, "tools": [], "tool_choice": "auto", "stream": False,
                           "instructions": _COMPACT_PROMPT, "previous_response_id": None}
        summary_payload.pop("text", None)
        _, response = self.responses(summary_payload)
        assert isinstance(response, dict)
        return {"object": "response.compaction", "model": payload.get("model"),
                "output": _build_compact_v1_output(_extract_compact_user_messages(payload.get("input")), _answer(response)),
                "usage": response.get("usage")}


class GeminiWebProvider(TextWebProvider):
    """Gemini-Web2API specialization (backwards-compatible public entry point)."""
