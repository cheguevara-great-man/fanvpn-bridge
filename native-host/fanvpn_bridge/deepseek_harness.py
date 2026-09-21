"""OpenAI Responses compatibility layer for an authenticated DeepSeek Web session."""
from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import json
import os
import queue
import re
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEEPSEEK_PREFIX = "deepseek-web/"
_COMPLETION_PATH = "/api/v0/chat/completion"
_HISTORY_PATH = "/api/v0/chat/history_messages"
_CREATE_SESSION_PATH = "/api/v0/chat_session/create"
_POW_PATH = "/api/v0/chat/create_pow_challenge"
_UPLOAD_FILE_PATH = "/api/v0/file/upload_file"
_FETCH_FILES_PATH = "/api/v0/file/fetch_files"
_MAX_UPSTREAM_BODY = 8 * 1024 * 1024
_MAX_VISION_IMAGES = 4
_MAX_VISION_IMAGE_BYTES = 8 * 1024 * 1024
_FILE_READY_POLL_SECONDS = 0.5
_FILE_READY_TIMEOUT_SECONDS = 15.0
_EMPTY_RESPONSE_RECOVERY_POLL_SECONDS = 0.35
_EMPTY_RESPONSE_RECOVERY_TIMEOUT_SECONDS = 4.0
_DIRECT_TOOL_TAG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")
_DATA_URI_RE = re.compile(
    r"data:([^;,\s]+)(?:;[^,\s]*)?;base64,[A-Za-z0-9+/=_-]+",
    re.IGNORECASE,
)
_ACCEPTED_FILE_AUDIT_RESULTS = {"PASS", "PASSED", "SUCCESS", "OK", "UNKNOWN"}
_REJECTED_FILE_AUDIT_RESULTS = {
    "REJECT", "REJECTED", "FAIL", "FAILED", "ERROR", "BLOCK", "BLOCKED", "DENY", "DENIED",
}
_FAILED_FILE_STATUSES = {"FAIL", "FAILED", "ERROR"}
_DEEPSEEK_CONTEXT_WINDOW = 1_000_000
_DEEPSEEK_AUTO_COMPACT_TOKEN_LIMIT = 900_000
_COMPACT_RETAINED_USER_CHAR_BUDGET = 20_000 * 4
_COMPACT_PROMPT = """You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work."""
_SUMMARY_PREFIX = "Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work. Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:"


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
    needs_validation: bool = False


@dataclass(frozen=True)
class _DeepSeekImage:
    mime_type: str
    data: bytes
    filename: str


