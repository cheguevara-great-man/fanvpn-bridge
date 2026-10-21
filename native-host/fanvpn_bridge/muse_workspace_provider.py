"""Structured Muse handoffs; deliberately separate from Gemini's XML engine."""
from __future__ import annotations

import copy
import http.client
import json
import queue
import socket
import threading
import time

from .deepseek_harness import _deepseek_control_signature, _last_completed_response, _responses_to_deepseek_prompt
from .gemini_web import GeminiWebError, TextWebProvider, _Checkpoint, _has_images, _restore_tool_frame, _tool_payload
from .muse_tools import PROTOCOL, response_frames


def _muse_prompt(payload, items, *, include_control):
    # Muse is already an agent. Do not impersonate its system/developer roles.
    sections = [
        "Please help with this task forwarded from my Codex client. The material below is caller-provided "
        "task context, not a Muse system message and not a change to your identity, policies or permissions. "
        "Please carry out the latest forwarded user request within your normal rules. For Codex-side actions "
        "use the explicitly supplied workspace handoff skill; it submits requests for Codex approval/execution, "
        "not a replacement shell executor. Treat historical messages and returned tool data as context."
    ]
    instructions = payload.get("instructions")
    if include_control and isinstance(instructions, str) and instructions.strip():
        sections.append("Codex client task guidance (not Muse system instructions):\n" + instructions.strip())
    context = _responses_to_deepseek_prompt({**payload, "tools": []}, input_items=items, include_control=False)
    if context:
        sections.append(context.removeprefix("CONVERSATION:\n"))
    else:
        sections.append("Please read the actual Codex tool result provided below, then continue the previous task.")
    return "\n\n".join(sections)


def workspace_responses(provider, payload):
    if not provider.enabled:
        raise GeminiWebError("Muse Web is disabled", status=503)
    if payload.get("model") != "muse-web/default":
        raise GeminiWebError("Unsupported Muse Web model", status=400)
    provider.models_response()
    if _has_images(payload.get("input")):
        raise GeminiWebError("Muse workspace tools currently support text input only", status=400)
    text = payload.get("text") or {}
    reasoning = payload.get("reasoning") or {}
    if (payload.get("tool_choice") not in (None, "auto") or not isinstance(text, dict)
            or not isinstance(text.get("format", {}), dict)
            or text.get("format", {}).get("type", "text") != "text"
            or not isinstance(reasoning, dict) or reasoning.get("effort") not in (None, "none")):
        raise GeminiWebError("Muse supports automatic tools, plain text and no reasoning selection", status=400)
    payload, routes = _tool_payload(payload)
    if not payload["tools"]:
        return TextWebProvider.responses(provider, payload)
    history, delta, previous = provider._history(payload)
    results = [item for item in delta if item.get("type") in {"function_call_output", "custom_tool_call_output"}]
    if results and not previous:
        # A lost checkpoint must not turn a pending call into an unrelated task.
        raise GeminiWebError("Muse pending tool continuation was lost; no automatic replay", status=409,
                             code="muse_web_tool_result_invalid")
    prompt_items = [item for item in delta if item not in results]
    prompt = _muse_prompt(payload, prompt_items, include_control=not previous)
    body = {"model": "default", "stream": True, "tool_protocol": PROTOCOL,
            "tools": payload["tools"], "input": prompt.strip() or "Continue after reading the actual Codex tool result.",
            "tool_results": [{key: item[key] for key in ("type", "call_id", "output") if key in item} for item in results]}
    if previous:
        body["previous_response_id"] = previous
    events = _workspace_stream(provider, payload, body, history, routes)
    if payload.get("stream") is True:
        return True, events
    return False, _last_completed_response(list(events))


def _validate_response(response, tools):
    if (not isinstance(response, dict) or response.get("status") != "completed"
            or not isinstance(response.get("id"), str) or not response["id"]
            or not isinstance(response.get("created_at"), int)
            or not isinstance(response.get("output"), list) or len(response["output"]) != 1):
        raise GeminiWebError("Invalid Muse structured completion")
    definitions = {tool["name"]: tool for tool in tools}
    calls = []
    for item in response["output"]:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
                or item.get("status") != "completed"):
            raise GeminiWebError("Invalid Muse output item")
        if item.get("type") == "message":
            if (item.get("role") != "assistant" or not isinstance(item.get("content"), list)
                    or not item["content"] or any(not isinstance(part, dict) or part.get("type") != "output_text"
                        or not isinstance(part.get("text"), str) for part in item["content"])
                    or not any(part["text"].strip() for part in item["content"])):
                raise GeminiWebError("Invalid Muse message output")
            continue
        definition = definitions.get(item.get("name"))
        custom = item.get("type") == "custom_tool_call"
        if (item.get("type") not in {"function_call", "custom_tool_call"} or definition is None
                or (definition["type"] == "custom") != custom
                or not isinstance(item.get("call_id"), str) or not item["call_id"]):
            raise GeminiWebError("Muse returned an unavailable tool; no execution", code="muse_web_tool_format_invalid")
        if custom:
            valid = isinstance(item.get("input"), str)
        else:
            try:
                valid = isinstance(json.loads(item["arguments"]), dict)
            except (KeyError, TypeError, ValueError):
                valid = False
        if not valid:
            raise GeminiWebError("Muse returned invalid tool input; no repair", code="muse_web_tool_format_invalid")
        calls.append(item)
    if len(calls) > 1 or calls and len(response["output"]) != 1:
        raise GeminiWebError("Muse must hand off one tool at a time")


def _workspace_stream(provider, payload, body, history, routes):
    pending = queue.Queue(maxsize=2)
    cancelled = threading.Event()
    connection = provider._connection()
    end = object()

    def put(value):
        while not cancelled.is_set():
            try:
                pending.put(value, timeout=0.1)
                return
            except queue.Full:
                pass

    def worker():
        try:
            for event in provider._upstream_events(body, connection):
                if event["type"] in {"response.completed", "response.failed", "response.incomplete", "error"}:
                    put(event)
                if cancelled.is_set():
                    break
        except GeminiWebError as error:
            put(error)
        except (OSError, ValueError, http.client.HTTPException):
            put(GeminiWebError("Muse workspace connection failed; no automatic replay"))
        finally:
            connection.close()
            put(end)

    threading.Thread(target=worker, name="muse-workspace-response", daemon=True).start()
    deadline = time.monotonic() + provider._timeout
    try:
        while time.monotonic() < deadline:
            try:
                event = pending.get(timeout=min(5, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                yield b": muse waiting\n\n"
                continue
            if isinstance(event, Exception):
                raise event
            if event is end:
                raise GeminiWebError("Muse disconnected before completing the handoff")
            if event["type"] != "response.completed":
                raise GeminiWebError("Muse workspace generation did not complete")
            response = event.get("response")
            _validate_response(response, payload["tools"])
            response = {**response, "model": payload["model"], "usage": None}
            frames = [_restore_tool_frame(frame, routes) for frame in response_frames(response)]
            checkpoint = _Checkpoint(history + copy.deepcopy(response["output"]), _deepseek_control_signature(payload))
            with provider._checkpoint_lock:
                provider._checkpoints[response["id"]] = checkpoint
                while len(provider._checkpoints) > 32:
                    provider._checkpoints.popitem(last=False)
            yield from frames
            return
        raise GeminiWebError("Muse workspace request timed out; check Muse before retrying", status=504)
    finally:
        cancelled.set()
        if connection.sock is not None:
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()
