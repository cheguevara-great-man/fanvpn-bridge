"""Offline tests against the optional pinned client: no account or socket used."""
from __future__ import annotations

import base64
import json
import os
import threading
import unittest

from fanvpn_bridge.muse_tools import WorkspaceTools
from test_muse_web import FakeMuse, TOOLS

try:
    from muse_cli.gateway import (Gateway, NoiseTransportFrame, ServiceFrame,
                                  ServiceRequest, ServiceResponse)
    from noise.connection import Keypair, NoiseConnection
    from noise.exceptions import NoiseInvalidMessage
except ImportError:
    Gateway = None


@unittest.skipIf(Gateway is None, "Run with dist-muse-web Python for the pinned Noise client")
class MuseNoiseTransportTests(unittest.TestCase):
    def setUp(self):
        sender = NoiseConnection.from_name(b"Noise_XX_25519_AESGCM_SHA256")
        receiver = NoiseConnection.from_name(b"Noise_XX_25519_AESGCM_SHA256")
        sender.set_as_initiator()
        receiver.set_as_responder()
        for connection in (sender, receiver):
            connection.set_keypair_from_private_bytes(Keypair.STATIC, os.urandom(32))
            connection.start_handshake()
        receiver.read_message(bytes(sender.write_message()))
        sender.read_message(bytes(receiver.write_message()))
        receiver.read_message(bytes(sender.write_message()))
        self.assertTrue(sender.handshake_finished and receiver.handshake_finished)
        self.requests, self.sizes = [], []
        owner = self

        class Wire:
            response = None

            def send_bytes(self, raw):
                owner.sizes.append(len(raw))
                frame = NoiseTransportFrame.FromString(bytes(receiver.decrypt(raw)))
                request = ServiceRequest.FromString(frame.payload)
                request = ServiceFrame.FromString(request.payload)
                owner.requests.append(json.loads(request.request.body))

            def recv(self):
                return self.response, 0

        self.wire = Wire()
        self.client = Gateway.__new__(Gateway)
        self.client.noise, self.client.ws, self.client.stream = sender, self.wire, 1
        self.client._send_lock = threading.Lock()
        self.client._recv_lock = threading.Lock()
        self.fake = FakeMuse()
        backend = self.fake.connect()

        class Loopback:
            vm_id = "offline-vm"

            def call_json(self, method, *, body, timeout):
                stream = owner.client._open(method, body=body)
                decoded = owner.requests[-1]
                owner.assertEqual(decoded, body)
                # Only the filesystem response is simulated; framing/encryption
                # in both directions uses the actual optional client/Noise code.
                value = backend.call_json(method, body=decoded, timeout=timeout)
                response = ServiceFrame(stream_id=stream)
                response.response.status = 200
                response.response.body = json.dumps(value).encode()
                response.response.end_body = True
                outer = ServiceResponse(payload=response.SerializeToString())
                frame = NoiseTransportFrame(chunk_id=1, chunk_index=0, total_chunks=1,
                                            payload=outer.SerializeToString())
                raw = bytes(receiver.encrypt(frame.SerializeToString()))
                owner.sizes.append(len(raw))
                owner.wire.response = raw
                decoded = owner.client._read_frame()
                return json.loads(decoded.response.body)

        self.gateway = Loopback()

    def test_original_single_upload_exceeds_noise_limit(self):
        with self.assertRaises(NoiseInvalidMessage):
            self.client._open("fs.write", body={"path": "unused", "data_base64":
                                               base64.b64encode(b"x" * 70000).decode()})
        self.assertEqual(self.requests, [])

    def test_large_catalog_and_results_roundtrip_and_cache_without_oversized_frame(self):
        workspace = WorkspaceTools("workspace/custom-skills/offline_test")
        tools = [{**TOOLS[0], "description": "中文 `tool`\n" * 6000}]
        first = workspace.prepare(self.gateway, tools, 60)
        second = workspace.prepare(self.gateway, tools, 60)
        self.assertNotEqual(first, second)
        catalog_uploads = [body for method, body in self.fake.rpcs
                           if method == "fs.write" and "/catalogs/" in body["path"] and not body["append"]]
        self.assertEqual(len(catalog_uploads), 1)
        value = {"run": first, "output": "中文 `result`\n" * 8000}
        path = workspace.path(first, "result")
        workspace.write(self.gateway, path, value)
        self.assertEqual(workspace.read(self.gateway, path), value)
        self.assertLess(max(self.sizes), 65535)


if __name__ == "__main__":
    unittest.main()