class _DeepSeekLiveResponse:
    """Translate ordinary DeepSeek answer deltas into Responses SSE events.

    Direct tool calls deliberately stay buffered. Their first non-whitespace
    character must be ``<`` under our tool protocol, so ordinary prose can be
    streamed without leaking a half-written tool block to Codex.
    """

    def __init__(self, model: str, has_tools: bool, emit: Callable[[bytes], None]) -> None:
        self.model = model
        self.has_tools = has_tools
        self.emit = emit
        self.response_id = "resp_" + uuid.uuid4().hex
        self.message_id = "msg_" + uuid.uuid4().hex
        self.created_at = int(time.time())
        self.sequence = 0
        self.started = False
        self.streamed_this_attempt = False
        self._mode = "undecided"
        self._pending = ""
        self.final_events: list[bytes] = []

    def begin_attempt(self) -> None:
        self.streamed_this_attempt = False
        self._mode = "streaming" if not self.has_tools else "undecided"
        self._pending = ""

    def on_answer_delta(self, delta: str) -> None:
        if not delta:
            return
        if self._mode == "buffered":
            return
        if self._mode == "undecided":
            self._pending += delta
            stripped = self._pending.lstrip()
            if not stripped:
                return
            if stripped.startswith("<"):
                self._mode = "buffered"
                return
            self._mode = "streaming"
            delta = self._pending
            self._pending = ""
        self._start_text()
        self.streamed_this_attempt = True
        self.emit(self._event(
            "response.output_text.delta",
            item_id=self.message_id,
            output_index=0,
            content_index=0,
            delta=delta,
            logprobs=[],
        ))

    def finish_text(self, completed: Mapping[str, Any]) -> None:
        output = completed.get("output") if isinstance(completed.get("output"), list) else []
        message = next(
            (
                item for item in output
                if isinstance(item, dict)
                and item.get("type") == "message"
                and item.get("id") == self.message_id
            ),
            None,
        )
        if not isinstance(message, dict):
            raise DeepSeekHarnessError("DeepSeek live stream lost its final assistant message")
        content = message.get("content") if isinstance(message.get("content"), list) else []
        part = content[0] if content and isinstance(content[0], dict) else {
            "type": "output_text",
            "text": "",
            "annotations": [],
        }
        text = str(part.get("text") or "")
        self.emit(self._event(
            "response.output_text.done",
            item_id=self.message_id,
            output_index=0,
            content_index=0,
            text=text,
            logprobs=[],
        ))
        self.emit(self._event(
            "response.content_part.done",
            item_id=self.message_id,
            output_index=0,
            content_index=0,
            part=part,
        ))
        self.emit(self._event("response.output_item.done", output_index=0, item=message))
        self.emit(self._event("response.completed", response=dict(completed)))
        self.emit(b"data: [DONE]\n\n")

    def _start_text(self) -> None:
        if self.started:
            return
        self.started = True
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self.emit(self._event(
            "response.created",
            response=_response_object(self.response_id, self.model, self.created_at, "in_progress", [], usage),
        ))
        item = {
            "id": self.message_id,
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        }
        self.emit(self._event("response.output_item.added", output_index=0, item=item))
        part = {"type": "output_text", "text": "", "annotations": []}
        self.emit(self._event(
            "response.content_part.added",
            item_id=self.message_id,
            output_index=0,
            content_index=0,
            part=part,
        ))

    def _event(self, event_type: str, **values: Any) -> bytes:
        event = {"type": event_type, "sequence_number": self.sequence, **values}
        self.sequence += 1
        data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        return f"event: {event_type}\ndata: {data}\n\n".encode("utf-8")


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
        state_path: Path | None = None,
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
        self._state_path = state_path
        self._state_file_lock = threading.Lock()
        self._live_response_context = threading.local()
        self._load_conversation_states()

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
                    "context_window": _DEEPSEEK_CONTEXT_WINDOW,
                    "max_context_window": _DEEPSEEK_CONTEXT_WINDOW,
                    "effective_context_window_percent": 90,
                    "auto_compact_token_limit": _DEEPSEEK_AUTO_COMPACT_TOKEN_LIMIT,
                    "input_modalities": ["text", "image"],
                },
                {
                    "id": "deepseek-web/reasoner",
                    "object": "model",
                    "created": now,
                    "owned_by": "deepseek-web",
                    "display_name": "DeepSeek Web Reasoner",
                    "default_reasoning_level": "high",
                    "supported_reasoning_levels": ["low", "medium", "high"],
                    "context_window": _DEEPSEEK_CONTEXT_WINDOW,
                    "max_context_window": _DEEPSEEK_CONTEXT_WINDOW,
                    "effective_context_window_percent": 90,
                    "auto_compact_token_limit": _DEEPSEEK_AUTO_COMPACT_TOKEN_LIMIT,
                    "input_modalities": ["text", "image"],
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
        if _is_local_compaction_request(payload):
            summary = self._compaction_summary(payload)
            self._forget_conversation_state(_deepseek_conversation_key(payload))
            events = list(_responses_events(model, summary, []))
            completed = _last_completed_response(events)
            if bool(payload.get("stream", False)):
                return True, iter(events)
            return False, completed

        if bool(payload.get("stream", False)) and self._current_live_response() is None:
            stream = self._live_responses(payload, model)
            try:
                first = next(stream)
            except StopIteration as exc:
                raise DeepSeekHarnessError("DeepSeek live stream ended without a response") from exc

            def replay_first() -> Iterator[bytes]:
                yield first
                yield from stream

            return True, replay_first()

        conversation_key = _deepseek_conversation_key(payload)
        state = self._conversation_state(conversation_key) if conversation_key else _DeepSeekConversationState()
        with state.lock:
            self._validate_restored_state(state)
            input_items = _prompt_input_items(payload)
            control_signature = _deepseek_control_signature(payload)
            previous_response_id = payload.get("previous_response_id")

            can_reuse = (
                state.session_id is not None
                and state.parent_message_id is not None
                and state.control_signature == control_signature
            )
            delta_items: list[dict[str, Any]] | None = None
            input_fingerprints: tuple[str, ...] | None = None
            represented_after_input: tuple[str, ...]

            # Normal continuation: hash only this request's delta so state can
            # still recover later without rescanning the represented history.
            if (
                can_reuse
                and isinstance(previous_response_id, str)
                and previous_response_id == state.last_response_id
            ):
                delta_items = input_items
                input_fingerprints = tuple(_history_item_fingerprint(item) for item in input_items)
                represented_after_input = state.represented_items + input_fingerprints
            elif can_reuse:
                # Recovery path for Codex replaying canonical/full history
                # without a matching previous_response_id.
                input_fingerprints = tuple(_history_item_fingerprint(item) for item in input_items)
                if _starts_with(input_fingerprints, state.represented_items):
                    delta_items = input_items[len(state.represented_items):]
                    represented_after_input = input_fingerprints

            if not can_reuse or not delta_items:
                state.session_id = self._create_session()
                state.parent_message_id = None
                state.represented_items = ()
                state.control_signature = control_signature
                state.last_response_id = None
                if input_fingerprints is None:
                    input_fingerprints = tuple(_history_item_fingerprint(item) for item in input_items)
                represented_after_input = input_fingerprints
                turn_items = input_items
                include_control = True
            else:
                turn_items = delta_items
                include_control = False

            # Upload only images that belong to the actual DeepSeek turn. In
            # particular, do not re-upload images present only in replayed history.
            ref_file_ids = self._upload_input_images(turn_items)
            prompt = _responses_to_deepseek_prompt(
                payload,
                input_items=turn_items,
                include_control=include_control,
            )

            reasoning = payload.get("reasoning")
            effort = str(reasoning.get("effort") or "").lower() if isinstance(reasoning, dict) else ""
            reasoner = model.endswith("/reasoner")
            tool_definitions = _deepseek_tool_definitions(payload)
            available_tools = {str(item["name"]) for item in tool_definitions}
            freeform_tools = {
                str(item["name"])
                for item in tool_definitions
                if item.get("freeform") is True
            }
            thinking_enabled = reasoner or effort in {"low", "medium", "high"}
            current_prompt = prompt
            parent_message_id = state.parent_message_id
            answer_text = ""
            response_message_id: str | int | None = None
            tool_calls: list[dict[str, Any]] | None = None
            live_response = self._current_live_response()
            for recovery_attempt in range(3):
                turn_ref_file_ids = ref_file_ids if recovery_attempt == 0 else []
                vision_enabled = bool(turn_ref_file_ids)
                turn_thinking_enabled = thinking_enabled and not vision_enabled
                challenge = self._create_pow_challenge(_COMPLETION_PATH)
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
                    "model_type": "vision" if vision_enabled else ("expert" if reasoner else "default"),
                    "prompt": current_prompt,
                    "ref_file_ids": turn_ref_file_ids,
                    "thinking_enabled": turn_thinking_enabled,
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
                if (
                    turn_thinking_enabled
                    and response_message_id is not None
                    and not (live_response and live_response.streamed_this_attempt)
                ):
                    history_turn = self._history_turn(state.session_id, response_message_id)
                    if history_turn is not None and history_turn[1].strip():
                        response_message_id, answer_text, _reasoning_text = history_turn
                if not answer_text.strip() and response_message_id is not None:
                    history_turn = self._wait_for_history_answer(state.session_id, response_message_id)
                    if history_turn is not None and history_turn[1].strip():
                        response_message_id, answer_text, _reasoning_text = history_turn
                if not answer_text.strip():
                    raise DeepSeekHarnessError(
                        "DeepSeek Web completed without an answer. Its web stream format may have changed.",
                        code="deepseek_empty_response",
                    )
                if live_response and live_response.streamed_this_attempt:
                    # Once ordinary prose has been emitted it cannot be retracted.
                    # Valid direct tool calls always begin with '<' and therefore
                    # remain buffered instead of reaching this branch.
                    tool_calls = None
                    invalid_tool_attempt = False
                else:
                    tool_calls = _parse_tool_calls(answer_text, available_tools, freeform_tools)
                    invalid_tool_attempt = tool_calls is None and _looks_like_tool_call_attempt(
                        answer_text,
                        available_tools,
                    )
                if not invalid_tool_attempt:
                    break
                if recovery_attempt == 2:
                    raise DeepSeekHarnessError(
                        "DeepSeek Web repeatedly emitted an invalid direct tool call",
                        code="deepseek_tool_call_invalid",
                    )
                if response_message_id is None:
                    raise DeepSeekHarnessError(
                        "DeepSeek Web emitted an invalid direct tool call and no continuation id",
                        code="deepseek_tool_call_invalid",
                    )
                parent_message_id = response_message_id
                current_prompt = _tool_call_recovery_prompt(tool_definitions)

            response_text = answer_text if tool_calls is None else ""
            events = list(_responses_events(
                model,
                response_text,
                tool_calls or [],
                response_id=live_response.response_id if live_response else None,
                created_at=live_response.created_at if live_response else None,
                message_id=live_response.message_id if live_response and response_text else None,
            ))
            completed = _last_completed_response(events)
            if live_response is not None:
                live_response.final_events = events

            state.parent_message_id = response_message_id
            state.control_signature = control_signature
            state.last_response_id = str(completed.get("id") or "") or None
            state.represented_items = (
                represented_after_input + _response_history_fingerprints(answer_text, tool_calls)
            )
            self._save_state_file()

            if bool(payload.get("stream", False)):
                return True, iter(events)
            return False, completed

    def _live_responses(self, payload: dict[str, Any], model: str) -> Iterator[bytes]:
        pending: queue.Queue[bytes | Exception | object] = queue.Queue()
        finished = object()
        has_tools = any(
            isinstance(item, dict) and item.get("type") == "function" and item.get("name")
            for item in payload.get("tools") or []
        )
        live = _DeepSeekLiveResponse(model, has_tools, pending.put)

        def worker() -> None:
            self._live_response_context.value = live
            try:
                nonstream_payload = copy.deepcopy(payload)
                nonstream_payload["stream"] = False
                streaming, completed = self.responses(nonstream_payload)
                if streaming or not isinstance(completed, dict):
                    raise DeepSeekHarnessError("DeepSeek live stream did not produce a completed response")
                if live.started:
                    live.finish_text(completed)
                else:
                    if not live.final_events:
                        raise DeepSeekHarnessError("DeepSeek live stream produced no final events")
                    for event in live.final_events:
                        pending.put(event)
            except Exception as exc:
                pending.put(exc)
            finally:
                try:
                    del self._live_response_context.value
                except AttributeError:
                    pass
                pending.put(finished)

        threading.Thread(target=worker, name="deepseek-live-response", daemon=True).start()
        while True:
            item = pending.get()
            if item is finished:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    def _current_live_response(self) -> _DeepSeekLiveResponse | None:
        value = getattr(self._live_response_context, "value", None)
        return value if isinstance(value, _DeepSeekLiveResponse) else None

    def compact(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = str(payload.get("model") or "deepseek-web/chat").strip()
        if not is_deepseek_model(model):
            raise DeepSeekHarnessError(
                f"Unsupported DeepSeek Web model: {model}",
                status=400,
                code="deepseek_model_unsupported",
            )
        summary = self._compaction_summary(payload)
        self._forget_conversation_state(_deepseek_conversation_key(payload))
        return {
            "output": _build_compact_v1_output(
                _extract_compact_user_messages(payload.get("input")),
                summary,
            )
        }

    def _compaction_summary(self, payload: Mapping[str, Any]) -> str:
        model = str(payload.get("model") or "deepseek-web/chat").strip()
        reasoner = model.endswith("/reasoner")
        reasoning = payload.get("reasoning")
        effort = str(reasoning.get("effort") or "").lower() if isinstance(reasoning, dict) else ""
        thinking_enabled = reasoner or effort in {"low", "medium", "high"}
        compact_payload = dict(payload)
        compact_payload["tools"] = []
        input_items = [
            item for item in _prompt_input_items(compact_payload)
            if str(item.get("type") or "") != "compaction_trigger"
        ]
        prompt = _responses_to_deepseek_prompt(
            compact_payload,
            input_items=input_items,
            include_control=True,
        )
        prompt = (prompt + "\n\nCOMPACTION TASK:\n" + _COMPACT_PROMPT).strip()
        session_id = self._create_session()
        challenge = self._create_pow_challenge(_COMPLETION_PATH)
        try:
            answer = self._pow_solver(challenge)
        except Exception as exc:
            raise DeepSeekHarnessError(
                f"DeepSeek proof of work failed: {exc}",
                code="deepseek_pow_failed",
            ) from exc
        completion = {
            "chat_session_id": session_id,
            "parent_message_id": None,
            "model_type": "expert" if reasoner else "default",
            "prompt": prompt,
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
                "x-ds-pow-response": _encode_pow_response(challenge, answer),
            },
        )
        self._raise_for_status(status, raw, "compaction")
        summary, _reasoning_text = _parse_deepseek_stream(raw)
        response_message_id = _deepseek_response_message_id(raw)
        if thinking_enabled and response_message_id is not None:
            history_turn = self._history_turn(session_id, response_message_id)
            if history_turn is not None and history_turn[1].strip():
                summary = history_turn[1]
        if not summary.strip() and response_message_id is not None:
            history_turn = self._wait_for_history_answer(session_id, response_message_id)
            if history_turn is not None and history_turn[1].strip():
                summary = history_turn[1]
        if not summary.strip():
            raise DeepSeekHarnessError(
                "DeepSeek Web compaction completed without a summary",
                code="deepseek_compaction_empty",
            )
        return summary.strip()

    def _load_conversation_states(self) -> None:
        if self._state_path is None:
            return
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(value, dict) or value.get("version") != 1:
            return
        raw_states = value.get("states")
        if not isinstance(raw_states, dict):
            return
        for key, raw in raw_states.items():
            restored = _conversation_state_from_json(raw)
            if isinstance(key, str) and restored is not None:
                self._conversation_states[key] = restored

    def _save_state_file(self) -> None:
        if self._state_path is None:
            return
        with self._state_file_lock:
            with self._conversation_states_lock:
                states = {
                    key: _conversation_state_to_json(state)
                    for key, state in self._conversation_states.items()
                    if state.session_id and state.parent_message_id is not None
                }
            value = {"version": 1, "states": states}
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self._state_path.with_name(
                    f"{self._state_path.name}.{os.getpid()}.next"
                )
                temporary.write_text(
                    json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, self._state_path)
            except OSError:
                return

    def _validate_restored_state(self, state: _DeepSeekConversationState) -> None:
        if not state.needs_validation:
            return
        session_id = state.session_id
        parent_message_id = state.parent_message_id
        if not session_id or parent_message_id is None:
            _clear_conversation_state(state)
            return

        query = urllib.parse.urlencode({"chat_session_id": session_id})
        try:
            status, _headers, raw = self._request(
                "GET",
                f"{_HISTORY_PATH}?{query}",
                None,
                {"accept": "application/json"},
            )
        except DeepSeekHarnessError:
            raise
        if status in {401, 403}:
            self._raise_for_status(status, raw, "session validation")
        if not 200 <= status < 300:
            _clear_conversation_state(state)
            self._save_state_file()
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            _clear_conversation_state(state)
            self._save_state_file()
            return
        if not _history_contains_message(payload, parent_message_id):
            _clear_conversation_state(state)
            self._save_state_file()
            return
        state.needs_validation = False
        self._save_state_file()

    def _conversation_state(self, key: str) -> _DeepSeekConversationState:
        with self._conversation_states_lock:
            state = self._conversation_states.get(key)
            if state is None:
                state = _DeepSeekConversationState()
                self._conversation_states[key] = state
            return state

    def _forget_conversation_state(self, key: str | None) -> None:
        if not key:
            return
        with self._conversation_states_lock:
            self._conversation_states.pop(key, None)
        self._save_state_file()

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
        if isinstance(data, dict) and isinstance(data.get("biz_data"), dict):
            biz = data["biz_data"]
        elif isinstance(data, dict) and isinstance(data.get("bizData"), dict):
            biz = data["bizData"]
        else:
            biz = data
        messages = None
        if isinstance(biz, dict):
            messages = biz.get("chat_messages")
            if not isinstance(messages, list):
                messages = biz.get("chatMessages")
        if not isinstance(messages, list):
            return None

        expected = str(response_message_id)
        selected: dict[str, Any] | None = None
        normalized: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_id = message.get("message_id", message.get("id", message.get("uuid")))
            if message_id is None:
                continue
            normalized.append(message)
            if str(message_id) == expected:
                selected = message
        if selected is None and normalized:
            for message in reversed(normalized):
                role = str(message.get("message_role", message.get("role", ""))).lower()
                if role != "user":
                    selected = message
                    break
            if selected is None:
                selected = normalized[-1]
        if selected is None:
            return None

        message_id = selected.get("message_id", selected.get("id", selected.get("uuid")))
        fragments = selected.get("fragments")
        answer: list[str] = []
        reasoning: list[str] = []
        if isinstance(fragments, list):
            for fragment in fragments:
                if not isinstance(fragment, dict):
                    continue
                content = fragment.get("content")
                if not isinstance(content, str):
                    content = fragment.get("text")
                if not isinstance(content, str) or not content:
                    continue
                fragment_type = str(fragment.get("type") or "RESPONSE").upper()
                if fragment_type == "THINK":
                    reasoning.append(content)
                elif fragment_type in {"RESPONSE", "TOOL"}:
                    answer.append(content)
        if not answer:
            for key in ("content", "text", "markdown"):
                value = selected.get(key)
                if isinstance(value, str) and value:
                    answer.append(value)
                    break
        return message_id, "".join(answer), "".join(reasoning)

    def _wait_for_history_answer(
        self,
        session_id: str | None,
        response_message_id: str | int,
    ) -> tuple[str | int, str, str] | None:
        """Recover a response whose SSE ended before the visible answer reached the stream."""
        deadline = time.monotonic() + _EMPTY_RESPONSE_RECOVERY_TIMEOUT_SECONDS
        latest: tuple[str | int, str, str] | None = None
        while True:
            latest = self._history_turn(session_id, response_message_id)
            if latest is not None and latest[1].strip():
                return latest
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return latest
            time.sleep(min(_EMPTY_RESPONSE_RECOVERY_POLL_SECONDS, remaining))

    def _upload_input_images(self, input_items: list[dict[str, Any]]) -> list[str]:
        images = _extract_deepseek_images(input_items)
        return [self._upload_image(image) for image in images]

    def _upload_image(self, image: _DeepSeekImage) -> str:
        challenge = self._create_pow_challenge(_UPLOAD_FILE_PATH)
        try:
            answer = self._pow_solver(challenge)
        except Exception as exc:
            raise DeepSeekHarnessError(
                f"DeepSeek image upload proof of work failed: {exc}",
                code="deepseek_pow_failed",
            ) from exc

        boundary = "----FanVPNBridge" + uuid.uuid4().hex
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{image.filename}"\r\n'
            f"Content-Type: {image.mime_type}\r\n\r\n"
        ).encode("ascii")
        body = prefix + image.data + f"\r\n--{boundary}--\r\n".encode("ascii")
        status, _headers, raw = self._request(
            "POST",
            _UPLOAD_FILE_PATH,
            body,
            {
                "accept": "application/json",
                "content-type": f"multipart/form-data; boundary={boundary}",
                "x-ds-pow-response": _encode_pow_response(
                    challenge,
                    answer,
                    target_path=_UPLOAD_FILE_PATH,
                ),
                "x-thinking-enabled": "0",
                "x-model-type": "vision",
                "x-file-size": str(len(image.data)),
            },
        )
        self._raise_for_status(status, raw, "file upload")
        value = _json_object(raw, "DeepSeek file upload")
        data = value.get("data") if isinstance(value.get("data"), dict) else {}
        uploaded = _uploaded_file_record(value)
        if data.get("biz_code") != 0 or uploaded is None:
            raise DeepSeekHarnessError(
                "DeepSeek Web image upload did not return a usable file",
                status=401 if _is_auth_biz_error(value) else 502,
                code="deepseek_auth_required" if _is_auth_biz_error(value) else "deepseek_image_upload_failed",
            )
        _validate_uploaded_file(uploaded, image.filename)
        file_id = str(uploaded["id"])
        if _uploaded_file_ready(uploaded):
            return file_id

        deadline = time.monotonic() + _FILE_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(_FILE_READY_POLL_SECONDS)
            uploaded = self._fetch_uploaded_file(file_id)
            if uploaded is None:
                continue
            _validate_uploaded_file(uploaded, image.filename)
            if _uploaded_file_ready(uploaded):
                return file_id
        raise DeepSeekHarnessError(
            f"DeepSeek Web image {image.filename} is still processing after "
            f"{int(_FILE_READY_TIMEOUT_SECONDS)}s",
            code="deepseek_image_processing_timeout",
        )

    def _fetch_uploaded_file(self, file_id: str) -> dict[str, Any] | None:
        query = urllib.parse.urlencode({"file_ids": file_id})
        status, _headers, raw = self._request(
            "GET",
            f"{_FETCH_FILES_PATH}?{query}",
            None,
            {"accept": "application/json"},
        )
        if not 200 <= status < 300:
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        data = value.get("data") if isinstance(value.get("data"), dict) else {}
        if data.get("biz_code") != 0:
            return None
        biz = _deepseek_biz_data(value)
        files = biz.get("files") if isinstance(biz, dict) else None
        if not isinstance(files, list):
            return None
        for item in files:
            uploaded = _uploaded_file_record(item)
            if uploaded is not None and str(uploaded.get("id") or "") == file_id:
                return uploaded
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

    def _create_pow_challenge(self, target_path: str = _COMPLETION_PATH) -> dict[str, object]:
        body = json.dumps({"target_path": target_path}, separators=(",", ":")).encode("utf-8")
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
            live_response = self._current_live_response()
            live_completion = (
                live_response is not None
                and upstream_path == _COMPLETION_PATH
                and 200 <= response.status < 300
                and "text/event-stream" in str(headers.get("accept") or "").lower()
            )
            if live_completion:
                live_response.begin_attempt()
                raw = bytearray()
                pending = bytearray()
                stream_state: dict[str, Any] = {"types": [], "current": -1, "observed": False}
                while True:
                    chunk = response.read1(64 * 1024)
                    if not chunk:
                        break
                    raw.extend(chunk)
                    if len(raw) > _MAX_UPSTREAM_BODY:
                        raise DeepSeekHarnessError("DeepSeek Web response exceeded the bridge safety limit")
                    pending.extend(chunk)
                    while True:
                        boundary = re.search(br"\r?\n\r?\n", pending)
                        if boundary is None:
                            break
                        block = bytes(pending[:boundary.start()]).decode("utf-8", errors="replace")
                        del pending[:boundary.end()]
                        delta_text, _delta_reasoning = _parse_deepseek_sse_block(block, stream_state)
                        live_response.on_answer_delta(delta_text)
                if pending:
                    block = bytes(pending).decode("utf-8", errors="replace")
                    delta_text, _delta_reasoning = _parse_deepseek_sse_block(block, stream_state)
                    live_response.on_answer_delta(delta_text)
                return (
                    response.status,
                    {name.lower(): value for name, value in response.getheaders()},
                    bytes(raw),
                )

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


