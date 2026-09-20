"""OpenAI Responses compatibility layer for an authenticated DeepSeek Web session."""
from __future__ import annotations

import base64
import html
import http.client
import json
import re
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any


DEEPSEEK_PREFIX = "deepseek-web/"
_COMPLETION_PATH = "/api/v0/chat/completion"
_HISTORY_PATH = "/api/v0/chat/history_messages"
_CREATE_SESSION_PATH = "/api/v0/chat_session/create"
_POW_PATH = "/api/v0/chat/create_pow_challenge"
_MAX_UPSTREAM_BODY = 8 * 1024 * 1024
_TOOL_CALL_RE = re.compile(
    r"<codex_tool_call>\s*(.*?)\s*</codex_tool_call>",
    re.DOTALL | re.IGNORECASE,
)
_DSML_INVOKE_RE = re.compile(
    r'<(?P<marker>[^<>\s]*DSML[^<>\s]*)\s+invoke\s+name=(?P<quote>["\'])(?P<name>.*?)(?P=quote)\s*>'
    r'(?P<body>.*?)</(?P=marker)\s+invoke\s*>',
    re.DOTALL | re.IGNORECASE,
)
_DIRECT_TOOL_TAG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")


class DeepSeekHarnessError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int = 502,
        code: str = "deepseek_harness_error",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass
class _DeepSeekConversationState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    session_id: str | None = None
    parent_message_id: str | int | None = None
    represented_items: tuple[str, ...] = ()
    control_signature: str | None = None
    last_response_id: str | None = None


def is_deepseek_model(value: object) -> bool:
    return isinstance(value, str) and value.startswith(DEEPSEEK_PREFIX)


