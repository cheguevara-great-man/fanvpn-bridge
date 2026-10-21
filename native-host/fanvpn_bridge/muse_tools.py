"""Muse-specific workspace mailbox, never a Codex tool executor."""
from __future__ import annotations

import base64
import json
import re
import secrets
import time

from .deepseek_harness import _responses_events
from .gemini_web import _event_bytes

MAX_BYTES = 2 * 1024 * 1024
PROTOCOL = "workspace-v1"


class MailboxError(ValueError):
    pass


class WorkspaceTools:
    def __init__(self, root):
        if not isinstance(root, str) or not re.fullmatch(r"workspace/custom-skills/[a-z][a-z0-9_]{0,63}", root):
            raise ValueError("Invalid Muse tool workspace path")
        self.root = root

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
        try:
            value = gateway.call_json("fs.read", body={"path": path, "offset": 0, "len": MAX_BYTES + 1}, timeout=30)
        except Exception as error:
            # Only explicit not-found is optional, never a permission/transport error.
            if optional and self.missing(error):
                return None
            raise
        if not isinstance(value, dict):
            raise MailboxError("Invalid Muse mailbox read")
        raw = (base64.b64decode(value["data_base64"], validate=True) if "data_base64" in value
               else value.get("text", "").encode("utf-8"))
        if len(raw) > MAX_BYTES or value.get("eof") is not True:
            raise MailboxError("Incomplete or oversized Muse mailbox")
        return raw

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
        if len(raw) > MAX_BYTES:
            raise MailboxError("Muse mailbox exceeds size limit")
        # Temp + rename: the helper must never see a partially uploaded manifest/result.
        temporary = path + "." + secrets.token_hex(8) + ".tmp"
        gateway.call_json("fs.write", body={"path": temporary, "data_base64": base64.b64encode(raw).decode("ascii"),
                          "overwrite": False, "append": False, "create_parent": True}, timeout=30)
        gateway.call_json("fs.rename", body={"source": temporary, "destination": path,
                          "create_parent": True, "overwrite": False}, timeout=30)

    def prepare(self, gateway, tools, timeout):
        run = secrets.token_hex(16)
        self.write(gateway, self.path(run, "manifest"), {"schema_version": 1, "run": run,
                   "expires_at": time.time() + timeout, "tools": tools})
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