def _extract_deepseek_images(input_items: list[dict[str, Any]]) -> list[_DeepSeekImage]:
    images: list[_DeepSeekImage] = []

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return

        part_type = str(value.get("type") or "").lower()
        if part_type in {"input_image", "image", "output_image"}:
            image_url: object = value.get("image_url")
            if isinstance(image_url, Mapping):
                image_url = image_url.get("url")
            if not isinstance(image_url, str) or not image_url:
                raise DeepSeekHarnessError(
                    "DeepSeek Web vision requires image input as an inline data:image/...;base64 URL",
                    status=400,
                    code="deepseek_image_input_unsupported",
                )
            images.append(_decode_deepseek_image_data_uri(image_url, len(images) + 1))
            if len(images) > _MAX_VISION_IMAGES:
                raise DeepSeekHarnessError(
                    f"DeepSeek Web supports at most {_MAX_VISION_IMAGES} image attachments per turn",
                    status=400,
                    code="deepseek_image_count_exceeded",
                )
            return

        for key in ("content", "output"):
            if key in value:
                visit(value.get(key))

    visit(input_items)
    return images


def _decode_deepseek_image_data_uri(image_url: str, index: int) -> _DeepSeekImage:
    if not image_url.lower().startswith("data:") or "," not in image_url:
        raise DeepSeekHarnessError(
            "DeepSeek Web vision currently requires inline data:image/...;base64 image input",
            status=400,
            code="deepseek_image_url_unsupported",
        )
    header, encoded = image_url.split(",", 1)
    metadata = header[5:].split(";")
    mime_type = metadata[0].strip().lower()
    flags = {item.strip().lower() for item in metadata[1:] if item.strip()}
    if not mime_type.startswith("image/") or "base64" not in flags:
        raise DeepSeekHarnessError(
            "DeepSeek Web vision received an invalid image data URL",
            status=400,
            code="deepseek_image_data_invalid",
        )

    encoded = "".join(encoded.split())
    max_encoded_length = ((_MAX_VISION_IMAGE_BYTES + 2) // 3) * 4
    if len(encoded) > max_encoded_length + 4:
        raise DeepSeekHarnessError(
            f"DeepSeek Web image {index} exceeds the {_MAX_VISION_IMAGE_BYTES // (1024 * 1024)} MiB upload limit",
            status=400,
            code="deepseek_image_too_large",
        )
    try:
        data = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise DeepSeekHarnessError(
            "DeepSeek Web vision received invalid Base64 image data",
            status=400,
            code="deepseek_image_data_invalid",
        ) from exc
    if len(data) > _MAX_VISION_IMAGE_BYTES:
        raise DeepSeekHarnessError(
            f"DeepSeek Web image {index} exceeds the {_MAX_VISION_IMAGE_BYTES // (1024 * 1024)} MiB upload limit",
            status=400,
            code="deepseek_image_too_large",
        )

    extension_map = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/bmp": "bmp",
        "image/avif": "avif",
        "image/svg+xml": "svg",
    }
    extension = extension_map.get(mime_type, "img")
    return _DeepSeekImage(mime_type=mime_type, data=data, filename=f"codex-input-image-{index}.{extension}")