class DeepSeekHarnessProvider:
    """Translate Codex Responses turns into DeepSeek Web chat turns.

    Codex remains the agent/tool executor. DeepSeek receives a strict textual
    tool protocol and this adapter converts compliant tool requests back into
    Responses ``function_call`` output items.
    """

    def __init__(
        self,
        *,
        bridge_url: str = "http://127.0.0.1:18888",
        pow_solver: Callable[[Mapping[str, object]], int],
        timeout_seconds: float = 600,
    ) -> None:
        parsed = urllib.parse.urlsplit(bridge_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("DeepSeekHarness bridge_url must be a loopback HTTP URL")
        self._host = parsed.hostname
        self._port = parsed.port or 80
        self._pow_solver = pow_solver
        self._timeout = timeout_seconds
        self._conversation_states: dict[str, _DeepSeekConversationState] = {}
        self._conversation_states_lock = threading.Lock()

    def models_response(self) -> dict[str, object]:
        now = int(time.time())
        return {
            "object": "list",
            "data": [
                {
                    "id": "deepseek-web/chat",
                    "object": "model",
                    "created": now,
                    "owned_by": "deepseek-web",
                    "display_name": "DeepSeek Web Chat",
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": ["low", "medium", "high"],
                },
                {
                    "id": "deepseek-web/reasoner",
                    "object": "model",
                    "created": now,
                    "owned_by": "deepseek-web",
                    "display_name": "DeepSeek Web Reasoner",
                    "default_reasoning_level": "high",
                    "supported_reasoning_levels": ["low", "medium", "high"],
                },
            ],
        }

    def responses(self, payload: dict[str, Any]) -> tuple[bool, dict[str, Any] | Iterator[bytes]]:
        model = str(payload.get("model") or "deepseek-web/chat").strip()
        if not is_deepseek_model(model):
            raise DeepSeekHarnessError(
                f"Unsupported DeepSeek Web model: {model}",
                status=400,
                code="deepseek_model_unsupported",
            )
        conversation_key = _deepseek_conversation_key(payload)
        state = self._conversation_state(conversation_key) if conversation_key else _DeepSeekConversationState()
        with state.lock:
            input_items = _prompt_input_items(payload)
            input_fingerprints = tuple(_history_item_fingerprint(item) for item in input_items)
            control_signature = _deepseek_control_signature(payload)
            previous_response_id = payload.get("previous_response_id")

            can_reuse = (
                state.session_id is not None
                and state.parent_message_id is not None
                and state.control_signature == control_signature
            )
            delta_items: list[dict[str, Any]] | None = None
            represented_after_input = input_fingerprints
            if can_reuse and _starts_with(input_fingerprints, state.represented_items):
                delta_items = input_items[len(state.represented_items):]
            elif (
                can_reuse
                and isinstance(previous_response_id, str)
                and previous_response_id == state.last_response_id
            ):
                delta_items = input_items
                represented_after_input = state.represented_items + input_fingerprints

            if not can_reuse or not delta_items:
                state.session_id = self._create_session()
                state.parent_message_id = None
                state.represented_items = ()
                state.control_signature = control_signature
                state.last_response_id = None
                prompt = _responses_to_deepseek_prompt(payload, input_items=input_items, include_control=True)
            else:
                prompt = _responses_to_deepseek_prompt(payload, input_items=delta_items, include_control=False)

            reasoning = payload.get("reasoning")
            effort = str(reasoning.get("effort") or "").lower() if isinstance(reasoning, dict) else ""
            reasoner = model.endswith("/reasoner")
            available_tools = {
                str(item.get("name"))
                for item in payload.get("tools") or []
                if isinstance(item, dict) and item.get("type") == "function" and item.get("name")
            }
            thinking_enabled = reasoner or effort in {"low", "medium", "high"}
            current_prompt = prompt
            parent_message_id = state.parent_message_id
            answer_text = ""
            response_message_id: str | int | None = None
            tool_calls: list[dict[str, Any]] | None = None
            for recovery_attempt in range(3):
                challenge = self._create_pow_challenge()
                try:
                    answer = self._pow_solver(challenge)
                except Exception as exc:
                    raise DeepSeekHarnessError(
                        f"DeepSeek proof of work failed: {exc}",
                        code="deepseek_pow_failed",
                    ) from exc
                pow_header = _encode_pow_response(challenge, answer)
                completion = {
                    "chat_session_id": state.session_id,
                    "parent_message_id": parent_message_id,
                    "model_type": "expert" if reasoner else "default",
                    "prompt": current_prompt,
                    "ref_file_ids": [],
                    "thinking_enabled": thinking_enabled,
                    "search_enabled": False,
                    "action": None,
                    "preempt": False,
                }
                status, _headers, raw = self._request(
                    "POST",
                    _COMPLETION_PATH,
                    json.dumps(completion, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                    {
                        "accept": "text/event-stream",
                        "content-type": "application/json",
                        "x-ds-pow-response": pow_header,
                    },
                )
                self._raise_for_status(status, raw, "completion")
                answer_text, _reasoning_text = _parse_deepseek_stream(raw)
                response_message_id = _deepseek_response_message_id(raw)
                if thinking_enabled and response_message_id is not None:
                    history_turn = self._history_turn(state.session_id, response_message_id)
                    if history_turn is not None and history_turn[1].strip():
                        response_message_id, answer_text, _reasoning_text = history_turn
                if not answer_text.strip():
                    raise DeepSeekHarnessError(
                        "DeepSeek Web completed without an answer. Its web stream format may have changed.",
                        code="deepseek_empty_response",
                    )
                tool_calls = _parse_tool_calls(answer_text, available_tools)
                protocol_error = None if tool_calls is not None else _tool_call_protocol_error(answer_text, available_tools)
                if protocol_error is None:
                    break
                if recovery_attempt == 2:
                    raise DeepSeekHarnessError(
                        f"DeepSeek Web repeatedly emitted an invalid tool call: {protocol_error}",
                        code="deepseek_tool_call_invalid",
                    )
                if response_message_id is None:
                    raise DeepSeekHarnessError(
                        f"DeepSeek Web emitted an invalid tool call and no continuation id: {protocol_error}",
                        code="deepseek_tool_call_invalid",
                    )
                parent_message_id = response_message_id
                current_prompt = _tool_call_recovery_prompt(protocol_error)

            events = list(_responses_events(model, answer_text if tool_calls is None else "", tool_calls or []))
            completed = _last_completed_response(events)

            state.parent_message_id = response_message_id
            state.control_signature = control_signature
            state.last_response_id = str(completed.get("id") or "") or None
            state.represented_items = (
                represented_after_input + _response_history_fingerprints(answer_text, tool_calls)
            )

            if bool(payload.get("stream", False)):
                return True, iter(events)
            return False, completed

    def _conversation_state(self, key: str) -> _DeepSeekConversationState:
        with self._conversation_states_lock:
            state = self._conversation_states.get(key)
            if state is None:
                state = _DeepSeekConversationState()
                self._conversation_states[key] = state
            return state

    def _history_turn(
        self,
        session_id: str | None,
        response_message_id: str | int,
    ) -> tuple[str | int, str, str] | None:
        if not session_id:
            return None
        query = urllib.parse.urlencode({"chat_session_id": session_id})
        try:
            status, _headers, raw = self._request(
                "GET",
                f"{_HISTORY_PATH}?{query}",
                None,
                {"accept": "application/json"},
            )
        except DeepSeekHarnessError:
            return None
        if not 200 <= status < 300:
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        biz = data.get("biz_data") if isinstance(data, dict) and isinstance(data.get("biz_data"), dict) else data
        messages = biz.get("chat_messages") if isinstance(biz, dict) else None
        if not isinstance(messages, list):
            return None

        expected = str(response_message_id)
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_id = message.get("message_id", message.get("id"))
            if message_id is None or str(message_id) != expected:
                continue
            fragments = message.get("fragments")
            if not isinstance(fragments, list):
                return None
            answer: list[str] = []
            reasoning: list[str] = []
            for fragment in fragments:
                if not isinstance(fragment, dict):
                    continue
                content = fragment.get("content")
                if not isinstance(content, str):
                    content = fragment.get("text")
                if not isinstance(content, str) or not content:
                    continue
                fragment_type = str(fragment.get("type") or "").upper()
                if fragment_type == "THINK":
                    reasoning.append(content)
                elif fragment_type == "RESPONSE":
                    answer.append(content)
            return message_id, "".join(answer), "".join(reasoning)
        return None

    def _create_session(self) -> str:
        status, _headers, raw = self._request(
            "POST",
            _CREATE_SESSION_PATH,
            b"{}",
            {"accept": "application/json", "content-type": "application/json"},
        )
        self._raise_for_status(status, raw, "session create")
        value = _json_object(raw, "DeepSeek session create")
        data = value.get("data") if isinstance(value.get("data"), dict) else {}
        biz = data.get("biz_data") if isinstance(data.get("biz_data"), dict) else {}
        session = biz.get("chat_session") if isinstance(biz.get("chat_session"), dict) else {}
        session_id = session.get("id")
        if data.get("biz_code") != 0 or not isinstance(session_id, str) or not session_id:
            raise DeepSeekHarnessError(
                "DeepSeek Web did not return a chat session. Open chat.deepseek.com in Chrome and sign in, then retry.",
                status=401 if _is_auth_biz_error(value) else 502,
                code="deepseek_auth_required" if _is_auth_biz_error(value) else "deepseek_session_failed",
            )
        return session_id

    def _create_pow_challenge(self) -> dict[str, object]:
        body = json.dumps({"target_path": _COMPLETION_PATH}, separators=(",", ":")).encode("utf-8")
        status, _headers, raw = self._request(
            "POST",
            _POW_PATH,
            body,
            {"accept": "application/json", "content-type": "application/json"},
        )
        self._raise_for_status(status, raw, "PoW challenge")
        value = _json_object(raw, "DeepSeek PoW challenge")
        data = value.get("data") if isinstance(value.get("data"), dict) else {}
        biz = data.get("biz_data") if isinstance(data.get("biz_data"), dict) else {}
        challenge = biz.get("challenge") if isinstance(biz.get("challenge"), dict) else None
        if data.get("biz_code") != 0 or challenge is None:
            raise DeepSeekHarnessError(
                "DeepSeek Web did not return a proof-of-work challenge",
                status=401 if _is_auth_biz_error(value) else 502,
                code="deepseek_auth_required" if _is_auth_biz_error(value) else "deepseek_pow_failed",
            )
        try:
            normalized: dict[str, object] = {
                "algorithm": str(challenge["algorithm"]),
                "challenge": str(challenge["challenge"]),
                "salt": str(challenge["salt"]),
                "difficulty": int(challenge["difficulty"]),
                "signature": str(challenge["signature"]),
                "expireAt": float(challenge.get("expire_at", challenge.get("expireAt"))),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise DeepSeekHarnessError("DeepSeek Web returned an invalid PoW challenge") from exc
        return normalized

    def _request(
        self,
        method: str,
        upstream_path: str,
        body: bytes | None,
        headers: Mapping[str, str],
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        request_headers = {
            "x-app-version": "2.0.0",
            "x-client-platform": "web",
            "x-client-version": "2.0.0",
            **headers,
        }
        try:
            connection.request(method, "/deepseek-web" + upstream_path, body=body, headers=request_headers)
            response = connection.getresponse()
            raw = response.read(_MAX_UPSTREAM_BODY + 1)
            if len(raw) > _MAX_UPSTREAM_BODY:
                raise DeepSeekHarnessError("DeepSeek Web response exceeded the bridge safety limit")
            return response.status, {name.lower(): value for name, value in response.getheaders()}, raw
        except (OSError, http.client.HTTPException) as exc:
            raise DeepSeekHarnessError(f"DeepSeek Web bridge request failed: {exc}") from exc
        finally:
            connection.close()

    @staticmethod
    def _raise_for_status(status: int, body: bytes, operation: str) -> None:
        if 200 <= status < 300:
            return
        if status in {401, 403}:
            raise DeepSeekHarnessError(
                "DeepSeek Web login is unavailable. Open https://chat.deepseek.com in Chrome, sign in, and retry.",
                status=401,
                code="deepseek_auth_required",
            )
        preview = body.decode("utf-8", errors="replace").replace("\r", " ").replace("\n", " ")[:300]
        raise DeepSeekHarnessError(
            f"DeepSeek Web {operation} failed with HTTP {status}: {preview}",
            status=502,
            code="deepseek_upstream_failed",
        )


def _prompt_input_items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    input_value = payload.get("input")
    if isinstance(input_value, list):
        return [item for item in input_value if isinstance(item, dict)]
    return [{"type": "message", "role": "user", "content": input_value}]


def _canonical_json(value: object) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _history_item_fingerprint(item: Mapping[str, Any]) -> str:
    item_type = str(item.get("type") or ("message" if item.get("role") else ""))
    if item_type == "message":
        return _canonical_json({
            "type": "message",
            "role": str(item.get("role") or "user"),
            "content": _content_text(item.get("content")),
        })
    if item_type == "function_call":
        return _canonical_json({
            "type": "function_call",
            "name": str(item.get("name") or "tool"),
            "arguments": _canonical_json(item.get("arguments")),
        })
    if item_type == "function_call_output":
        return _canonical_json({
            "type": "function_call_output",
            "call_id": str(item.get("call_id") or ""),
            "output": _function_output_text(item.get("output")),
        })
    return _canonical_json(dict(item))


def _response_history_fingerprints(
    answer_text: str,
    tool_calls: list[dict[str, Any]] | None,
) -> tuple[str, ...]:
    if tool_calls:
        return tuple(
            _history_item_fingerprint({
                "type": "function_call",
                "name": call.get("name"),
                "arguments": call.get("arguments"),
            })
            for call in tool_calls
        )
    if answer_text:
        return (_history_item_fingerprint({
            "type": "message",
            "role": "assistant",
            "content": answer_text,
        }),)
    return ()


def _deepseek_control_signature(payload: Mapping[str, Any]) -> str:
    tools = [
        tool
        for tool in payload.get("tools") or []
        if isinstance(tool, dict) and tool.get("type") == "function"
    ]
    return _canonical_json({
        "model": payload.get("model"),
        "instructions": payload.get("instructions"),
        "tools": tools,
    })


def _starts_with(values: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return len(values) >= len(prefix) and values[:len(prefix)] == prefix


def _deepseek_conversation_key(payload: Mapping[str, Any]) -> str | None:
    metadata = payload.get("client_metadata")
    if isinstance(metadata, Mapping):
        raw = metadata.get("x-codex-turn-metadata")
        turn_metadata: object = raw
        if isinstance(raw, str):
            try:
                turn_metadata = json.loads(raw)
            except json.JSONDecodeError:
                turn_metadata = None
        if isinstance(turn_metadata, Mapping):
            thread_id = turn_metadata.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                return "thread:" + thread_id.strip()
    prompt_cache_key = payload.get("prompt_cache_key")
    if isinstance(prompt_cache_key, str) and prompt_cache_key.strip():
        return "cache:" + prompt_cache_key.strip()
    return None


def _deepseek_response_message_id(raw: bytes) -> str | int | None:
    def from_event(value: object) -> str | int | None:
        if isinstance(value, list):
            for item in reversed(value):
                found = from_event(item)
                if found is not None:
                    return found
            return None
        if not isinstance(value, dict):
            return None

        path = value.get("p")
        event_value = value.get("v")
        if path in {"response/message_id", "response/response_message_id", "response/id"}:
            if isinstance(event_value, (str, int)):
                return event_value

        direct = value.get("response_message_id")
        if isinstance(direct, (str, int)):
            return direct

        response = value.get("response")
        if isinstance(response, dict):
            for key in ("message_id", "response_message_id", "id"):
                candidate = response.get(key)
                if isinstance(candidate, (str, int)):
                    return candidate

        if value.get("o") == "BATCH" and isinstance(event_value, list):
            return from_event(event_value)
        if isinstance(event_value, dict):
            return from_event(event_value)
        return None

    text = raw.decode("utf-8", errors="replace")
    for block in reversed(re.split(r"\r?\n\r?\n", text)):
        data_lines = [line[5:].strip() for line in block.splitlines() if line.startswith("data:")]
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        if data == "[DONE]":
            continue
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            continue
        found = from_event(parsed)
        if found is not None:
            return found
    return None


def _responses_to_deepseek_prompt(
    payload: Mapping[str, Any],
    *,
    input_items: list[dict[str, Any]] | None = None,
    include_control: bool = True,
) -> str:
    sections: list[str] = []
    instructions = payload.get("instructions")
    if include_control and isinstance(instructions, str) and instructions.strip():
        sections.append("SYSTEM / DEVELOPER INSTRUCTIONS:\n" + instructions.strip())

    items = input_items if input_items is not None else _prompt_input_items(payload)
    conversation: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or ("message" if item.get("role") else ""))
        if item_type == "message":
            role = str(item.get("role") or "user").upper()
            text = _content_text(item.get("content"))
            if text:
                conversation.append(f"{role}:\n{text}")
        elif item_type == "function_call":
            name = str(item.get("name") or "tool")
            call_id = str(item.get("call_id") or item.get("id") or "")
            arguments = item.get("arguments")
            conversation.append(
                "ASSISTANT TOOL CALL:\n"
                + json.dumps(
                    {"call_id": call_id, "name": name, "arguments": arguments},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        elif item_type == "function_call_output":
            call_id = str(item.get("call_id") or "")
            conversation.append(
                f"TOOL RESULT ({call_id}):\n{_function_output_text(item.get('output'))}"
            )
    if conversation:
        sections.append("CONVERSATION:\n" + "\n\n".join(conversation))

    tools: list[dict[str, object]] = []
    for tool in (payload.get("tools") or []) if include_control else []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        tools.append(
            {
                "name": str(tool.get("name") or "tool"),
                "description": str(tool.get("description") or ""),
                "parameters": tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {},
            }
        )
    if tools:
        tag_map = _tool_tag_map(str(tool["name"]) for tool in tools)
        tool_sections = [
            _render_tool_prompt(tool, tag_map[str(tool["name"])])
            for tool in tools
        ]
        sections.append(
            "CODEX TOOL PROTOCOL:\n"
            "Codex, not you, executes tools. Each available tool has its own direct XML tag and its own JSON argument schema below.\n"
            "When a tool is required, output only one or more direct tool blocks and no prose outside them. "
            "The XML tag name itself selects the tool; the tag body MUST be one valid JSON object containing only that tool's arguments.\n"
            "Use the exact tag shown for that tool. Do not add attributes to tool tags. Do not wrap arguments in `name`, `arguments`, or `tool`.\n"
            "Never use `<codex_tool_call>`, `<invoke>`, `<tool_call>`, DSML, function-call XML, Markdown code fences, or any other tool-call syntax.\n"
            "For Windows paths inside JSON, use forward slashes when practical or correctly escaped backslashes. "
            "Tool-call XML belongs in the final RESPONSE, never in private reasoning/THINK content.\n"
            "Never invent a tool result. After Codex returns TOOL RESULT in a later turn, continue the task normally. "
            "If no tool is required, answer normally and emit no tool tags.\n\n"
            + "\n\n".join(tool_sections)
        )
    return "\n\n".join(sections).strip()


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
            elif part.get("type") in {"input_image", "image"}:
                parts.append("[Image input omitted by the current DeepSeek Web text adapter]")
    return "\n".join(parts)


def _function_output_text(output: object) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, (dict, list)):
        return json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    return "" if output is None else str(output)


def _tool_tag_map(tool_names: Iterator[str] | list[str] | set[str] | tuple[str, ...]) -> dict[str, str]:
    """Build deterministic XML-safe direct tags for Responses function tools."""
    names = sorted(set(tool_names))
    used: set[str] = set()
    result: dict[str, str] = {}
    for name in names:
        if _DIRECT_TOOL_TAG_RE.fullmatch(name):
            candidate = name
        else:
            candidate = re.sub(r"[^A-Za-z0-9_.:-]", "_", name)
            if not candidate or not re.match(r"^[A-Za-z_]", candidate):
                candidate = "tool_" + candidate
        base = candidate
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        result[name] = candidate
    return result


def _schema_example(schema: object, property_name: str = "value") -> object:
    if not isinstance(schema, dict):
        return None
    for key in ("example", "default", "const"):
        if key in schema:
            return schema[key]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((item for item in schema_type if item != "null"), schema_type[0] if schema_type else None)
    if schema_type == "object" or isinstance(schema.get("properties"), dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        return {name: _schema_example(value, name) for name, value in properties.items()}
    if schema_type == "array":
        return [_schema_example(schema.get("items"), property_name)]
    if schema_type == "boolean":
        return False
    if schema_type == "integer":
        return 1
    if schema_type == "number":
        return 1.0
    if schema_type == "null":
        return None
    lowered = property_name.lower()
    if lowered in {"cmd", "command"}:
        return "Get-Content a.txt"
    if lowered in {"workdir", "cwd", "directory"}:
        return "C:/path/to/workspace"
    if "path" in lowered:
        return "path/to/file"
    return "value"


def _render_tool_prompt(tool: Mapping[str, object], tag_name: str) -> str:
    name = str(tool.get("name") or "tool")
    description = str(tool.get("description") or "").strip() or "No description provided."
    parameters = tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {"type": "object"}
    example = _schema_example(parameters)
    if not isinstance(example, dict):
        example = {}
    lines = [
        f"### Tool {name}",
        f"Description: {description}",
    ]
    if tag_name != name:
        lines.append(f"Direct tag name: `{tag_name}` (maps to Responses tool `{name}`).")
    lines.extend(
        [
            f"Valid call format for {name}:",
            f"<{tag_name}>",
            json.dumps(example, ensure_ascii=False, separators=(",", ":")),
            f"</{tag_name}>",
            "The tag body must be valid JSON matching this Parameters JSON Schema:",
            json.dumps(parameters, ensure_ascii=False, separators=(",", ":")),
        ]
    )
    return "\n".join(lines)


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeepSeekHarnessError(f"{label} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise DeepSeekHarnessError(f"{label} returned a non-object JSON value")
    return value


def _is_auth_biz_error(value: Mapping[str, Any]) -> bool:
    data = value.get("data") if isinstance(value.get("data"), dict) else {}
    return data.get("biz_code") in {40002, 40003} or value.get("code") in {40002, 40003}


def _encode_pow_response(challenge: Mapping[str, object], answer: int) -> str:
    payload = {
        "algorithm": challenge["algorithm"],
        "challenge": challenge["challenge"],
        "salt": challenge["salt"],
        "answer": answer,
        "signature": challenge["signature"],
        "target_path": _COMPLETION_PATH,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _parse_deepseek_stream(raw: bytes) -> tuple[str, str]:
    text = raw.decode("utf-8", errors="replace")
    state: dict[str, Any] = {"types": [], "current": -1, "observed": False}
    answer: list[str] = []
    reasoning: list[str] = []
    for block in re.split(r"\r?\n\r?\n", text):
        data_lines = [line[5:].strip() for line in block.splitlines() if line.startswith("data:")]
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        if data == "[DONE]":
            continue
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            continue
        delta_text, delta_reasoning = _split_deepseek_text(parsed, state)
        if delta_text:
            answer.append(delta_text)
        if delta_reasoning:
            reasoning.append(delta_reasoning)
    return "".join(answer), "".join(reasoning)


def _split_deepseek_text(parsed: object, state: dict[str, Any]) -> tuple[str, str]:
    if not isinstance(parsed, dict):
        return "", ""
    if parsed.get("o") == "BATCH" and isinstance(parsed.get("v"), list):
        texts: list[str] = []
        reasons: list[str] = []
        for item in parsed["v"]:
            text, reason = _split_deepseek_text(item, state)
            texts.append(text)
            reasons.append(reason)
        return "".join(texts), "".join(reasons)

    path = parsed.get("p")
    value = parsed.get("v")
    if isinstance(path, str) and path.endswith("/fragments") and parsed.get("o") == "APPEND" and isinstance(value, list):
        types = [str(item.get("type") or "RESPONSE") if isinstance(item, dict) else "RESPONSE" for item in value]
        state["types"].extend(types)
        state["current"] = len(state["types"]) - 1
        state["observed"] = True
        return _fragment_initial_text(value, types)

    if path is None and isinstance(value, dict):
        response = value.get("response") if isinstance(value.get("response"), dict) else None
        fragments = response.get("fragments") if response and isinstance(response.get("fragments"), list) else None
        if fragments:
            first = not state["observed"]
            types = [str(item.get("type") or "RESPONSE") if isinstance(item, dict) else "RESPONSE" for item in fragments]
            state["types"] = types
            state["current"] = len(types) - 1
            state["observed"] = True
            return _fragment_initial_text(fragments, types) if first else ("", "")

    if isinstance(path, str) and path.split("/")[-1] in {"reasoning_content", "thinking_content"} and isinstance(value, str):
        return "", value
    if isinstance(path, str) and path.startswith("response/") and path.split("/")[-1] in {"content", "text", "markdown", "delta"} and isinstance(value, str):
        match = re.match(r"^response/fragments/(-?\d+)/", path)
        index = int(match.group(1)) if match else -1
        return _route_fragment_text(value, _fragment_type(state, index))
    if path is None and isinstance(value, str):
        return _route_fragment_text(value, _fragment_type(state, state["current"]))
    return "", ""


def _fragment_initial_text(fragments: list[object], types: list[str]) -> tuple[str, str]:
    texts: list[str] = []
    reasons: list[str] = []
    for index, fragment in enumerate(fragments):
        if not isinstance(fragment, dict):
            continue
        value = fragment.get("content") if isinstance(fragment.get("content"), str) else fragment.get("text")
        if not isinstance(value, str) or not value:
            continue
        text, reason = _route_fragment_text(value, types[index] if index < len(types) else "RESPONSE")
        texts.append(text)
        reasons.append(reason)
    return "".join(texts), "".join(reasons)


def _fragment_type(state: Mapping[str, Any], index: int) -> str:
    types = state.get("types") if isinstance(state.get("types"), list) else []
    if index == -1:
        index = int(state.get("current", -1))
    return str(types[index]) if 0 <= index < len(types) else "RESPONSE"


def _route_fragment_text(value: str, fragment_type: str) -> tuple[str, str]:
    return ("", value) if fragment_type.upper() == "THINK" else (value, "")


def _parse_tool_calls(text: str, available_tools: set[str]) -> list[dict[str, Any]] | None:
    direct_calls = _parse_direct_tool_calls(text, available_tools)
    if direct_calls is not None:
        return direct_calls
    matches = list(_TOOL_CALL_RE.finditer(text))
    if not matches:
        return _parse_dsml_tool_calls(text, available_tools)
    calls: list[dict[str, Any]] = []
    for match in matches:
        value = _load_tool_call_json(match.group(1))
        if value is None:
            return None
        if not isinstance(value, dict):
            return None
        name = value.get("name")
        arguments = value.get("arguments")
        # DeepSeek occasionally emits exec_command's argument object directly,
        # omitting the required {"name": ..., "arguments": ...} envelope.  This
        # shape is unambiguous when exec_command is available and `cmd` is a
        # string, so normalize it instead of leaking the raw protocol tag back
        # to Codex as assistant text.
        if (
            name is None
            and arguments is None
            and "exec_command" in available_tools
            and isinstance(value.get("cmd"), str)
        ):
            name = "exec_command"
            arguments = value
        if not isinstance(name, str) or name not in available_tools or not isinstance(arguments, dict):
            return None
        calls.append({"name": name, "arguments": arguments})
    return calls or None


def _tool_call_protocol_error(text: str, available_tools: set[str]) -> str | None:
    """Describe malformed tool syntax so DeepSeek can correct it on a continuation turn."""
    if not available_tools:
        return None
    tag_map = _tool_tag_map(available_tools)
    for tool_name, tag_name in tag_map.items():
        opening = f"<{tag_name}>"
        closing = f"</{tag_name}>"
        search_from = 0
        while True:
            open_at = text.find(opening, search_from)
            if open_at < 0:
                break
            start = open_at + len(opening)
            end = text.find(closing, start)
            if end < 0:
                return f"tool {tool_name!r} is missing its exact closing tag {closing}"
            raw = text[start:end].strip()
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError as exc:
                return (
                    f"tool {tool_name!r} has invalid JSON arguments at character {exc.pos}: {exc.msg}. "
                    "Quotes inside JSON string values must be escaped as \\.\"."
                )
            if not isinstance(arguments, dict):
                return f"tool {tool_name!r} arguments must be one JSON object"
            search_from = end + len(closing)
    if "<codex_tool_call>" in text.lower():
        return "the legacy <codex_tool_call> wrapper is not allowed; use the tool's direct XML tag"
    return None


def _tool_call_recovery_prompt(error: str) -> str:
    return (
        "TOOL CALL FORMAT ERROR:\n"
        f"Your previous RESPONSE could not be executed: {error}\n"
        "Re-emit the intended tool call(s) now. Output ONLY the corrected direct tool XML block(s), with no explanation. "
        "Use the exact per-tool tag names and put one valid JSON object in each tag body. "
        "Escape every double quote that occurs inside a JSON string value as \\.\". "
        "Do not use <codex_tool_call>, <invoke>, <tool_call>, DSML, Markdown fences, or any wrapper object."
    )


def _parse_direct_tool_calls(text: str, available_tools: set[str]) -> list[dict[str, Any]] | None:
    if not available_tools:
        return None
    tag_map = _tool_tag_map(available_tools)
    tool_by_tag = {tag: name for name, tag in tag_map.items()}
    tag_alternation = "|".join(re.escape(tag) for tag in sorted(tool_by_tag, key=len, reverse=True))
    pattern = re.compile(
        rf"<(?P<tag>{tag_alternation})>\s*(?P<body>.*?)\s*</(?P=tag)>",
        re.DOTALL,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    calls: list[dict[str, Any]] = []
    for match in matches:
        try:
            arguments = json.loads(match.group("body"))
        except json.JSONDecodeError:
            return None
        if not isinstance(arguments, dict):
            return None
        calls.append({"name": tool_by_tag[match.group("tag")], "arguments": arguments})
    return calls or None


def _load_tool_call_json(raw: str) -> object | None:
    """Parse a tool block, repairing only missing trailing JSON closers."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        # DeepSeek sometimes emits the inner `arguments` closing brace but
        # omits the final brace for the outer tool-call object.  Repair only
        # end-of-input truncation; malformed JSON in the middle stays invalid.
        if error.pos < max(0, len(raw) - 1):
            return None

    stack: list[str] = []
    in_string = False
    escaped = False
    for char in raw:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            stack.append("}")
        elif char == "[":
            stack.append("]")
        elif char in "}]":
            if not stack or stack[-1] != char:
                return None
            stack.pop()

    if in_string or escaped or not stack or len(stack) > 2:
        return None
    repaired = raw + "".join(reversed(stack))
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


def _parse_dsml_tool_calls(text: str, available_tools: set[str]) -> list[dict[str, Any]] | None:
    """Normalize DeepSeek's occasional internal DSML tool syntax into Codex calls."""
    matches = list(_DSML_INVOKE_RE.finditer(text))
    if not matches:
        return None
    calls: list[dict[str, Any]] = []
    for match in matches:
        name = html.unescape(match.group("name"))
        if name not in available_tools:
            return None
        marker = re.escape(match.group("marker"))
        parameter_re = re.compile(
            rf'<{marker}\s+parameter\s+name=(?P<quote>["\'])(?P<name>.*?)(?P=quote)'
            rf'(?:\s+string=(?P<string_quote>["\'])(?P<string>true|false)(?P=string_quote))?\s*>'
            rf'(?P<value>.*?)</{marker}\s+parameter\s*>',
            re.DOTALL | re.IGNORECASE,
        )
        arguments: dict[str, Any] = {}
        body = match.group("body")
        parameter_matches = list(parameter_re.finditer(body))
        if not parameter_matches:
            return None
        for parameter in parameter_matches:
            key = html.unescape(parameter.group("name"))
            raw_value = html.unescape(parameter.group("value"))
            if parameter.group("string") is None or parameter.group("string").lower() == "true":
                value: Any = raw_value
            else:
                try:
                    value = json.loads(raw_value)
                except json.JSONDecodeError:
                    return None
            arguments[key] = value
        calls.append({"name": name, "arguments": arguments})
    return calls or None


def _responses_events(model: str, text: str, tool_calls: list[dict[str, Any]]) -> Iterator[bytes]:
    response_id = "resp_" + uuid.uuid4().hex
    created_at = int(time.time())
    sequence = 0
    output: list[dict[str, Any]] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def emit(event_type: str, **values: Any) -> bytes:
        nonlocal sequence
        event = {"type": event_type, "sequence_number": sequence, **values}
        sequence += 1
        data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        return f"event: {event_type}\ndata: {data}\n\n".encode("utf-8")

    yield emit("response.created", response=_response_object(response_id, model, created_at, "in_progress", [], usage))
    if text:
        message_id = "msg_" + uuid.uuid4().hex
        index = len(output)
        item = {"id": message_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}
        yield emit("response.output_item.added", output_index=index, item=item)
        part = {"type": "output_text", "text": "", "annotations": []}
        yield emit("response.content_part.added", item_id=message_id, output_index=index, content_index=0, part=part)
        yield emit("response.output_text.delta", item_id=message_id, output_index=index, content_index=0, delta=text, logprobs=[])
        yield emit("response.output_text.done", item_id=message_id, output_index=index, content_index=0, text=text, logprobs=[])
        part = {"type": "output_text", "text": text, "annotations": []}
        yield emit("response.content_part.done", item_id=message_id, output_index=index, content_index=0, part=part)
        message = {"id": message_id, "type": "message", "status": "completed", "role": "assistant", "content": [part]}
        yield emit("response.output_item.done", output_index=index, item=message)
        output.append(message)
    for call in tool_calls:
        index = len(output)
        arguments = json.dumps(call["arguments"], ensure_ascii=False, separators=(",", ":"))
        item_id = "fc_" + uuid.uuid4().hex
        call_id = "call_" + uuid.uuid4().hex
        item = {
            "id": item_id,
            "type": "function_call",
            "status": "in_progress",
            "call_id": call_id,
            "name": call["name"],
            "arguments": "",
        }
        yield emit("response.output_item.added", output_index=index, item=item)
        yield emit("response.function_call_arguments.delta", item_id=item_id, output_index=index, delta=arguments)
        completed = {**item, "status": "completed", "arguments": arguments}
        yield emit("response.function_call_arguments.done", item_id=item_id, output_index=index, arguments=arguments)
        yield emit("response.output_item.done", output_index=index, item=completed)
        output.append(completed)
    response = _response_object(response_id, model, created_at, "completed", output, usage)
    yield emit("response.completed", response=response)
    yield b"data: [DONE]\n\n"


def _response_object(
    response_id: str,
    model: str,
    created_at: int,
    status: str,
    output: list[dict[str, Any]],
    usage: dict[str, int],
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "error": None,
        "incomplete_details": None,
        "usage": usage,
    }


def _last_completed_response(events: list[bytes]) -> dict[str, Any]:
    for raw in reversed(events):
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line.startswith("data: {"):
                continue
            value = json.loads(line[6:])
            if value.get("type") == "response.completed" and isinstance(value.get("response"), dict):
                return value["response"]
    raise DeepSeekHarnessError("DeepSeek Web adapter produced no completed response")
