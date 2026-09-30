"""OpenAI Responses compatibility layer for anonymous OpenCode Zen models.

Codex only speaks the Responses protocol, while the Zen endpoint exposes the
Chat Completions API. This provider translates in both directions: it lowers a
Responses request into a chat/completions body and raises the upstream stream
back into Responses SSE events, including native tool calls.

Unlike the DeepSeek and Gemini providers, no browser egress is involved. The
upstream is a public HTTPS endpoint, so a direct client keeps the whole path
free of Chrome, Native Messaging and PoW state.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator, Mapping
from typing import Any

from .zen_models import (
    ZEN_API_BASE,
    ZEN_SLUG_PREFIX,
    ZenModelCatalog,
    ZenModelError,
)


_CHAT_COMPLETIONS_PATH = "/chat/completions"
_MAX_UPSTREAM_BODY = 32 * 1024 * 1024
#: Cloudflare rejects the stock ``python-urllib`` agent with error 1010, so
#: every request identifies the Bridge explicitly.
_USER_AGENT = "FanVPNBridge/1.0 (+opencode-zen-responses)"
_EFFORT_PARAM_CANDIDATES = ("reasoning_effort",)
_COMPACT_RETAINED_USER_CHAR_BUDGET = 20_000 * 4
_COMPACT_PROMPT = """You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work."""


class ZenProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int = 502,
        code: str = "zen_provider_error",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def is_zen_model(value: object) -> bool:
    return isinstance(value, str) and value.startswith(ZEN_SLUG_PREFIX)


def _resolve_zen_model(value: object, catalog: ZenModelCatalog) -> str:
    model_id = catalog.resolve_model_id(value) if isinstance(value, str) else None
    if model_id is None:
        raise ZenProviderError(
            f"Unsupported Zen model: {value}",
            status=400,
            code="zen_model_unsupported",
        )
    return model_id


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _output_text(item: Mapping[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, Mapping):
                continue
            # Codex sends request content as ``input_text`` and assistant content
            # as ``output_text``; both lower to plain chat text.
            if part.get("type") in {"input_text", "output_text", "text", "summary_text"}:
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _tool_result_text(item: Mapping[str, Any]) -> str:
    output = item.get("output")
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts: list[str] = []
        for part in output:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if output is None:
        return ""
    return json.dumps(output, ensure_ascii=False)


def _prompt_input_items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    input_value = payload.get("input")
    if isinstance(input_value, list):
        return [item for item in input_value if isinstance(item, dict)]
    if input_value is None:
        return []
    return [{"type": "message", "role": "user", "content": input_value}]


def _chat_tool_definitions(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for item in payload.get("tools") or []:
        if not isinstance(item, Mapping):
            continue
        tool_type = str(item.get("type") or "")
        if tool_type == "function":
            name = item.get("name")
            if not isinstance(name, str) or not name:
                continue
            parameters = item.get("parameters")
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(item.get("description") or ""),
                    "parameters": parameters if isinstance(parameters, Mapping) else {"type": "object", "properties": {}},
                },
            })
        elif tool_type == "custom":
            name = item.get("name")
            if not isinstance(name, str) or not name:
                continue
            # The Chat Completions API has no freeform tool variant, so a custom
            # tool is presented as a single-string parameter.
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(item.get("description") or ""),
                    "parameters": {
                        "type": "object",
                        "properties": {"input": {"type": "string"}},
                        "required": ["input"],
                    },
                },
            })
    return tools


def _custom_tool_names(payload: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for item in payload.get("tools") or []:
        if isinstance(item, Mapping) and str(item.get("type") or "") == "custom":
            name = item.get("name")
            if isinstance(name, str) and name:
                names.add(name)
    return names


def _reasoning_effort(payload: Mapping[str, Any]) -> str | None:
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, Mapping):
        return None
    effort = reasoning.get("effort")
    if isinstance(effort, str) and effort.strip():
        return effort.strip().lower()
    return None


def _to_chat_request(
    payload: Mapping[str, Any],
    model_id: str,
    *,
    stream: bool,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    custom_tools = _custom_tool_names(payload)
    pending_calls: dict[str, dict[str, Any]] = {}

    for item in _prompt_input_items(payload):
        item_type = str(item.get("type") or "")
        if item_type == "message" or "role" in item:
            role = str(item.get("role") or "user")
            if role == "developer":
                role = "system"
            if role not in {"system", "user", "assistant"}:
                role = "user"
            text = _output_text(item)
            if text:
                messages.append({"role": role, "content": text})
            continue
        if item_type in {"function_call", "custom_tool_call"}:
            name = str(item.get("name") or "")
            if not name:
                continue
            call_id = str(item.get("call_id") or item.get("id") or name)
            if item_type == "custom_tool_call":
                arguments: Any = {"input": str(item.get("input") or "")}
            else:
                raw_arguments = item.get("arguments")
                arguments = _decode_arguments(raw_arguments)
            pending_calls[call_id] = {"name": name, "arguments": arguments}
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                    },
                }],
            })
            continue
        if item_type == "function_call_output" or item_type == "custom_tool_call_output":
            call_id = str(item.get("call_id") or item.get("id") or "")
            text = _tool_result_text(item)
            if call_id in pending_calls and pending_calls[call_id]["name"] in custom_tools:
                # A custom tool's result is reported as a plain user message
                # because the wire form was a single-string function.
                messages.append({"role": "user", "content": text})
            else:
                messages.append({"role": "tool", "tool_call_id": call_id, "content": text})
            continue
        if item_type == "reasoning":
            continue
        if item_type in {"local_shell_call", "local_shell_call_output", "web_search_call"}:
            continue

    body: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
    }
    tools = _chat_tool_definitions(payload)
    if tools:
        body["tools"] = tools
    effort = _reasoning_effort(payload)
    if effort is not None:
        for candidate in _EFFORT_PARAM_CANDIDATES:
            body[candidate] = effort
    if stream:
        body["stream"] = True
    max_output_tokens = payload.get("max_output_tokens")
    if isinstance(max_output_tokens, int) and max_output_tokens > 0:
        body["max_tokens"] = max_output_tokens
    return body


def _decode_arguments(value: object) -> Any:
    if isinstance(value, (Mapping, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value) if value.strip() else {}
        except json.JSONDecodeError:
            return {"input": value}
    return {}


class _ZenStreamTranslator:
    """Raise Chat Completions chunks into Responses SSE events.

    Reasoning is buffered rather than forwarded as it arrives. Codex rejects a
    response whose reasoning is interleaved inconsistently with its output, and
    Zen emits ``reasoning_content`` freely alongside the answer, so holding it
    until ``finish_reason`` keeps the emitted event ordering deterministic.
    """

    def __init__(self, model_slug: str) -> None:
        self.model = model_slug
        self.events: list[bytes] = []
        self.response_id = "resp_" + uuid.uuid4().hex
        self.message_id = "msg_" + uuid.uuid4().hex
        self.created_at = int(time.time())
        self.sequence = 0
        self.output: list[dict[str, Any]] = []
        self.usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self.text = ""
        self.reasoning = ""
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self._text_started = False

    def start(self) -> None:
        self._emit(
            "response.created",
            response=_response_object(self.response_id, self.model, self.created_at, "in_progress", [], self.usage),
        )

    def on_chunk(self, chunk: Mapping[str, Any]) -> None:
        choices = chunk.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
            choice = choices[0]
            delta = choice.get("delta")
            if isinstance(delta, Mapping):
                reasoning = delta.get("reasoning_content")
                if isinstance(reasoning, str):
                    self.reasoning += reasoning
                content = delta.get("content")
                if isinstance(content, str) and content:
                    self.text += content
                self._on_tool_call_delta(delta.get("tool_calls"))
        usage = chunk.get("usage")
        if isinstance(usage, Mapping):
            self._apply_usage(usage)

    def _apply_usage(self, usage: Mapping[str, Any]) -> None:
        def count(value: object) -> int:
            return value if isinstance(value, int) and value > 0 else 0

        prompt = count(usage.get("prompt_tokens"))
        completion = count(usage.get("completion_tokens"))
        details = usage.get("completion_tokens_details")
        if isinstance(details, Mapping) and count(details.get("reasoning_tokens")):
            # Reasoning tokens are billed as output; keep the totals honest.
            completion = max(completion, count(details.get("reasoning_tokens")))
        total = count(usage.get("total_tokens")) or (prompt + completion)
        self.usage = {
            "input_tokens": prompt,
            "output_tokens": completion,
            "total_tokens": total,
        }

    def _on_tool_call_delta(self, raw: Any) -> None:
        if not isinstance(raw, list):
            return
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            index = entry.get("index")
            slot = index if isinstance(index, int) else len(self.tool_calls)
            call = self.tool_calls.setdefault(slot, {"id": None, "name": "", "arguments": ""})
            call_id = entry.get("id")
            if isinstance(call_id, str) and call_id:
                call["id"] = call_id
            function = entry.get("function")
            if isinstance(function, Mapping):
                name = function.get("name")
                if isinstance(name, str) and name:
                    call["name"] = name
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    call["arguments"] += arguments

    def _start_text(self) -> None:
        if self._text_started:
            return
        self._text_started = True
        item = {
            "id": self.message_id,
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        }
        self._emit("response.output_item.added", output_index=len(self.output), item=item)
        part = {"type": "output_text", "text": "", "annotations": []}
        self._emit(
            "response.content_part.added",
            item_id=self.message_id,
            output_index=len(self.output),
            content_index=0,
            part=part,
        )

    def finish(self) -> None:
        # Reasoning is intentionally not forwarded. The Bridge marks these rows
        # as not supporting reasoning summaries, so emitting a bare
        # reasoning_summary event would be an orphan Codex cannot attach to an
        # output item. The text is still counted toward output usage.
        if self.text:
            self._start_text()
            self._emit(
                "response.output_text.delta",
                item_id=self.message_id,
                output_index=len(self.output),
                content_index=0,
                delta=self.text,
                logprobs=[],
            )
            self._emit(
                "response.output_text.done",
                item_id=self.message_id,
                output_index=len(self.output),
                content_index=0,
                text=self.text,
                logprobs=[],
            )
            part = {"type": "output_text", "text": self.text, "annotations": []}
            self._emit(
                "response.content_part.done",
                item_id=self.message_id,
                output_index=len(self.output),
                content_index=0,
                part=part,
            )
            message = {
                "id": self.message_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [part],
            }
            self._emit("response.output_item.done", output_index=len(self.output), item=message)
            self.output.append(message)
        for slot in sorted(self.tool_calls):
            call = self.tool_calls[slot]
            name = call["name"]
            if not name:
                continue
            index = len(self.output)
            call_id = call["id"] or ("call_" + uuid.uuid4().hex)
            item_id = "fc_" + uuid.uuid4().hex
            item = {
                "id": item_id,
                "type": "function_call",
                "status": "in_progress",
                "call_id": call_id,
                "name": name,
                "arguments": "",
            }
            self._emit("response.output_item.added", output_index=index, item=item)
            arguments = call["arguments"] or "{}"
            self._emit(
                "response.function_call_arguments.delta",
                item_id=item_id,
                output_index=index,
                delta=arguments,
            )
            self._emit(
                "response.function_call_arguments.done",
                item_id=item_id,
                output_index=index,
                arguments=arguments,
            )
            completed = {**item, "status": "completed", "arguments": arguments}
            self._emit("response.output_item.done", output_index=index, item=completed)
            self.output.append(completed)
        self._emit(
            "response.completed",
            response=_response_object(self.response_id, self.model, self.created_at, "completed", self.output, self.usage),
        )
        self.events.append(b"data: [DONE]\n\n")

    def _emit(self, event_type: str, **values: Any) -> None:
        event = {"type": event_type, "sequence_number": self.sequence, **values}
        self.sequence += 1
        data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        self.events.append(f"event: {event_type}\ndata: {data}\n\n".encode("utf-8"))


def _response_object(
    response_id: str,
    model: str,
    created_at: int,
    status: str,
    output: list[dict[str, Any]],
    usage: Mapping[str, int],
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
        "usage": dict(usage),
    }


def _iter_sse_blocks(raw: bytes) -> Iterator[tuple[bytes, bytes]]:
    """Split an SSE byte stream into (event name, data) pairs."""

    buffer = bytearray(raw)
    while True:
        boundary = raw.find(b"\n\n")
        if boundary == -1:
            break
        del buffer[: boundary + 2]
        block = raw[:boundary]
        raw = raw[boundary + 2:]
        name = b""
        data_lines: list[bytes] = []
        for line in block.split(b"\n"):
            line = line.rstrip(b"\r")
            if line.startswith(b"event:"):
                name = line[6:].strip()
            elif line.startswith(b"data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            yield name, b"\n".join(data_lines)


def _parse_chat_stream(raw: bytes) -> Iterator[dict[str, Any]]:
    for _name, data in _iter_sse_blocks(raw):
        if data == b"[DONE]":
            return
        try:
            chunk = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(chunk, Mapping):
            yield dict(chunk)


class ZenProvider:
    """Translate Codex Responses turns into anonymous Zen chat completions."""

    def __init__(
        self,
        *,
        catalog: ZenModelCatalog | None = None,
        timeout_seconds: float = 600,
    ) -> None:
        self._catalog = catalog or ZenModelCatalog()
        self._timeout = timeout_seconds

    def models_response(self, *, force: bool = False) -> dict[str, object]:
        try:
            # The catalog returns fresh cached entries immediately and re-probes
            # only when they are stale, absent, or explicitly forced.
            entries = self._catalog.refresh(force=force)
        except ZenModelError:
            # Keep serving the last verified set rather than emptying the
            # picker because the catalog host is briefly unreachable.
            entries = self._catalog.entries()
        return {"object": "list", "data": entries}

    def responses(self, payload: dict[str, Any]) -> tuple[bool, dict[str, Any] | Iterator[bytes]]:
        slug = str(payload.get("model") or "").strip()
        model_id = _resolve_zen_model(slug, self._catalog)
        wants_stream = bool(payload.get("stream", False))
        if _is_local_compaction_request(payload):
            return self._compact(payload, model_id, wants_stream)
        if wants_stream:
            return True, self._stream(payload, model_id, slug)
        raw = self._post(_to_chat_request(payload, model_id, stream=False))
        translator = _ZenStreamTranslator(slug)
        translator.start()
        translator.on_chunk(_decode_chat_completion(raw))
        translator.finish()
        return False, _last_completed_response(translator.events)

    def _stream(self, payload: Mapping[str, Any], model_id: str, slug: str) -> Iterator[bytes]:
        raw = self._post(_to_chat_request(payload, model_id, stream=True), accept="text/event-stream")
        translator = _ZenStreamTranslator(slug)
        translator.start()
        for chunk in _parse_chat_stream(raw):
            translator.on_chunk(chunk)
        translator.finish()
        yield from translator.events

    def _post(self, body: Mapping[str, Any], *, accept: str = "application/json") -> bytes:
        request = urllib.request.Request(
            ZEN_API_BASE + _CHAT_COMPLETIONS_PATH,
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={
                "content-type": "application/json",
                "accept": accept,
                "user-agent": _USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read(_MAX_UPSTREAM_BODY + 1)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(2048).decode("utf-8", errors="replace").replace("\n", " ")[:300]
            except (OSError, UnicodeError):
                detail = ""
            raise ZenProviderError(
                f"Zen request failed with HTTP {exc.code}: {detail}" if detail
                else f"Zen request failed with HTTP {exc.code}",
                status=502 if exc.code >= 500 else exc.code,
                code="zen_upstream_failed",
            ) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise ZenProviderError(f"Zen request failed: {exc}") from exc
        if len(raw) > _MAX_UPSTREAM_BODY:
            raise ZenProviderError("Zen response exceeded the bridge safety limit")
        return raw

    def _compact(
        self,
        payload: Mapping[str, Any],
        model_id: str,
        wants_stream: bool,
    ) -> tuple[bool, dict[str, Any] | Iterator[bytes]]:
        """Summarize history locally instead of paying an upstream compaction call."""

        transcript: list[str] = []
        budget = _COMPACT_RETAINED_USER_CHAR_BUDGET
        for item in _prompt_input_items(payload):
            item_type = str(item.get("type") or "")
            if item_type == "message":
                text = _output_text(item)
                if text:
                    transcript.append(f"USER: {text[-budget:]}")
            elif item_type in {"function_call", "custom_tool_call"}:
                transcript.append(f"TOOL CALL: {item.get('name')}({str(item.get('arguments') or item.get('input') or '')[:2000]})")
            elif item_type in {"function_call_output", "custom_tool_call_output"}:
                transcript.append(f"TOOL RESULT: {_tool_result_text(item)[:2000]}")
        body = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": _COMPACT_PROMPT},
                {"role": "user", "content": "\n\n".join(transcript) or "(empty history)"},
            ],
            "max_tokens": 4000,
        }
        document = _decode_chat_completion(self._post(body))
        delta = document["choices"][0]["delta"]
        summary = str(delta.get("content") or "")
        slug = str(payload.get("model") or "").strip()
        events = list(_responses_events(slug, summary, []))
        if wants_stream:
            return True, iter(events)
        return False, _last_completed_response(events)

    def compact(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Answer a ``/responses/compact`` request with a local summary."""

        model_id = _resolve_zen_model(str(payload.get("model") or "").strip(), self._catalog)
        _streaming, result = self._compact(payload, model_id, False)
        if isinstance(result, dict):
            return result
        raise ZenProviderError("Zen compaction produced a stream for a non-streaming request")