def _deepseek_biz_data(value: Mapping[str, Any]) -> dict[str, Any]:
    data = value.get("data") if isinstance(value.get("data"), Mapping) else None
    for container in (data, value):
        if not isinstance(container, Mapping):
            continue
        for key in ("biz_data", "bizData"):
            candidate = container.get(key)
            if isinstance(candidate, Mapping):
                return dict(candidate)
    return {}


def _uploaded_file_record(value: Mapping[str, Any]) -> dict[str, Any] | None:
    candidate: Mapping[str, Any] = _deepseek_biz_data(value) or value
    files = candidate.get("files")
    if isinstance(files, list):
        for item in files:
            if isinstance(item, Mapping):
                normalized = _uploaded_file_record(item)
                if normalized is not None:
                    return normalized
        return None

    file_id = next(
        (candidate.get(key) for key in ("id", "file_id", "fileId") if isinstance(candidate.get(key), (str, int))),
        None,
    )
    if file_id is None or not str(file_id):
        return None
    return {
        "id": str(file_id),
        "file_name": next(
            (candidate.get(key) for key in ("file_name", "fileName", "name") if isinstance(candidate.get(key), str)),
            None,
        ),
        "status": candidate.get("status") if isinstance(candidate.get("status"), str) else None,
        "audit_result": next(
            (candidate.get(key) for key in ("audit_result", "auditResult") if isinstance(candidate.get(key), str)),
            None,
        ),
        "retryable": candidate.get("retryable") if isinstance(candidate.get("retryable"), bool) else None,
    }


