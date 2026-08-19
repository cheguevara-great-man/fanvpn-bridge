"""Codex Responses compatibility provider backed by a Google AI account.

The provider deliberately stops at the model boundary: Codex keeps ownership of
the agent loop and tools, while Google Code Assist supplies model inference.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from collections import OrderedDict
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any, Iterable, Iterator
import urllib.error
import urllib.request
import uuid


_CREDENTIAL_TARGET = "gemini:antigravity"
_DEFAULT_MODEL = "gemini-3.7-flash"
_DEFAULT_UPSTREAM_MODEL = "gemini-3.7-flash-tiered"
_MAX_CREDENTIAL_BYTES = 1024 * 1024
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_SIGNATURE_FALLBACK = "skip_thought_signature_validator"
_CODE_ASSIST_USER_AGENT = "antigravity/1.1.5 windows/amd64"
_MAX_SIGNATURES = 2048
_QUOTA_CACHE_SECONDS = 60.0


class GeminiAccountError(RuntimeError):
    def __init__(self, message: str, *, status: int = 502, code: str = "gemini_account_error") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True, slots=True)
class GoogleAccountCredential:
    access_token: str
    refresh_token: str
    expires_at: float

    @property
    def needs_refresh(self) -> bool:
        return not self.access_token or (self.expires_at > 0 and self.expires_at <= time.time() + 120)


@dataclass(frozen=True, slots=True)
class GeminiModelChoice:
    id: str
    display_name: str
    default_reasoning_level: str
    supported_reasoning_levels: tuple[str, ...]
    routes: tuple[tuple[str, str], ...]


def _model_display_name(model_id: str) -> str:
    return " ".join(part if re.fullmatch(r"\d+(?:\.\d+)*", part) else part.title() for part in model_id.split("-"))


def _normalize_available_models(models_value: object) -> tuple[GeminiModelChoice, ...]:
    """Turn Code Assist's internal aliases into user-facing model families."""
    if not isinstance(models_value, dict):
        return (
            GeminiModelChoice(
                _DEFAULT_MODEL,
                _model_display_name(_DEFAULT_MODEL),
                "medium",
                ("low", "medium", "high"),
                tuple((effort, _DEFAULT_UPSTREAM_MODEL) for effort in ("low", "medium", "high")),
            ),
        )

    tiered: dict[str, str] = {}
    fixed: dict[str, dict[str, str]] = {}
    display_names: dict[str, str] = {}
    display_pattern = re.compile(
        r"^Gemini\s+(?P<version>\d+(?:\.\d+)*)\s+(?P<family>.+?)\s+\((?P<effort>Low|Medium|High)\)$",
        re.IGNORECASE,
    )
    for raw_id, raw_metadata in models_value.items():
        raw_id = str(raw_id)
        if not raw_id.startswith("gemini-"):
            continue
        tiered_match = re.fullmatch(r"(?P<base>gemini-[a-z0-9.-]+)-tiered", raw_id)
        if tiered_match:
            tiered[tiered_match.group("base")] = raw_id
            continue
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        display_name = str(metadata.get("displayName") or "").strip()
        display_match = display_pattern.fullmatch(display_name)
        if not display_match:
            continue
        family = re.sub(r"[^a-z0-9]+", "-", display_match.group("family").lower()).strip("-")
        base = f"gemini-{display_match.group('version')}-{family}"
        effort = display_match.group("effort").lower()
        routes = fixed.setdefault(base, {})
        existing = routes.get(effort, "")
        prefer_agent = effort == "high" and "agent" in raw_id and "agent" not in existing
        prefer_non_agent = effort != "high" and "agent" in existing and "agent" not in raw_id
        if not existing or prefer_agent or prefer_non_agent:
            routes[effort] = raw_id
        display_names[base] = re.sub(r"\s+\((?:Low|Medium|High)\)$", "", display_name, flags=re.IGNORECASE)

    choices: list[GeminiModelChoice] = []
    for base in sorted(set(tiered) | set(fixed)):
        if base in tiered:
            levels = ("low", "medium", "high")
            routes = tuple((effort, tiered[base]) for effort in levels)
        else:
            routes_by_effort = fixed[base]
            levels = tuple(effort for effort in ("low", "medium", "high") if effort in routes_by_effort)
            routes = tuple((effort, routes_by_effort[effort]) for effort in levels)
        if not levels:
            continue
        default = "medium" if "medium" in levels else "high" if "high" in levels else levels[0]
        choices.append(
            GeminiModelChoice(
                base,
                display_names.get(base) or _model_display_name(base),
                default,
                levels,
                routes,
            )
        )

    def rank(choice: GeminiModelChoice) -> tuple[tuple[int, ...], int, str]:
        match = re.match(r"^gemini-(\d+(?:\.\d+)*)-", choice.id)
        version = tuple(int(part) for part in match.group(1).split(".")) if match else (0,)
        return version, 1 if "flash" in choice.id else 0, choice.id

    return tuple(sorted(choices, key=rank, reverse=True)) or _normalize_available_models(None)


