"""Muse-specific workspace mailbox, never a Codex tool executor."""
from __future__ import annotations

import base64
from collections import OrderedDict
import hashlib
import json
import re
import secrets
import threading
import time

from .deepseek_harness import _responses_events
from .gemini_web import _event_bytes

MAX_BYTES = 2 * 1024 * 1024
# Base64 plus the RPC/Noise envelopes must fit a 65535-byte encrypted frame.
FILE_CHUNK_BYTES = 24 * 1024
PROTOCOL = "workspace-v1"
CATALOG_SCHEMA_VERSION = 2


class MailboxError(ValueError):
    pass


class MailboxCompatibilityError(MailboxError):
    pass


class WorkspaceTools:
    def __init__(self, root):
        if not isinstance(root, str) or not re.fullmatch(r"workspace/custom-skills/[a-z][a-z0-9_]{0,63}", root):
            raise ValueError("Invalid Muse tool workspace path")
        self.root = root
        self._catalog_lock = threading.Lock()
        self._verified_catalogs = OrderedDict()

    @staticmethod
    def missing(error):
        payload = getattr(error, "payload", None)
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except (ValueError, UnicodeError):
                payload = None
        detail = payload.get("error", {}) if isinstance(payload, dict) else {}
        return (getattr(error, "status", None) == 404 or
                isinstance(detail, dict) and detail.get("code") == "not_found")

    def read_bytes(self, gateway, path, *, optional=False):
        raw = bytearray()
        while True:
            length = min(FILE_CHUNK_BYTES, MAX_BYTES + 1 - len(raw))
            try:
                value = gateway.call_json("fs.read", body={"path": path, "offset": len(raw), "len": length}, timeout=30)
            except Exception as error:
                # A file disappearing mid-read is not an absent optional request.
                if optional and not raw and self.missing(error):
                    return None
                raise
            try:
                if not isinstance(value, dict) or type(value.get("eof")) is not bool:
                    raise ValueError()
                chunk = (base64.b64decode(value["data_base64"], validate=True) if "data_base64" in value
                         else value["text"].encode("utf-8"))
            except (ValueError, KeyError, TypeError, AttributeError):
                raise MailboxError("Invalid Muse mailbox read") from None
            raw.extend(chunk)
            if len(chunk) > length or len(raw) > MAX_BYTES or not chunk and not value["eof"]:
                raise MailboxError("Incomplete or oversized Muse mailbox")
            if value["eof"]:
                return bytes(raw)

    def read(self, gateway, path, *, optional=False):
        raw = self.read_bytes(gateway, path, optional=optional)
        if raw is None:
            return None
        try:
            return json.loads(raw, parse_constant=_reject_constant)
        except (ValueError, UnicodeError) as error:
            raise MailboxError("Invalid Muse mailbox JSON") from error

    def write(self, gateway, path, value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.write_bytes(gateway, path, raw)

    def write_bytes(self, gateway, path, raw):
        if len(raw) > MAX_BYTES:
            raise MailboxError("Muse mailbox exceeds size limit")
        # Temp + rename: the helper must never see a partially uploaded manifest/result.
        temporary = path + "." + secrets.token_hex(8) + ".tmp"
        for offset in range(0, max(1, len(raw)), FILE_CHUNK_BYTES):
            chunk = raw[offset:offset + FILE_CHUNK_BYTES]
            gateway.call_json("fs.write", body={"path": temporary, "data_base64": base64.b64encode(chunk).decode("ascii"),
                              "overwrite": False, "append": offset > 0, "create_parent": True}, timeout=30)
        gateway.call_json("fs.rename", body={"source": temporary, "destination": path,
                          "create_parent": True, "overwrite": False}, timeout=30)

    def check_compatible(self, gateway):
        source = self.read_bytes(gateway, self.root + "/scripts/mailbox.py", optional=True)
        marker = f"CATALOG_SCHEMA_VERSION = {CATALOG_SCHEMA_VERSION}".encode("ascii")
        if source is None or marker not in source.splitlines():
            raise MailboxCompatibilityError("Muse handoff helper needs catalog-reference support")

    def catalog(self, gateway, tools):
        raw = json.dumps({"schema_version": 1, "tools": tools}, ensure_ascii=False,
                         allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise MailboxError("Muse tool catalog exceeds size limit")
        digest = hashlib.sha256(raw).hexdigest()
        path = f"{self.root}/catalogs/{digest}.json"
        # A VM identifier scopes the verification cache to its actual filesystem.
        vm_id = getattr(gateway, "vm_id", None)
        key = (vm_id, digest) if isinstance(vm_id, str) and vm_id else None
        with self._catalog_lock:
            try:
                gateway.call_json("fs.stat", body={"path": path}, timeout=30)
            except Exception as error:
                if not self.missing(error):
                    raise
                if key is not None:
                    self._verified_catalogs.pop(key, None)
                self.write_bytes(gateway, path, raw)
            if key is None or key not in self._verified_catalogs:
                existing = self.read_bytes(gateway, path)
                if existing != raw:
                    raise MailboxError("Muse cached tool catalog does not match its content hash")
            if key is not None:
                self._verified_catalogs[key] = True
                self._verified_catalogs.move_to_end(key)
                while len(self._verified_catalogs) > 128:
                    self._verified_catalogs.popitem(last=False)
        return digest

    def prepare(self, gateway, tools, timeout):
        digest = self.catalog(gateway, tools)
        run = secrets.token_hex(16)
        self.write(gateway, self.path(run, "manifest"), {"schema_version": CATALOG_SCHEMA_VERSION, "run": run,
                   "expires_at": time.time() + timeout, "catalog_sha256": digest})
        return run

    def path(self, run, kind):
        if not re.fullmatch(r"[0-9a-f]{32}", run) or kind not in {"manifest", "request", "result"}:
            raise MailboxError("Invalid mailbox identity")
        return f"{self.root}/runs/{run}/{kind}.json"

    def prompt(self, run, previous_run=""):
        helper = "python3 ~/" + self.root + "/scripts/mailbox.py"
        result = (f"First read Codex's actual tool result: {helper} result --run {previous_run}\n"
                  if previous_run else "")
        return ("CODEX WORKSPACE TOOL HANDOFF\n"
                f"Read ~/{self.root}/SKILL.md using muse.read and follow it.\n" + result +
                f"Current run: {run}. Discover supplied tools: {helper} tools --run {run}\n"
                "Read a tool definition with --name before requesting it. Submit at most one request via the helper, "
                "then end this turn immediately. Codex alone executes/approves its tools. Do not execute these actions "
                "with cloud tools or muse-bridge, and never fabricate results or XML calls. "
                "For large edits split at complete semantic boundaries without shortening the intended content. "
                "When no action is needed answer normally.")

    def request(self, gateway, run, tools):
        value = self.read(gateway, self.path(run, "request"), optional=True)
        if value is None:
            return None
        if not isinstance(value, dict) or value.get("run") != run or value.get("schema_version") != 1:
            raise MailboxError("Invalid Muse tool request identity")
        definition = next((tool for tool in tools if tool["name"] == value.get("name")), None)
        if definition is None:
            raise MailboxError("Muse requested an unavailable tool")
        custom = definition["type"] == "custom"
        field = "input" if custom else "arguments"
        if set(value) != {"schema_version", "run", "name", field} or not isinstance(value[field], str if custom else dict):
            raise MailboxError("Muse tool request has invalid arguments; no repair attempted")
        return {"name": value["name"], field: value[field], "freeform": custom}


def _reject_constant(_value):
    raise ValueError("Non-JSON numeric constant")


def response_frames(response):
    """Project authoritative structured output; preserve call IDs across every frame."""
    calls, text = [], ""
    for item in response["output"]:
        if item["type"] == "message":
            text += "".join(part["text"] for part in item["content"] if part["type"] == "output_text")
        else:
            custom = item["type"] == "custom_tool_call"
            calls.append({"name": item["name"], "freeform": custom,
                          **({"input": item["input"]} if custom else {"arguments": json.loads(item["arguments"])})})
    frames = _responses_events(response["model"], text, calls, response_id=response["id"], created_at=response["created_at"])
    # The shared projector generates fresh IDs; map them back to the committed output.
    identifiers = {}
    for frame in frames:
        if frame == b"data: [DONE]\n\n":
            yield frame
            continue
        event = json.loads(frame.decode("utf-8").split("data: ", 1)[1])
        index = event.get("output_index")
        item = event.get("item")
        if isinstance(item, dict) and isinstance(index, int):
            original = response["output"][index]
            for key in ("id", "call_id"):
                if key in item:
                    identifiers[item[key]] = original[key]
                    item[key] = original[key]
            if original.get("namespace"):
                item["namespace"] = original["namespace"]
        if event.get("item_id") in identifiers:
            event["item_id"] = identifiers[event["item_id"]]
        if isinstance(event.get("response"), dict):
            if event["type"] == "response.completed":
                event["response"] = response
            else:
                event["response"]["usage"] = None
        yield _event_bytes(event)