def _validate_uploaded_file(file: Mapping[str, Any], fallback_name: str) -> None:
    name = str(file.get("file_name") or file.get("id") or fallback_name)
    status = str(file.get("status") or "").strip().upper()
    audit_result = str(file.get("audit_result") or "").strip().upper()
    if audit_result in _REJECTED_FILE_AUDIT_RESULTS:
        raise DeepSeekHarnessError(
            f"DeepSeek rejected {name}: audit_result={audit_result}",
            status=400,
            code="deepseek_image_rejected",
        )
    if status in _FAILED_FILE_STATUSES:
        raise DeepSeekHarnessError(
            f"DeepSeek failed to process {name}: status={status}",
            code="deepseek_image_processing_failed",
        )


def _uploaded_file_ready(file: Mapping[str, Any]) -> bool:
    status = str(file.get("status") or "").strip().upper()
    audit_result = str(file.get("audit_result") or "").strip().upper()
    return status == "SUCCESS" and (
        not audit_result or audit_result in _ACCEPTED_FILE_AUDIT_RESULTS
    )


def _canonical_json(value: object) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _history_image_fingerprints(value: object) -> tuple[str, ...]:
    fingerprints: list[str] = []

    def visit(current: object) -> None:
        if isinstance(current, list):
            for item in current:
                visit(item)
            return
        if not isinstance(current, Mapping):
            return

        part_type = str(current.get("type") or "").lower()
        if part_type in {"input_image", "image", "output_image"}:
            image_url: object = current.get("image_url")
            if isinstance(image_url, Mapping):
                image_url = image_url.get("url")
            identity = image_url if isinstance(image_url, str) else _canonical_json(dict(current))
            fingerprints.append(hashlib.sha256(str(identity).encode("utf-8")).hexdigest())
            return

        for key in ("content", "output"):
            if key in current:
                visit(current.get(key))

    visit(value)
    return tuple(fingerprints)