class ThoughtSignatureCache(MutableMapping[str, str]):
    """Small thread-safe LRU used only to complete Gemini tool round trips."""

    def __init__(self, limit: int = _MAX_SIGNATURES) -> None:
        self._limit = max(1, limit)
        self._values: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()

    def __getitem__(self, key: str) -> str:
        with self._lock:
            value = self._values[key]
            self._values.move_to_end(key)
            return value

    def __setitem__(self, key: str, value: str) -> None:
        with self._lock:
            self._values[key] = value
            self._values.move_to_end(key)
            while len(self._values) > self._limit:
                self._values.popitem(last=False)

    def __delitem__(self, key: str) -> None:
        with self._lock:
            del self._values[key]

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            return iter(tuple(self._values))

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)

    def get(self, key: str, default: str | None = None) -> str | None:
        try:
            return self[key]
        except KeyError:
            return default


if os.name == "nt":
    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]


class WindowsAntigravityCredentialStore:
    """Read the official CLI credential without copying it to project files."""

    def read(self) -> GoogleAccountCredential:
        if os.name != "nt":
            raise GeminiAccountError(
                "Google account mode currently requires Windows Credential Manager",
                status=501,
                code="platform_not_supported",
            )
        advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        cred_read = advapi32.CredReadW
        cred_read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
        cred_read.restype = wintypes.BOOL
        cred_free = advapi32.CredFree
        cred_free.argtypes = [ctypes.c_void_p]
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not cred_read(_CREDENTIAL_TARGET, 1, 0, ctypes.byref(pointer)):
            raise GeminiAccountError(
                "Google account login was not found; sign in once with agy-browser.exe",
                status=401,
                code="google_login_missing",
            )
        try:
            size = int(pointer.contents.CredentialBlobSize)
            if size <= 0 or size > _MAX_CREDENTIAL_BYTES:
                raise GeminiAccountError("Google account credential has an invalid size", status=401)
            raw = ctypes.string_at(pointer.contents.CredentialBlob, size)
        finally:
            cred_free(pointer)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GeminiAccountError("Google account credential is invalid", status=401) from exc
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, dict):
            raise GeminiAccountError("Google account credential has no token", status=401)
        access = str(token.get("access_token") or "").strip()
        refresh = str(token.get("refresh_token") or "").strip()
        expiry = _parse_expiry(token.get("expiry"))
        if not access:
            raise GeminiAccountError("Google account access token is missing", status=401)
        return GoogleAccountCredential(access, refresh, expiry)


