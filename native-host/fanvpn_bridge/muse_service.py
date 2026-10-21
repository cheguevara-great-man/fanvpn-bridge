"""Standalone, authenticated loopback service for the pinned Muse CLI transport.

Run explicitly with ``python -m fanvpn_bridge.muse_service --config PATH``.
Importing this module never opens a socket, reads cookies or loads Muse CLI.
"""
from __future__ import annotations

import argparse
import hmac
import json
import secrets
import select
import socket
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .deepseek_harness import _last_completed_response, _responses_events
from .gemini_web import _event_bytes
from .muse_auth import BrowserTicketClient, MuseAuthError
from .muse_tools import PROTOCOL, MailboxError, WorkspaceTools, response_frames

MAX_BODY = 2 * 1024 * 1024
MODEL = {"id": "default", "display_name": "Muse Web", "context_window": 32768,
         "thinking_levels": []}


class ServiceError(RuntimeError):
    def __init__(self, message: str, status: int = 502, code: str = "muse_web_error") -> None:
        super().__init__(message)
        self.status, self.code = status, code


@dataclass
class Session:
    session_id: str
    head: str = ""
    valid: bool = True
    pending: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


def gateway_factory(config: dict[str, Any]) -> Callable[[], Any]:
    """Only the sidecar imports optional native dependencies; never log errors verbatim."""
    from muse_cli.gateway import AuthError, Gateway, load_cookies

    browser = None if config.get("cookies_file") else BrowserTicketClient(
        bridge_url=config.get("bridge_url", "http://127.0.0.1:18888"), api_key=config["api_key"],
    )

    def connect():
        try:
            if browser is not None:
                # A fresh browser ticket per connection. No Cookie export,
                # on-disk tokens, expiry guesses or replay of an old login.
                ticket = browser.ticket()
                return Gateway("", vm_id=ticket["vm_id"], access_token=ticket["access_token"],
                               hatch_token=ticket["hatch_token"])
            cookies = load_cookies(config["cookies_file"])
            if not cookies.strip():
                raise ServiceError("Muse cookies file is empty", 401, "muse_web_auth_required")
            # Upstream pins TLS verification on its WebSocket. Never disable it.
            return Gateway(cookies, vm_id=config.get("vm_id"))
        except MuseAuthError as error:
            raise ServiceError(str(error), error.status, error.code) from None
        except AuthError:
            raise ServiceError("Muse authentication failed; renew the local login", 401,
                               "muse_web_auth_required") from None
        except ServiceError:
            raise
        except Exception:
            # Upstream exceptions can contain tokens, cookie paths or raw replies.
            raise ServiceError("Cannot connect to the Muse VM; check login and VM availability") from None

    return connect