def _history_item_fingerprint(item: Mapping[str, Any]) -> str:
    item_type = str(item.get("type") or ("message" if item.get("role") else ""))
    if item_type == "message":
        normalized: object = {
            "type": "message",
            "role": str(item.get("role") or "user"),
            "content": _content_text(item.get("content")),
            "images": _history_image_fingerprints(item.get("content")),
        }
    elif item_type == "function_call":
        normalized = {
            "type": "function_call",
            "name": str(item.get("name") or "tool"),
            "arguments": _canonical_json(item.get("arguments")),
        }
    elif item_type == "custom_tool_call":
        normalized = {
            "type": "custom_tool_call",
            "name": str(item.get("name") or "tool"),
            "input": str(item.get("input") or ""),
        }
    elif item_type == "function_call_output":
        normalized = {
            "type": "function_call_output",
            "call_id": str(item.get("call_id") or ""),
            "output": _function_output_text(item.get("output")),
            "images": _history_image_fingerprints(item.get("output")),
        }
    elif item_type == "custom_tool_call_output":
        normalized = {
            "type": "custom_tool_call_output",
            "call_id": str(item.get("call_id") or ""),
            "output": _function_output_text(item.get("output")),
            "images": _history_image_fingerprints(item.get("output")),
        }
    else:
        normalized = dict(item)
    return hashlib.sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()


