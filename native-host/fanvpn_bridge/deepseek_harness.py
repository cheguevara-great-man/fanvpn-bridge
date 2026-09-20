"""OpenAI Responses compatibility layer for an authenticated DeepSeek Web session."""
from __future__ import annotations

import base64
import html
import http.client
import json
import re
import time
import urllib.parse
import uuid
from collections.abc import Callable, Iterator, Mapping
from typing import Any


DEEPSEEK_PREFIX = "deepseek-web/"
_COMPLETION_PATH = "/api/v0/chat/completion"
_CREATE_SESSION_PATH = "/api/v0/chat_session/create"
_POW_PATH = "/api/v0/chat/create_pow_challenge"
_MAX_UPSTREAM_BODY = 8 * 1024 * 1024
_TOOL_CALL_RE = re.compile(
    r"<codex_tool_call>\s*(\{.*?\})\s*</codex_tool_call>",
    re.DOTALL | re.IGNORECASE,
)
_DSML_INVOKE_RE = re.compile(
    r'<(?P<marker>[^<>\s]*DSML[^<>\s]*)\s+invoke\s+name=(?P<quote>["\'])(?P<name>.*?)(?P=quote)\s*>'
    r'(?P<body>.*?)</(?P=marker)\s+invoke\s*>',
    re.DOTALL | re.IGNORECASE,
)


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
        prompt = _responses_to_deepseek_prompt(payload)
        session_id = self._create_session()
        challenge = self._create_pow_challenge()
        try:
            answer = self._pow_solver(challenge)
        except Exception as exc:
            raise DeepSeekHarnessError(
                f"DeepSeek proof of work failed: {exc}",
                code="deepseek_pow_failed",
            ) from exc
        pow_header = _encode_pow_response(challenge, answer)
        reasoning = payload.get("reasoning")
        effort = str(reasoning.get("effort") or "").lower() if isinstance(reasoning, dict) else ""
        reasoner = model.endswith("/reasoner")
        completion = {
            "chat_session_id": session_id,
            "parent_message_id": None,
            "model_type": "expert" if reasoner else "default",
            "prompt": prompt,
            "ref_file_ids": [],
            "thinking_enabled": reasoner or effort in {"low", "medium", "high"},
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
        if not answer_text.strip():
            raise DeepSeekHarnessError(
                "DeepSeek Web completed without an answer. Its web stream format may have changed.",
                code="deepseek_empty_response",
            )
        available_tools = {
            str(item.get("name"))
            for item in payload.get("tools") or []
            if isinstance(item, dict) and item.get("type") == "function" and item.get("name")
        }
        tool_calls = _parse_tool_calls(answer_text, available_tools)
        events = list(_responses_events(model, answer_text if tool_calls is None else "", tool_calls or []))
        if bool(payload.get("stream", False)):
            return True, iter(events)
        return False, _last_completed_response(events)

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


def _responses_to_deepseek_prompt(payload: Mapping[str, Any]) -> str:
    sections: list[str] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        sections.append("SYSTEM / DEVELOPER INSTRUCTIONS:\n" + instructions.strip())

    input_value = payload.get("input")
    items = input_value if isinstance(input_value, list) else [
        {"type": "message", "role": "user", "content": input_value}
    ]
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
    for tool in payload.get("tools") or []:
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
        sections.append(
            "CODEX TOOLS AVAILABLE:\n"
            + json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
            + "\n\nTOOL PROTOCOL:\n"
            "Codex, not you, executes tools. When a tool is required, you MUST output only one or more tool-call blocks and no prose outside them.\n"
            "The opening tag MUST be exactly `<codex_tool_call>` and the closing tag MUST be exactly `</codex_tool_call>`. "
            "Do not add quotes, attributes, spaces, backslashes, or any other characters inside either tag.\n"
            "Inside each block, output one valid JSON object with exactly two outer fields: `name` and `arguments`. "
            "`name` must be one listed tool name. `arguments` must be a JSON object containing all tool parameters. "
            "Never put parameters such as `cmd`, `workdir`, `path`, or `max_output_tokens` at the top level.\n"
            "Correct example:\n"
            '<codex_tool_call>{"name":"exec_command","arguments":{"cmd":"Get-Content a.txt","workdir":"C:\\\\tmp","max_output_tokens":3000}}</codex_tool_call>\n'
            "Wrong examples that you MUST NOT emit:\n"
            '<codex_tool_call\\">{"cmd":"Get-Content a.txt"}</codex_tool_call>\n'
            '<codex_tool_call>{"cmd":"Get-Content a.txt"}</codex_tool_call>\n'
            '<codex_tool_call>{"name":"exec_command","cmd":"Get-Content a.txt"}</codex_tool_call>\n'
            "Do not emit an extra `</codex_tool_call>` after the final block. "
            "Do not use DSML, function-call XML, or any other internal tool syntax. "
            "Use only listed tool names and valid JSON arguments. Never invent a tool result. "
            "After Codex returns TOOL RESULT in a later turn, continue the task normally. "
            "If no tool is required, answer normally and never emit codex_tool_call tags."
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
    if path == "response/fragments" and parsed.get("o") == "APPEND" and isinstance(value, list):
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
    matches = list(_TOOL_CALL_RE.finditer(text))
    if not matches:
        return _parse_dsml_tool_calls(text, available_tools)
    calls: list[dict[str, Any]] = []
    for match in matches:
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
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
