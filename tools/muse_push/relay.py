"""Authenticated tool-message relay. Never executes commands or persists payloads.

The PC keeps a WebSocket open. Each cloud helper makes ONE HTTPS call whose
response waits for the actual PC result. There is no request/result file polling.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import hmac
import json
from pathlib import Path
import secrets
import ssl
import time

from aiohttp import WSMsgType, web

MAX_BYTES = 2 * 1024 * 1024
MAX_ROOMS = 32
MAX_CALLS = 64
PROTOCOL = "codex-pc-rpc-v1"


def authorized(request, token):
    return hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token)


@dataclass
class Room:
    run: str
    expires: float
    pc_token: str = field(default_factory=lambda: secrets.token_hex(32), repr=False)
    cloud_token: str = field(default_factory=lambda: secrets.token_hex(32), repr=False)
    socket: object = field(default=None, repr=False)
    sequence: int = 1
    pending: object = field(default=None, repr=False)
    request_id: str = ""
    seen: set = field(default_factory=set, repr=False)
    valid: bool = True

    def abort(self):
        self.valid = False
        if self.pending is not None and not self.pending.done():
            self.pending.set_exception(ConnectionError("Tool exchange ended; no replay"))


def make_app(admin_token, refresh_certificate=None):
    if not isinstance(admin_token, str) or len(admin_token) < 32:
        raise ValueError("A protected relay administrator token is required")
    rooms = {}

    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return web.json_response({"error": "invalid_request"}, status=400)
        except (ConnectionError, TimeoutError):
            return web.json_response({"error": "exchange_ended_no_replay"}, status=409)

    app = web.Application(client_max_size=MAX_BYTES, middlewares=[errors])

    async def cleanup_once():
        for run, room in list(rooms.items()):
            if room.expires <= time.time() or not room.valid:
                room.abort()
                if room.socket is not None:
                    await room.socket.close()
                rooms.pop(run, None)

    async def create(request):
        if not authorized(request, admin_token):
            raise web.HTTPUnauthorized()
        body = await request.json()
        run, expires = body["run"], body["expires_at"]
        if (not isinstance(run, str) or len(run) != 32 or any(c not in "0123456789abcdef" for c in run)
                or type(expires) not in (int, float) or not time.time() < expires <= time.time() + 2401):
            raise ValueError()
        await cleanup_once()
        if run in rooms:
            raise web.HTTPConflict()
        if len(rooms) >= MAX_ROOMS:
            raise web.HTTPTooManyRequests()
        room = Room(run, expires)
        rooms[run] = room
        return web.json_response({"pc_token": room.pc_token, "cloud_token": room.cloud_token})

    def get_room(request, role):
        room = rooms.get(request.match_info["run"])
        if room is None or not authorized(request, getattr(room, role + "_token")):
            raise web.HTTPUnauthorized()
        if not room.valid or room.expires <= time.time():
            raise web.HTTPGone()
        return room

    async def pc(request):
        room = get_room(request, "pc")
        if room.socket is not None:
            raise web.HTTPConflict()
        socket = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_BYTES)
        await socket.prepare(request)
        room.socket = socket
        try:
            await socket.send_json({"type": "ready"})
            async for event in socket:
                if event.type != WSMsgType.TEXT:
                    break
                body = json.loads(event.data)
                if body.get("type") == "close":
                    break
                if (body.get("type") != "result" or body.get("request_id") != room.request_id
                        or type(body.get("sequence")) is not int or body["sequence"] != room.sequence
                        or room.pending is None or room.pending.done()):
                    raise ValueError()
                sequence = room.sequence
                room.pending.set_result(body["result"])
                # Acknowledge publication, not cloud receipt or execution status.
                await socket.send_json({"type": "result_accepted", "sequence": sequence})
        finally:
            room.abort()
            await socket.close()
        return socket

    async def call(request):
        room = get_room(request, "cloud")
        body = await request.json()
        request_id, name, expires = body["request_id"], body["name"], body["expires_at"]
        field_name = "input" if "input" in body else "arguments"
        if (set(body) - {"sent_at_ms"} != {"request_id", "name", "expires_at", field_name}
                or not isinstance(request_id, str) or len(request_id) != 32
                or any(c not in "0123456789abcdef" for c in request_id)
                or not isinstance(name, str) or not 0 < len(name) <= 256
                or not isinstance(body[field_name], str if field_name == "input" else dict)
                or type(expires) not in (int, float) or not time.time() < expires <= room.expires
                or expires > time.time() + 1801):
            raise ValueError()
        if room.socket is None or room.socket.closed:
            raise web.HTTPServiceUnavailable()
        if room.pending is not None or room.sequence > MAX_CALLS or request_id in room.seen:
            raise web.HTTPConflict()
        room.seen.add(request_id)
        room.request_id = request_id
        room.pending = asyncio.get_running_loop().create_future()
        pending = room.pending
        try:
            await room.socket.send_json({"type": "call", "request_id": request_id, "request": {
                "schema_version": 1, "protocol": PROTOCOL, "run": room.run,
                "sequence": room.sequence, "expires_at": expires, "name": name,
                field_name: body[field_name],
            }, "relay_received_ms": time.time_ns() / 1_000_000, "cloud_sent_ms": body.get("sent_at_ms")})
            result = await asyncio.wait_for(pending, max(0.001, expires - time.time()))
            # The request is consumed exactly once; no retry/fallback endpoint.
            return web.json_response({"result": result})
        except BaseException:
            room.abort()
            if not room.socket.closed:
                await room.socket.send_json({"type": "cancel", "request_id": request_id})
            raise
        finally:
            # Advance before another cloud call can enter. Failed rooms stay invalid.
            if pending.done() and not pending.cancelled():
                pending.exception()  # Consume an abort exception, if any.
            else:
                pending.cancel()
            room.pending = None
            room.sequence += 1

    async def lifespan(app):
        async def sweep():
            while True:
                await asyncio.sleep(10)
                await cleanup_once()
                if refresh_certificate is not None:
                    refresh_certificate()
        task = asyncio.create_task(sweep())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for room in rooms.values():
            room.abort()
            if room.socket is not None:
                await room.socket.close()

    app.cleanup_ctx.append(lifespan)
    async def health(request):
        return web.json_response({"status": "ok", "protocol": "muse-push-v1",
                                  "server_time_ms": time.time_ns() / 1_000_000})

    app.router.add_get("/health", health)
    app.router.add_post("/rooms", create)
    app.router.add_get("/rooms/{run}/pc", pc)
    app.router.add_post("/rooms/{run}/call", call)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config["cert_file"], config["key_file"])
    certificate_version = Path(config["cert_file"]).stat().st_mtime_ns

    def refresh():
        nonlocal certificate_version
        try:
            version = Path(config["cert_file"]).stat().st_mtime_ns
            if version != certificate_version:
                context.load_cert_chain(config["cert_file"], config["key_file"])
                certificate_version = version
        except (OSError, ssl.SSLError):
            return  # Keep the active certificate; never interrupt existing calls.

    web.run_app(make_app(config["admin_token"], refresh), host=config.get("host", "0.0.0.0"),
                port=config.get("port", 19445), ssl_context=context, access_log=None,
                handler_cancellation=True, print=None)


if __name__ == "__main__":
    main()