def _response_history_fingerprints(
    answer_text: str,
    tool_calls: list[dict[str, Any]] | None,
) -> tuple[str, ...]:
    if tool_calls:
        return tuple(
            _history_item_fingerprint({
                "type": "custom_tool_call" if call.get("freeform") else "function_call",
                "name": call.get("name"),
                **(
                    {"input": call.get("input")}
                    if call.get("freeform")
                    else {"arguments": call.get("arguments")}
                ),
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
    tools = _deepseek_tool_definitions(payload)
    return _canonical_json({
        "model": payload.get("model"),
        "instructions": payload.get("instructions"),
        "tools": tools,
    })


def _deepseek_tool_definitions(payload: Mapping[str, Any]) -> list[dict[str, object]]:
    tools: list[dict[str, object]] = []
    for item in payload.get("tools") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        tool_type = str(item.get("type") or "")
        if tool_type == "function":
            tools.append({
                "name": str(item.get("name")),
                "description": str(item.get("description") or ""),
                "parameters": item.get("parameters") if isinstance(item.get("parameters"), dict) else {},
                "freeform": False,
            })
        elif tool_type == "custom":
            tools.append({
                "name": str(item.get("name")),
                "description": str(item.get("description") or ""),
                "parameters": {},
                "freeform": True,
            })
    return tools


def _starts_with(values: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return len(values) >= len(prefix) and values[:len(prefix)] == prefix


def _conversation_state_to_json(state: _DeepSeekConversationState) -> dict[str, object]:
    return {
        "session_id": state.session_id,
        "parent_message_id": state.parent_message_id,
        "represented_items": list(state.represented_items),
        "control_signature": state.control_signature,
        "last_response_id": state.last_response_id,
    }


def _conversation_state_from_json(value: object) -> _DeepSeekConversationState | None:
    if not isinstance(value, Mapping):
        return None
    session_id = value.get("session_id")
    parent_message_id = value.get("parent_message_id")
    represented_items = value.get("represented_items")
    control_signature = value.get("control_signature")
    last_response_id = value.get("last_response_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(parent_message_id, (str, int)) or isinstance(parent_message_id, bool):
        return None
    if not isinstance(represented_items, list) or not all(
        isinstance(item, str) for item in represented_items
    ):
        return None
    if control_signature is not None and not isinstance(control_signature, str):
        return None
    if last_response_id is not None and not isinstance(last_response_id, str):
        return None
    return _DeepSeekConversationState(
        session_id=session_id,
        parent_message_id=parent_message_id,
        represented_items=tuple(represented_items),
        control_signature=control_signature,
        last_response_id=last_response_id,
        needs_validation=True,
    )


def _clear_conversation_state(state: _DeepSeekConversationState) -> None:
    state.session_id = None
    state.parent_message_id = None
    state.represented_items = ()
    state.control_signature = None
    state.last_response_id = None
    state.needs_validation = False


def _history_contains_message(payload: object, message_id: str | int) -> bool:
    if not isinstance(payload, Mapping):
        return False
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    if isinstance(data, Mapping) and isinstance(data.get("biz_data"), Mapping):
        biz = data["biz_data"]
    elif isinstance(data, Mapping) and isinstance(data.get("bizData"), Mapping):
        biz = data["bizData"]
    else:
        biz = data
    if not isinstance(biz, Mapping):
        return False
    messages = biz.get("chat_messages")
    if not isinstance(messages, list):
        messages = biz.get("chatMessages")
    if not isinstance(messages, list):
        return False
    expected = str(message_id)
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        current = message.get("message_id", message.get("id", message.get("uuid")))
        if current is not None and str(current) == expected:
            return True
    return False


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
        if isinstance(path, str) and (
            path in {"response/message_id", "response/response_message_id", "response/id"}
            or "response_message_id" in path
            or "responseMessageId" in path
        ):
            if isinstance(event_value, (str, int)):
                return event_value

        for key in ("response_message_id", "responseMessageId"):
            direct = value.get(key)
            if isinstance(direct, (str, int)):
                return direct

        response = value.get("response")
        if isinstance(response, dict):
            for key in ("message_id", "response_message_id", "id"):
                candidate = response.get(key)
                if isinstance(candidate, (str, int)):
                    return candidate

        if isinstance(event_value, list):
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
            text = _redact_data_uris(_content_text(item.get("content")))
            if text:
                conversation.append(f"{role}:\n{text}")
        elif item_type == "function_call":
            name = str(item.get("name") or "tool")
            call_id = str(item.get("call_id") or item.get("id") or "")
            arguments = item.get("arguments")
            rendered = (
                "ASSISTANT TOOL CALL:\n" + json.dumps(
                    {"call_id": call_id, "name": name, "arguments": arguments},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            rendered = _redact_data_uris(rendered)
            conversation.append(rendered)
        elif item_type == "custom_tool_call":
            name = str(item.get("name") or "tool")
            call_id = str(item.get("call_id") or item.get("id") or "")
            rendered = (
                f"ASSISTANT FREEFORM TOOL CALL ({call_id}, {name}):\n"
                f"{str(item.get('input') or '')}"
            )
            conversation.append(_redact_data_uris(rendered))
        elif item_type == "function_call_output":
            call_id = str(item.get("call_id") or "")
            rendered = f"TOOL RESULT ({call_id}):\n{_function_output_text(item.get('output'))}"
            conversation.append(rendered)
        elif item_type == "custom_tool_call_output":
            call_id = str(item.get("call_id") or "")
            rendered = f"TOOL RESULT ({call_id}):\n{_function_output_text(item.get('output'))}"
            conversation.append(rendered)
    if conversation:
        sections.append("CONVERSATION:\n" + "\n\n".join(conversation))

    tools = _deepseek_tool_definitions(payload)
    if tools and include_control:
        tag_map = _tool_tag_map(str(tool["name"]) for tool in tools)
        tool_sections = [
            _render_tool_prompt(tool, tag_map[str(tool["name"])])
            for tool in tools
        ]
        sections.append(
            "CODEX TOOL PROTOCOL:\n"
            "Codex, not you, executes tools. Each available tool has its own direct XML tag. "
            "Structured tools use JSON bodies; freeform tools use raw text bodies.\n"
            f"{_tool_format_requirements()}\n\n"
            + "\n\n".join(tool_sections)
        )
    elif tools:
        sections.append(_tool_format_reminder(tools))
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
                parts.append("[Image attached separately for DeepSeek Vision]")
    return "\n".join(parts)


def _function_output_text(output: object) -> str:
    sanitized = _sanitize_context_value(output)
    if isinstance(sanitized, str):
        rendered = sanitized
    elif isinstance(sanitized, (dict, list)):
        rendered = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    else:
        rendered = "" if sanitized is None else str(sanitized)
    return rendered


def _sanitize_context_value(value: object) -> object:
    """Remove binary/image payloads before rendering Codex history as text."""
    if isinstance(value, str):
        return _redact_data_uris(value)
    if isinstance(value, list):
        return [_sanitize_context_value(item) for item in value]
    if isinstance(value, dict):
        part_type = str(value.get("type") or "").lower()
        if part_type in {"input_image", "image", "output_image"}:
            return f"[Image payload omitted: {part_type}]"
        return {str(key): _sanitize_context_value(item) for key, item in value.items()}
    return value


def _redact_data_uris(text: str) -> str:
    return _DATA_URI_RE.sub(lambda match: f"[Binary data URI omitted: {match.group(1)}]", text)


def _extract_compact_user_messages(input_value: object) -> list[dict[str, Any]]:
    """Mirror Codex/WebGPT compaction: retain only genuine user messages as checkpoint anchors."""
    if not isinstance(input_value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in input_value:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type not in {None, "message"} or item.get("role") != "user":
            continue
        blocks = _compact_content_blocks(item)
        text = "".join(
            str(block.get("text") or "")
            for block in blocks
            if _is_compact_text_block(block)
        ).strip()
        if re.fullmatch(r'<codex_internal_context source="[a-z][a-z0-9_]*">[\s\S]*</codex_internal_context>', text):
            continue
        if re.fullmatch(r"<goal_context>[\s\S]*</goal_context>", text):
            continue
        if text.startswith(_SUMMARY_PREFIX + "\n"):
            continue
        result.append(copy.deepcopy(item))
    return result


def _compact_content_blocks(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    content = item.get("content")
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if not isinstance(content, list):
        return []
    return [copy.deepcopy(block) for block in content if isinstance(block, dict)]


def _is_compact_text_block(block: Mapping[str, Any]) -> bool:
    return block.get("type") in {"input_text", "text"} and isinstance(block.get("text"), str)


def _is_compact_image_block(block: Mapping[str, Any]) -> bool:
    return block.get("type") == "input_image" and isinstance(block.get("image_url"), str)


def _build_compact_v1_output(
    user_messages: list[dict[str, Any]],
    summary: str,
    *,
    max_images: int = 10,
) -> list[dict[str, Any]]:
    """Build the same replacement-history shape used by WebGPT/Codex v1 compaction."""
    selected: list[dict[str, Any]] = []
    remaining = _COMPACT_RETAINED_USER_CHAR_BUDGET
    retained_images = 0
    for source in reversed(user_messages):
        if remaining <= 0 and retained_images >= max_images:
            break
        message = copy.deepcopy(source)
        kept_reversed: list[dict[str, Any]] = []
        for block in reversed(_compact_content_blocks(message)):
            if _is_compact_image_block(block):
                if retained_images < max_images:
                    retained_images += 1
                    kept_reversed.append(block)
                continue
            if not _is_compact_text_block(block) or remaining <= 0:
                continue
            text = str(block["text"])
            if len(text) <= remaining:
                remaining -= len(text)
                kept_reversed.append({**block, "type": "input_text", "text": text})
            else:
                kept_reversed.append({**block, "type": "input_text", "text": text[-remaining:]})
                remaining = 0
        content = list(reversed(kept_reversed))
        if content:
            message["type"] = "message"
            message["role"] = "user"
            message["content"] = content
            selected.append(message)
    selected.reverse()
    summary_text = f"{_SUMMARY_PREFIX}\n{summary}" if summary.strip() else "(no summary available)"
    selected.append({
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": summary_text}],
    })
    return selected


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
    if tool.get("freeform") is True:
        example = (
            "*** Begin Patch\n*** Update File: path/to/file\n@@\n-old\n+new\n*** End Patch"
            if name == "apply_patch"
            else "RAW TOOL INPUT"
        )
        lines = [
            f"### Tool {name}",
            f"Description: {description}",
        ]
        if tag_name != name:
            lines.append(f"Direct tag name: `{tag_name}` (maps to Responses tool `{name}`).")
        lines.extend([
            f"Valid freeform call format for {name}:",
            f"<{tag_name}>",
            example,
            f"</{tag_name}>",
            "The tag body is raw freeform tool input. Do NOT JSON-encode it, quote it, or wrap it in an `input` object.",
        ])
        return "\n".join(lines)
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


def _encode_pow_response(
    challenge: Mapping[str, object],
    answer: int,
    *,
    target_path: str = _COMPLETION_PATH,
) -> str:
    payload = {
        "algorithm": challenge["algorithm"],
        "challenge": challenge["challenge"],
        "salt": challenge["salt"],
        "answer": answer,
        "signature": challenge["signature"],
        "target_path": target_path,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _parse_deepseek_stream(raw: bytes) -> tuple[str, str]:
    text = raw.decode("utf-8", errors="replace")
    state: dict[str, Any] = {"types": [], "current": -1, "observed": False}
    answer: list[str] = []
    reasoning: list[str] = []
    for block in re.split(r"\r?\n\r?\n", text):
        delta_text, delta_reasoning = _parse_deepseek_sse_block(block, state)
        if delta_text:
            answer.append(delta_text)
        if delta_reasoning:
            reasoning.append(delta_reasoning)
    return "".join(answer), "".join(reasoning)


def _parse_deepseek_sse_block(block: str, state: dict[str, Any]) -> tuple[str, str]:
    data_lines = [line[5:].strip() for line in block.splitlines() if line.startswith("data:")]
    if not data_lines:
        return "", ""
    data = "\n".join(data_lines)
    if data == "[DONE]":
        return "", ""
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return "", ""
    return _split_deepseek_text(parsed, state)


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


def _parse_tool_calls(
    text: str,
    available_tools: set[str],
    freeform_tools: set[str] | None = None,
) -> list[dict[str, Any]] | None:
    return _parse_direct_tool_calls(text, available_tools, freeform_tools or set())


def _looks_like_tool_call_attempt(text: str, available_tools: set[str]) -> bool:
    """Detect a likely tool-call attempt without trying to diagnose every possible mistake."""
    if not available_tools:
        return False
    tag_map = _tool_tag_map(available_tools)
    if any(f"<{tag}" in text or f"</{tag}" in text for tag in tag_map.values()):
        return True
    stripped = text.strip()
    if not stripped.startswith("<") or ">" not in stripped:
        return False
    return any(name in stripped for name in available_tools)


def _tool_format_requirements() -> str:
    return (
        "When a tool is required, output only one or more direct tool blocks and no prose outside them. "
        "The XML tag name itself selects the tool. Structured tools MUST contain exactly one valid JSON object containing only that tool's arguments. "
        "Freeform tools MUST contain raw tool input and MUST NOT JSON-encode that input.\n"
        "For structured tools, JSON string values MUST use valid JSON escaping: escape embedded double quotes as \\\" and backslashes as \\\\ when needed. "
        "For Windows paths inside structured JSON, prefer forward slashes or correctly escaped backslashes.\n"
        "Use the exact opening and closing tag shown for that tool. Every opening tag MUST have exactly one matching closing tag; "
        "after closing a tool block, either start the next complete tool block or stop. Never switch to a different tool-call syntax or closing delimiter.\n"
        "Do not add attributes to tool tags. Do not wrap arguments in `name`, `arguments`, or `tool`. "
        "This direct per-tool XML format is the only valid tool-call syntax. This is the only valid tool-call format.\n"
        "Tool-call XML belongs in the final RESPONSE, never in private reasoning/THINK content. "
        "Never invent a tool result. After Codex returns TOOL RESULT in a later turn, continue the task normally. "
        "If no tool is required, answer normally and emit no tool tags."
    )


def _tool_format_reminder(tools: list[Mapping[str, object]]) -> str:
    tag_map = _tool_tag_map(str(tool.get("name") or "tool") for tool in tools)
    examples: list[str] = []
    for tool in tools:
        name = str(tool.get("name") or "tool")
        tag = tag_map[name]
        if tool.get("freeform") is True:
            example = "*** Begin Patch\n*** End Patch" if name == "apply_patch" else "RAW TOOL INPUT"
            examples.append(f"<{tag}>{example}</{tag}>")
        else:
            parameters = tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {"type": "object"}
            example = _schema_example(parameters)
            if not isinstance(example, dict):
                example = {}
            examples.append(
                f"<{tag}>{json.dumps(example, ensure_ascii=False, separators=(',', ':'))}</{tag}>"
            )
    valid_examples = "\n".join(examples)
    return (
        "CODEX TOOL FORMAT REMINDER:\n"
        "If a tool is required, output ONLY direct tool XML blocks using the exact complete examples below as the format.\n"
        "Complete valid tool-call examples this turn:\n"
        f"{valid_examples}\n"
        f"{_tool_format_requirements()}"
    )


def _tool_call_recovery_prompt(tools: list[Mapping[str, object]]) -> str:
    return (
        "TOOL CALL FORMAT ERROR:\n"
        "Your previous RESPONSE could not be executed as a valid tool call.\n"
        f"{_tool_format_reminder(tools)}\n"
        "Re-emit the intended tool call(s) now using that exact format, with no explanation."
    )


def _parse_direct_tool_calls(
    text: str,
    available_tools: set[str],
    freeform_tools: set[str],
) -> list[dict[str, Any]] | None:
    if not available_tools:
        return None
    tag_map = _tool_tag_map(available_tools)
    tool_by_tag = {tag: name for name, tag in tag_map.items()}
    tag_alternation = "|".join(re.escape(tag) for tag in sorted(tool_by_tag, key=len, reverse=True))
    pattern = re.compile(rf"<(?P<tag>{tag_alternation})>(?P<body>.*?)</(?P=tag)>", re.DOTALL)
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    calls: list[dict[str, Any]] = []
    cursor = 0
    for match in matches:
        if text[cursor:match.start()].strip():
            return None
        name = tool_by_tag[match.group("tag")]
        body = match.group("body")
        if name in freeform_tools:
            raw_input = body.strip()
            if not raw_input:
                return None
            calls.append({"name": name, "input": raw_input, "freeform": True})
        else:
            try:
                arguments = json.loads(body.strip())
            except json.JSONDecodeError:
                return None
            if not isinstance(arguments, dict):
                return None
            calls.append({"name": name, "arguments": arguments})
        cursor = match.end()
    if text[cursor:].strip():
        return None
    return calls or None


def _responses_events(
    model: str,
    text: str,
    tool_calls: list[dict[str, Any]],
    *,
    response_id: str | None = None,
    created_at: int | None = None,
    message_id: str | None = None,
) -> Iterator[bytes]:
    response_id = response_id or ("resp_" + uuid.uuid4().hex)
    created_at = int(time.time()) if created_at is None else created_at
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
        message_id = message_id or ("msg_" + uuid.uuid4().hex)
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
        if call.get("freeform"):
            raw_input = str(call.get("input") or "")
            item_id = "ctc_" + uuid.uuid4().hex
            item = {
                "id": item_id,
                "type": "custom_tool_call",
                "status": "in_progress",
                "call_id": call_id,
                "name": call["name"],
                "input": "",
            }
            yield emit("response.output_item.added", output_index=index, item=item)
            yield emit("response.custom_tool_call_input.delta", item_id=item_id, output_index=index, delta=raw_input)
            completed = {**item, "status": "completed", "input": raw_input}
            yield emit("response.custom_tool_call_input.done", item_id=item_id, output_index=index, input=raw_input)
            yield emit("response.output_item.done", output_index=index, item=completed)
            output.append(completed)
            continue
        arguments = json.dumps(call["arguments"], ensure_ascii=False, separators=(",", ":"))
        item_id = "fc_" + uuid.uuid4().hex
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