class GeminiAccountProvider:
    """Translate OpenAI Responses traffic to Google Code Assist traffic."""

    def __init__(
        self,
        *,
        bridge_url: str = "http://127.0.0.1:18888",
        credential_store: WindowsAntigravityCredentialStore | None = None,
        timeout_seconds: float = 600,
    ) -> None:
        self._bridge_url = bridge_url.rstrip("/")
        self._credential_store = credential_store or WindowsAntigravityCredentialStore()
        self._timeout = timeout_seconds
        self._refresh_lock = threading.Lock()
        self._project_lock = threading.Lock()
        self._project_id = ""
        self._project_token_digest = b""
        self._models: tuple[GeminiModelChoice, ...] = ()
        self._models_at = 0.0
        self._models_token_digest = b""
        self._quota: dict[str, object] | None = None
        self._quota_at = 0.0
        self._quota_token_digest = b""
        self._signatures: MutableMapping[str, str] = ThoughtSignatureCache()

    def quota_summary_response(self) -> dict[str, object]:
        """Return the Google account quota summary, cached briefly for popup reuse."""
        try:
            credential = self._valid_credential()
            token_digest = _token_digest(credential.access_token)
            if (
                self._quota is not None
                and self._quota_token_digest == token_digest
                and time.monotonic() - self._quota_at < _QUOTA_CACHE_SECONDS
            ):
                return self._quota
            project = self._project(credential.access_token)
            raw = self._post_json(
                "/antigravity/v1internal:retrieveUserQuotaSummary",
                {"project": project},
                credential.access_token,
            )
            if not isinstance(raw.get("groups"), list):
                return {"ok": False, "error": "Google account returned an invalid quota summary"}
            result: dict[str, object] = {
                "ok": True,
                "fetchedAt": int(time.time()),
                "quota": raw,
            }
            self._quota = result
            self._quota_at = time.monotonic()
            self._quota_token_digest = token_digest
            return result
        except GeminiAccountError as error:
            return {"ok": False, "error": str(error)}
        except Exception as error:
            return {"ok": False, "error": f"Failed to fetch Gemini quota: {error}"}

    def models_response(self) -> dict[str, object]:
        models = self._available_models()
        now = int(time.time())
        return {
            "object": "list",
            "data": [
                {
                    "id": model.id,
                    "object": "model",
                    "created": now,
                    "owned_by": "google-account",
                    "display_name": model.display_name,
                    "default_reasoning_level": model.default_reasoning_level,
                    "supported_reasoning_levels": list(model.supported_reasoning_levels),
                }
                for model in models
            ],
        }

    def responses(self, payload: dict[str, Any]) -> tuple[bool, dict[str, Any] | Iterator[bytes]]:
        requested_model = str(payload.get("model") or "").strip()
        models = self._available_models()
        selected = next((item for item in models if item.id == requested_model), models[0])
        reasoning = payload.get("reasoning")
        requested_effort = str(reasoning.get("effort") or "").lower() if isinstance(reasoning, dict) else ""
        effort = requested_effort if requested_effort in selected.supported_reasoning_levels else selected.default_reasoning_level
        routes = dict(selected.routes)
        model = routes[effort]
        stream = bool(payload.get("stream", False))
        normalized_payload = dict(payload)
        normalized_reasoning = dict(reasoning) if isinstance(reasoning, dict) else {}
        normalized_reasoning["effort"] = effort
        normalized_payload["reasoning"] = normalized_reasoning
        upstream_request = _responses_to_gemini(normalized_payload, self._signatures)
        credential = self._valid_credential()
        project = self._project(credential.access_token)
        wrapper = {
            "project": project,
            "requestId": "agent-" + uuid.uuid4().hex,
            "request": upstream_request,
            "model": model,
            "userAgent": "antigravity",
            "requestType": "agent",
        }
        chunks = self._stream_generate(wrapper, credential.access_token)
        events = _gemini_to_responses_events(chunks, selected.id, self._signatures)
        if stream:
            return True, events
        output_events = list(events)
        completed = _last_completed_event(output_events)
        return False, completed.get("response", completed)

    def _valid_credential(self) -> GoogleAccountCredential:
        credential = self._credential_store.read()
        if not credential.needs_refresh:
            return credential
        with self._refresh_lock:
            credential = self._credential_store.read()
            if not credential.needs_refresh:
                return credential
            self._refresh_with_official_cli()
            credential = self._credential_store.read()
            if credential.needs_refresh:
                raise GeminiAccountError(
                    "Google account token could not be refreshed; run agy-browser.exe and sign in again",
                    status=401,
                    code="google_refresh_failed",
                )
            return credential

    def _refresh_with_official_cli(self) -> None:
        executable = Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy-browser.exe"
        if not executable.is_file():
            raise GeminiAccountError(
                "The official Antigravity CLI is required to refresh Google login",
                status=401,
                code="google_refresh_helper_missing",
            )
        environment = dict(os.environ)
        environment["CLOUD_CODE_URL"] = self._bridge_url + "/antigravity"
        completed = subprocess.run(
            [str(executable), "models"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=90,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            raise GeminiAccountError(
                "The official Google login refresh helper failed",
                status=401,
                code="google_refresh_failed",
            )

    def _project(self, access_token: str) -> str:
        token_digest = _token_digest(access_token)
        if self._project_id and self._project_token_digest == token_digest:
            return self._project_id
        with self._project_lock:
            if self._project_id and self._project_token_digest == token_digest:
                return self._project_id
            response = self._post_json(
                "/antigravity/v1internal:loadCodeAssist",
                {
                    "metadata": {
                        "ideType": "IDE_UNSPECIFIED",
                        "platform": "PLATFORM_UNSPECIFIED",
                        "pluginType": "GEMINI",
                    }
                },
                access_token,
            )
            project = str(response.get("cloudaicompanionProject") or "").strip()
            if not project:
                raise GeminiAccountError("Google account project could not be resolved", status=403)
            self._project_id = project
            self._project_token_digest = token_digest
            return project

    def _available_models(self) -> tuple[GeminiModelChoice, ...]:
        credential = self._valid_credential()
        token_digest = _token_digest(credential.access_token)
        if self._models and self._models_token_digest == token_digest and time.monotonic() - self._models_at < 300:
            return self._models
        payload = self._post_json(
            "/antigravity/v1internal:fetchAvailableModels",
            {"project": self._project(credential.access_token)},
            credential.access_token,
        )
        models_value = payload.get("models")
        self._models = _normalize_available_models(models_value)
        self._models_at = time.monotonic()
        self._models_token_digest = token_digest
        return self._models

    def _post_json(self, path: str, body: dict[str, Any], access_token: str) -> dict[str, Any]:
        request = urllib.request.Request(
            self._bridge_url + path,
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bearer " + access_token,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _CODE_ASSIST_USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise _upstream_error(exc) from exc
        except OSError as exc:
            raise GeminiAccountError("Google account backend is unavailable") from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise GeminiAccountError("Google account response exceeded the size limit")
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GeminiAccountError("Google account backend returned invalid JSON") from exc
        return value if isinstance(value, dict) else {}

    def _stream_generate(self, body: dict[str, Any], access_token: str) -> Iterator[dict[str, Any]]:
        request = urllib.request.Request(
            self._bridge_url + "/antigravity/v1internal:streamGenerateContent?alt=sse",
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bearer " + access_token,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "User-Agent": _CODE_ASSIST_USER_AGENT,
            },
        )
        try:
            response = urllib.request.urlopen(request, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            raise _upstream_error(exc) from exc
        except OSError as exc:
            raise GeminiAccountError("Google account model request could not be started") from exc
        def iterate() -> Iterator[dict[str, Any]]:
            total = 0
            try:
                for raw_line in response:
                    total += len(raw_line)
                    if total > _MAX_RESPONSE_BYTES:
                        raise GeminiAccountError("Google account stream exceeded the size limit")
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith(("event:", "id:", "retry:")):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line or line == "[DONE]":
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        yield value
            finally:
                response.close()

        return iterate()


def _responses_to_gemini(payload: dict[str, Any], signatures: MutableMapping[str, str]) -> dict[str, Any]:
    contents: list[dict[str, Any]] = []
    system_parts: list[dict[str, str]] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions:
        system_parts.append({"text": instructions})
    call_names: dict[str, str] = {}
    input_value = payload.get("input")
    items = input_value if isinstance(input_value, list) else [{"type": "message", "role": "user", "content": input_value}]
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or ("message" if item.get("role") else ""))
        role = str(item.get("role") or "user").lower()
        if item_type == "message":
            if role in {"system", "developer"}:
                system_parts.extend(_text_parts(item.get("content")))
                continue
            parts = _message_parts(item.get("content"))
            if parts:
                _append_content(contents, "model" if role in {"assistant", "model"} else "user", parts)
        elif item_type == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or uuid.uuid4().hex)
            name = _safe_function_name(str(item.get("name") or "tool"))
            call_names[call_id] = name
            arguments = item.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"raw": arguments}
            if not isinstance(arguments, dict):
                arguments = {}
            _append_content(
                contents,
                "model",
                [{
                    "functionCall": {"id": call_id, "name": name, "args": arguments},
                    "thoughtSignature": signatures.get(call_id, _SIGNATURE_FALLBACK),
                }],
            )
        elif item_type == "function_call_output":
            call_id = str(item.get("call_id") or "")
            output = item.get("output")
            resp_obj, media_parts = _extract_function_output(output, call_id)
            fr_dict: dict[str, Any] = {
                "id": call_id,
                "name": call_names.get(call_id, "tool"),
                "response": resp_obj,
            }
            if media_parts:
                fr_dict["parts"] = media_parts
            _append_content(
                contents,
                "user",
                [{"functionResponse": fr_dict}],
            )
    request: dict[str, Any] = {"contents": contents}
    if system_parts:
        request["systemInstruction"] = {"role": "user", "parts": system_parts}
    declarations: list[dict[str, Any]] = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        declaration: dict[str, Any] = {
            "name": _safe_function_name(str(tool.get("name") or "tool")),
            "description": str(tool.get("description") or ""),
        }
        if isinstance(tool.get("parameters"), dict):
            declaration["parametersJsonSchema"] = tool["parameters"]
        declarations.append(declaration)
    if declarations:
        request["tools"] = [{"functionDeclarations": declarations}]
        request["toolConfig"] = {"functionCallingConfig": {"mode": "VALIDATED"}}
    generation: dict[str, Any] = {}
    if isinstance(payload.get("max_output_tokens"), int):
        generation["maxOutputTokens"] = max(256, int(payload["max_output_tokens"]))
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        effort = str(reasoning.get("effort") or "").lower()
        if effort in {"minimal", "low", "medium", "high"}:
            generation["thinkingConfig"] = {"includeThoughts": True, "thinkingLevel": effort}
    if "thinkingConfig" not in generation:
        generation["thinkingConfig"] = {"includeThoughts": True, "thinkingBudget": -1}
    if generation:
        request["generationConfig"] = generation
    return request