class MuseSessions:
    def __init__(self, factory: Callable[[], Any], *, timeout: float = 600, poll: float = 2,
                 max_parallel: int = 4, max_snapshots: int = 128,
                 tool_workspace: str | None = None, result_timeout: float = 1800) -> None:
        self.factory, self.timeout, self.poll = factory, timeout, poll
        self.max_snapshots = max_snapshots
        self.node_id = secrets.token_hex(8)
        self.snapshots: OrderedDict[str, Session] = OrderedDict()
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(max_parallel)
        self.workspace = WorkspaceTools(tool_workspace) if tool_workspace else None
        self.result_timeout = result_timeout

    @staticmethod
    def history(gateway: Any, session_id: str, timeout: float) -> list[dict[str, Any]]:
        value = gateway.call_json("chat.history", body={"session_id": session_id, "limit": 50}, timeout=timeout)
        events = value.get("chat_events") if isinstance(value, dict) else None
        if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
            raise ServiceError("Muse returned an invalid conversation history")
        return events

    def generate(self, body: dict[str, Any], progress: Callable[[], None] = lambda: None) -> dict[str, Any]:
        tools = body.get("tools") or []
        results = body.get("tool_results") or []
        workspace_mode = body.get("tool_protocol") == PROTOCOL
        if (body.get("model") != "default" or not isinstance(body.get("input"), str)
                or not body["input"].strip() or body.get("reasoning")
                or (tools or results) and not workspace_mode):
            raise ServiceError("This Muse service accepts text-only default-model requests", 400)
        if workspace_mode:
            if self.workspace is None:
                raise ServiceError("Muse tool workspace is not configured; install the independent handoff skill first",
                                   503, "muse_web_tools_unavailable")
            if (not isinstance(tools, list) or not tools or any(not isinstance(tool, dict)
                    or tool.get("type") not in {"function", "custom"} or not isinstance(tool.get("name"), str)
                    or not tool["name"] for tool in tools)
                    or len({tool["name"] for tool in tools}) != len(tools) or not isinstance(results, list)):
                raise ServiceError("Invalid Muse workspace tools", 400)
        previous = body.get("previous_response_id")
        if previous is not None and (not isinstance(previous, str) or not previous):
            raise ServiceError("Invalid previous_response_id", 400)
        state = None
        if previous:
            with self.lock:
                state = self.snapshots.get(previous)
            if state is None or not state.valid or state.head != previous:
                raise ServiceError("Previous response not found; provide full history", 404,
                                   "muse_web_session_missing")
        if results and (state is None or state.pending is None):
            raise ServiceError("Tool results require the current pending Muse call", 409, "muse_web_tool_result_invalid")
        if not self.slots.acquire(blocking=False):
            raise ServiceError("Muse parallel request limit reached; retry later", 429, "muse_web_busy")
        gateway = None
        acquired = False
        submitted = False
        try:
            if state is not None:
                acquired = state.lock.acquire(blocking=False)
                if not acquired:
                    raise ServiceError("Muse conversation is already generating", 409, "muse_web_busy")
                # Recheck after taking the lock: another request may have completed.
                if not state.valid or state.head != previous:
                    raise ServiceError("Previous response not found; provide full history", 404,
                                       "muse_web_session_missing")
            previous_run = ""
            if state is not None and state.pending is not None:
                pending = state.pending
                expected_type = "custom_tool_call_output" if pending["custom"] else "function_call_output"
                if (not workspace_mode or time.monotonic() > pending["expires"] or tools != pending["tools"]
                        or len(results) != 1 or not isinstance(results[0], dict)
                        or set(results[0]) != {"type", "call_id", "output"}
                        or results[0].get("call_id") != pending["call_id"]
                        or results[0].get("type") != expected_type
                        or not isinstance(results[0].get("output"), (str, list))):
                    raise ServiceError("Expected the matching unexpired Muse tool result; no message sent", 409,
                                       "muse_web_tool_result_invalid")
                previous_run = pending["run"]
            progress()
            started = time.monotonic()
            deadline = started + self.timeout
            gateway = self.factory()
            if state is None:
                value = gateway.call_json("session.start", body={"method": "/api/session/start", "params": {
                    "origin": "fresh", "lifecycle": "persistent", "title": "Codex Muse " + secrets.token_hex(4),
                }}, timeout=min(30, self.timeout))
                session_id = (value.get("session_id") or value.get("id")) if isinstance(value, dict) else None
                if not isinstance(session_id, str) or not session_id:
                    raise ServiceError("Muse did not create an isolated conversation; no message sent")
                state = Session(session_id)
                acquired = state.lock.acquire(blocking=False)
            events = self.history(gateway, state.session_id, min(30, self.timeout))
            baseline = max((event.get("seq", 0) for event in events), default=0)
            run = ""
            prompt = body["input"]
            if workspace_mode:
                if previous_run:
                    # Mark ambiguous writes as submitted too: never overwrite/replay a result.
                    submitted = True
                    self.workspace.write(gateway, self.workspace.path(previous_run, "result"), {
                        "schema_version": 1, "run": previous_run, "result": results[0],
                    })
                    state.pending = None
                run = self.workspace.prepare(gateway, tools, max(1, deadline - time.monotonic()))
                prompt += "\n\n" + self.workspace.prompt(run, previous_run)
            progress()
            # A single consumer performs all RPCs on this connection. Never
            # automatically resend after this point: delivery may be ambiguous.
            submitted = True
            gateway._open("chat.stream", body={"session_id": state.session_id,
                          "node_id": self.node_id, "capabilities": {},
                          "items": [{"type": "text", "text": prompt}]})
            while time.monotonic() < deadline:
                progress()
                events = self.history(gateway, state.session_id, max(0.1, min(30, deadline - time.monotonic())))
                for event in sorted(events, key=lambda event: event.get("seq", 0)):
                    payload = event.get("payload")
                    if (event.get("event_name") != "message.assistant" or event.get("seq", 0) <= baseline
                            or not isinstance(payload, dict) or event.get("reply_to_message_id")
                            or payload.get("reply_to_message_id")):
                        continue
                    status = payload.get("status")
                    if status in {"failed", "cancelled", "error"}:
                        raise ServiceError("Muse generation did not complete", 502, "muse_web_generation_failed")
                    # Never use a quiet-DOM timeout or any truthy status as completion.
                    if status != "completed":
                        continue
                    text = payload.get("display_text") or payload.get("content")
                    call = self.workspace.request(gateway, run, tools) if workspace_mode else None
                    if not call and (not isinstance(text, str) or not text.strip()):
                        continue
                    progress()
                    response_id = "resp_muse_" + secrets.token_hex(16)
                    response = _last_completed_response(list(_responses_events(
                        "default", "" if call else text, [call] if call else [], response_id=response_id)))
                    response["usage"] = None  # Muse does not expose measured token usage here.
                    if call:
                        state.pending = {"run": run, "call_id": response["output"][0]["call_id"],
                                         "custom": call["freeform"], "tools": tools,
                                         "expires": time.monotonic() + self.result_timeout}
                    with self.lock:
                        state.head = response_id
                        self.snapshots[response_id] = state
                        while len(self.snapshots) > self.max_snapshots:
                            self.snapshots.popitem(last=False)
                    return response
                # Small interruptible waits keep disconnect detection responsive.
                until = min(deadline, time.monotonic() + self.poll)
                while time.monotonic() < until:
                    time.sleep(min(0.2, max(0, until - time.monotonic())))
                    progress()
            raise ServiceError("Muse response timed out; delivery may have occurred; check Muse before retrying", 504,
                               "muse_web_timeout")
        except ServiceError:
            if state is not None and submitted:
                state.valid = False
            raise
        except MailboxError:
            if state is not None and submitted:
                state.valid = False
            raise ServiceError("Muse workspace request is invalid; no tool was executed", 502,
                               "muse_web_tool_format_invalid") from None
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            if state is not None and submitted:
                state.valid = False
            raise
        except Exception:
            if state is not None and submitted:
                state.valid = False
            raise ServiceError("Muse protocol request failed; no automatic resend; check the Muse conversation", 502,
                               "muse_web_transport_error") from None
        finally:
            if gateway is not None:
                gateway.close()
            if acquired and state is not None:
                state.lock.release()
            self.slots.release()