def _decode_chat_completion(raw: bytes) -> dict[str, Any]:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ZenProviderError("Zen returned an invalid completion payload") from exc
    if not isinstance(document, Mapping):
        raise ZenProviderError("Zen returned an unexpected completion payload")
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise ZenProviderError("Zen returned a completion without choices")
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        message = {}
    usage = document.get("usage")
    return {
        "choices": [{"delta": message}],
        "usage": usage if isinstance(usage, Mapping) else None,
    }


def _is_local_compaction_request(payload: Mapping[str, Any]) -> bool:
    metadata = payload.get("client_metadata")
    if not isinstance(metadata, Mapping):
        return False
    raw = metadata.get("x-codex-turn-metadata")
    turn_metadata: object = raw
    if isinstance(raw, str):
        try:
            turn_metadata = json.loads(raw)
        except json.JSONDecodeError:
            return False
    return isinstance(turn_metadata, Mapping) and turn_metadata.get("request_kind") == "compaction"


def _responses_events(
    model: str,
    text: str,
    tool_calls: list[dict[str, Any]],
) -> Iterator[bytes]:
    response_id = "resp_" + uuid.uuid4().hex
    message_id = "msg_" + uuid.uuid4().hex
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
        call_id = "call_" + uuid.uuid4().hex
        item_id = "fc_" + uuid.uuid4().hex
        arguments = json.dumps(call.get("arguments") or {}, ensure_ascii=False, separators=(",", ":"))
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
    yield emit("response.completed", response=_response_object(response_id, model, created_at, "completed", output, usage))
    yield b"data: [DONE]\n\n"


def _last_completed_response(events: list[bytes]) -> dict[str, Any]:
    for raw in reversed(events):
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line.startswith("data: {"):
                continue
            value = json.loads(line[6:])
            if value.get("type") == "response.completed" and isinstance(value.get("response"), dict):
                return value["response"]
    raise ZenProviderError("Zen adapter produced no completed response")


__all__ = ["ZenProvider", "ZenProviderError", "is_zen_model"]