def _gemini_to_responses_events(
    chunks: Iterable[dict[str, Any]],
    model: str,
    signatures: MutableMapping[str, str],
) -> Iterator[bytes]:
    response_id = "resp_" + uuid.uuid4().hex
    created_at = int(time.time())
    sequence = 0
    output: list[dict[str, Any]] = []
    message_id = "msg_" + uuid.uuid4().hex
    text_started = False
    message_output_index: int | None = None
    text_value = ""
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def emit(event_type: str, **values: Any) -> bytes:
        nonlocal sequence
        event = {"type": event_type, "sequence_number": sequence, **values}
        sequence += 1
        return _sse(event_type, event)

    yield emit("response.created", response=_response_object(response_id, model, created_at, "in_progress", [], usage))

    for wrapper in chunks:
        inner = wrapper.get("response") if isinstance(wrapper.get("response"), dict) else wrapper
        metadata = inner.get("usageMetadata") if isinstance(inner, dict) else None
        if isinstance(metadata, dict):
            cached_count = int(metadata.get("cachedContentTokenCount") or 0)
            input_count = int(metadata.get("promptTokenCount") or 0)
            output_count = int(metadata.get("candidatesTokenCount") or 0)
            thoughts_count = int(metadata.get("thoughtsTokenCount") or 0)
            total_count = int(metadata.get("totalTokenCount") or (input_count + output_count))
            usage = {
                "input_tokens": input_count,
                "input_tokens_details": {
                    "cached_tokens": cached_count,
                },
                "output_tokens": output_count,
                "output_tokens_details": {
                    "reasoning_tokens": thoughts_count,
                },
                "total_tokens": total_count,
            }
        candidates = inner.get("candidates") if isinstance(inner, dict) else None
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            content = candidate.get("content") if isinstance(candidate, dict) else None
            parts = content.get("parts") if isinstance(content, dict) else None
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict) or part.get("thought") is True:
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    if not text_started:
                        text_started = True
                        message_output_index = len(output)
                        item = {"id": message_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}
                        yield emit("response.output_item.added", output_index=message_output_index, item=item)
                        yield emit("response.content_part.added", item_id=message_id, output_index=message_output_index, content_index=0, part={"type": "output_text", "text": "", "annotations": []})
                    text_value += text
                    yield emit("response.output_text.delta", item_id=message_id, output_index=message_output_index, content_index=0, delta=text, logprobs=[])
                function_call = part.get("functionCall")
                if isinstance(function_call, dict):
                    call_id = str(function_call.get("id") or "call_" + uuid.uuid4().hex)
                    name = _safe_function_name(str(function_call.get("name") or "tool"))
                    arguments = json.dumps(function_call.get("args") or {}, ensure_ascii=False, separators=(",", ":"))
                    signature = str(part.get("thoughtSignature") or "").strip()
                    if signature:
                        signatures[call_id] = signature
                    index = len(output) + (1 if text_started else 0)
                    item = {"id": "fc_" + uuid.uuid4().hex, "type": "function_call", "status": "in_progress", "call_id": call_id, "name": name, "arguments": ""}
                    yield emit("response.output_item.added", output_index=index, item=item)
                    yield emit("response.function_call_arguments.delta", item_id=item["id"], output_index=index, delta=arguments)
                    item = {**item, "status": "completed", "arguments": arguments}
                    yield emit("response.function_call_arguments.done", item_id=item["id"], output_index=index, arguments=arguments)
                    yield emit("response.output_item.done", output_index=index, item=item)
                    output.append(item)
    if text_started:
        index = message_output_index if message_output_index is not None else len(output)
        yield emit("response.output_text.done", item_id=message_id, output_index=index, content_index=0, text=text_value, logprobs=[])
        part = {"type": "output_text", "text": text_value, "annotations": []}
        yield emit("response.content_part.done", item_id=message_id, output_index=index, content_index=0, part=part)
        message = {"id": message_id, "type": "message", "status": "completed", "role": "assistant", "content": [part]}
        yield emit("response.output_item.done", output_index=index, item=message)
        output.insert(index, message)
    response = _response_object(response_id, model, created_at, "completed", output, usage)
    yield emit("response.completed", response=response)
    yield b"data: [DONE]\n\n"