def make_server(config: dict[str, Any], sessions: MuseSessions) -> ThreadingHTTPServer:
    token = config.get("api_key")
    if not isinstance(token, str) or len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("Muse local api_key must be at least 32 non-whitespace ASCII characters")

    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.0 close-delimits SSE; no implicit keepalive or chunk framing.
        def log_message(self, *_args):
            pass  # Never log Cookie, Authorization, prompt bodies or upstream exceptions.

        def send_json(self, status, value):
            raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def authorized(self):
            self.connection.settimeout(15)
            # No CORS; browser-origin requests cannot use the local service.
            supplied = self.headers.get("Authorization", "").encode("utf-8")
            if (self.headers.get("Origin") is not None
                    or not hmac.compare_digest(supplied, ("Bearer " + token).encode("ascii"))):
                self.send_json(401, {"error": {"code": "muse_web_auth_required"}})
                return False
            return True

        def do_GET(self):
            if self.authorized():
                if self.path == "/v1/models":
                    self.send_json(200, {"object": "list", "data": [MODEL]})
                else:
                    self.send_json(404, {"error": {"code": "not_found"}})

        def do_POST(self):
            if not self.authorized():
                return
            streaming = False
            headers_sent = False
            last_heartbeat = 0.0
            try:
                if self.path != "/v1/responses":
                    raise ServiceError("Unknown endpoint", 404)
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < length <= MAX_BODY:
                    raise ServiceError("Invalid request body length", 413)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ServiceError("Incomplete request body", 400)
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ServiceError("Expected a JSON object", 400)
                streaming = body.get("stream") is True

                def progress():
                    nonlocal last_heartbeat, headers_sent
                    readable, _, _ = select.select([self.connection], [], [], 0)
                    if readable and not self.connection.recv(1, socket.MSG_PEEK):
                        raise ConnectionAbortedError()
                    if streaming and time.monotonic() - last_heartbeat >= 5:
                        if not headers_sent:
                            self.send_response(200)
                            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                            self.send_header("Cache-Control", "no-cache")
                            self.end_headers()
                            headers_sent = True
                        self.wfile.write(b": muse waiting\n\n")
                        self.wfile.flush()
                        last_heartbeat = time.monotonic()

                result = sessions.generate(body, progress)
                if not streaming:
                    self.send_json(200, result)
                else:
                    progress()
                    # Authoritative text or structured handoff; not token streaming.
                    for frame in response_frames(result):
                        self.wfile.write(frame)
                    self.wfile.flush()
            except (ValueError, UnicodeError):
                self.send_json(400, {"error": {"code": "invalid_json"}})
            except ServiceError as error:
                value = {"error": {"message": str(error), "code": error.code}}
                if not headers_sent:
                    self.send_json(error.status, value)
                else:
                    event = {"type": "error", **value["error"]}
                    self.wfile.write(("event: error\ndata: " + json.dumps(event) + "\n\n").encode())
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", config.get("port", 8008)), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--tool-workspace", help="Explicit independent Muse handoff skill directory")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if (not isinstance(config, dict) or not isinstance(config.get("api_key"), str)
            or ("cookies_file" in config and not isinstance(config["cookies_file"], str))):
        raise SystemExit("Muse config requires a local api_key; browser login is the default")
    sessions = MuseSessions(gateway_factory(config), tool_workspace=args.tool_workspace or config.get("tool_workspace"))
    server = make_server(config, sessions)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
