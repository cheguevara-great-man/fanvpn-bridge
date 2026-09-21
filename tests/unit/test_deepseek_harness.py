from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fanvpn_bridge.deepseek_harness import (
    DeepSeekHarnessError,
    DeepSeekHarnessProvider,
    _deepseek_response_message_id,
    _encode_pow_response,
    _extract_deepseek_images,
    _looks_like_tool_call_attempt,
    _parse_deepseek_stream,
    _parse_tool_calls,
    _responses_events,
    _responses_to_deepseek_prompt,
)


class DeepSeekHarnessTests(unittest.TestCase):
    def test_models_are_exposed_under_a_separate_prefix(self) -> None:
        provider = DeepSeekHarnessProvider(pow_solver=lambda _challenge: 0)
        models = provider.models_response()["data"]
        ids = [item["id"] for item in models]
        self.assertEqual(ids, ["deepseek-web/chat", "deepseek-web/reasoner"])
        self.assertEqual(models[0]["context_window"], 1_000_000)
        self.assertEqual(models[0]["auto_compact_token_limit"], 900_000)
        self.assertEqual(models[0]["input_modalities"], ["text", "image"])

    def test_responses_prompt_keeps_codex_as_tool_executor(self) -> None:
        prompt = _responses_to_deepseek_prompt(
            {
                "instructions": "Follow the repository rules.",
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "inspect it"}]},
                    {"type": "function_call", "call_id": "call_old", "name": "read_file", "arguments": "{\"path\":\"a.py\"}"},
                    {"type": "function_call_output", "call_id": "call_old", "output": "contents"},
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "read_file",
                        "description": "Read one file",
                        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                    },
                    {
                        "type": "function",
                        "name": "exec_command",
                        "description": "Run a command",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "cmd": {"type": "string"},
                                "workdir": {"type": "string"},
                                "max_output_tokens": {"type": "integer"},
                            },
                            "required": ["cmd"],
                        },
                    },
                ],
            }
        )
        self.assertIn("Follow the repository rules.", prompt)
        self.assertIn("USER:\ninspect it", prompt)
        self.assertIn("TOOL RESULT (call_old):\ncontents", prompt)
        self.assertIn("Codex, not you, executes tools", prompt)
        self.assertIn("### Tool read_file", prompt)
        self.assertIn("### Tool exec_command", prompt)
        self.assertIn("<read_file>\n{\"path\":\"path/to/file\"}\n</read_file>", prompt)
        self.assertIn("<exec_command>\n", prompt)
        self.assertIn('"cmd":"Get-Content a.txt"', prompt)
        self.assertIn("Parameters JSON Schema", prompt)
        self.assertIn("This direct per-tool XML format is the only valid tool-call syntax", prompt)
        self.assertIn("Every opening tag MUST be closed by the exact matching closing tag", prompt)
        self.assertIn("never switch to a different tool-call syntax or closing delimiter", prompt)
        self.assertNotIn("<codex_tool_call>", prompt)
        self.assertIn("Do not wrap arguments in `name`, `arguments`, or `tool`", prompt)

    def test_continuation_prompt_repeats_short_valid_tool_tag_reminder(self) -> None:
        prompt = _responses_to_deepseek_prompt(
            {
                "instructions": "Follow the repository rules.",
                "input": [{"type": "message", "role": "user", "content": "continue"}],
                "tools": [
                    {
                        "type": "function",
                        "name": "exec_command",
                        "description": "Run a command",
                        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
                    },
                    {
                        "type": "function",
                        "name": "write_stdin",
                        "description": "Continue a command",
                        "parameters": {"type": "object", "properties": {"session_id": {"type": "integer"}}},
                    },
                ],
            },
            include_control=False,
        )
        self.assertIn("USER:\ncontinue", prompt)
        self.assertIn("CODEX TOOL FORMAT REMINDER", prompt)
        self.assertIn("<exec_command>...</exec_command>", prompt)
        self.assertIn("<write_stdin>...</write_stdin>", prompt)
        self.assertIn("Copy the matching closing tag exactly as shown", prompt)
        self.assertIn("do not substitute another tool-call syntax or closing delimiter", prompt)
        self.assertNotIn("Parameters JSON Schema", prompt)
        self.assertNotIn("Follow the repository rules.", prompt)

    def test_prompt_omits_image_base64_from_tool_results(self) -> None:
        payload = "data:image/png;base64," + ("A" * 100_000)
        prompt = _responses_to_deepseek_prompt({
            "input": [
                {"type": "message", "role": "user", "content": "inspect image"},
                {
                    "type": "function_call_output",
                    "call_id": "call_image",
                    "output": [{"type": "input_image", "image_url": payload}],
                },
            ]
        })
        self.assertIn("[Image payload omitted: input_image]", prompt)
        self.assertNotIn("data:image/png;base64", prompt)
        self.assertNotIn("A" * 1000, prompt)

    def test_prompt_redacts_embedded_data_uri_inside_plain_tool_text(self) -> None:
        payload = "data:image/jpeg;base64," + ("B" * 20_000)
        prompt = _responses_to_deepseek_prompt({
            "input": [{
                "type": "function_call_output",
                "call_id": "call_image_text",
                "output": f'preview={{"image_url":"{payload}"}}',
            }]
        })
        self.assertIn("[Binary data URI omitted: image/jpeg]", prompt)
        self.assertNotIn("data:image/jpeg;base64", prompt)
        self.assertNotIn("B" * 1000, prompt)

    def test_prompt_keeps_large_text_tool_result_intact(self) -> None:
        output = "HEAD:" + ("x" * 40_000) + ":TAIL"
        prompt = _responses_to_deepseek_prompt({
            "input": [
                {"type": "message", "role": "user", "content": "continue"},
                {"type": "function_call_output", "call_id": "call_big", "output": output},
            ]
        })
        self.assertIn(output, prompt)
        self.assertNotIn("chars omitted", prompt)

    def test_deepseek_stream_separates_thinking_from_answer(self) -> None:
        raw = (
            'data: {"p":"response/fragments","o":"APPEND","v":'
            '[{"type":"THINK","content":"private"},{"type":"RESPONSE","content":"hello"}]}\n\n'
            'data: {"p":"response/fragments/-1/content","v":" world"}\n\n'
            'data: {"p":"response/status","v":"FINISHED"}\n\n'
        ).encode()
        text, reasoning = _parse_deepseek_stream(raw)
        self.assertEqual(text, "hello world")
        self.assertEqual(reasoning, "private")

    def test_deepseek_stream_exposes_parent_message_id_for_continuation(self) -> None:
        raw = (
            'data: {"p":"response/message_id","v":"message-123"}\n\n'
            'data: {"p":"response/status","v":"FINISHED"}\n\n'
        ).encode()
        self.assertEqual(_deepseek_response_message_id(raw), "message-123")

    def test_deepseek_stream_finds_nested_response_message_id(self) -> None:
        raw = (
            'data: {"o":"BATCH","v":[{"p":"meta/response_message_id","v":42}]}\n\n'
            'data: {"p":"response/status","v":"FINISHED"}\n\n'
        ).encode()
        self.assertEqual(_deepseek_response_message_id(raw), 42)

    def test_direct_tool_blocks_are_the_only_accepted_tool_syntax(self) -> None:
        text = '<read_file>{"path":"a.py"}</read_file>'
        calls = _parse_tool_calls(text, {"read_file"})
        self.assertEqual(calls, [{"name": "read_file", "arguments": {"path": "a.py"}}])
        mixed = "I will inspect it first.\n\n" + text
        self.assertIsNone(_parse_tool_calls(mixed, {"read_file"}))
        self.assertTrue(_looks_like_tool_call_attempt(mixed, {"read_file"}))
        self.assertIsNone(_parse_tool_calls(text, {"different_tool"}))

    def test_direct_per_tool_tags_become_function_calls(self) -> None:
        text = (
            '<exec_command>{"cmd":"Get-Content a.txt","max_output_tokens":3000}</exec_command>\n'
            '<write_stdin>{"session_id":12,"chars":"y\\n"}</write_stdin>'
        )
        self.assertEqual(
            _parse_tool_calls(text, {"exec_command", "write_stdin"}),
            [
                {"name": "exec_command", "arguments": {"cmd": "Get-Content a.txt", "max_output_tokens": 3000}},
                {"name": "write_stdin", "arguments": {"session_id": 12, "chars": "y\n"}},
            ],
        )

    def test_direct_tool_tag_requires_valid_json_object(self) -> None:
        broken_json = '<exec_command>{"cmd":BROKEN}</exec_command>'
        wrong_shape = '<exec_command>["not-an-object"]</exec_command>'
        self.assertIsNone(_parse_tool_calls(broken_json, {"exec_command"}))
        self.assertIsNone(_parse_tool_calls(wrong_shape, {"exec_command"}))
        self.assertTrue(_looks_like_tool_call_attempt(broken_json, {"exec_command"}))
        self.assertTrue(_looks_like_tool_call_attempt(wrong_shape, {"exec_command"}))

    def test_one_valid_block_followed_by_broken_block_rejects_whole_response(self) -> None:
        text = (
            '<exec_command>{"cmd":"ok"}</exec_command>\n'
            '<exec_command>{"cmd":"broken"}'
        )
        self.assertIsNone(_parse_tool_calls(text, {"exec_command"}))
        self.assertTrue(_looks_like_tool_call_attempt(text, {"exec_command"}))

    def test_old_or_alternate_tool_syntax_is_not_accepted(self) -> None:
        legacy = '<codex_tool_call>{"name":"read_file","arguments":{"path":"a.py"}}</codex_tool_call>'
        dsml = (
            '<|DSML| invoke name="read_file">'
            '<|DSML| parameter name="path" string="true">a.py</|DSML| parameter>'
            '</|DSML| invoke>'
        )
        self.assertIsNone(_parse_tool_calls(legacy, {"read_file"}))
        self.assertIsNone(_parse_tool_calls(dsml, {"read_file"}))
        self.assertTrue(_looks_like_tool_call_attempt(legacy, {"read_file"}))
        self.assertTrue(_looks_like_tool_call_attempt(dsml, {"read_file"}))

    def test_pow_response_matches_deepseek_web_shape(self) -> None:
        encoded = _encode_pow_response(
            {
                "algorithm": "DeepSeekHashV1",
                "challenge": "a" * 64,
                "salt": "salt",
                "signature": "signature",
                "expireAt": 123,
            },
            42,
        )
        value = json.loads(base64.b64decode(encoded))
        self.assertEqual(value["answer"], 42)
        self.assertEqual(value["target_path"], "/api/v0/chat/completion")

        upload_encoded = _encode_pow_response(
            {
                "algorithm": "DeepSeekHashV1",
                "challenge": "a" * 64,
                "salt": "salt",
                "signature": "signature",
            },
            7,
            target_path="/api/v0/file/upload_file",
        )
        upload_value = json.loads(base64.b64decode(upload_encoded))
        self.assertEqual(upload_value["target_path"], "/api/v0/file/upload_file")

    def test_image_input_uses_official_upload_then_vision_completion(self) -> None:
        image_bytes = b"\x89PNG\r\n\x1a\nabc"
        image_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")

        class FakeProvider(DeepSeekHarnessProvider):
            def __init__(self):
                super().__init__(pow_solver=lambda _challenge: 17)
                self.requests = []

            def _request(self, method, upstream_path, body, headers):
                self.requests.append((method, upstream_path, body, dict(headers)))
                if upstream_path.endswith("create_pow_challenge"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "challenge": {
                                    "algorithm": "DeepSeekHashV1",
                                    "challenge": "a" * 64,
                                    "salt": "salt",
                                    "difficulty": 1,
                                    "signature": "signature",
                                    "expire_at": 12345,
                                }
                            },
                        }
                    }).encode()
                if upstream_path.endswith("file/upload_file"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "id": "file-image-1",
                                "file_name": "codex-input-image-1.png",
                                "status": "SUCCESS",
                                "audit_result": "unknown",
                            },
                        }
                    }).encode()
                if upstream_path.endswith("chat_session/create"):
                    return 200, {}, json.dumps({
                        "data": {"biz_code": 0, "biz_data": {"chat_session": {"id": "session-1"}}}
                    }).encode()
                return 200, {"content-type": "text/event-stream"}, (
                    'data: {"p":"response/message_id","v":"message-1"}\n\n'
                    'data: {"p":"response/fragments","o":"APPEND","v":'
                    '[{"type":"RESPONSE","content":"I can see it"}]}\n\n'
                    'data: {"p":"response/status","v":"FINISHED"}\n\n'
                ).encode()

        provider = FakeProvider()
        streaming, response = provider.responses({
            "model": "deepseek-web/reasoner",
            "reasoning": {"effort": "high"},
            "input": [{
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "what is in this image?"},
                    {"type": "input_image", "image_url": image_url},
                ],
            }],
        })

        self.assertFalse(streaming)
        self.assertEqual(response["output"][0]["content"][0]["text"], "I can see it")
        pow_requests = [
            json.loads(body)
            for _method, path, body, _headers in provider.requests
            if path.endswith("create_pow_challenge")
        ]
        self.assertEqual(
            [item["target_path"] for item in pow_requests],
            ["/api/v0/file/upload_file", "/api/v0/chat/completion"],
        )
        upload = next(item for item in provider.requests if item[1].endswith("file/upload_file"))
        self.assertEqual(upload[3]["x-model-type"], "vision")
        self.assertEqual(upload[3]["x-file-size"], str(len(image_bytes)))
        self.assertIn(image_bytes, upload[2])
        upload_pow = json.loads(base64.b64decode(upload[3]["x-ds-pow-response"]))
        self.assertEqual(upload_pow["target_path"], "/api/v0/file/upload_file")

        completion_request = next(item for item in provider.requests if item[1].endswith("chat/completion"))
        completion = json.loads(completion_request[2])
        self.assertEqual(completion["model_type"], "vision")
        self.assertEqual(completion["ref_file_ids"], ["file-image-1"])
        self.assertFalse(completion["thinking_enabled"])
        self.assertIn("[Image attached separately for DeepSeek Vision]", completion["prompt"])
        self.assertNotIn("data:image/png;base64", completion["prompt"])
        self.assertNotIn(base64.b64encode(image_bytes).decode("ascii"), completion["prompt"])

    def test_image_input_validation_limits_count_size_and_format(self) -> None:
        tiny = "data:image/png;base64," + base64.b64encode(b"abc").decode("ascii")
        with self.assertRaisesRegex(DeepSeekHarnessError, "at most 4 image attachments"):
            _extract_deepseek_images([{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_image", "image_url": tiny} for _ in range(5)],
            }])

        with patch("fanvpn_bridge.deepseek_harness._MAX_VISION_IMAGE_BYTES", 3):
            too_large = "data:image/png;base64," + base64.b64encode(b"abcd").decode("ascii")
            with self.assertRaisesRegex(DeepSeekHarnessError, "upload limit"):
                _extract_deepseek_images([{
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": too_large}],
                }])

        with self.assertRaisesRegex(DeepSeekHarnessError, "invalid Base64"):
            _extract_deepseek_images([{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_image", "image_url": "data:image/png;base64,%%%"}],
            }])

    def test_image_upload_waits_for_processing_and_rejects_audit_failure(self) -> None:
        image_url = "data:image/png;base64," + base64.b64encode(b"abc").decode("ascii")

        class PendingProvider(DeepSeekHarnessProvider):
            def __init__(self, audit_result="PASS"):
                super().__init__(pow_solver=lambda _challenge: 1)
                self.audit_result = audit_result

            def _request(self, method, upstream_path, body, headers):
                if upstream_path.endswith("create_pow_challenge"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "challenge": {
                                    "algorithm": "DeepSeekHashV1",
                                    "challenge": "a" * 64,
                                    "salt": "salt",
                                    "difficulty": 1,
                                    "signature": "signature",
                                    "expire_at": 12345,
                                }
                            },
                        }
                    }).encode()
                if upstream_path.endswith("file/upload_file"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {"id": "file-image-1", "file_name": "shot.png", "status": "PENDING"},
                        }
                    }).encode()
                if upstream_path.startswith("/api/v0/file/fetch_files?"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "bizData": {
                                "files": [{
                                    "fileId": "file-image-1",
                                    "fileName": "shot.png",
                                    "status": "SUCCESS",
                                    "auditResult": self.audit_result,
                                }]
                            },
                        }
                    }).encode()
                raise AssertionError(upstream_path)

        with patch("fanvpn_bridge.deepseek_harness._FILE_READY_POLL_SECONDS", 0):
            provider = PendingProvider()
            self.assertEqual(provider._upload_input_images([{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_image", "image_url": image_url}],
            }]), ["file-image-1"])

            rejected = PendingProvider(audit_result="REJECTED")
            with self.assertRaisesRegex(DeepSeekHarnessError, "audit_result=REJECTED"):
                rejected._upload_input_images([{
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": image_url}],
                }])

    def test_tool_call_is_returned_as_responses_function_call(self) -> None:
        events = list(
            _responses_events(
                "deepseek-web/chat",
                "",
                [{"name": "read_file", "arguments": {"path": "a.py"}}],
            )
        )
        decoded = [line[6:] for event in events for line in event.decode().splitlines() if line.startswith("data: {")]
        completed = next(json.loads(line) for line in decoded if json.loads(line).get("type") == "response.completed")
        output = completed["response"]["output"]
        self.assertEqual(output[0]["type"], "function_call")
        self.assertEqual(output[0]["name"], "read_file")
        self.assertEqual(json.loads(output[0]["arguments"]), {"path": "a.py"})

    def test_provider_runs_session_pow_completion_and_returns_tool_call(self) -> None:
        solved = []

        class FakeProvider(DeepSeekHarnessProvider):
            def __init__(self):
                super().__init__(pow_solver=lambda challenge: solved.append(challenge) or 23)
                self.requests = []

            def _request(self, method, upstream_path, body, headers):
                self.requests.append((method, upstream_path, body, dict(headers)))
                if upstream_path.endswith("chat_session/create"):
                    return 200, {}, json.dumps({
                        "data": {"biz_code": 0, "biz_data": {"chat_session": {"id": "session-1"}}}
                    }).encode()
                if upstream_path.endswith("create_pow_challenge"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "challenge": {
                                    "algorithm": "DeepSeekHashV1",
                                    "challenge": "a" * 64,
                                    "salt": "salt",
                                    "difficulty": 100,
                                    "signature": "signature",
                                    "expire_at": 12345,
                                }
                            },
                        }
                    }).encode()
                return 200, {"content-type": "text/event-stream"}, (
                    'data: {"p":"response/fragments","o":"APPEND","v":'
                    '[{"type":"RESPONSE","content":"<read_file>{\\"path\\":\\"main.py\\"}</read_file>"}]}\n\n'
                    'data: {"p":"response/status","v":"FINISHED"}\n\n'
                ).encode()

        provider = FakeProvider()
        streaming, response = provider.responses({
            "model": "deepseek-web/chat",
            "input": "inspect main.py",
            "tools": [{
                "type": "function",
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object"},
            }],
        })
        self.assertFalse(streaming)
        self.assertEqual(response["output"][0]["type"], "function_call")
        self.assertEqual(response["output"][0]["name"], "read_file")
        self.assertEqual([item[1] for item in provider.requests], [
            "/api/v0/chat_session/create",
            "/api/v0/chat/create_pow_challenge",
            "/api/v0/chat/completion",
        ])
        self.assertEqual(solved[0]["difficulty"], 100)
        completion = json.loads(provider.requests[-1][2])
        self.assertEqual(completion["chat_session_id"], "session-1")
        self.assertEqual(completion["model_type"], "default")
        pow_value = json.loads(base64.b64decode(provider.requests[-1][3]["x-ds-pow-response"]))
        self.assertEqual(pow_value["answer"], 23)

    def test_provider_recovers_invalid_direct_tool_json_with_continuation(self) -> None:
        class FakeProvider(DeepSeekHarnessProvider):
            def __init__(self):
                super().__init__(pow_solver=lambda _challenge: 1)
                self.requests = []
                self.completion_count = 0

            def _request(self, method, upstream_path, body, headers):
                self.requests.append((method, upstream_path, body, dict(headers)))
                if upstream_path.endswith("chat_session/create"):
                    return 200, {}, json.dumps({
                        "data": {"biz_code": 0, "biz_data": {"chat_session": {"id": "session-1"}}}
                    }).encode()
                if upstream_path.endswith("create_pow_challenge"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "challenge": {
                                    "algorithm": "DeepSeekHashV1",
                                    "challenge": "a" * 64,
                                    "salt": "salt",
                                    "difficulty": 1,
                                    "signature": "signature",
                                    "expire_at": 12345,
                                }
                            },
                        }
                    }).encode()
                self.completion_count += 1
                if self.completion_count == 1:
                    message_id = "message-bad"
                    answer = '<exec_command>{"cmd":"echo "hello""}</exec_command>'
                else:
                    message_id = "message-good"
                    answer = '<exec_command>{"cmd":"echo \\"hello\\""}</exec_command>'
                escaped = json.dumps(answer)
                return 200, {"content-type": "text/event-stream"}, (
                    f'data: {{"p":"response/message_id","v":"{message_id}"}}\n\n'
                    f'data: {{"p":"response/fragments","o":"APPEND","v":'
                    f'[{{"type":"RESPONSE","content":{escaped}}}]}}\n\n'
                    'data: {"p":"response/status","v":"FINISHED"}\n\n'
                ).encode()

        provider = FakeProvider()
        _streaming, response = provider.responses({
            "model": "deepseek-web/chat",
            "input": "run it",
            "tools": [{
                "type": "function",
                "name": "exec_command",
                "description": "Run a command",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            }],
        })
        self.assertEqual(response["output"][0]["type"], "function_call")
        self.assertEqual(response["output"][0]["name"], "exec_command")
        self.assertEqual(json.loads(response["output"][0]["arguments"]), {"cmd": 'echo "hello"'})
        completions = [
            json.loads(body)
            for _method, path, body, _headers in provider.requests
            if path.endswith("/chat/completion")
        ]
        self.assertEqual(len(completions), 2)
        self.assertIsNone(completions[0]["parent_message_id"])
        self.assertEqual(completions[1]["parent_message_id"], "message-bad")
        self.assertIn("TOOL CALL FORMAT ERROR", completions[1]["prompt"])
        self.assertIn("<exec_command>...</exec_command>", completions[1]["prompt"])
        self.assertIn("This is the only valid tool-call format", completions[1]["prompt"])
        self.assertNotIn("invalid JSON arguments", completions[1]["prompt"])
        self.assertNotIn("codex_tool_call", completions[1]["prompt"])

    def test_provider_uses_web_history_to_keep_thinking_out_of_final_answer(self) -> None:
        class FakeProvider(DeepSeekHarnessProvider):
            def __init__(self):
                super().__init__(pow_solver=lambda _challenge: 1)

            def _request(self, method, upstream_path, body, headers):
                if upstream_path.endswith("chat_session/create"):
                    return 200, {}, json.dumps({
                        "data": {"biz_code": 0, "biz_data": {"chat_session": {"id": "session-1"}}}
                    }).encode()
                if upstream_path.endswith("create_pow_challenge"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "challenge": {
                                    "algorithm": "DeepSeekHashV1",
                                    "challenge": "a" * 64,
                                    "salt": "salt",
                                    "difficulty": 1,
                                    "signature": "signature",
                                    "expire_at": 12345,
                                }
                            },
                        }
                    }).encode()
                if upstream_path.startswith("/api/v0/chat/history_messages?"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "chat_messages": [{
                                    "message_id": 20,
                                    "role": "ASSISTANT",
                                    "fragments": [
                                        {"type": "THINK", "content": "private planning"},
                                        {"type": "RESPONSE", "content": "visible answer"},
                                        {"type": "TIP", "content": "AI generated"},
                                    ],
                                }]
                            },
                        }
                    }).encode()
                return 200, {"content-type": "text/event-stream"}, (
                    'event: ready\n'
                    'data: {"response_message_id":20,"model_type":"default"}\n\n'
                    'data: {"v":"private planning"}\n\n'
                    'data: {"p":"response/fragments","o":"APPEND","v":'
                    '[{"type":"RESPONSE","content":"visible answer"}]}\n\n'
                    'data: {"p":"response/status","v":"FINISHED"}\n\n'
                ).encode()

        provider = FakeProvider()
        streaming, response = provider.responses({
            "model": "deepseek-web/chat",
            "reasoning": {"effort": "high"},
            "input": "answer me",
        })
        self.assertFalse(streaming)
        self.assertEqual(response["output"][0]["content"][0]["text"], "visible answer")

    def test_provider_recovers_reasoner_answer_from_history_when_stream_has_only_thinking(self) -> None:
        class FakeProvider(DeepSeekHarnessProvider):
            def __init__(self):
                super().__init__(pow_solver=lambda _challenge: 1)

            def _request(self, method, upstream_path, body, headers):
                if upstream_path.endswith("chat_session/create"):
                    return 200, {}, json.dumps({
                        "data": {"biz_code": 0, "biz_data": {"chat_session": {"id": "session-1"}}}
                    }).encode()
                if upstream_path.endswith("create_pow_challenge"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "challenge": {
                                    "algorithm": "DeepSeekHashV1",
                                    "challenge": "a" * 64,
                                    "salt": "salt",
                                    "difficulty": 1,
                                    "signature": "signature",
                                    "expire_at": 12345,
                                }
                            },
                        }
                    }).encode()
                if upstream_path.startswith("/api/v0/chat/history_messages?"):
                    return 200, {}, json.dumps({
                        "data": {
                            "bizData": {
                                "chatMessages": [{
                                    "uuid": 42,
                                    "message_role": "ASSISTANT",
                                    "fragments": [
                                        {"type": "THINK", "content": "private planning"},
                                        {"type": "RESPONSE", "content": "final answer"},
                                    ],
                                }]
                            }
                        }
                    }).encode()
                return 200, {"content-type": "text/event-stream"}, (
                    'data: {"o":"BATCH","v":[{"p":"meta/response_message_id","v":42}]}\n\n'
                    'data: {"p":"response/fragments","o":"APPEND","v":'
                    '[{"type":"THINK","content":"private planning"}]}\n\n'
                    'data: {"p":"response/status","v":"FINISHED"}\n\n'
                ).encode()

        provider = FakeProvider()
        streaming, response = provider.responses({
            "model": "deepseek-web/reasoner",
            "input": "answer me",
        })
        self.assertFalse(streaming)
        self.assertEqual(response["output"][0]["content"][0]["text"], "final answer")

    def test_provider_waits_for_history_when_stream_finishes_before_visible_answer(self) -> None:
        class FakeProvider(DeepSeekHarnessProvider):
            def __init__(self):
                super().__init__(pow_solver=lambda _challenge: 1)
                self.history_reads = 0

            def _request(self, method, upstream_path, body, headers):
                if upstream_path.endswith("chat_session/create"):
                    return 200, {}, json.dumps({
                        "data": {"biz_code": 0, "biz_data": {"chat_session": {"id": "session-1"}}}
                    }).encode()
                if upstream_path.endswith("create_pow_challenge"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "challenge": {
                                    "algorithm": "DeepSeekHashV1",
                                    "challenge": "a" * 64,
                                    "salt": "salt",
                                    "difficulty": 1,
                                    "signature": "signature",
                                    "expire_at": 12345,
                                }
                            },
                        }
                    }).encode()
                if upstream_path.startswith("/api/v0/chat/history_messages?"):
                    self.history_reads += 1
                    answer = "" if self.history_reads == 1 else "eventually visible answer"
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "chat_messages": [{
                                    "message_id": "message-delayed",
                                    "role": "ASSISTANT",
                                    "fragments": [{"type": "RESPONSE", "content": answer}],
                                }]
                            },
                        }
                    }).encode()
                return 200, {"content-type": "text/event-stream"}, (
                    'data: {"p":"response/message_id","v":"message-delayed"}\n\n'
                    'data: {"p":"response/status","v":"FINISHED"}\n\n'
                ).encode()

        provider = FakeProvider()
        with patch("fanvpn_bridge.deepseek_harness._EMPTY_RESPONSE_RECOVERY_POLL_SECONDS", 0):
            streaming, response = provider.responses({
                "model": "deepseek-web/chat",
                "input": "answer me",
            })
        self.assertFalse(streaming)
        self.assertEqual(response["output"][0]["content"][0]["text"], "eventually visible answer")
        self.assertEqual(provider.history_reads, 2)

    def test_provider_reuses_same_deepseek_session_and_sends_only_new_turn(self) -> None:
        class FakeProvider(DeepSeekHarnessProvider):
            def __init__(self):
                super().__init__(pow_solver=lambda _challenge: 1)
                self.requests = []
                self.session_count = 0
                self.completion_count = 0

            def _request(self, method, upstream_path, body, headers):
                self.requests.append((method, upstream_path, body, dict(headers)))
                if upstream_path.endswith("chat_session/create"):
                    self.session_count += 1
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {"chat_session": {"id": f"session-{self.session_count}"}},
                        }
                    }).encode()
                if upstream_path.endswith("create_pow_challenge"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "challenge": {
                                    "algorithm": "DeepSeekHashV1",
                                    "challenge": "a" * 64,
                                    "salt": "salt",
                                    "difficulty": 1,
                                    "signature": "signature",
                                    "expire_at": 12345,
                                }
                            },
                        }
                    }).encode()
                self.completion_count += 1
                answers = {
                    1: "first answer",
                    2: "second answer",
                    3: "third answer",
                    4: "fourth answer",
                }
                answer = answers[self.completion_count]
                message_id = f"message-{self.completion_count}"
                return 200, {"content-type": "text/event-stream"}, (
                    f'data: {{"p":"response/message_id","v":"{message_id}"}}\n\n'
                    f'data: {{"p":"response/fragments","o":"APPEND","v":'
                    f'[{{"type":"RESPONSE","content":"{answer}"}}]}}\n\n'
                    'data: {"p":"response/status","v":"FINISHED"}\n\n'
                ).encode()

        provider = FakeProvider()
        common = {
            "model": "deepseek-web/chat",
            "instructions": "Follow repo rules.",
            "tools": [{
                "type": "function",
                "name": "read_file",
                "description": "Read one file",
                "parameters": {"type": "object"},
            }],
            "prompt_cache_key": "thread-1",
            "client_metadata": {
                "x-codex-turn-metadata": json.dumps({
                    "thread_id": "thread-1",
                    "turn_id": "turn-1",
                })
            },
        }
        _streaming, first = provider.responses({
            **common,
            "input": [{"type": "message", "role": "user", "content": "first question"}],
        })
        _streaming, second = provider.responses({
            **common,
            "client_metadata": {
                "x-codex-turn-metadata": json.dumps({
                    "thread_id": "thread-1",
                    "turn_id": "turn-2",
                })
            },
            "previous_response_id": first["id"],
            "input": [{"type": "message", "role": "user", "content": "second question"}],
        })

        self.assertEqual(first["output"][0]["content"][0]["text"], "first answer")
        self.assertEqual(second["output"][0]["content"][0]["text"], "second answer")
        self.assertEqual(provider.session_count, 1)
        completions = [
            json.loads(body)
            for _method, path, body, _headers in provider.requests
            if path.endswith("/chat/completion")
        ]
        self.assertEqual(completions[0]["chat_session_id"], "session-1")
        self.assertIsNone(completions[0]["parent_message_id"])
        self.assertEqual(completions[1]["chat_session_id"], "session-1")
        self.assertEqual(completions[1]["parent_message_id"], "message-1")
        self.assertIn("second question", completions[1]["prompt"])
        self.assertNotIn("first question", completions[1]["prompt"])
        self.assertNotIn("first answer", completions[1]["prompt"])
        self.assertNotIn("Follow repo rules.", completions[1]["prompt"])
        self.assertNotIn("CODEX TOOLS AVAILABLE", completions[1]["prompt"])

        _streaming, third = provider.responses({
            **common,
            "client_metadata": {
                "x-codex-turn-metadata": json.dumps({
                    "thread_id": "thread-1",
                    "turn_id": "turn-3",
                })
            },
            "previous_response_id": second["id"],
            "input": [{"type": "message", "role": "user", "content": "third question"}],
        })
        self.assertEqual(third["output"][0]["content"][0]["text"], "third answer")
        self.assertEqual(provider.session_count, 1)
        completions = [
            json.loads(body)
            for _method, path, body, _headers in provider.requests
            if path.endswith("/chat/completion")
        ]
        self.assertEqual(completions[2]["chat_session_id"], "session-1")
        self.assertEqual(completions[2]["parent_message_id"], "message-2")
        self.assertIn("third question", completions[2]["prompt"])
        self.assertNotIn("second question", completions[2]["prompt"])
        self.assertNotIn("CODEX TOOLS AVAILABLE", completions[2]["prompt"])

        # Codex can also replay canonical/full history without a usable
        # previous_response_id. The adapter should recover the delta and keep
        # using the same DeepSeek Web session.
        _streaming, fourth = provider.responses({
            **common,
            "client_metadata": {
                "x-codex-turn-metadata": json.dumps({
                    "thread_id": "thread-1",
                    "turn_id": "turn-4",
                })
            },
            "input": [
                {"type": "message", "role": "user", "content": "first question"},
                {"type": "message", "role": "assistant", "content": "first answer"},
                {"type": "message", "role": "user", "content": "second question"},
                {"type": "message", "role": "assistant", "content": "second answer"},
                {"type": "message", "role": "user", "content": "third question"},
                {"type": "message", "role": "assistant", "content": "third answer"},
                {"type": "message", "role": "user", "content": "fourth question"},
            ],
        })
        self.assertEqual(fourth["output"][0]["content"][0]["text"], "fourth answer")
        self.assertEqual(provider.session_count, 1)
        completions = [
            json.loads(body)
            for _method, path, body, _headers in provider.requests
            if path.endswith("/chat/completion")
        ]
        self.assertEqual(completions[3]["chat_session_id"], "session-1")
        self.assertEqual(completions[3]["parent_message_id"], "message-3")
        self.assertIn("fourth question", completions[3]["prompt"])
        self.assertNotIn("first question", completions[3]["prompt"])
        self.assertNotIn("third answer", completions[3]["prompt"])

    def test_provider_restores_deepseek_session_after_restart(self) -> None:
        class FakeProvider(DeepSeekHarnessProvider):
            def __init__(self, state_path: Path):
                super().__init__(
                    pow_solver=lambda _challenge: 1,
                    state_path=state_path,
                )
                self.requests = []
                self.created_sessions = 0

            def _request(self, method, upstream_path, body, headers):
                self.requests.append((method, upstream_path, body, dict(headers)))
                if upstream_path.endswith("chat_session/create"):
                    self.created_sessions += 1
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {"chat_session": {"id": "session-1"}},
                        }
                    }).encode()
                if upstream_path.endswith("history_messages?chat_session_id=session-1"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "chat_messages": [{
                                    "message_id": "message-1",
                                    "role": "ASSISTANT",
                                    "fragments": [{"type": "RESPONSE", "content": "first answer"}],
                                }]
                            },
                        }
                    }).encode()
                if upstream_path.endswith("create_pow_challenge"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "challenge": {
                                    "algorithm": "DeepSeekHashV1",
                                    "challenge": "a" * 64,
                                    "salt": "salt",
                                    "difficulty": 1,
                                    "signature": "signature",
                                    "expire_at": 12345,
                                }
                            },
                        }
                    }).encode()
                completion = json.loads(body)
                parent = completion.get("parent_message_id")
                message_id = "message-1" if parent is None else "message-2"
                answer = "first answer" if parent is None else "second answer"
                return 200, {"content-type": "text/event-stream"}, (
                    f'data: {{"p":"response/message_id","v":"{message_id}"}}\n\n'
                    f'data: {{"p":"response/fragments","o":"APPEND","v":'
                    f'[{{"type":"RESPONSE","content":"{answer}"}}]}}\n\n'
                    'data: {"p":"response/status","v":"FINISHED"}\n\n'
                ).encode()

        common = {
            "model": "deepseek-web/chat",
            "instructions": "Follow repo rules.",
            "tools": [],
            "prompt_cache_key": "thread-restart",
            "client_metadata": {
                "x-codex-turn-metadata": json.dumps({
                    "thread_id": "thread-restart",
                    "turn_id": "turn-1",
                })
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "deepseek-conversations.json"
            first_provider = FakeProvider(state_path)
            _streaming, first = first_provider.responses({
                **common,
                "input": [{"type": "message", "role": "user", "content": "first question"}],
            })
            self.assertEqual(first_provider.created_sessions, 1)
            self.assertTrue(state_path.exists())

            restarted_provider = FakeProvider(state_path)
            _streaming, second = restarted_provider.responses({
                **common,
                "client_metadata": {
                    "x-codex-turn-metadata": json.dumps({
                        "thread_id": "thread-restart",
                        "turn_id": "turn-2",
                    })
                },
                "previous_response_id": first["id"],
                "input": [{"type": "message", "role": "user", "content": "second question"}],
            })

        self.assertEqual(second["output"][0]["content"][0]["text"], "second answer")
        self.assertEqual(restarted_provider.created_sessions, 0)
        history_reads = [
            path for _method, path, _body, _headers in restarted_provider.requests
            if "history_messages" in path
        ]
        self.assertEqual(len(history_reads), 1)
        completions = [
            json.loads(body)
            for _method, path, body, _headers in restarted_provider.requests
            if path.endswith("/chat/completion")
        ]
        self.assertEqual(completions[0]["chat_session_id"], "session-1")
        self.assertEqual(completions[0]["parent_message_id"], "message-1")
        self.assertIn("second question", completions[0]["prompt"])
        self.assertNotIn("first question", completions[0]["prompt"])

    def test_replayed_history_does_not_reupload_old_images(self) -> None:
        image_bytes = b"\x89PNG\r\n\x1a\nabc"
        image_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")

        class FakeProvider(DeepSeekHarnessProvider):
            def __init__(self):
                super().__init__(pow_solver=lambda _challenge: 1)
                self.requests = []
                self.session_count = 0
                self.completion_count = 0
                self.image_batches = []

            def _upload_input_images(self, input_items):
                images = _extract_deepseek_images(input_items)
                self.image_batches.append(len(images))
                return ["file-image-1"] if images else []

            def _request(self, method, upstream_path, body, headers):
                self.requests.append((method, upstream_path, body, dict(headers)))
                if upstream_path.endswith("chat_session/create"):
                    self.session_count += 1
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {"chat_session": {"id": f"session-{self.session_count}"}},
                        }
                    }).encode()
                if upstream_path.endswith("create_pow_challenge"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "challenge": {
                                    "algorithm": "DeepSeekHashV1",
                                    "challenge": "a" * 64,
                                    "salt": "salt",
                                    "difficulty": 1,
                                    "signature": "signature",
                                    "expire_at": 12345,
                                }
                            },
                        }
                    }).encode()
                self.completion_count += 1
                answer = "image answer" if self.completion_count == 1 else "follow-up answer"
                message_id = f"message-{self.completion_count}"
                return 200, {"content-type": "text/event-stream"}, (
                    f'data: {{"p":"response/message_id","v":"{message_id}"}}\n\n'
                    f'data: {{"p":"response/fragments","o":"APPEND","v":'
                    f'[{{"type":"RESPONSE","content":"{answer}"}}]}}\n\n'
                    'data: {"p":"response/status","v":"FINISHED"}\n\n'
                ).encode()

        provider = FakeProvider()
        common = {
            "model": "deepseek-web/chat",
            "prompt_cache_key": "image-thread",
            "client_metadata": {
                "x-codex-turn-metadata": json.dumps({"thread_id": "image-thread", "turn_id": "turn-1"})
            },
        }
        first_user = {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "inspect this"},
                {"type": "input_image", "image_url": image_url},
            ],
        }
        provider.responses({**common, "input": [first_user]})
        provider.responses({
            **common,
            "client_metadata": {
                "x-codex-turn-metadata": json.dumps({"thread_id": "image-thread", "turn_id": "turn-2"})
            },
            "input": [
                first_user,
                {"type": "message", "role": "assistant", "content": "image answer"},
                {"type": "message", "role": "user", "content": "tell me more"},
            ],
        })

        self.assertEqual(provider.session_count, 1)
        self.assertEqual(provider.image_batches, [1, 0])
        completions = [
            json.loads(body)
            for _method, path, body, _headers in provider.requests
            if path.endswith("/chat/completion")
        ]
        self.assertEqual(completions[0]["ref_file_ids"], ["file-image-1"])
        self.assertEqual(completions[1]["ref_file_ids"], [])
        self.assertEqual(completions[1]["model_type"], "default")
        self.assertIn("tell me more", completions[1]["prompt"])
        self.assertNotIn("inspect this", completions[1]["prompt"])
        self.assertNotIn("[Image attached separately for DeepSeek Vision]", completions[1]["prompt"])

    def test_compaction_uses_dedicated_summary_turn_and_returns_checkpoint(self) -> None:
        class FakeProvider(DeepSeekHarnessProvider):
            def __init__(self):
                super().__init__(pow_solver=lambda _challenge: 1)
                self.requests = []

            def _request(self, method, upstream_path, body, headers):
                self.requests.append((method, upstream_path, body, dict(headers)))
                if upstream_path.endswith("chat_session/create"):
                    return 200, {}, json.dumps({
                        "data": {"biz_code": 0, "biz_data": {"chat_session": {"id": "compact-session"}}}
                    }).encode()
                if upstream_path.endswith("create_pow_challenge"):
                    return 200, {}, json.dumps({
                        "data": {
                            "biz_code": 0,
                            "biz_data": {
                                "challenge": {
                                    "algorithm": "DeepSeekHashV1",
                                    "challenge": "a" * 64,
                                    "salt": "salt",
                                    "difficulty": 1,
                                    "signature": "signature",
                                    "expire_at": 12345,
                                }
                            },
                        }
                    }).encode()
                return 200, {"content-type": "text/event-stream"}, (
                    'data: {"p":"response/message_id","v":"compact-message"}\n\n'
                    'data: {"p":"response/fragments","o":"APPEND","v":'
                    '[{"type":"RESPONSE","content":"checkpoint summary"}]}\n\n'
                    'data: {"p":"response/status","v":"FINISHED"}\n\n'
                ).encode()

        provider = FakeProvider()
        image = "data:image/png;base64," + ("A" * 1000)
        result = provider.compact({
            "model": "deepseek-web/chat",
            "input": [
                {"type": "message", "role": "user", "content": "old question"},
                {"type": "message", "role": "assistant", "content": "old answer"},
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "latest question"},
                        {"type": "input_image", "image_url": image},
                    ],
                },
            ],
            "tools": [{
                "type": "function",
                "name": "exec_command",
                "description": "Run a command",
                "parameters": {"type": "object"},
            }],
        })
        self.assertEqual(result["output"][-1]["role"], "user")
        self.assertIn("checkpoint summary", result["output"][-1]["content"][0]["text"])
        self.assertIn("Another language model started", result["output"][-1]["content"][0]["text"])
        retained_images = [
            block
            for message in result["output"][:-1]
            for block in message.get("content", [])
            if block.get("type") == "input_image"
        ]
        self.assertEqual(retained_images[0]["image_url"], image)
        completion = next(
            json.loads(body)
            for _method, path, body, _headers in provider.requests
            if path.endswith("/chat/completion")
        )
        self.assertIn("COMPACTION TASK", completion["prompt"])
        self.assertNotIn("CODEX TOOL PROTOCOL", completion["prompt"])
        self.assertNotIn("data:image/png;base64", completion["prompt"])

    def test_local_compaction_metadata_returns_summary_as_normal_response(self) -> None:
        class FakeProvider(DeepSeekHarnessProvider):
            def __init__(self):
                super().__init__(pow_solver=lambda _challenge: 1)

            def _compaction_summary(self, payload):
                return "local summary"

        provider = FakeProvider()
        streaming, response = provider.responses({
            "model": "deepseek-web/chat",
            "stream": False,
            "input": [{"type": "message", "role": "user", "content": "long history"}],
            "client_metadata": {
                "x-codex-turn-metadata": json.dumps({"request_kind": "compaction"})
            },
        })
        self.assertFalse(streaming)
        self.assertEqual(response["output"][0]["content"][0]["text"], "local summary")


if __name__ == "__main__":
    unittest.main()