def _response_object(response_id: str, model: str, created_at: int, status: str, output: list[dict[str, Any]], usage: dict[str, int]) -> dict[str, Any]:
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


_DATA_IMAGE_RE = re.compile(r"^data:(image/(?:png|jpeg|jpg|webp));base64,(.+)$", re.DOTALL | re.IGNORECASE)


def _downscale_image_part(mime_type: str, b64_data: str, max_dimension: int = 2048) -> tuple[str, str]:
    try:
        import base64
        import io
        from PIL import Image

        raw_bytes = base64.b64decode(b64_data)
        with Image.open(io.BytesIO(raw_bytes)) as img:
            width, height = img.size
            if max(width, height) > max_dimension:
                scale = max_dimension / max(width, height)
                new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                resized = img.resize(new_size, Image.Resampling.LANCZOS)
            else:
                resized = img.copy()

            out_buf = io.BytesIO()
            norm_mime = mime_type.lower()
            if "png" in norm_mime:
                resized.save(out_buf, format="PNG", optimize=True)
                new_mime = "image/png"
            else:
                if resized.mode != "RGB":
                    resized = resized.convert("RGB")
                resized.save(out_buf, format="JPEG", quality=88, optimize=True)
                new_mime = "image/jpeg"
            new_b64 = base64.b64encode(out_buf.getvalue()).decode("ascii")
            return new_mime, new_b64
    except Exception:
        if len(b64_data) > 100_000:
            return mime_type, ""
        return mime_type, b64_data


def _extract_function_output(output: Any, call_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return {"result": output}, []
        output = parsed

    if isinstance(output, list):
        response_items: list[Any] = []
        media_parts: list[dict[str, Any]] = []
        for index, item in enumerate(output):
            if isinstance(item, dict) and item.get("type") == "input_image":
                url = str(item.get("image_url") or item.get("url") or "")
                match = _DATA_IMAGE_RE.match(url)
                if match:
                    raw_mime, raw_b64 = match.group(1), match.group(2)
                    mime, data = _downscale_image_part(raw_mime, raw_b64)
                    if data:
                        ext = "png" if "png" in mime else "jpg"
                        display_name = f"tool_{call_id}_{index}.{ext}"
                        media_parts.append({
                            "inlineData": {
                                "mimeType": mime,
                                "displayName": display_name,
                                "data": data,
                            }
                        })
                        response_items.append({
                            "type": "image",
                            "$ref": display_name,
                        })
                        continue
            response_items.append(item)
        return {"result": response_items}, media_parts

    if isinstance(output, dict):
        return {"result": output}, []
    return {"result": output}, []


def _message_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if not isinstance(content, list):
        return []
    parts: list[dict[str, Any]] = []
    for value in content:
        if not isinstance(value, dict):
            continue
        kind = str(value.get("type") or "input_text")
        if kind in {"input_text", "output_text", "text"} and isinstance(value.get("text"), str):
            parts.append({"text": value["text"]})
        elif kind == "input_image":
            url = str(value.get("image_url") or value.get("url") or "")
            if url.startswith("data:") and ";base64," in url:
                metadata, data = url[5:].split(";base64,", 1)
                mime = metadata or "image/png"
                mime, data = _downscale_image_part(mime, data)
                parts.append({"inlineData": {"mimeType": mime, "data": data}})
    return parts


def _text_parts(content: Any) -> list[dict[str, str]]:
    return [part for part in _message_parts(content) if "text" in part]


def _append_content(contents: list[dict[str, Any]], role: str, parts: list[dict[str, Any]]) -> None:
    if contents and contents[-1].get("role") == role:
        contents[-1]["parts"].extend(parts)
    else:
        contents.append({"role": role, "parts": list(parts)})


def _safe_function_name(value: str) -> str:
    safe = "".join(character if character.isalnum() or character in "_-" else "_" for character in value)
    return safe[:64] or "tool"


def _token_digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _parse_expiry(value: Any) -> float:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0
    try:
        numeric = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return numeric / 1000 if numeric > 10_000_000_000 else numeric


def _upstream_error(exc: urllib.error.HTTPError) -> GeminiAccountError:
    if exc.code in {401, 403}:
        return GeminiAccountError("Google account authorization was rejected", status=exc.code, code="google_authorization_failed")
    if exc.code == 429:
        return GeminiAccountError("Google account model quota is temporarily unavailable", status=429, code="google_quota_exhausted")
    return GeminiAccountError(f"Google account backend returned HTTP {exc.code}", status=502)


def _sse(event_type: str, payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {data}\n\n".encode("utf-8")


def _last_completed_event(events: list[bytes]) -> dict[str, Any]:
    for raw in reversed(events):
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if line.startswith("data: {"):
                value = json.loads(line[6:])
                if value.get("type") == "response.completed":
                    return value
    raise GeminiAccountError("Google account model returned no completed response")
